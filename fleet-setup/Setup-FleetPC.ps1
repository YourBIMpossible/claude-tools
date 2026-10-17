#Requires -Version 5.1
<#
.SYNOPSIS
    One-pass setup for a new fleet PC (White, Zen, MyBuddy, Plexy, Lazy, ...).

.DESCRIPTION
    Run from the USB stick through Start-Setup.cmd. Every step checks first and skips
    what is already done, so it is safe to re-run after a failure or reboot.

    Steps: machine name, Remote Desktop, winget apps, Claude Code CLI, user PATH,
    Buzz Desktop, sign-ins (GitHub, Claude, Google Drive, Tailscale), Claude profile
    bootstrap, repo clones, Claude prompt for the follow-up session.

    Sign-ins are always typed by the owner in the windows this script opens. The script
    never reads, stores, or prints a token, password, or key.

.PARAMETER MachineName
    Computer name to set. Prompted for when omitted.

.PARAMETER DryRun
    Run every check and print what would change, without changing anything.
#>
[CmdletBinding()]
param(
    [string]$MachineName,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$KitDir = $PSScriptRoot
$Config = Import-PowerShellDataFile -Path (Join-Path $KitDir 'config.psd1')
$Results = New-Object System.Collections.Generic.List[object]

# --- helpers -----------------------------------------------------------------

function Write-Step([string]$Text) { Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
function Write-Info([string]$Text) { Write-Host "  $Text" }
function Write-Warn2([string]$Text) { Write-Host "  ! $Text" -ForegroundColor Yellow }

function Add-Result([string]$Step, [ValidateSet('OK', 'DONE', 'SKIP', 'WOULD', 'WARN', 'FAIL')][string]$Status, [string]$Note = '') {
    $Results.Add([pscustomobject]@{ Step = $Step; Status = $Status; Note = $Note })
}

# Runs a native command without PS 5.1's stderr-under-Stop trap; returns the exit code.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments | Out-Host
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
}

# Same, output discarded; for yes/no checks.
function Test-Native([string]$Exe, [string[]]$Arguments) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user;$env:USERPROFILE\.local\bin;$env:APPDATA\npm"
}

function Test-Command([string]$Name) { [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Read-YesNo([string]$Question, [bool]$Default = $true) {
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $answer = Read-Host "  $Question $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim().ToLowerInvariant().StartsWith('y')
}

function Wait-Owner([string]$Text) {
    Write-Host "  >> $Text" -ForegroundColor Green
    [void](Read-Host '  Press Enter when done')
}

# --- elevation: re-launch as admin under the SAME account ------------------------

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $DryRun) {
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', "`"$PSCommandPath`"")
    if ($MachineName) { $argList += @('-MachineName', $MachineName) }
    if ($DryRun) { $argList += '-DryRun' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList
    return
}

# Per-user installs (Claude CLI, PATH, Buzz, sign-ins) must land in the person's own profile.
# If UAC elevated with a different admin account, everything would go to the wrong profile.
$consoleUser = (Get-CimInstance Win32_ComputerSystem).UserName
if ($consoleUser -and ($consoleUser -ne $identity.Name)) {
    Write-Host "Elevated as $($identity.Name) but signed in as $consoleUser." -ForegroundColor Red
    Write-Host 'Make the signed-in account a local administrator, sign in again, and re-run.' -ForegroundColor Red
    return
}

$logDir = Join-Path $env:ProgramData 'FleetSetup'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("setup-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
Start-Transcript -Path $logFile | Out-Null
$rebootNeeded = $false

try {
    if ($DryRun) { Write-Host 'DRY RUN: checks only, nothing will change.' -ForegroundColor Magenta }

    # --- 1. preflight ------------------------------------------------------------
    Write-Step 'Preflight'
    $os = Get-CimInstance Win32_OperatingSystem
    Write-Info "$($os.Caption) build $($os.BuildNumber), user $($identity.Name)"
    if ($os.Caption -notmatch 'Pro|Enterprise|Education') {
        Write-Warn2 'Not a Pro edition: this PC cannot host Remote Desktop.'
        Add-Result 'Edition' 'WARN' 'Home edition: no RDP host'
    } else { Add-Result 'Edition' 'OK' $os.Caption }

    if (-not (Test-Command 'winget')) {
        throw 'winget not found. Update "App Installer" from the Microsoft Store, then re-run.'
    }
    if (-not $DryRun) { [void](Invoke-Native 'winget' @('source', 'update')) }

    # --- 2. machine name ---------------------------------------------------------
    Write-Step 'Machine name'
    if (-not $MachineName) { $MachineName = Read-Host '  Name for this PC (e.g. LAZY)' }
    $MachineName = $MachineName.Trim().ToUpperInvariant()
    if ($MachineName -notmatch '^[A-Z0-9-]{1,15}$') { throw "Invalid computer name '$MachineName' (letters, digits, '-', max 15)." }
    if ($env:COMPUTERNAME -eq $MachineName) {
        Add-Result 'Machine name' 'OK' $MachineName
    } elseif ($DryRun) {
        Add-Result 'Machine name' 'WOULD' "$env:COMPUTERNAME -> $MachineName"
    } else {
        Rename-Computer -NewName $MachineName -Force
        $rebootNeeded = $true
        Add-Result 'Machine name' 'DONE' "$env:COMPUTERNAME -> $MachineName (after reboot)"
    }

    # --- 3. Remote Desktop -------------------------------------------------------
    Write-Step 'Remote Desktop'
    $tsKey = 'HKLM:\System\CurrentControlSet\Control\Terminal Server'
    $nlaKey = "$tsKey\WinStations\RDP-Tcp"
    $rdpOn = (Get-ItemProperty $tsKey).fDenyTSConnections -eq 0
    $nlaOn = (Get-ItemProperty $nlaKey).UserAuthentication -eq 1
    $fwOn = @(Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue | Where-Object { $_.Enabled -eq 'True' }).Count -gt 0
    if ($rdpOn -and $nlaOn -and $fwOn) {
        Add-Result 'Remote Desktop' 'OK' 'on, NLA on, firewall on'
    } elseif ($DryRun) {
        Add-Result 'Remote Desktop' 'WOULD' "rdp=$rdpOn nla=$nlaOn firewall=$fwOn -> all on"
    } else {
        Set-ItemProperty $tsKey -Name fDenyTSConnections -Value 0
        Set-ItemProperty $nlaKey -Name UserAuthentication -Value 1
        Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'
        Add-Result 'Remote Desktop' 'DONE' 'on, NLA on, firewall on (Plexy needed NLA off if sign-in fails)'
    }

    # --- 4. apps (winget) --------------------------------------------------------
    Write-Step 'Apps'
    $packages = @($Config.Packages)
    foreach ($opt in $Config.OptionalPackages) {
        if ($DryRun) { Add-Result "App $opt" 'WOULD' 'optional - asked during setup'; continue }
        if (Read-YesNo "Install optional package $opt?" $true) { $packages += $opt }
    }
    # Apps installed outside winget (python.org, Visual Studio) don't show in `winget list`;
    # probe the tool itself so a re-run doesn't add a second copy.
    $probes = @{
        'Python.Python.3.14'      = { (Test-Command 'py') -and (Test-Native 'py' @('-3.14', '--version')) }
        'Microsoft.DotNet.SDK.10' = { (Test-Command 'dotnet') -and (@(& dotnet --list-sdks 2>$null) -match '^10\.').Count -gt 0 }
    }
    foreach ($id in $packages) {
        $present = Test-Native 'winget' @('list', '--id', $id, '-e', '--accept-source-agreements')
        if (-not $present -and $probes.ContainsKey($id)) { $present = [bool](& $probes[$id]) }
        if ($present) {
            Add-Result "App $id" 'OK' 'already installed'
            continue
        }
        if ($DryRun) { Add-Result "App $id" 'WOULD' 'install'; continue }
        Write-Info "Installing $id ..."
        $code = Invoke-Native 'winget' @('install', '--id', $id, '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements')
        if ($code -eq 0) { Add-Result "App $id" 'DONE' } else { Add-Result "App $id" 'FAIL' "winget exit $code" }
    }
    Update-SessionPath

    # --- 5. Claude Code CLI ------------------------------------------------------
    Write-Step 'Claude Code CLI'
    $claudeExe = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
    if (Test-Path $claudeExe) {
        Add-Result 'Claude Code CLI' 'OK' $claudeExe
    } elseif ($DryRun) {
        Add-Result 'Claude Code CLI' 'WOULD' "install from $($Config.ClaudeCodeInstaller)"
    } else {
        Invoke-Expression (Invoke-RestMethod -Uri $Config.ClaudeCodeInstaller -UseBasicParsing)
        if (Test-Path $claudeExe) { Add-Result 'Claude Code CLI' 'DONE' $claudeExe } else { Add-Result 'Claude Code CLI' 'FAIL' 'claude.exe not found after install' }
    }

    # --- 6. user PATH ------------------------------------------------------------
    Write-Step 'User PATH'
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($null -eq $userPath) { $userPath = '' }
    $parts = @($userPath -split ';' | Where-Object { $_ })
    $missing = @("$env:APPDATA\npm", "$env:USERPROFILE\.local\bin" | Where-Object { $parts -notcontains $_ })
    if ($missing.Count -eq 0) {
        Add-Result 'User PATH' 'OK'
    } elseif ($DryRun) {
        Add-Result 'User PATH' 'WOULD' ('add ' + ($missing -join ', '))
    } else {
        [Environment]::SetEnvironmentVariable('Path', (($parts + $missing) -join ';'), 'User')
        Update-SessionPath
        Add-Result 'User PATH' 'DONE' ('added ' + ($missing -join ', '))
    }

    # --- 7. Buzz Desktop ---------------------------------------------------------
    Write-Step 'Buzz Desktop'
    $buzzExe = Join-Path $env:LOCALAPPDATA 'Buzz\buzz-desktop.exe'
    if (Test-Path $buzzExe) {
        Add-Result 'Buzz Desktop' 'OK' ((Get-Item $buzzExe).VersionInfo.ProductVersion)
    } elseif ($DryRun) {
        Add-Result 'Buzz Desktop' 'WOULD' "download + verify $($Config.Buzz.Version), install"
    } else {
        $installer = Join-Path $env:TEMP ([IO.Path]::GetFileName($Config.Buzz.Url))
        Invoke-WebRequest -Uri $Config.Buzz.Url -OutFile $installer -UseBasicParsing
        $hash = (Get-FileHash $installer -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne $Config.Buzz.Sha256) {
            Remove-Item $installer -Force
            Add-Result 'Buzz Desktop' 'FAIL' "hash mismatch ($hash); installer deleted"
        } else {
            Start-Process -FilePath $installer -ArgumentList '/S' -Wait
            if (Test-Path $buzzExe) { Add-Result 'Buzz Desktop' 'DONE' $Config.Buzz.Version }
            else { Add-Result 'Buzz Desktop' 'WARN' "installer ran; $buzzExe not found - check Start menu" }
        }
    }

    # --- 8. sign-ins (owner types these) -----------------------------------------
    Write-Step 'Sign-ins'
    if ($DryRun) {
        $gh = if (Test-Native 'gh' @('auth', 'status')) { 'signed in' } else { 'needs sign-in' }
        Add-Result 'GitHub sign-in' 'WOULD' $gh
        Add-Result 'Claude sign-in' 'WOULD' 'check / prompt'
        Add-Result 'Google Drive sign-in' 'WOULD' 'prompt'
    } else {
        if (Test-Native 'gh' @('auth', 'status')) {
            Add-Result 'GitHub sign-in' 'OK'
        } else {
            Write-Info 'GitHub: choose GitHub.com, HTTPS, "Login with a web browser".'
            [void](Invoke-Native 'gh' @('auth', 'login', '--hostname', 'github.com', '--git-protocol', 'https', '--web'))
            [void](Invoke-Native 'gh' @('auth', 'setup-git'))
            if (Test-Native 'gh' @('auth', 'status')) { Add-Result 'GitHub sign-in' 'DONE' } else { Add-Result 'GitHub sign-in' 'FAIL' 'not signed in' }
        }

        if (Test-Path $claudeExe) {
            $status = ''
            try { $status = (& $claudeExe auth status 2>$null | Out-String) } catch { $status = '' }
            if ($status -match '"loggedIn"\s*:\s*true') {
                Add-Result 'Claude sign-in' 'OK'
            } else {
                Write-Info 'Claude: decide first - Team org or personal Max login.'
                [void](Invoke-Native $claudeExe @('auth', 'login'))
                Add-Result 'Claude sign-in' 'DONE' 'verify with: claude auth status'
            }
        } else { Add-Result 'Claude sign-in' 'SKIP' 'CLI missing' }

        $drive = Get-ChildItem "$env:ProgramFiles\Google\Drive File Stream" -Recurse -Filter 'GoogleDriveFS.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($drive) {
            Start-Process $drive.FullName
            Wait-Owner 'Sign in to Google Drive (tray icon) with the account that holds Coord-Desktop.'
            Add-Result 'Google Drive sign-in' 'DONE' 'owner confirmed'
        } else { Add-Result 'Google Drive sign-in' 'SKIP' 'Drive not installed' }

        if (Test-Command 'tailscale') {
            if (Read-YesNo 'Bring Tailscale up now (opens a browser sign-in)?' $true) {
                [void](Invoke-Native 'tailscale' @('up'))
                Add-Result 'Tailscale' 'DONE'
            } else { Add-Result 'Tailscale' 'SKIP' 'owner declined' }
        }
    }

    # --- 9. Claude profile bootstrap ---------------------------------------------
    Write-Step 'Claude profile'
    $profileDir = $Config.ProfileDir
    if ($profileDir -match '\s') { throw "ProfileDir '$profileDir' contains a space; bootstrap.ps1 refuses that." }
    $bootstrapped = Test-Path (Join-Path $env:USERPROFILE '.claude\skills')
    if ($DryRun) {
        $state = if ($bootstrapped) { 'already bootstrapped; would pull + re-run' } else { 'would clone + run bootstrap.ps1' }
        Add-Result 'Claude profile' 'WOULD' $state
    } elseif (-not (Test-Native 'gh' @('auth', 'status'))) {
        Add-Result 'Claude profile' 'SKIP' 'GitHub not signed in'
    } else {
        $ok = $true
        if (Test-Path (Join-Path $profileDir '.git')) {
            $ok = (Invoke-Native 'git' @('-C', $profileDir, 'pull', '--ff-only')) -eq 0
        } else {
            $ok = (Invoke-Native 'git' @('clone', $Config.ProfileRepo, $profileDir)) -eq 0
        }
        if (-not $ok) {
            Add-Result 'Claude profile' 'FAIL' 'clone/pull failed (repo access?)'
        } else {
            $running = @(Get-Process -Name 'claude' -ErrorAction SilentlyContinue)
            if ($running.Count -gt 0) {
                Wait-Owner 'Quit Claude (desktop app and any claude terminals) - bootstrap needs them closed.'
            }
            $code = Invoke-Native 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $profileDir 'bootstrap.ps1'))
            if ($code -eq 0) { Add-Result 'Claude profile' 'DONE' $profileDir } else { Add-Result 'Claude profile' 'FAIL' "bootstrap exit $code" }
        }
    }

    # --- 10. repos ---------------------------------------------------------------
    Write-Step 'Repos'
    $root = $Config.RepoRoot
    if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $root | Out-Null }
    foreach ($repo in $Config.Repos) {
        $dest = Join-Path $root $repo
        if (Test-Path (Join-Path $dest '.git')) { Add-Result "Repo $repo" 'OK' $dest; continue }
        if ($DryRun) { Add-Result "Repo $repo" 'WOULD' "clone to $dest"; continue }
        $url = "https://github.com/$($Config.RepoOwner)/$repo.git"
        if ((Invoke-Native 'git' @('clone', $url, $dest)) -eq 0) {
            [void](Invoke-Native 'git' @('-C', $dest, 'remote', 'set-head', 'origin', '-a'))
            Add-Result "Repo $repo" 'DONE' $dest
        } else {
            Add-Result "Repo $repo" 'WARN' 'no access with this GitHub account - skipped'
        }
    }

    # --- 11. Claude prompt for the follow-up session -----------------------------
    Write-Step 'Claude prompt'
    $template = Join-Path $KitDir 'CLAUDE-PROMPT.template.md'
    $target = Join-Path ([Environment]::GetFolderPath('Desktop')) "$MachineName-CLAUDE-PROMPT.md"
    if ($DryRun) {
        Add-Result 'Claude prompt' 'WOULD' $target
    } else {
        $text = (Get-Content $template -Raw).Replace('{{MACHINE}}', $MachineName)
        Set-Content -Path $target -Value $text -Encoding UTF8
        Add-Result 'Claude prompt' 'DONE' $target
    }
} catch {
    Add-Result 'Setup' 'FAIL' $_.Exception.Message
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
} finally {
    Write-Step 'Summary'
    $Results | Format-Table -AutoSize | Out-Host
    $summaryCopy = Join-Path $KitDir ("logs\{0}-{1:yyyyMMdd-HHmmss}.txt" -f $env:COMPUTERNAME, (Get-Date))
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path $summaryCopy) | Out-Null
        $Results | Format-Table -AutoSize | Out-String -Width 200 | Set-Content -Path $summaryCopy -Encoding UTF8
        Write-Info "Summary also saved to $summaryCopy"
    } catch { Write-Warn2 "Could not write summary to the kit folder: $($_.Exception.Message)" }
    Write-Info "Full log: $logFile"
    Stop-Transcript | Out-Null
}

if (-not $DryRun -and ($rebootNeeded -or $Results.Status -contains 'DONE')) {
    Write-Host "`nNext: reboot, then open Claude Code in C:\src and paste the prompt from the Desktop file." -ForegroundColor Green
    if (Read-YesNo 'Reboot now?' $false) { Restart-Computer -Force }
}

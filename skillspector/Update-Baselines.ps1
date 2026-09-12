<#
.SYNOPSIS
    Regenerate the accepted-findings baseline for every installed skill.

.DESCRIPTION
    Run this AFTER reviewing drift reported by Run-SkillSpector-Watch.ps1, to
    accept the current state as the new normal. It overwrites every baseline,
    so only run it once you have actually read the new findings -- running it
    blindly re-accepts whatever an update introduced, which defeats the point.

    One baseline per skill; see SkillTargets.ps1 for why per-skill scanning is
    the only arrangement in which --baseline actually works.

    Static analysis only, deliberately: baselines must be deterministic, and
    the LLM stage is not (see Run-SkillSpector-Watch.ps1).

.PARAMETER Reason
    Text recorded against every suppressed finding, for auditability.
#>
[CmdletBinding()]
param(
    [string]$Reason = "Accepted $(Get-Date -Format 'yyyy-MM-dd'): reviewed and approved"
)

$ErrorActionPreference = 'Stop'

# Resolve in the body, not a param default -- see Run-SkillSpector-Watch.ps1.
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $scriptRoot) { $scriptRoot = $PSScriptRoot }

. (Join-Path $scriptRoot 'SkillTargets.ps1')

$ss          = Join-Path $scriptRoot 'venv\Scripts\skillspector.exe'
$baselineDir = Join-Path $scriptRoot 'baselines'

if (-not (Test-Path $ss)) { Write-Host "Scanner not found at $ss" -ForegroundColor Red; exit 2 }

$targets = Get-SkillTargets
Write-Host "Generating baselines for $($targets.Count) skill(s)..."
Write-Host ''

$total = 0
$failed = 0
foreach ($t in $targets) {
    $scopeDir = Join-Path $baselineDir $t.Scope
    if (-not (Test-Path $scopeDir)) { New-Item -ItemType Directory -Force -Path $scopeDir | Out-Null }
    $out = Join-Path $scopeDir "$($t.Name).yaml"

    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    # Start-Process -ArgumentList joins on spaces without quoting, so any
    # argument containing a space (notably --reason) must be quoted here or the
    # CLI sees it as extra positional arguments and exits 2.
    $ssArgs = @('baseline', "`"$($t.Path)`"", '-o', "`"$out`"", '--no-llm', '--reason', "`"$Reason`"")
    $proc = Start-Process -FilePath $ss -ArgumentList $ssArgs `
        -Wait -PassThru -NoNewWindow -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue

    if ((Test-Path $out) -and $proc.ExitCode -eq 0) {
        $n = ([regex]::Matches((Get-Content $out -Raw), '(?m)^- hash:')).Count
        $total += $n
        Write-Host ("  {0,-22} {1,-34} {2} accepted" -f $t.Scope, $t.Name, $n) -ForegroundColor DarkGray
    } else {
        $failed++
        Write-Host ("  {0,-22} {1,-34} FAILED (exit {2})" -f $t.Scope, $t.Name, $proc.ExitCode) -ForegroundColor Red
    }
}

# Record the plugin versions this review covered. Run-SkillSpector-Watch.ps1
# compares against this, so an upgrade is reported until it is reviewed and
# re-baselined here -- it cannot be cleared by re-running the watch.
$versionFile = Join-Path $baselineDir 'plugin-versions.json'
$map = Get-PluginVersionMap -Targets $targets
$map | ConvertTo-Json | Set-Content -Path $versionFile -Encoding utf8
Write-Host ''
Write-Host "Plugin versions recorded ($($map.Count)):"
foreach ($k in $map.Keys) { Write-Host ("  {0,-28} {1}" -f $k, $map[$k]) -ForegroundColor DarkGray }

Write-Host ''
Write-Host "$total finding(s) accepted across $($targets.Count) skill(s); $failed failure(s)."
Write-Host "Baselines: $baselineDir"
if ($failed -gt 0) { exit 2 }

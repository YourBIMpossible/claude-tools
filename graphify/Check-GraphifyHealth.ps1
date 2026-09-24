# ============================================================
# Check-GraphifyHealth.ps1 — daily, READ-ONLY health + update monitor.
#
# Reads the health record Refresh-Graphs.ps1 wrote, asks PyPI what the latest
# graphifyy release is, asks Task Scheduler how the last refresh actually
# exited, turns all of it into alerts, and renders a Dashboard panel file.
#
# THIS SCRIPT NEVER INSTALLS OR UPGRADES ANYTHING. The 0.9.x upgrade changed
# node-ID behavior, so an unattended upgrade would silently invalidate graphs,
# recall baselines, and every downstream signal. Version drift is reported for
# a human to action; `pip install graphifyy==<new>` stays a deliberate,
# supervised step followed by a full rebuild.
#
# It also never refreshes a graph — refresh is Refresh-Graphs.ps1's job
# (weekly). Separating the two means a monitoring bug can't corrupt a graph.
#
# PUBLIC BOUNDARY: alerts.json is local and keeps full diagnostics (each
# alert's `detail`, target scan paths). graphify-health.js is PUBLIC: it is
# built only through ConvertTo-GraphifyPublicHealth (GraphifyPublic.ps1), an
# allowlist projection, then checked by Test-GraphifyPublicSafe. Alert
# `message`/`action`/`context` text must therefore be path-free templates;
# anything local goes in `detail`.
#
# OWNERSHIP: this script owns alerts.json, graphify-health.js, and
# publish-settings.lkg.json (last-known-good python/dashboard settings).
#   - health.json / health-history.jsonl are written by Refresh-Graphs.ps1.
#   - graphify-health.js is rendered into the Dashboard-auto clone but is
#     COMMITTED BY Refresh-Dashboard.ps1 (it is in that script's git add list).
#     This script deliberately does not commit or push: one committer per repo.
# ============================================================

$ErrorActionPreference = 'Stop'
# $root defaults to this script's folder; override with $env:GRAPHIFY_ROOT.
$root      = if ($env:GRAPHIFY_ROOT) { $env:GRAPHIFY_ROOT } else { $PSScriptRoot }
$health    = Join-Path $root 'health.json'
$alertsOut = Join-Path $root 'alerts.json'
$log       = Join-Path $root 'health-check-log.txt'
$verCache  = Join-Path $root 'pypi-version-cache.json'
$lkgPath   = Join-Path $root 'publish-settings.lkg.json'
$taskName  = 'Graphify Weekly Graph Refresh'

# Refresh is weekly; 8 days allows one missed-by-hours run before alerting.
$StaleAfterDays = 8

function NowIso { (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture) }
function Log { param([string]$m) "$(NowIso) $m" | Add-Content -Path $log -Encoding utf8 }

$alerts = @()
# Message/Action/Context are PUBLIC (path-free templates). Detail is local-only.
# Returns the alert so a caller can attach context/detail to it later.
function Add-Alert {
    param([string]$Severity, [string]$Code, [string]$Message, [string]$Action = '', [string]$Detail = '')
    $a = [pscustomobject][ordered]@{
        severity = $Severity; code = $Code; message = $Message; action = $Action; context = ''; detail = $Detail
    }
    $script:alerts += $a
    return $a
}

# -- 0. local config (python path, dashboard dirs, targets) -------------------
# Same file Refresh-Graphs.ps1 reads. Any config failure is ONE authoritative
# error alert, `config-invalid`; what that failure causes downstream (the
# refresh's recorded config abort, the task's exit 1) is folded into it.
# Settings resolve even when targets are invalid, so the dashboard still gets
# a current status. If the JSON is unreadable, or python_exe / dashboard_dirs
# themselves fail validation, that setting comes from env, else from the
# last-known-good settings saved from the last fully valid config -- never
# invented.

. (Join-Path $PSScriptRoot 'GraphifyConfig.ps1')
. (Join-Path $PSScriptRoot 'GraphifyPublic.ps1')
$cfg = Get-GraphifyConfig
$configAlert = $null
if ($cfg.Status -ne 'ok') {
    $cfgDetail = "config $($cfg.Status): $($cfg.Errors -join ' | ')"
    $configAlert = Add-Alert 'error' 'config-invalid' 'Graphify local configuration is missing or invalid.' `
        'Fix graphify.local.json on the refresh machine (details in its local alerts.json), then re-run Refresh-Graphs.ps1.' `
        $cfgDetail
    Log $cfgDetail
}

$python   = $cfg.PythonExe
$dashDirs = @($cfg.DashboardDirs)
if ($cfg.Status -eq 'ok') {
    # Only a fully valid config becomes last-known-good, and only its own
    # values: a one-off env override must not replace the saved settings.
    try {
        ConvertTo-Json ([ordered]@{ saved_at = NowIso; python_exe = $cfg.FilePythonExe; dashboard_dirs = @($cfg.FileDashboardDirs) }) |
            Set-Content -Path $lkgPath -Encoding utf8
    } catch { Log "could not save last-known-good settings: $($_.Exception.Message)" }
} else {
    $needPython = -not $env:PYTHON_EXE -and (-not $cfg.Parsed -or $cfg.PythonExeFailed)
    $needDash   = -not $env:GRAPHIFY_DASHBOARD_DIRS -and (-not $cfg.Parsed -or $cfg.DashboardDirsFailed)
    if ($needPython -or $needDash) {
        $lkg = $null
        try { $lkg = Get-Content -LiteralPath $lkgPath -Raw -ErrorAction Stop | ConvertFrom-Json } catch {}
        if ($lkg) {
            if ($needPython -and $lkg.python_exe -is [string] -and $lkg.python_exe) { $python = $lkg.python_exe }
            if ($needDash) {
                # Keep the entries that did validate; add the saved ones.
                $dashDirs = @(@($dashDirs) + @($lkg.dashboard_dirs | Where-Object { $_ -is [string] -and $_ }) | Select-Object -Unique)
            }
            Log "config settings unusable; using last-known-good settings saved $($lkg.saved_at)"
        } elseif ($needDash -and $dashDirs.Count -eq 0) {
            Log 'dashboard_dirs unusable and no last-known-good settings: dashboard status NOT published'
        }
    }
}

# -- 1. the refresh record ---------------------------------------------------

$h = $null
try { $h = Get-Content $health -Raw -ErrorAction Stop | ConvertFrom-Json }
catch { Add-Alert 'error' 'no-health-record' 'No graphify health record found - the weekly refresh has never completed.' 'Run Refresh-Graphs.ps1 manually.' | Out-Null }

$installed = if ($h) { "$($h.installed_version)" } else { '' }
if (-not $installed) {
    $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $installed = (& $python -c "from importlib.metadata import version; print(version('graphifyy'))" 2>$null | Select-Object -First 1)
    $ErrorActionPreference = $eap
}

if ($h) {
    if ($h.last_run -and $h.last_run.config_error) {
        # Same incident as a current config failure: fold it in. Otherwise the
        # config was repaired after the aborted run; ask for a re-run. Either
        # way the recorded config_error (local paths, parser text) stays local.
        $prior = "last refresh ($($h.last_run.ended_at)) aborted: $($h.last_run.config_error)"
        if ($configAlert) {
            $configAlert.detail += " | $prior"
        } else {
            $configAlert = Add-Alert 'error' 'refresh-config' 'Graphify refresh could not run because local configuration needed attention.' `
                'Configuration now loads; re-run Refresh-Graphs.ps1 to refresh the graphs.' $prior
        }
    } elseif ($h.last_run -and $h.last_run.exit_code -ne 0) {
        Add-Alert 'error' 'refresh-failed' "Last graph refresh exited $(ConvertTo-PublicInt $h.last_run.exit_code)." 'Check refresh-log.txt for the failing target.' | Out-Null
    }
    # Public text takes only validated values: the target name through the same
    # identifier check as the targets[] projection, numbers as numbers. Commit
    # SHAs stay in the local detail.
    foreach ($t in @($h.last_run.targets)) {
        if ($null -eq $t) { continue }
        $tn = ConvertTo-PublicName $t.name
        if (-not $t.ok) {
            Add-Alert 'error' 'target-failed' "Refresh failed for $tn (exit $(ConvertTo-PublicInt $t.exit_code))." 'Check refresh-log.txt.' | Out-Null
            continue
        }
        if ($t.head_match -eq $false) {
            Add-Alert 'warn' 'head-behind' "${tn}: graph was built from an older commit than the repo HEAD." `
                'Commits landed after the last refresh; re-run Refresh-Graphs.ps1 for current signals.' `
                "built_at_commit=$($t.built_at_commit) repo_head=$($t.repo_head)" | Out-Null
        }
        if ($t.drift_warning) {
            Add-Alert 'warn' 'count-drift' "${tn}: node count moved $(ConvertTo-PublicNumber $t.node_drift_pct)% (was $(ConvertTo-PublicInt $t.prev_nodes), now $(ConvertTo-PublicInt $t.nodes)) - above the $(ConvertTo-PublicNumber $h.drift_threshold_pct)% threshold." 'Confirm a refactor explains it; otherwise inspect the extraction scope.' | Out-Null
        }
    }

    if ($h.last_success_at) {
        $age = (Get-Date).ToUniversalTime() - ([datetime]::Parse($h.last_success_at)).ToUniversalTime()
        if ($age.TotalDays -gt $StaleAfterDays) {
            Add-Alert 'error' 'stale' "No successful graph refresh in $([math]::Floor($age.TotalDays)) days (limit $StaleAfterDays)." 'The weekly task may be disabled or failing.' | Out-Null
        }
    } else {
        Add-Alert 'error' 'never-succeeded' 'No successful graph refresh on record.' 'Run Refresh-Graphs.ps1 manually.' | Out-Null
    }
}

# -- 2. how the scheduled task actually exited -------------------------------
# A task can fail in ways the script never sees (never launched, killed,
# wrong path), leaving the failure buried in Task Scheduler.

$taskInfo = $null
try {
    $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
    if ($null -ne $taskInfo.LastTaskResult -and $taskInfo.LastTaskResult -ne 0 -and $taskInfo.LastTaskResult -ne 267011) {
        $hex = '0x{0:X}' -f $taskInfo.LastTaskResult
        # Exit 1 is exactly what Refresh-Graphs.ps1 returns on a config abort:
        # context on the config incident, not a second competing error.
        if ($configAlert -and $taskInfo.LastTaskResult -eq 1 -and $h -and $h.last_run.config_error) {
            $configAlert.context = "Scheduled task '$taskName' last exited $hex (the config abort)."
        } else {
            Add-Alert 'error' 'task-result' "Scheduled task '$taskName' last exited $hex." 'Open Task Scheduler history for the failure detail.' | Out-Null
        }
    }
} catch {
    Add-Alert 'error' 'task-missing' "Scheduled task '$taskName' not found - weekly refresh is not registered." 'Re-register it with schtasks /Create.' | Out-Null
}

# -- 3. PyPI: is there a newer release? (notify only, never install) ---------

$latest = ''; $verChecked = $null; $verSource = 'none'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $resp   = Invoke-RestMethod -Uri 'https://pypi.org/pypi/graphifyy/json' -TimeoutSec 25 -UseBasicParsing
    $latest = "$($resp.info.version)"
    $verChecked = NowIso; $verSource = 'pypi'
    ConvertTo-Json ([ordered]@{ latest = $latest; checked_at = $verChecked }) | Set-Content -Path $verCache -Encoding utf8
} catch {
    # Offline or PyPI hiccup: fall back to the cached answer rather than
    # claiming "up to date" on no evidence.
    Log "PyPI check failed: $($_.Exception.Message)"
    try {
        $c = Get-Content $verCache -Raw -ErrorAction Stop | ConvertFrom-Json
        $latest = "$($c.latest)"; $verChecked = "$($c.checked_at)"; $verSource = 'cache'
    } catch {
        Add-Alert 'warn' 'version-check-failed' 'Could not reach PyPI and no cached version is available.' 'Network issue; the check retries tomorrow.' | Out-Null
    }
}

# Versions come from PyPI, a cache file and python stdout: anything that is not
# a plain version string is dropped before it can reach public alert text.
$installed = ConvertTo-PublicVersion $installed
$latest    = ConvertTo-PublicVersion $latest
if ($latest -and $installed -and $latest -ne $installed) {
    Add-Alert 'info' 'update-available' "graphifyy $latest is available (installed $installed)." 'Upgrade is deliberate only: pip install graphifyy==<ver>, then rebuild both graphs with extract --code-only --force. Node IDs can change.' | Out-Null
}

# -- 4. emit ------------------------------------------------------------------

$errCount  = @($alerts | Where-Object { $_.severity -eq 'error' }).Count
$warnCount = @($alerts | Where-Object { $_.severity -eq 'warn'  }).Count
$status    = if ($errCount) { 'error' } elseif ($warnCount) { 'warn' } else { 'ok' }

$doc = [ordered]@{
    checked_at        = NowIso
    status            = $status
    installed_version = $installed
    latest_version    = $latest
    version_checked   = $verChecked
    version_source    = $verSource
    update_available  = [bool]($latest -and $installed -and $latest -ne $installed)
    last_success_at   = $(if ($h) { $h.last_success_at } else { $null })
    task_last_result  = $(if ($taskInfo) { $taskInfo.LastTaskResult } else { $null })
    task_next_run     = $(if ($taskInfo -and $taskInfo.NextRunTime) { ([datetime]$taskInfo.NextRunTime).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture) } else { $null })
    alerts            = $alerts
    targets           = @(if ($h) { $h.last_run.targets })
}

# alerts.json: LOCAL, full detail (alert detail, target scan paths).
ConvertTo-Json $doc -Depth 6 | Set-Content -Path $alertsOut -Encoding utf8

# graphify-health.js: PUBLIC. Allowlist projection + independent leak guard;
# a guard hit publishes an error stub instead (never leaves the stale file).
# Written into each folder in dashboard_dirs (config) /
# $env:GRAPHIFY_DASHBOARD_DIRS; empty = skip.
if ($dashDirs.Count -gt 0) {
    $pubJson = ConvertTo-Json (ConvertTo-GraphifyPublicHealth -Doc $doc) -Depth 6 -Compress
    $leaks = @(Test-GraphifyPublicSafe -Json $pubJson)
    if ($leaks.Count) {
        Log "public projection rejected ($($leaks -join ', ')); publishing error stub"
        $pubJson = ConvertTo-Json (New-GraphifyPublicRejectedStub -CheckedAt $doc.checked_at) -Depth 6 -Compress
        $errCount++
    }
    $js = 'window.GRAPHIFY_HEALTH = ' + $pubJson + ';'
    foreach ($dir in $dashDirs) {
        if (Test-Path -LiteralPath $dir -PathType Container) {
            try { Set-Content -LiteralPath (Join-Path $dir 'graphify-health.js') -Value $js -Encoding utf8 }
            catch { Log "could not write graphify-health.js to ${dir}: $($_.Exception.Message)"; $errCount++ }
        } else {
            Log "dashboard dir missing, graphify-health.js not written: $dir"
        }
    }
}

Log "status=$status errors=$errCount warns=$warnCount installed=$installed latest=$latest ($verSource)"

# -- 5. toast, only when someone is actually at the machine -------------------

if ($alerts.Count -and [Environment]::UserInteractive) {
    $top  = @($alerts | Sort-Object @{ E = { switch ($_.severity) { 'error' { 0 } 'warn' { 1 } default { 2 } } } })[0]
    $body = "$($top.message)" + $(if ($alerts.Count -gt 1) { " (+$($alerts.Count - 1) more)" } else { '' })
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Information
        $icon.BalloonTipTitle = "Graphify health: $status"
        $icon.BalloonTipText  = $body
        $icon.Visible = $true
        $icon.ShowBalloonTip(10000)
        Start-Sleep -Seconds 8
        $icon.Dispose()
    } catch { Log "toast failed: $($_.Exception.Message)" }
}

# Exit non-zero on error-severity findings so Task Scheduler's own
# LastTaskResult reflects the health state too.
if ($errCount) { exit 1 } else { exit 0 }

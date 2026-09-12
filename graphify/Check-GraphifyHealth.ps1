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
# OWNERSHIP: this script owns alerts.json and graphify-health.js.
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
$python    = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { 'python' }
$taskName  = 'Graphify Weekly Graph Refresh'

# Refresh is weekly; 8 days allows one missed-by-hours run before alerting.
$StaleAfterDays = 8

function NowIso { (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
function Log { param([string]$m) "$(NowIso) $m" | Add-Content -Path $log -Encoding utf8 }

$alerts = @()
function Add-Alert {
    param([string]$Severity, [string]$Code, [string]$Message, [string]$Action = '')
    $script:alerts += [pscustomobject][ordered]@{
        severity = $Severity; code = $Code; message = $Message; action = $Action
    }
}

# -- 1. the refresh record ---------------------------------------------------

$h = $null
try { $h = Get-Content $health -Raw -ErrorAction Stop | ConvertFrom-Json }
catch { Add-Alert 'error' 'no-health-record' 'No graphify health record found - the weekly refresh has never completed.' 'Run Refresh-Graphs.ps1 manually.' }

$installed = if ($h) { "$($h.installed_version)" } else { '' }
if (-not $installed) {
    $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $installed = (& $python -c "from importlib.metadata import version; print(version('graphifyy'))" 2>$null | Select-Object -First 1)
    $ErrorActionPreference = $eap
}

if ($h) {
    if ($h.last_run -and $h.last_run.exit_code -ne 0) {
        Add-Alert 'error' 'refresh-failed' "Last graph refresh exited $($h.last_run.exit_code)." 'Check refresh-log.txt for the failing target.'
    }
    foreach ($t in @($h.last_run.targets)) {
        if (-not $t.ok) {
            Add-Alert 'error' 'target-failed' "Refresh failed for $($t.name) (exit $($t.exit_code))." 'Check refresh-log.txt.'
            continue
        }
        if ($t.head_match -eq $false) {
            $bc = "$($t.built_at_commit)"; $rh = "$($t.repo_head)"
            Add-Alert 'warn' 'head-behind' "$($t.name): graph built from $($bc.Substring(0,[Math]::Min(8,$bc.Length))) but repo HEAD is $($rh.Substring(0,[Math]::Min(8,$rh.Length)))." 'Commits landed after the last refresh; re-run Refresh-Graphs.ps1 for current signals.'
        }
        if ($t.drift_warning) {
            Add-Alert 'warn' 'count-drift' "$($t.name): node count moved $($t.node_drift_pct)% (was $($t.prev_nodes), now $($t.nodes)) - above the $($h.drift_threshold_pct)% threshold." 'Confirm a refactor explains it; otherwise inspect the extraction scope.'
        }
    }

    if ($h.last_success_at) {
        $age = (Get-Date).ToUniversalTime() - ([datetime]::Parse($h.last_success_at)).ToUniversalTime()
        if ($age.TotalDays -gt $StaleAfterDays) {
            Add-Alert 'error' 'stale' "No successful graph refresh in $([math]::Floor($age.TotalDays)) days (limit $StaleAfterDays)." 'The weekly task may be disabled or failing.'
        }
    } else {
        Add-Alert 'error' 'never-succeeded' 'No successful graph refresh on record.' 'Run Refresh-Graphs.ps1 manually.'
    }
}

# -- 2. how the scheduled task actually exited -------------------------------
# A task can fail in ways the script never sees (never launched, killed,
# wrong path), leaving the failure buried in Task Scheduler.

$taskInfo = $null
try {
    $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
    if ($null -ne $taskInfo.LastTaskResult -and $taskInfo.LastTaskResult -ne 0 -and $taskInfo.LastTaskResult -ne 267011) {
        Add-Alert 'error' 'task-result' "Scheduled task '$taskName' last exited 0x$('{0:X}' -f $taskInfo.LastTaskResult)." 'Open Task Scheduler history for the failure detail.'
    }
} catch {
    Add-Alert 'error' 'task-missing' "Scheduled task '$taskName' not found - weekly refresh is not registered." 'Re-register it with schtasks /Create.'
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
        Add-Alert 'warn' 'version-check-failed' 'Could not reach PyPI and no cached version is available.' 'Network issue; the check retries tomorrow.'
    }
}

if ($latest -and $installed -and $latest -ne $installed) {
    Add-Alert 'info' 'update-available' "graphifyy $latest is available (installed $installed)." 'Upgrade is deliberate only: pip install graphifyy==<ver>, then rebuild both graphs with extract --code-only --force. Node IDs can change.'
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
    task_next_run     = $(if ($taskInfo -and $taskInfo.NextRunTime) { ([datetime]$taskInfo.NextRunTime).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') } else { $null })
    alerts            = $alerts
    targets           = $(if ($h) { $h.last_run.targets } else { @() })
}

ConvertTo-Json $doc -Depth 6 | Set-Content -Path $alertsOut -Encoding utf8

# Optional: render health as a JS global for a dashboard panel. Set
# $env:GRAPHIFY_DASHBOARD_DIRS to a semicolon-separated list of folders to
# receive graphify-health.js; leave unset to skip (the default).
$dashDirs = if ($env:GRAPHIFY_DASHBOARD_DIRS) { $env:GRAPHIFY_DASHBOARD_DIRS -split ';' } else { @() }
if ($dashDirs.Count -gt 0) {
    $js = 'window.GRAPHIFY_HEALTH = ' + (ConvertTo-Json $doc -Depth 6 -Compress) + ';'
    foreach ($dir in $dashDirs) {
        if (Test-Path $dir) {
            try { Set-Content -Path (Join-Path $dir 'graphify-health.js') -Value $js -Encoding utf8 }
            catch { Log "could not write graphify-health.js to ${dir}: $($_.Exception.Message)" }
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

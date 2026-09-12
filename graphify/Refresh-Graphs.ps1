# ============================================================
# Refresh-Graphs.ps1 — scheduled AST-only refresh of the graphify code graphs.
#
# Edit the $targets array below to point at YOUR repositories.
# Runs `graphify extract --code-only`
# (no LLM, no API key; incremental via the manifest gate), then re-clusters +
# regenerates GRAPH_REPORT.md, then writes a graph-meta.json sidecar
# (wall-clock + repo HEAD + package version; graphify itself stamps
# built_at_commit inside graph.json) and a structured health record.
#
# OWNERSHIP: this script owns graph.json refreshes, graph-meta.json,
#   health.json, and health-history.jsonl. If you keep a separate metrics
#   ledger or dashboard renderer, this script never touches those.
#   - alerts.json is owned by Check-GraphifyHealth.ps1 (daily task).
#
# WHY A SCHEDULED TASK, NOT graphify's git hook: `graphify hook install` fires
# unbounded concurrent rebuilds with no lock on every commit/checkout
# (upstream #791, closed without fix). Never install the hook.
#
# PYTHONHASHSEED=0: Louvain clustering is iteration-order-sensitive (#1667)
# and the deterministic Leiden path can't run on Python >=3.13 (graspologic).
# Seeding makes graph.json byte-identical across runs on identical input.
#
# `extract --code-only` (NOT `update`): `graphify update` has no --code-only
# concept — proven 2026-08-07, it re-extracted the skipped docs and grew the
# just-built Add-Ins graph 6,585 -> 8,610 nodes (1,987 .md nodes). Plain
# extract honors --code-only AND is incremental (the manifest gate skips
# unchanged files; --force is what disables that). Trade-off: we lose update's
# refuses-to-shrink guard — the health record's drift warning covers that.
#
# REFRESH vs UPGRADE: this script never installs or upgrades graphifyy.
# The 0.9.x upgrade changed node-ID behavior; an unattended upgrade would
# silently invalidate graphs, baselines, and downstream signals.
# Check-GraphifyHealth.ps1 notifies when PyPI has a newer release — a human
# decides when to upgrade and rebuild.
# ============================================================

$ErrorActionPreference = 'Stop'
# $root defaults to this script's own folder; override with -Root or $env:GRAPHIFY_ROOT.
$root     = if ($env:GRAPHIFY_ROOT) { $env:GRAPHIFY_ROOT } else { $PSScriptRoot }
$log      = Join-Path $root 'refresh-log.txt'
$health   = Join-Path $root 'health.json'
$history  = Join-Path $root 'health-history.jsonl'
# Resolve graphify/python from PATH; override with env vars if not on PATH.
$graphify = if ($env:GRAPHIFY_EXE) { $env:GRAPHIFY_EXE } else { 'graphify' }
$python   = if ($env:PYTHON_EXE)   { $env:PYTHON_EXE }   else { 'python' }

# Node-count drift above this (percent, vs the previous recorded run) sets
# drift_warning on the target record; Check-GraphifyHealth.ps1 turns that into
# an alert. Approved default 15% — raise deliberately after big refactors.
$DriftThresholdPct = 15

$env:PYTHONHASHSEED             = '0'
$env:GRAPHIFY_QUERY_LOG_DISABLE = '1'

function NowIso { (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }

$runStart = NowIso
"=== $runStart refresh start ===" | Add-Content -Path $log -Encoding utf8

# Run a native command without PS 5.1 turning benign stderr into a terminating
# error under EAP=Stop; log combined output, return the real exit code.
function Invoke-Logged {
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @())
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out  = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $eap
    if ($out) { $out | Add-Content -Path $log -Encoding utf8 }
    return $code
}

# Counts + built_at_commit straight from graph.json (the artifact, not the
# log), so the health record can never disagree with what is on disk.
function Get-GraphStats {
    param([Parameter(Mandatory)][string]$GraphJson)
    $eap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $out = & $python -c @"
import json, pathlib, sys
d = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
nodes = d.get('nodes', [])
comms = {n.get('community') for n in nodes if n.get('community') is not None}
print(json.dumps({'nodes': len(nodes),
                  'edges': len(d.get('links', d.get('edges', []))),
                  'communities': len(comms),
                  'built_at_commit': d.get('built_at_commit') or ''}))
"@ $GraphJson 2>$null
    $ErrorActionPreference = $eap
    if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1 | ConvertFrom-Json) }
    return $null
}

# Previous health record, for drift comparison. Absent/corrupt -> no drift calc.
$prevHealth = $null
try { $prevHealth = Get-Content $health -Raw -ErrorAction Stop | ConvertFrom-Json } catch {}

$eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$installedVer = (& $python -c "from importlib.metadata import version; print(version('graphifyy'))" 2>$null | Select-Object -First 1)
$ErrorActionPreference = $eap

# EDIT THESE — one entry per repo you want graphed.
#   Name = label used in reports / health records
#   Scan = folder to extract the AST graph from (graphify-out\ is written here)
#   Repo = repo root (used to record HEAD; may equal Scan)
$targets = @(
    @{ Name = 'my-backend';  Scan = 'C:\path\to\backend'; Repo = 'C:\path\to\repo' },
    @{ Name = 'my-frontend'; Scan = 'C:\path\to\frontend'; Repo = 'C:\path\to\repo' }
)

$failed  = 0
$records = @()
foreach ($t in $targets) {
    $tStart    = NowIso
    $graphJson = Join-Path $t.Scan 'graphify-out\graph.json'
    $rec = [ordered]@{
        name = $t.Name; scan = $t.Scan; started_at = $tStart; ended_at = $null
        ok = $false; exit_code = $null
        nodes = $null; edges = $null; communities = $null
        prev_nodes = $null; node_drift_pct = $null; drift_warning = $false
        built_at_commit = $null; repo_head = $null; head_match = $null
        code_only = $true; force = $false; pythonhashseed = '0'
    }

    if (-not (Test-Path $graphJson)) {
        "SKIP $($t.Name): no graph at $graphJson" | Add-Content -Path $log -Encoding utf8
        $rec.ended_at = NowIso; $rec.exit_code = -1
        $records += [pscustomobject]$rec
        $failed++
        continue
    }

    Push-Location $t.Scan
    try {
        $code = Invoke-Logged $graphify @('extract', '.', '--code-only')
        $rec.exit_code = $code
        if ($code -ne 0) {
            $failed++
            "FAIL $($t.Name): graphify extract returned non-zero" | Add-Content -Path $log -Encoding utf8
            $rec.ended_at = NowIso
            $records += [pscustomobject]$rec
            continue
        }

        # Always re-cluster + regenerate GRAPH_REPORT.md: extract rewrites
        # graph.json even when every file is cached ("0 re-extracted"), so a
        # file-hash fast-path never fires. Both steps are cheap, deterministic
        # (PYTHONHASHSEED=0), and LLM-free; --no-label keeps community naming
        # out of the unattended path (naming is an interactive/editorial pass).
        if ((Invoke-Logged $graphify @('cluster-only', '.', '--no-label')) -ne 0) {
            "WARN $($t.Name): cluster-only failed - graph.json is fresh, report may lag" | Add-Content -Path $log -Encoding utf8
        }

        $stats = Get-GraphStats $graphJson
        $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $head = (& git -C $t.Repo rev-parse HEAD 2>$null | Select-Object -First 1)
        $ErrorActionPreference = $eap

        if ($stats) {
            $rec.nodes = $stats.nodes; $rec.edges = $stats.edges; $rec.communities = $stats.communities
            $rec.built_at_commit = $stats.built_at_commit
            $rec.repo_head  = "$head"
            $rec.head_match = ($stats.built_at_commit -and $head -and ($stats.built_at_commit -eq "$head"))

            $prevRec = $null
            if ($prevHealth -and $prevHealth.last_run -and $prevHealth.last_run.targets) {
                $prevRec = $prevHealth.last_run.targets | Where-Object { $_.name -eq $t.Name -and $_.nodes } | Select-Object -First 1
            }
            if ($prevRec) {
                $rec.prev_nodes = $prevRec.nodes
                if ($prevRec.nodes -gt 0) {
                    $rec.node_drift_pct = [math]::Round([math]::Abs($stats.nodes - $prevRec.nodes) * 100.0 / $prevRec.nodes, 1)
                    $rec.drift_warning  = ($rec.node_drift_pct -gt $DriftThresholdPct)
                }
            }
            $rec.ok = $true
        } else {
            "WARN $($t.Name): could not read stats from graph.json" | Add-Content -Path $log -Encoding utf8
        }

        # Sidecar stamp (graph.json carries no wall-clock timestamp upstream).
        $meta = [ordered]@{
            generated_at      = NowIso
            built_at_commit   = "$head"
            graphifyy_version = "$installedVer"
            refreshed_by      = 'Refresh-Graphs.ps1'
        }
        ConvertTo-Json $meta | Set-Content -Path (Join-Path $t.Scan 'graphify-out\graph-meta.json') -Encoding utf8

        $rec.ended_at = NowIso
        $records += [pscustomobject]$rec
        "$($t.Name): OK ($($rec.nodes) nodes, drift $($rec.node_drift_pct)%, head_match=$($rec.head_match))" | Add-Content -Path $log -Encoding utf8
    } finally {
        Pop-Location
    }
}

$runEnd = NowIso
$run = [ordered]@{
    started_at = $runStart
    ended_at   = $runEnd
    exit_code  = $(if ($failed) { 1 } else { 0 })
    targets    = $records
}

$lastSuccess = $runEnd
if ($failed) {
    $lastSuccess = $null
    if ($prevHealth -and $prevHealth.last_success_at) { $lastSuccess = $prevHealth.last_success_at }
}

$healthDoc = [ordered]@{
    updated_at          = $runEnd
    installed_version   = "$installedVer"
    drift_threshold_pct = $DriftThresholdPct
    last_success_at     = $lastSuccess
    last_run            = $run
}
ConvertTo-Json $healthDoc -Depth 6 | Set-Content -Path $health -Encoding utf8
(ConvertTo-Json $run -Depth 6 -Compress) | Add-Content -Path $history -Encoding utf8

"=== $runEnd refresh done ($failed failed) ===" | Add-Content -Path $log -Encoding utf8
if ($failed) { exit 1 } else { exit 0 }

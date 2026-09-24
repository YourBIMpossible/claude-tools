# ============================================================
# GraphifyConfig.ps1 — dot-sourced by Refresh-Graphs.ps1 and
# Check-GraphifyHealth.ps1. Loads the machine-local config that holds the
# real scan targets and tool paths, so the tracked scripts carry no local paths.
#
# File: graphify.local.json next to these scripts (gitignored), or the path in
# $env:GRAPHIFY_CONFIG. Template: graphify.local.example.json.
#
# Precedence per setting: environment variable > config file > default.
# A missing/invalid config is NOT silently defaulted: Read-GraphifyConfig
# throws, and each caller turns that into a recorded, non-zero failure.
# ============================================================

function Get-GraphifyConfigPath {
    if ($env:GRAPHIFY_CONFIG) { return $env:GRAPHIFY_CONFIG }
    return (Join-Path $PSScriptRoot 'graphify.local.json')
}

# Returns a pscustomobject: Path, Targets (Name/Scan/Repo hashtables),
# GraphifyExe, PythonExe, DashboardDirs. Throws with an actionable message.
function Read-GraphifyConfig {
    param([switch]$RequireTargets)
    $path = Get-GraphifyConfigPath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "graphify config not found at '$path'. Copy graphify.local.example.json to graphify.local.json and fill in real targets (or set GRAPHIFY_CONFIG)."
    }
    try {
        $raw = Get-Content -LiteralPath $path -Raw -Encoding utf8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "graphify config '$path' is not valid JSON: $($_.Exception.Message)"
    }

    $targets = @()
    $i = 0
    foreach ($t in @($raw.targets)) {
        if ($null -eq $t) { continue }
        $i++
        $name = "$($t.name)"; $scan = "$($t.scan)"
        $repo = if ($t.repo) { "$($t.repo)" } else { $scan }
        if (-not $name -or -not $scan) {
            throw "graphify config '$path': targets[$i] needs non-empty 'name' and 'scan'."
        }
        if ($scan -match '^<.*>$' -or $repo -match '^<.*>$') {
            throw "graphify config '$path': targets[$i] ('$name') still holds a template placeholder."
        }
        $targets += @{ Name = $name; Scan = $scan; Repo = $repo }
    }
    if ($RequireTargets -and $targets.Count -eq 0) {
        throw "graphify config '$path' defines no targets."
    }

    $graphify = if ($env:GRAPHIFY_EXE) { $env:GRAPHIFY_EXE } elseif ($raw.graphify_exe) { "$($raw.graphify_exe)" } else { 'graphify' }
    $python   = if ($env:PYTHON_EXE)   { $env:PYTHON_EXE }   elseif ($raw.python_exe)   { "$($raw.python_exe)" }   else { 'python' }
    $dash     = if ($env:GRAPHIFY_DASHBOARD_DIRS) { @($env:GRAPHIFY_DASHBOARD_DIRS -split ';' | Where-Object { $_ }) }
                else { @($raw.dashboard_dirs | Where-Object { $_ } | ForEach-Object { "$_" }) }

    return [pscustomobject]@{
        Path          = $path
        Targets       = $targets
        GraphifyExe   = $graphify
        PythonExe     = $python
        DashboardDirs = $dash
    }
}

# ============================================================
# GraphifyConfig.ps1 — dot-sourced by Refresh-Graphs.ps1 and
# Check-GraphifyHealth.ps1. Loads the machine-local config that holds the
# real scan targets and tool paths, so the tracked scripts carry no local paths.
#
# File: graphify.local.json next to these scripts (gitignored), or the path in
# $env:GRAPHIFY_CONFIG. Template: graphify.local.example.json.
#
# Four separate stages, so a bad target never hides the settings the health
# check needs to publish a current status:
#   1. parse + shape    (Import-GraphifyConfigJson)   file exists, is a JSON object
#   2. path normalizing (Resolve-GraphifyConfigPath)  relative -> config dir, absolute
#   3. settings         (python/graphify exe, dashboard dirs) resolved independently
#   4. targets          (Test-GraphifyConfigTargets)  names, paths, placeholders
# Get-GraphifyConfig runs all four and NEVER throws: it returns Status + local
# Errors. Callers decide: Refresh-Graphs.ps1 refuses to run unless Status is
# 'ok'; Check-GraphifyHealth.ps1 alerts but still publishes.
#
# Precedence per setting: environment variable > config file > default.
# Environment values are explicit overrides and are used verbatim.
#
# Relative-path policy (config file values only): every filesystem path —
# targets[].scan, targets[].repo, dashboard_dirs[], and graphify_exe /
# python_exe when they contain a path separator — resolves against the folder
# holding the config file and is normalized to an absolute path. A bare command
# name ("python", "graphify") is looked up on PATH. Drive- or root-relative
# forms ("\x", "C:x") are rejected: they depend on the current drive.
#
# Errors are LOCAL diagnostics (they name the field and the config path). They
# belong in local logs/health/alerts only — never in the public dashboard.
# ============================================================

$script:GraphifyTargetNamePattern = '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
# A template placeholder anywhere in a value, e.g. "<path-to-backend>" or "repos/<name>".
$script:GraphifyPlaceholderPattern = '<[^<>]*>'

function Get-GraphifyConfigPath {
    if ($env:GRAPHIFY_CONFIG) { return $env:GRAPHIFY_CONFIG }
    return (Join-Path $PSScriptRoot 'graphify.local.json')
}

# Stage 1. Returns @{ Raw; Error; Status } — Status 'ok' | 'missing' | 'invalid-json'.
function Import-GraphifyConfigJson {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return @{ Raw = $null; Status = 'missing'
                  Error = "graphify config not found at '$Path'. Copy graphify.local.example.json to graphify.local.json and fill in real targets (or set GRAPHIFY_CONFIG)." }
    }
    try {
        $text = Get-Content -LiteralPath $Path -Raw -Encoding utf8 -ErrorAction Stop
    } catch {
        return @{ Raw = $null; Status = 'invalid-json'; Error = "graphify config '$Path' could not be read: $($_.Exception.Message)" }
    }
    # Checked on the text: pwsh 7 unwraps a one-element top-level array, 5.1 does not.
    if (-not $text -or -not $text.TrimStart().StartsWith('{')) {
        return @{ Raw = $null; Status = 'invalid-json'; Error = "graphify config '$Path' must be a JSON object." }
    }
    try {
        $raw = $text | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return @{ Raw = $null; Status = 'invalid-json'; Error = "graphify config '$Path' is not valid JSON: $($_.Exception.Message)" }
    }
    if ($raw -isnot [System.Management.Automation.PSCustomObject]) {
        return @{ Raw = $null; Status = 'invalid-json'; Error = "graphify config '$Path' must be a JSON object." }
    }
    return @{ Raw = $raw; Status = 'ok'; Error = $null }
}

function Test-GraphifyFullyQualifiedPath {
    param([Parameter(Mandatory)][string]$Value)
    if ([IO.Path]::DirectorySeparatorChar -eq '\') {
        return ($Value -match '^[A-Za-z]:[\\/]' -or $Value -match '^[\\/]{2}[^\\/]')
    }
    return $Value.StartsWith('/')
}

# Stage 2. Returns @{ Value; Error }. Value is absolute, or $null on error.
function Resolve-GraphifyConfigPath {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value,
          [Parameter(Mandatory)][string]$BaseDir,
          [Parameter(Mandatory)][string]$Field)
    if (-not $Value.Trim()) { return @{ Value = $null; Error = "$Field is empty." } }
    if ($Value -match $script:GraphifyPlaceholderPattern) { return @{ Value = $null; Error = "$Field still holds a template placeholder." } }
    # .NET Framework (PS 5.1) throws on characters pwsh 7 accepts ('|', '"', a stray ':').
    try {
        if (Test-GraphifyFullyQualifiedPath $Value) {
            return @{ Value = [IO.Path]::GetFullPath($Value); Error = $null }
        }
        if ([IO.Path]::IsPathRooted($Value) -or $Value -match '^[A-Za-z]:') {
            return @{ Value = $null; Error = "$Field is drive- or root-relative; use an absolute path or one relative to the config file." }
        }
        return @{ Value = [IO.Path]::GetFullPath([IO.Path]::Combine($BaseDir, $Value)); Error = $null }
    } catch {
        return @{ Value = $null; Error = "$Field is not a valid path." }
    }
}

# Executables: a bare command name stays a PATH lookup; anything with a
# separator is a filesystem path and follows the relative-path policy.
function Resolve-GraphifyConfigExe {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value,
          [Parameter(Mandatory)][string]$BaseDir,
          [Parameter(Mandatory)][string]$Field)
    if ($Value -and $Value -notmatch '[\\/]' -and $Value -notmatch $script:GraphifyPlaceholderPattern) { return @{ Value = $Value; Error = $null } }
    return (Resolve-GraphifyConfigPath -Value $Value -BaseDir $BaseDir -Field $Field)
}

# Stage 4. Returns @{ Targets; Errors }.
function Test-GraphifyConfigTargets {
    param($Raw, [Parameter(Mandatory)][string]$BaseDir)
    $errors = @(); $targets = @()
    if ($null -eq $Raw.PSObject.Properties['targets']) {
        return @{ Targets = @(); Errors = @("targets is missing.") }
    }
    $list = $Raw.targets
    if ($list -isnot [array]) {
        return @{ Targets = @(); Errors = @("targets must be a JSON array.") }
    }
    if ($list.Count -eq 0) {
        return @{ Targets = @(); Errors = @("targets defines no targets.") }
    }
    $seen = @{}
    for ($i = 0; $i -lt $list.Count; $i++) {
        $t = $list[$i]; $f = "targets[$i]"
        if ($t -isnot [System.Management.Automation.PSCustomObject]) { $errors += "$f must be an object."; continue }
        $bad = $false
        foreach ($k in 'name', 'scan') {
            if ($t.$k -isnot [string] -or -not $t.$k) { $errors += "$f.$k must be a non-empty string."; $bad = $true }
        }
        if ($null -ne $t.PSObject.Properties['repo'] -and ($t.repo -isnot [string] -or -not $t.repo)) {
            $errors += "$f.repo must be a non-empty string when present."; $bad = $true
        }
        if ($bad) { continue }
        # The name is published on the dashboard: keep it a plain identifier.
        if ($t.name -notmatch $script:GraphifyTargetNamePattern) {
            $errors += "$f.name must match $($script:GraphifyTargetNamePattern) (it is shown publicly)."; continue
        }
        if ($seen.ContainsKey($t.name)) { $errors += "$f.name '$($t.name)' is a duplicate."; continue }
        $seen[$t.name] = $true
        $scan = Resolve-GraphifyConfigPath -Value $t.scan -BaseDir $BaseDir -Field "$f.scan"
        $repoIn = if ($t.repo) { $t.repo } else { $t.scan }
        $repo = Resolve-GraphifyConfigPath -Value $repoIn -BaseDir $BaseDir -Field "$f.repo"
        if ($scan.Error) { $errors += $scan.Error }
        if ($repo.Error -and $t.repo) { $errors += $repo.Error }
        if ($scan.Error -or $repo.Error) { continue }
        $targets += @{ Name = $t.name; Scan = $scan.Value; Repo = $repo.Value }
    }
    return @{ Targets = $targets; Errors = $errors }
}

# Runs all four stages. Never throws. Returns a pscustomobject:
#   Path, BaseDir, Parsed (JSON object loaded), Status, Errors (local detail),
#   Targets (Name/Scan/Repo hashtables, absolute), GraphifyExe, PythonExe,
#   DashboardDirs (effective, env applied), FilePythonExe, FileDashboardDirs
#   (config file only, no env), PythonExeFailed / DashboardDirsFailed (that
#   setting was present in the file but did not validate).
# Status: 'ok' | 'missing' | 'invalid-json' | 'invalid' (parsed, but a setting
# or target failed validation). Settings resolve whenever the JSON parsed, even
# if targets are invalid; an invalid setting falls back to its default.
function Get-GraphifyConfig {
    $path = Get-GraphifyConfigPath
    $full = [IO.Path]::GetFullPath($path)
    $base = Split-Path -Parent $full
    $errors = @()
    $loaded = Import-GraphifyConfigJson -Path $full
    $raw = $loaded.Raw
    if ($loaded.Error) { $errors += $loaded.Error }

    $graphify = 'graphify'; $python = 'python'; $dash = @(); $targets = @()
    $pythonFailed = $false; $dashFailed = $false
    if ($raw) {
        foreach ($pair in @(@('graphify_exe', 'graphify'), @('python_exe', 'python'))) {
            $key = $pair[0]
            if ($null -eq $raw.PSObject.Properties[$key]) { continue }
            $v = $raw.$key
            $r = if ($v -is [string]) { Resolve-GraphifyConfigExe -Value $v -BaseDir $base -Field $key }
                 else { @{ Value = $null; Error = "$key must be a string." } }
            if ($r.Error) {
                $errors += "graphify config '$full': $($r.Error)"
                if ($key -eq 'python_exe') { $pythonFailed = $true }
                continue
            }
            if ($key -eq 'graphify_exe') { $graphify = $r.Value } else { $python = $r.Value }
        }
        if ($null -ne $raw.PSObject.Properties['dashboard_dirs']) {
            $dd = $raw.dashboard_dirs
            if ($dd -isnot [array]) {
                $errors += "graphify config '$full': dashboard_dirs must be a JSON array."; $dashFailed = $true
            } else {
                for ($i = 0; $i -lt $dd.Count; $i++) {
                    if ($dd[$i] -isnot [string]) { $errors += "graphify config '$full': dashboard_dirs[$i] must be a string."; $dashFailed = $true; continue }
                    $r = Resolve-GraphifyConfigPath -Value $dd[$i] -BaseDir $base -Field "dashboard_dirs[$i]"
                    if ($r.Error) { $errors += "graphify config '$full': $($r.Error)"; $dashFailed = $true } else { $dash += $r.Value }
                }
            }
        }
        $tv = Test-GraphifyConfigTargets -Raw $raw -BaseDir $base
        $targets = $tv.Targets
        foreach ($e in $tv.Errors) { $errors += "graphify config '$full': $e" }
    }

    # File-derived values, before env overrides (the health check's last-known-good source).
    $filePython = $python; $fileDash = @($dash)
    if ($env:GRAPHIFY_EXE) { $graphify = $env:GRAPHIFY_EXE }
    if ($env:PYTHON_EXE)   { $python   = $env:PYTHON_EXE }
    if ($env:GRAPHIFY_DASHBOARD_DIRS) { $dash = @($env:GRAPHIFY_DASHBOARD_DIRS -split ';' | Where-Object { $_ }) }

    $status = if ($loaded.Status -ne 'ok') { $loaded.Status } elseif ($errors.Count) { 'invalid' } else { 'ok' }
    return [pscustomobject]@{
        Path          = $full
        BaseDir       = $base
        Parsed        = [bool]$raw
        Status        = $status
        Errors        = @($errors)
        Targets       = @($targets)
        GraphifyExe   = $graphify
        PythonExe     = $python
        DashboardDirs = @($dash)
        FilePythonExe     = $filePython
        FileDashboardDirs = @($fileDash)
        PythonExeFailed     = $pythonFailed
        DashboardDirsFailed = $dashFailed
    }
}

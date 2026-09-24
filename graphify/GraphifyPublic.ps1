# ============================================================
# GraphifyPublic.ps1 — dot-sourced by Check-GraphifyHealth.ps1. The ONLY way a
# health document reaches the dashboard (graphify-health.js, which is served
# publicly).
#
# Allowlist projection, not a denylist: every public field is copied by name
# and type-checked; anything not named here (scan/repo paths, tool paths,
# config errors, alert detail, future fields) never leaves the machine.
# Test-GraphifyPublicSafe is a second, independent guard over the serialized
# result; if it trips, the caller publishes a minimal error stub instead.
#
# Public schema (all other keys are dropped):
#   checked_at, last_success_at, version_checked, task_next_run : ISO-8601 UTC or null
#   status          : ok | warn | error
#   installed_version, latest_version : version string or ''
#   version_source  : pypi | cache | none
#   update_available: bool
#   task_last_result: integer or null
#   alerts[]  : severity (error|warn|info), code (slug), message, action, context
#   targets[] : name (identifier), ok, exit_code, nodes, edges, communities,
#               node_drift_pct, drift_warning, head_match, ended_at
# ============================================================

$script:PublicIsoPattern     = '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$'
$script:PublicVersionPattern = '^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$'
$script:PublicCodePattern    = '^[a-z0-9][a-z0-9-]{0,39}$'
$script:PublicNamePattern    = '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'

function ConvertTo-PublicIso {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [datetime]) { return $Value.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture) }
    $s = "$Value"
    if ($s -match $script:PublicIsoPattern) { return $s }
    return $null
}

function ConvertTo-PublicEnum {
    param($Value, [string[]]$Allowed, $Default)
    if ($Allowed -contains "$Value") { return "$Value" }
    return $Default
}

function ConvertTo-PublicInt {
    param($Value)
    if ($null -eq $Value) { return $null }
    $n = 0L
    if ([long]::TryParse("$Value", [ref]$n)) { return $n }
    return $null
}

function ConvertTo-PublicNumber {
    param($Value)
    if ($null -eq $Value) { return $null }
    $n = 0.0
    if ([double]::TryParse("$Value", [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$n)) { return $n }
    return $null
}

function ConvertTo-PublicBool {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [bool]) { return $Value }
    return $null
}

# A target name as shown publicly: a plain identifier, else the word 'target'.
function ConvertTo-PublicName {
    param($Value)
    $s = "$Value"
    if ($s -match $script:PublicNamePattern) { return $s }
    return 'target'
}

# A version string ('' when absent or not version-shaped).
function ConvertTo-PublicVersion {
    param($Value)
    $s = "$(@($Value) | Select-Object -First 1)".Trim()
    if ($s -match $script:PublicVersionPattern) { return $s }
    return ''
}

function ConvertTo-PublicText {
    param($Value)
    if ($null -eq $Value) { return '' }
    $s = "$Value"
    if ($s.Length -gt 400) { $s = $s.Substring(0, 400) }
    return $s
}

# $Doc: the local health document (alerts.json shape). Returns an ordered
# hashtable containing only allowlisted, type-checked fields.
function ConvertTo-GraphifyPublicHealth {
    param([Parameter(Mandatory)]$Doc)
    $alerts = @()
    foreach ($a in @($Doc.alerts)) {
        if ($null -eq $a) { continue }
        $code = "$($a.code)"
        if ($code -notmatch $script:PublicCodePattern) { $code = 'unknown' }
        $alerts += [ordered]@{
            severity = ConvertTo-PublicEnum $a.severity @('error', 'warn', 'info') 'error'
            code     = $code
            message  = ConvertTo-PublicText $a.message
            action   = ConvertTo-PublicText $a.action
            context  = ConvertTo-PublicText $a.context
        }
    }
    $targets = @()
    foreach ($t in @($Doc.targets)) {
        if ($null -eq $t) { continue }
        $targets += [ordered]@{
            name           = ConvertTo-PublicName $t.name
            ok             = [bool](ConvertTo-PublicBool $t.ok)
            exit_code      = ConvertTo-PublicInt $t.exit_code
            nodes          = ConvertTo-PublicInt $t.nodes
            edges          = ConvertTo-PublicInt $t.edges
            communities    = ConvertTo-PublicInt $t.communities
            node_drift_pct = ConvertTo-PublicNumber $t.node_drift_pct
            drift_warning  = [bool](ConvertTo-PublicBool $t.drift_warning)
            head_match     = ConvertTo-PublicBool $t.head_match
            ended_at       = ConvertTo-PublicIso $t.ended_at
        }
    }
    $iv = ConvertTo-PublicVersion $Doc.installed_version
    $lv = ConvertTo-PublicVersion $Doc.latest_version
    return [ordered]@{
        checked_at        = ConvertTo-PublicIso $Doc.checked_at
        status            = ConvertTo-PublicEnum $Doc.status @('ok', 'warn', 'error') 'error'
        installed_version = $iv
        latest_version    = $lv
        version_checked   = ConvertTo-PublicIso $Doc.version_checked
        version_source    = ConvertTo-PublicEnum $Doc.version_source @('pypi', 'cache', 'none') 'none'
        update_available  = [bool](ConvertTo-PublicBool $Doc.update_available)
        last_success_at   = ConvertTo-PublicIso $Doc.last_success_at
        task_last_result  = ConvertTo-PublicInt $Doc.task_last_result
        task_next_run     = ConvertTo-PublicIso $Doc.task_next_run
        alerts            = $alerts
        targets           = $targets
    }
}

# Independent guard over the serialized public JSON. Returns the list of
# violated rule names (empty = safe). Rules name the leak class, never echo it.
function Test-GraphifyPublicSafe {
    param([Parameter(Mandatory)][string]$Json)
    $rules = [ordered]@{
        'drive-path'     = '[A-Za-z]:(\\\\|/)'
        'backslash'      = '\\\\'
        'posix-home'     = '(^|[^A-Za-z0-9])/(home|Users|root|mnt|tmp|var|etc|opt|srv|Volumes|private|workspace)/'
        'home-relative'  = '(^|[^\w])~/'
        'parent-path'    = '\.\./'
        'url-encoded'    = '(?i)%(3A|5C|2F)'
        'private-key'    = '"(scan|repo|python_exe|graphify_exe|config_error|config_status|config_path|detail|dashboard_dirs|built_at_commit|repo_head)"\s*:'
        'unc-path'       = '(^|[\s"''(])//[A-Za-z0-9]'
    }
    $hits = @()
    foreach ($k in $rules.Keys) { if ($Json -match $rules[$k]) { $hits += $k } }
    return $hits
}

# Minimal stub published when the projection fails the guard: visible error,
# no stale "ok", nothing derived from local state.
function New-GraphifyPublicRejectedStub {
    param([string]$CheckedAt)
    return [ordered]@{
        checked_at = ConvertTo-PublicIso $CheckedAt
        status     = 'error'
        alerts     = @([ordered]@{ severity = 'error'; code = 'public-projection-rejected'
                                   message = 'Graphify health could not be published safely.'
                                   action = 'See the local health-check log on the refresh machine.'; context = '' })
        targets    = @()
    }
}

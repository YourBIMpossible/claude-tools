<#
.SYNOPSIS
    Drift-detection scan of installed Claude Code skills and plugins.

.DESCRIPTION
    Scans every installed skill against an accepted baseline so that only NEW
    findings surface. Known false positives recorded on 2026-07-25 are
    suppressed by the files under .\baselines\.

    One scan per skill -- see SkillTargets.ps1 for why. Short version:
    --baseline is silently ignored under --recursive, and a non-recursive scan
    of a parent directory misses planted skills entirely and reports CLEAN.

    Static analysis is the default and is deterministic. The LLM stage (-Llm)
    is opt-in because SkillSpector's claude_cli provider has a Windows
    temp-directory race (WinError 32) that makes analysis batches fail. When
    batches fail, SkillSpector keeps the findings "unfiltered" and still exits
    normally -- i.e. it silently degrades to static-only. This script treats
    that as INCONCLUSIVE rather than reporting clean off a partial analysis.

    CALIBRATION WARNING: SkillSpector's 0-100 risk score is not trustworthy.
    Measured 2026-07-25: a fixture containing SSH-key exfiltration, curl-pipe-
    bash, and prompt injection scored 10/100 LOW, while a legitimate, well
    documented UI skill scored 100/100 CRITICAL. The score tracks volume of
    reference material, not danger. Read the findings; ignore the score.

.PARAMETER Llm
    Enable the LLM semantic stage via the local claude CLI. Consumes Claude
    subscription usage. Any failed batch makes the whole run INCONCLUSIVE.

.PARAMETER ReportDir
    Where JSON reports are written. Defaults to .\reports.

.OUTPUTS
    Exit 0 = CLEAN, no new findings.
    Exit 1 = DRIFT, new findings or an unreviewed newly installed skill.
    Exit 2 = INCONCLUSIVE, the scan could not be trusted. Never reported clean.
#>
[CmdletBinding()]
param(
    [switch]$Llm,
    [string]$ReportDir
)

$ErrorActionPreference = 'Stop'

# Resolve the script's own directory in the body, NOT in a param default.
# Verified 2026-07-25: under `powershell.exe -File`, $PSScriptRoot evaluated
# empty inside this script's param defaults, so $ReportDir became "\reports"
# and a scheduled run silently wrote its output to a reports folder at the root of
# whatever drive the task's working directory happened to be on.
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $scriptRoot) { $scriptRoot = $PSScriptRoot }
if (-not $ReportDir)  { $ReportDir  = Join-Path $scriptRoot 'reports' }

. (Join-Path $scriptRoot 'SkillTargets.ps1')

$ss          = Join-Path $scriptRoot 'venv\Scripts\skillspector.exe'
$baselineDir = Join-Path $scriptRoot 'baselines'
$stamp       = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$runDir      = Join-Path $ReportDir $stamp

if (-not (Test-Path $ss)) {
    Write-Host "INCONCLUSIVE: scanner not found at $ss" -ForegroundColor Red
    exit 2
}

$targets = Get-SkillTargets
if ($targets.Count -eq 0) {
    Write-Host "INCONCLUSIVE: no skills found to scan." -ForegroundColor Red
    exit 2
}
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$drift        = @()
$unreviewed   = @()
$inconclusive = @()
$upgrades     = @()
$cleanCount   = 0

# --- Plugin upgrade detection ----------------------------------------------
# Baselines deliberately survive a plugin version bump, which means an upgrade
# would otherwise present as CLEAN: same skill names, same relative paths, new
# code underneath. Compare the recorded version map and report any change. It
# keeps reporting until Update-Baselines.ps1 re-records it, so an upgrade
# cannot be cleared by simply running the watch again.
$versionFile = Join-Path $baselineDir 'plugin-versions.json'
$current     = Get-PluginVersionMap -Targets $targets
if (Test-Path $versionFile) {
    try {
        $recorded = Get-Content $versionFile -Raw | ConvertFrom-Json
        foreach ($plugin in $current.Keys) {
            $was = $recorded.$plugin
            $now = $current[$plugin]
            if (-not $was)      { $upgrades += "$plugin installed at $now (not in the reviewed set)" }
            elseif ($was -ne $now) { $upgrades += "$plugin upgraded: $was -> $now" }
        }
        foreach ($p in $recorded.PSObject.Properties.Name) {
            if (-not $current.Contains($p)) { $upgrades += "$p removed (was $($recorded.$p))" }
        }
    } catch {
        $inconclusive += "plugin version map unreadable ($versionFile)"
    }
} else {
    $inconclusive += "no plugin version map recorded; run Update-Baselines.ps1"
}

foreach ($t in $targets) {
    $label    = "$($t.Scope)/$($t.Name)"
    $baseline = Join-Path $baselineDir "$($t.Scope)\$($t.Name).yaml"
    $report   = Join-Path $runDir "$($t.Scope)__$($t.Name).json"

    if (-not (Test-Path $baseline)) {
        # A skill with no baseline has never been reviewed. Treat as drift --
        # skipping it would let a freshly installed skill scan as clean.
        $unreviewed += $label
        continue
    }

    # Quote every path: Start-Process -ArgumentList joins on spaces without
    # quoting, and plugin cache paths can contain them.
    $ssArgs = @('scan', "`"$($t.Path)`"", '--baseline', "`"$baseline`"",
                '--format', 'json', '--output', "`"$report`"")
    if (-not $Llm) { $ssArgs += '--no-llm' }

    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    $proc = Start-Process -FilePath $ss -ArgumentList $ssArgs -Wait -PassThru -NoNewWindow `
                          -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $code   = $proc.ExitCode
    $stderr = ''
    if (Test-Path $errFile) { $stderr = (Get-Content $errFile -Raw -ErrorAction SilentlyContinue) }
    if ($null -eq $stderr) { $stderr = '' }
    Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue

    if ($Llm -and ($stderr -match 'LLM batch failed' -or $stderr -match 'batches failed')) {
        $n = ([regex]::Matches($stderr, 'LLM batch failed')).Count
        $inconclusive += "$label (LLM degraded: $n batch failure(s), findings left unfiltered)"
        continue
    }

    # Do NOT trust the exit code to mean "no findings". Verified 2026-07-25 on
    # v2.4.4: the exit code is severity-thresholded, not finding-based -- a
    # skill with 3 MEDIUM findings exits 0, and a skill with a HIGH YARA match
    # exits 0 when its overall score lands LOW. Relying on it would report new
    # LOW/MEDIUM findings as CLEAN. Count the post-suppression findings in the
    # JSON report instead; the exit code is only used to spot a crash.
    if ($code -gt 1) {
        $inconclusive += "$label (scanner exit $code)"
        continue
    }
    if (-not (Test-Path $report)) {
        $inconclusive += "$label (no report written)"
        continue
    }
    try {
        $j   = Get-Content $report -Raw | ConvertFrom-Json
        $new = @($j.issues).Count
    } catch {
        $inconclusive += "$label (unreadable report: $report)"
        continue
    }
    if ($new -gt 0) {
        $sev = ($j.issues | ForEach-Object { $_.severity } | Sort-Object -Unique) -join ','
        $drift += "$label ($new new finding(s): $sev) -> $report"
    } else {
        $cleanCount++
    }
}

# --- Report -----------------------------------------------------------------
$mode = 'static'
if ($Llm) { $mode = 'static + LLM (claude_cli)' }

$result = 'CLEAN'
$note   = 'no new findings since the accepted baseline.'
$code   = 0
if ($drift.Count -gt 0 -or $unreviewed.Count -gt 0 -or $upgrades.Count -gt 0) {
    $result = 'DRIFT'
    $note   = 'read the findings above. Accept them with Update-Baselines.ps1 only after review.'
    $code   = 1
}
if ($upgrades.Count -gt 0 -and $drift.Count -eq 0 -and $unreviewed.Count -eq 0) {
    # An upgrade with no new findings is the case worth calling out by name:
    # the code changed and the scanner had nothing to say about it. That is a
    # statement about the scanner's sensitivity, not about the new code.
    $note = 'plugin code changed but no new findings were raised. Scanner sensitivity is low -- review the upgrade yourself, then re-baseline.'
}
# Inconclusive outranks drift: a run we cannot trust must never be summarised
# as a clean or merely-drifting result.
if ($inconclusive.Count -gt 0) {
    $result = 'INCONCLUSIVE'
    $note   = 'scan could not be trusted, not reporting clean.'
    $code   = 2
}

$lines = @()
$lines += ''
$lines += "SkillSpector watch - $stamp - mode: $mode - $($targets.Count) skill(s)"
$lines += ("-" * 72)
$lines += "  CLEAN         $cleanCount skill(s) unchanged since baseline"
foreach ($g in $upgrades)     { $lines += "  PLUGIN CHANGE $g" }
foreach ($u in $unreviewed)   { $lines += "  UNREVIEWED    $u (newly installed, no baseline)" }
foreach ($d in $drift)        { $lines += "  NEW FINDINGS  $d" }
foreach ($i in $inconclusive) { $lines += "  INCONCLUSIVE  $i" }
$lines += ("-" * 72)
$lines += "Reports: $runDir"
$lines += "RESULT: $result - $note"

# Console for interactive runs...
$colour = 'Green'
if ($result -eq 'DRIFT')        { $colour = 'Yellow' }
if ($result -eq 'INCONCLUSIVE') { $colour = 'Red' }
$lines | ForEach-Object { Write-Host $_ }
Write-Host "(exit $code)" -ForegroundColor $colour

# ...and on disk, because a scheduled run has nobody watching the console.
# last-run.log is overwritten each time; history.log accumulates one line per
# run so a silent weeks-long INCONCLUSIVE streak is visible at a glance.
try {
    $lines | Set-Content -Path (Join-Path $ReportDir 'last-run.log') -Encoding utf8
    $summary = "{0}  {1,-12} clean={2} drift={3} unreviewed={4} pluginchange={5} inconclusive={6} mode={7}" -f `
               $stamp, $result, $cleanCount, $drift.Count, $unreviewed.Count, $upgrades.Count, $inconclusive.Count, $mode
    Add-Content -Path (Join-Path $ReportDir 'history.log') -Value $summary -Encoding utf8
} catch {
    Write-Host "WARNING: could not write run log: $_" -ForegroundColor Red
}

exit $code

# release-check.ps1 — run every safety gate against the COMMITTED tree before a
# public push. Exit non-zero if any gate fails. No network, no private repos.
#
#   pwsh tools/release-check.ps1
#
# Gates:
#   1. Gitleaks secret scan (bin/gitleaks.exe — fetch per bin/README.md)
#   2. Public-boundary check (tools/pre_publish_check.py)
#   3. Unit tests (ctxcheck, ctxdex gate)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
$fail = 0

function Step($name, $script) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $script
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; $script:fail++ }
}

$gitleaks = if ($env:GITLEAKS_EXE) { $env:GITLEAKS_EXE } else { Join-Path $root 'bin\gitleaks.exe' }
Step "Gitleaks (published tree only)" {
    if (-not (Test-Path $gitleaks)) {
        Write-Host "gitleaks not found at $gitleaks (see bin/README.md)"; $global:LASTEXITCODE = 1; return
    }
    # Scan EXACTLY the tracked/published set (what a fresh clone gets): export the
    # HEAD tree with `git archive`, so neither history nor git-ignored working-dir
    # dirs (skillspector/src, reports/, venvs) add noise or leak into the gate.
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("cltools-gl-" + [guid]::NewGuid().ToString('N'))
    $dst = Join-Path $tmp 'tree'
    New-Item -ItemType Directory -Path $dst -Force | Out-Null
    try {
        # zip + native Expand-Archive: avoids GNU tar treating "C:\..." as a remote host.
        $zip = Join-Path $tmp 'tree.zip'
        git archive --format=zip -o $zip HEAD
        Expand-Archive -Path $zip -DestinationPath $dst -Force
        $n = (Get-ChildItem $dst -Recurse -File | Measure-Object).Count
        if ($n -eq 0) { Write-Host "extraction produced 0 files — aborting gate"; $global:LASTEXITCODE = 1; return }
        Write-Host "scanning $n extracted files"
        & $gitleaks detect --source $dst --no-git --config (Join-Path $root '.gitleaks.toml') --no-banner
    } finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
Step "Public-boundary check" { python tools/pre_publish_check.py }
Step "ctxcheck tests"        { python ctxcheck/test_ctxcheck.py }
Step "ctxdex secret-gate tests" { python ctxdex/test_ctxdex_gate.py }
Step "local-audit census-only guard" { python local-audit/test_local_audit.py }
Step "slop_prepass self-test" { python local-audit/slop_prepass.py --self-test }

Pop-Location
if ($fail -gt 0) { Write-Host "`n$fail gate(s) FAILED." -ForegroundColor Red; exit 1 }
Write-Host "`nAll release gates passed." -ForegroundColor Green

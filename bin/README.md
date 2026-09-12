# bin/ — security scanner binaries

These executables are **not committed** (they are large, platform-specific, and
separately licensed). Download them here before using any tool or hook that
shells out to them. `.gitignore` keeps `bin/*.exe` untracked.

Always verify a download against the publisher's official checksums before use —
do not trust a binary you fetched without checking it.

## gitleaks — secret scanning
- Releases: https://github.com/gitleaks/gitleaks/releases
- **Pinned/known-good here: `8.18.4`** (the version this repo's docs and the
  `release-check` gate were validated against).
- Download the Windows `x64` archive, extract `gitleaks.exe` into this folder.
- **Verify** against the `checksums.txt` published on the release page:
  ```powershell
  # compare this hash to the matching line in the release's checksums.txt
  Get-FileHash .\bin\gitleaks.exe -Algorithm SHA256
  ```
- Confirm it runs: `bin/gitleaks.exe version`  (expect `8.18.4`)

## trivy — vulnerability / misconfig scanning
- Releases: https://github.com/aquasecurity/trivy/releases
- Download the Windows `64bit` archive, extract `trivy.exe` into this folder.
- **Verify** against the release's `trivy_*_checksums.txt`:
  ```powershell
  Get-FileHash .\bin\trivy.exe -Algorithm SHA256
  ```
- Confirm it runs: `bin/trivy.exe --version`

Both are single self-contained executables — no install step. Keep them on this
path (or set `$env:GITLEAKS_EXE` / edit the callers) so repo hooks and audit
scripts find them.

> Provenance note: pin the exact versions your team standardizes on and record
> their SHA-256 in your own environment docs. This file intentionally records the
> gitleaks version the public gates were tested with rather than a hash, because
> the hash is platform/version specific — take it from the publisher's signed
> checksums at download time.

# bin/ — security scanner binaries

These executables are **not committed** (they are large and version-specific).
Download them here before using any tool or hook that shells out to them.

## gitleaks — secret scanning
- Releases: https://github.com/gitleaks/gitleaks/releases
- Download the Windows `x64` archive, extract `gitleaks.exe` into this folder.
- Verify: `bin/gitleaks.exe version`

## trivy — vulnerability / misconfig scanning
- Releases: https://github.com/aquasecurity/trivy/releases
- Download the Windows `64bit` archive, extract `trivy.exe` into this folder.
- Verify: `bin/trivy.exe --version`

Both are single self-contained executables — no install step. Keep them on this
path (or edit the callers) so repo hooks and audit scripts find them.

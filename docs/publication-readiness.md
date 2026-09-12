# Publication readiness — claude-tools

Living record of the post-publication hardening pass. Non-sensitive by design.

## Baseline (Phase 1)

- Repository: `YourBIMpossible/claude-tools` (public)
- Branch: `main`
- Baseline SHA: `351a644ca6717e7dbbfc28de1db387b51c6f54a4`
- Captured: 2026-09-12T16:29Z
- Remote HEAD == local HEAD: yes
- Working tree: clean
- Tracked files: 74
- Tracked size: 212.7 KB (sum of blob sizes)

### Top-level inventory (baseline)

| Path | Tracked files |
|---|---|
| `skillspector/` | 44 (wrapper scripts + baselines) |
| `ctxcheck/` | 9 |
| `local-audit/` | 6 |
| `graphify/` | 6 |
| `ctxdex/` | 5 |
| `bin/` | 1 (README only; binaries untracked) |
| root | `README.md`, `.gitignore`, `.gitleaks.toml` |

## Public-boundary policy

Full policy: [public-boundary.md](public-boundary.md). Summary: this repo tracks
reusable **tool source only**. No BIMpossible private source, private architecture
metadata, local-machine data, private audit output, credentials, customer/financial
data, databases, indexes, generated logs, third-party binaries, or legacy AI-Dev
content. Evidence is always the committed Git tree, never `.gitignore` alone.

## Validation commands

| Purpose | Command |
|---|---|
| Committed-tree inventory | `git ls-tree -r --name-only HEAD` |
| Secret scan | `bin/gitleaks.exe detect --source . --no-banner` |
| Public-boundary scan | `python tools/pre_publish_check.py` |
| ctxcheck tests | `py ctxcheck/test_ctxcheck.py` |
| ctxdex gate tests | `py ctxdex/test_ctxdex_gate.py` |

## Results

(Filled in as phases complete — see sections below.)

### Gitleaks
- Version: 8.18.4
- Baseline result: no leaks found (exit 0)

### Reviewed exceptions
- `ctxdex/test_ctxdex_gate.py` — synthetic secret vectors; allowlisted in
  `.gitleaks.toml`, path-scoped. See Phase 3.

### Clean-clone result
- (Phase 7)

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

### Phase 2 — public-content boundary (done)
Baseline `351a644` had 74 tracked files. Removed 7 private-disclosure files and
anonymized 8; net tracked = **72**. Full disposition table:
[public-boundary.md](public-boundary.md#classification-log--hardening-pass-2026-09-12).

**Highest-severity find:** `ctxcheck/configs/bimpossible.toml` encoded a private
product's endpoint/route inventory, APIRouter prefixes, docker service topology,
and secret env-var *names*. Removed. **This content is still present in Git
history at `351a644` and in any third-party cache/fork of that commit** — a
corrective commit does not un-expose it. History rewrite is an **owner decision**
(carried to the Phase 8 report).

### Phase 3 — secret scanning & exception hygiene (done)
- Gitleaks version: **8.18.4**
- Command (published-tree gate): `git archive --format=zip HEAD` → extract →
  `bin/gitleaks.exe detect --source <tree> --no-git --config .gitleaks.toml --no-banner`
- Result on hardened HEAD tree (72 files): **no leaks found (exit 0)**
- `.gitleaks.toml`: `useDefault = true` (no default rule disabled); one
  path-scoped allowlist entry for `ctxdex/test_ctxdex_gate.py` only
  (separator-agnostic). No repo-wide or rule-level suppression.
- Release gate `tools/release-check.ps1` added: gitleaks (published tree) +
  `tools/pre_publish_check.py` boundary scan + ctxcheck + ctxdex tests. All pass.
- Baseline-tree note: a full git-history scan still flags the pre-hardening
  private files at `351a644` (see Phase 2) — expected until an owner history
  decision is made.

### Phase 4 — reproducibility & provenance (done)
- Added `CONTRIBUTING.md`, `SECURITY.md`, `docs/license-decision.md`.
- **LICENSE: none exists and no intent was previously stated → not invented.**
  Flagged as an owner decision (see `docs/license-decision.md`).
- Fixed a supply-chain footgun: README said `pip install graphify`; correct
  package is **`graphifyy`** (installs a `graphify` CLI command).
- `bin/README.md`: pinned gitleaks `8.18.4`, added checksum-verification steps
  for both binaries. Binaries remain untracked.
- skillspector upstream clone pinned at commit `fd25398d7aa9...` (documented in
  `skillspector/README.md`); `skillspector/src/` untracked.
- Verified: no private-path runtime defaults remain in source; tests build their
  own synthetic temp-dir fixtures (no private repo inputs).

### Reviewed exceptions
- `ctxdex/test_ctxdex_gate.py` — synthetic secret vectors (AWS public-docs example
  key, placeholder PEM, dummy connection strings). Allowlisted in `.gitleaks.toml`
  and `pre_publish_check.py`; the suite asserts ctxdex *refuses* to index each.

### Clean-clone result
- (Phase 7)

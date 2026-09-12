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

### Phase 7 — clean-clone & recovery validation (done)
Validated **outside** `F:\Claude-Tools`, on the pre-push hardening tip.

- Source: fresh `git clone` of branch `hardening/public-boundary` into a temp dir
  (not under `F:\Claude-Tools`). Cloned HEAD `0ef9898…`, **82 tracked files**.
- Confirmed git-ignored/untracked content is absent in a fresh clone:
  `skillspector/src/`, `reports/`, and `bin/*.exe` are not present (bin has only
  its README). No pre-built private artifact is required.
- Ran published README setup path (Python 3.11+) and all gates in the clone:
  - ctxcheck tests **48/48**, ctxdex secret-gate **43/43**,
    slop_prepass self-test **11/11**, census-only guard **held**.
  - Public-boundary check **PASS**; gitleaks over the published tree **no leaks**.
  - Smoke: `ctxcheck run --config configs/example.toml` executes end-to-end and
    correctly reports its generic declared endpoints as unmet against an arbitrary
    repo (expected — `example.toml` is a template to edit, not a passing config);
    `ctxdex stats` initializes a fresh local DB (0 chunks). Both prove the tools
    run from a clean clone.
  - `tools/release-check.ps1` in the clone: **All release gates passed.**

### Recovery bundle (authoritative — final pushed tip)
- Path (outside the repo): `F:\Claude-Tools-recovery\claude-tools-final-20260912T170512Z.bundle`
- Created (UTC): `2026-09-12T17:05:12Z`
- `git bundle verify`: "The bundle records a complete history."
- Contents: `main` @ `351a644…`, `hardening/public-boundary` @ `a661323…` (the
  final pushed tip, including this Phase 8 report commit).
- SHA-256: `feef196d9afde4a5b92a0fbe51c0a02fd9d007e9c1aefee19462beb7ebb4d659`
- Supersedes the `…T170206Z` (tip `3dc9612`) and `…T165910Z` (tip `0ef9898`)
  bundles; all remain on disk, this one is authoritative. (Recording this hash
  is itself the last commit, so the recorded SHA is captured one commit ahead of
  the doc line that names it — inherent and expected.)

### Post-merge recovery bundle (current authoritative)
- After PR #1 merged to `main` (merge `deeb728`).
- Path (outside the repo): `F:\Claude-Tools-recovery\claude-tools-postmerge-20260912T181302Z.bundle`
- Created (UTC): `2026-09-12T18:13:02Z`
- `git bundle verify`: "The bundle records a complete history."
- Contents: `main` @ `deeb728…` (merged), `hardening/public-boundary` @ `7646f2d…`,
  `governance/exposure-and-license` @ `c719020…`.
- SHA-256: `a1249710d9892bfe03aa01ff5a86a57cb04463a98b31239bcdac8f6e24303f5c`
- This captures the merged public history; the pre-merge bundles remain valid for
  the pre-merge tips.

## Phase 8 — final push, remote verification & disposition (done)

### 8.1 What was pushed
- Branch `hardening/public-boundary`, tip `3dc9612ab21a6a9cda136746ab931c254af13509`.
- Normal commits only; **no force-push, no history rewrite, no squash/rebase**.
- Commit graph: `3dc9612` (Ph7) ← `0ef9898` (Ph6) ← `d89e70c` (Ph5) ←
  `893d633` (Ph4) ← `0beabe1` (Ph2/3) ← `351a644` (baseline).
- `main` untouched: local and remote `main` both remain at `351a644`.
- PR #1 opened for owner review: https://github.com/YourBIMpossible/claude-tools/pull/1
  (**not merged** — merge is the owner's call).

### 8.2 Remote verification (post-push)
- Remote branch tip == local: `3dc9612…` ✓
- Remote tracked file count: **82** ✓
- Remote-tree private-file scan: no `bimpossible.toml`, no `reports/`, `state/`,
  `*.exe`, `*.db`, `.env*`, or AI-Dev/BASELINE artifacts. The only fuzzy hit was
  `graphify/recall/rerank_bm25.py` — a **false positive** (generic BM25 rerank
  *method*; unrelated to the removed private `RERANK-EXPERIMENT.md`). Tree clean. ✓

### 8.3 CI
- GitHub Actions `CI` on the PR: **success** (run `34706988401`, 14s).
- Read-only least-privilege (`contents: read`), SHA-pinned actions, no secrets,
  triggers limited to `pull_request` + `workflow_dispatch`.

### 8.4 Gate results at `3dc9612`
| Gate | Result |
|---|---|
| gitleaks (published tree, 8.18.4) | no leaks |
| `pre_publish_check.py` boundary | PASS |
| ctxcheck tests | 48/48 |
| ctxdex secret-gate | 43/43 |
| census-only guard (`test_local_audit.py`) | held (10/10) |
| slop_prepass self-test | 11/11 |
| `tools/release-check.ps1` (all of the above) | all gates passed |

### 8.5 Final disposition — **READY WITH OWNER DECISIONS**

Two items require the owner and are intentionally left unresolved (per spec: do
not rewrite history or invent a license without direction):

1. **Git-history exposure of removed private files.** The corrective commits
   remove `ctxcheck/configs/bimpossible.toml` (private endpoint/route/env-name
   inventory) and the other Phase-2 files from the **current** tree, but that
   content still exists in Git history at `351a644` and in any fork/cache/CI
   mirror of that commit. A clean current tree does **not** un-expose history.
   Owner decision required: (a) leave as-is, (b) rewrite history / re-create the
   repo from the hardened tip, and/or (c) rotate any identifiers that were
   exposed by name. See [public-boundary.md](public-boundary.md) for the file list.
2. **License.** ~~Not chosen at Phase 8.~~ **RESOLVED 2026-09-12:** after
   provenance verification (all tracked source first-party and stdlib-only; no
   third-party code redistributed), the repo is licensed **Apache-2.0** — `LICENSE`
   + `NOTICE` added on the `governance/exposure-and-license` branch. See
   [license-decision.md](license-decision.md). No longer an open item.

**Update (post-Phase-8):** PR #1 was merged to `main` (merge `deeb728`). The
license item above is resolved. The only remaining owner decision is the
Git-history exposure (item 1) — see
[history-exposure-assessment.md](history-exposure-assessment.md), which classifies
it as architecture/privacy disclosure (no secret values, no client data; rotation
not required) and lays out options A (accept) / B (rewrite history + cache
removal). Everything within the automated scope is green.

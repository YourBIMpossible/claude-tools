# Public-content boundary

`claude-tools` is a public, **source-only** toolbelt. This document defines what
may and may not enter the repository, and how contributors keep that boundary
intact. Evidence is always the **committed Git tree** (`git ls-tree -r HEAD`),
never `.gitignore` alone.

## Allowed

- Tool source: Python, PowerShell, `.cmd` launchers, and their tests.
- Generic, synthetic example configs and fixtures, clearly labelled as examples.
- Documentation describing generic methods and usage.
- Skill/plugin drift **baselines** that are empty suppression stubs or contain
  only public skill/plugin identifiers.
- Pointers to official upstream sources for third-party binaries and projects.

## Forbidden (never committed)

- Private source, private architecture metadata, endpoint/route inventories,
  internal class/command names, or feature maps of any private project.
- Real per-target configs that encode private repo paths, service topology,
  secret env-var name sets, or operational targets.
- Private/historical audit or report output; benchmark data derived from private
  source.
- Local-machine paths (absolute drive-letter or UNC paths, user-home paths), personal home dirs, private
  hostnames, private IPs, private git URLs, personal emails.
- Credentials, tokens, keys, `.env` / `.env.*`, database dumps, SQLite/Postgres
  DBs, FTS indexes, logs, caches, virtualenvs.
- Third-party executables / DLLs / vendored binaries.
- Any legacy private-workspace content.

## Examples & fixtures policy

Examples are allowed only if they are **synthetic, anonymized, and labelled**
with a top-of-file line such as:

> `Synthetic example — contains no private repository data or production findings.`

Verbatim historical audits of real projects, or benchmark fixtures/results
derived from private source, are not examples — they are forbidden content.

## Private index / data regeneration

Indexes, graphs, baselines, and reports are **regenerated locally** by each tool
from the user's own inputs. They are git-ignored and never distributed. A fresh
clone builds its own; nothing here depends on a pre-built private artifact.

## Contributor rules

1. Before any push, scan the **committed tree**:
   - `bin/gitleaks.exe detect --source . --no-banner`
   - `python tools/pre_publish_check.py`

   The boundary check scans every tracked file (no whole-file allowlist), reports
   every match redacted as file:line + rule, and exits nonzero on any finding.
   Rules: `windows-drive-path` (any letter, either separator), `unc-path`,
   `user-home-path`, `claude-user-home`, `claude-worktree-path`,
   `worktree-autoname`, `internal-state-path`, `private-namespace`,
   `private-source-path`, `email-address`, `private-ip`, `private-git-remote`, and
   `private-identifier` (SHA-256 list in `tools/private-identifiers.sha256`; add a
   name with `--hash-identifier`). Exceptions live in
   `tools/public-boundary-exceptions.json`, each bound to one path, one rule and
   the hash of one line, with a reason; an unused exception fails the check.
   `tools/test_pre_publish_check.py` covers every rule and the exception semantics.
2. Never attach secrets, `.env` files, customer/client data, private source
   trees, generated reports, or database dumps to commits, issues, or PRs.
3. If you find a questionable file, do **not** silently delete it: open an issue
   describing the file and why it is in doubt, and leave classification to a
   maintainer.
4. Removing a disclosure in a new commit does **not** remove it from Git history
   or third-party caches. Flag any such find explicitly so a history-rewrite
   decision can be made by the owner.

## Classification log — hardening pass 2026-09-12

Baseline `351a644`. Dispositions applied:

| Item | Classification | Action |
|---|---|---|
| `ctxcheck/configs/bimpossible.toml` | Remove — private endpoint/route/env inventory | Deleted; replaced by `configs/example.toml` |
| `ctxcheck/configs/<private-profile>.toml` | Remove — private repo inventory + local paths | Deleted |
| `ctxcheck/configs/memory.toml` | Remove — local machine path, private target | Deleted |
| `ctxcheck/audits/*` (2) | Remove — generated operational output, local paths | Deleted |
| `ctxdex/audits/*` (1) | Remove — generated operational output | Deleted |
| `graphify/recall/BASELINE.md` | Remove — benchmark data derived from private source | Deleted |
| `graphify/recall/RERANK-EXPERIMENT.md` | Remove — same | Deleted |
| `graphify/Refresh-Graphs.ps1` | Anonymize — machine paths + private scan targets | Parameterized ($PSScriptRoot, PATH, placeholder targets) |
| `graphify/Check-GraphifyHealth.ps1` | Anonymize — machine paths + private dashboard dirs | Parameterized (env-driven, default off) |
| `graphify/recall/measure_recall.py` | Anonymize — machine path + private example | PATH/env + generic example |
| `local-audit/audit-repo.cmd` | Anonymize — default scanned a private repo | Default now current dir |
| `local-audit/full-audit.cmd` | Anonymize — same | Default now current dir |
| `local-audit/README.md` | Anonymize — absolute self-paths | Relative paths |
| `ctxdex/README.md` | Anonymize — pointer to a legacy private-workspace path | Dropped path; kept public attribution link |
| `ctxcheck/README.md` | Anonymize — private ADR reference | Genericized |
| `ctxdex/test_ctxdex_gate.py` | Keep — synthetic secret vectors (Gitleaks-allowlisted) | Kept; see Phase 3 |
| skillspector baselines, other source | Safe | Kept |

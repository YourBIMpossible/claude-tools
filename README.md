# Claude-Tools

A local toolbelt for working with Claude Code across large repos: reality-checks,
recall, code-structure graphs, skill-drift watch, and a read-only code-audit lane.
Everything here is **local-first** — no service to sign up for, no data leaves the
machine. Tools are independent; take whichever ones are useful.

House recipe for every tool: single-file stdlib Python (or a thin PowerShell
wrapper), a CLI-subprocess test suite, configs kept *in the tool's own folder* so
target repos are never modified, and a declared-FAIL / discovered-WARN severity
split. `ctxcheck` and `ctxdex` are the cleanest templates.

## Tools

### ctxcheck — reality check
Validates that a repo's documented claims (anchor files, path refs, env/compose/
endpoint contracts, commit/ADR links, claimed commands, staleness) still match
reality. Reports drift; never fixes it.
- `py ctxcheck/ctxcheck.py run <name>` — targets configured under `ctxcheck/configs/`.
- Stateless: reads targets, writes nothing.
- Reach for it when the question is *"is this doc/config still true?"*

### ctxdex — text recall
Local FTS5 index for durable recall of large content already seen once (fetched
docs, big logs, exports). Lexical only, fully local.
- `py ctxdex/ctxdex.py index|search|stats|sources|purge`
- Per-project SQLite DBs under `ctxdex/data/` (git-ignored — regenerable).
- Reach for it when you need *content you already paid to read once*. Live code
  text → Grep. Structure → graphify. Published library docs → Context7.

### graphify — code structure
Persistent knowledge graph over code/docs for forward/impact questions ("what
does X call", "what breaks if I change X"), architecture, cross-file relations.
- Wraps the upstream `graphify` pip CLI. Ops scripts here: `graphify/Refresh-Graphs.ps1`,
  `graphify/Check-GraphifyHealth.ps1`. Edit the scan targets at the top of
  `Refresh-Graphs.ps1` to point at your own repos.
- `graphify/recall/` holds the retrieval-recall benchmark *method*
  (`measure_recall.py`, `rerank_bm25.py`) and its writeups. The query/baseline
  fixtures are repo-specific and git-ignored; bring your own.
- Reach for it when the question is relationships/impact. Backward exact-symbol
  ("who calls X") → Grep is faster.

### skillspector — skill/plugin drift watch
Detects drift in installed Claude skills/plugins against recorded baselines.
CLEAN means "unchanged", never "safe".
- `skillspector/Run-SkillSpector-Watch.ps1`; baselines via `Update-Baselines.ps1`.
- **Setup:** the upstream skillspector project is not vendored here — clone it
  into `skillspector/src/` (see `skillspector/README.md`).

### local-audit — read-only code-audit lane
Local, read-only audit runner (includes the slop-audit prepass: silent-catch
census, counter-integrity, tested-but-dead). Reports; never edits.
- `local-audit/audit-repo.cmd [path]`, `local-audit/full-audit.cmd [path]`
  (default target is set at the top of each `.cmd` — edit for your machine).

### bin/ — security scanners
`trivy` and `gitleaks` binaries, fetched at setup (not committed). See
`bin/README.md`.

## Setup

1. Python 3.11+ on PATH for the `ctx*` and audit tools.
2. `bin/`: download `trivy` and `gitleaks` per `bin/README.md`.
3. skillspector: clone upstream into `skillspector/src/`.
4. graphify: `pip install graphify`, then edit scan targets in `Refresh-Graphs.ps1`.

## What is deliberately not tracked

`.gitignore` keeps this repo to tool *source only*. Generated indexes/DBs, run
reports, health/state files, downloaded binaries, virtualenvs, and any repo-
specific fixtures (which can encode a private codebase's structure) stay local.

## Scheduled-routine acceptance (house rule)

A scheduled routine is **accepted only when it is armed AND has fired once,
verified from its artifact** — the report/log it produces, not the scheduler's
"Ready" status. A routine registered but never fired is a migration that failed
silently. Applies to new routines and to any change that could re-arm or break an
existing one (path moves, account changes, renamed scripts).

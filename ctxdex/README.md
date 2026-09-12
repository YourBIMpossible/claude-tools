# ctxdex

Local SQLite FTS5 knowledge index. Built 2026-08-07 as the self-built replacement for the one genuinely useful mechanism in [context-mode](https://github.com/mksglu/context-mode) — see the full vetting writeup at `F:\AI-Dev\_scratch\context-mode-assessment-2026-08-07.md`.

No hooks, no telemetry, no fake sandbox, no config rewriting. One file, stdlib only (`sqlite3`, `urllib`, `html.parser`).

## Usage

See [`../../claude-profile/skills/ctxdex/SKILL.md`](../../claude-profile/skills/ctxdex/SKILL.md) for the working rules Claude follows. Direct CLI:

```bash
python ctxdex.py index <path-or-url> --source LABEL
python ctxdex.py search "<query>" [--source LABEL] [--limit N]
python ctxdex.py stats
python ctxdex.py sources
python ctxdex.py purge --source LABEL   # or --older-than DAYS / --all
```

Each project gets its own DB under `data/<project>.db`, named for the invoking cwd unless `--project` is passed.

## Design

- `docs` table holds raw chunks (source, path, title, content, indexed_at).
- Two FTS5 virtual tables mirror it via triggers: `docs_fts` (porter-stemmed, for BM25 ranking) and `docs_trigram` (substring/fuzzy recall). Results merge via Reciprocal Rank Fusion (k=60), then re-sorted by exact query-term coverage.
- Chunking splits on markdown `#`/`##`/`###` headings when present, else paragraph-packed to a 4KB cap.
- No embeddings, no external services, no network except a URL you explicitly pass to `index`.

## Not built (deliberately)

Per the assessment: sandboxed execution, PreCompact/SessionStart session snapshots, and Bash/Read nudges were all judged duplicative of existing tools (Bash+scratchpad, LONG-TASK-HARNESS anchor docs, the context-budget skill) and were skipped.

One related mechanism was added later (2026-08-07): a PostToolUse hook (`../../claude-profile/hooks/ctxdex_autoindex.py`) that auto-indexes large (≥100 KiB) Bash output under `source=bash-output`. This differs from a "nudge" — it's a silent side effect (empty stdout, exit 0 always, fails open on any error) with no prompt-injection or conversational noise, gated by a secret-safety filter on both command and output, and a 14-day retention window scoped independently of manually-indexed content. See the SKILL.md "Auto-capture" section for the full contract.

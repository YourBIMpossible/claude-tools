# Slop audit — ctxdex

- **Date:** 2026-08-10
- **Window:** INCREMENTAL — no prior `audits/*__slop-audit*.md`, so commits since 2026-08-03 (2 commits, `402c0e6` initial + `dd0173b` secret gate). Full history — coverage is complete.
- **Scope:** silent-catch census, counter-integrity, tested-but-dead. Read-only, findings only.
- **In-window code:** `ctxdex.py` (FTS5 CLI), `test_ctxdex_gate.py`, `README.md`.

## Verdict

Clean on the gate and the counters. No CRITICAL/HIGH/MEDIUM. One LOW hypothesis on the query path.

### Check 1 — silent-catch census

Two swallows in the query path: `except sqlite3.OperationalError: porter_rows = []` (`ctxdex.py:291`) and `... continue` (`:307`). See LOW-1 — these are the only sites worth a note. The `except OSError` at `:233` is a file-read degrade during indexing and is handled (rejected/skipped file counted).

### Check 2 — counter-integrity

The secret gate counter is honest. `index_target` increments `rejected` per refused file (`ctxdex.py:238`), prints `refused N file(s) by the secret gate` (`:254`), and returns `2` — which `main` propagates as a non-zero process exit (`sys.exit(args.func(args) or 0)`, `:437`). Rejected files are skipped and never written; in a directory run the clean files still index (documented, `:199-206`). No "all indexed" success message masks a partial rejection — the count and the exit code both tell the truth. `dd0173b`'s stated contract ("reject, explain, exit non-zero") is met.

### Check 3 — tested-but-dead

`test_ctxdex_gate.py` drives the shipped `ctxdex.py index` entrypoint (the secret gate is on the real ingestion path used by the auto-index hook). Not a dead twin.

## Findings

### LOW-1 — a malformed FTS query reports "no results" identically to an empty index — HYPOTHESIS

- **Where:** `ctxdex.py:281-292` (porter query `except sqlite3.OperationalError → porter_rows = []`) and `:296-308` (trigram query `except → continue`).
- **Claim:** if a query string triggers an FTS5 syntax error (unbalanced quote, bare boolean operator, `NEAR` misuse), both branches swallow the `OperationalError`, `candidates` is empty, and the tool prints `no results` (`:312-315`) — the exact same output as a genuinely empty/unmatched index. The user gets no signal that their *query* was rejected rather than simply unmatched, so they may conclude the content isn't indexed when it is.
- **Severity rationale:** LOW — recall tool, no data mutation, no downstream decision gated on it; the porter→trigram fallback also legitimately rescues many partial-token cases. But "bad input looks like no data" is the silent-failure shape the audit targets.
- **Verification step:** run `ctxdex query '"unterminated'` (or another FTS5-invalid string) against a non-empty index and confirm the output is indistinguishable from a valid-but-unmatched query. If confirmed, the fix is a one-line stderr note when *both* branches raised while the index is non-empty (distinguish "query rejected" from "no matches").

## Appendix — silent-catch census (in-window)

| File:line | Pattern | Classification |
|---|---|---|
| `ctxdex.py:233` | `except OSError` (file read during index) | justified — file skipped/counted |
| `ctxdex.py:291` | `except OperationalError → porter_rows = []` | justified-but-silent (LOW-1) |
| `ctxdex.py:307` | `except OperationalError → continue` | justified-but-silent (LOW-1) |

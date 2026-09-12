# Slop audit — ctxcheck

- **Date:** 2026-08-10
- **Window:** INCREMENTAL — no prior `audits/*__slop-audit*.md`, so commits since 2026-08-03 (3 commits, `7951f87`..`3e37378`). This is effectively the tool's whole history (initial commit + two follow-ups), so coverage is full.
- **Scope:** silent-catch census, counter-integrity, tested-but-dead. Read-only, findings only.
- **In-window code:** `ctxcheck.py` (new CLI + 42-check suite), `test_ctxcheck.py`, `configs/*.toml`.

## Verdict

Clean. No CRITICAL/HIGH/MEDIUM, no LOW. No findings.

For a tool whose entire job is honest reporting, the reporting is honest: nothing is swallowed, and the PASS/WARN/FAIL tally drives the exit code.

### Check 1 — silent-catch census

Every catch surfaces the failure as a check result, not silence: `check_commands` maps `TimeoutExpired`→FAIL (`ctxcheck.py:378-380`), `OSError`→FAIL (`:381-383`), and a wrong exit code→FAIL with the stderr/stdout tail (`:386-390`). `git_ok`/`git_last_commit_ts` swallow `OSError`/`TimeoutExpired` (`:132,144`) but that is the *sensor* degrading to "git unavailable", after which `check_staleness` falls back to `stat().st_mtime` (`:399-401`) — a documented safe-degrade, not a hidden failure. Bad TOML is reported and exits 2 (`:442-444`).

### Check 2 — counter-integrity

This is the tool's core and it holds. `Report.add(status, ...)` appends every result; `counts()` tallies by status; the summary prints `pass/warn/fail` from the same counts (`ctxcheck.py:484-485`); and the exit code is derived directly — `if counts[FAIL] or (args.strict and counts[WARN]): return 1` (`:487-488`). There is no path where a FAIL is recorded but excluded from the total or the exit code. `shell=True` in `check_commands` (`:376`) is documented as intentional (trusted owner-authored config, never remote input) — noted, not a slop finding.

### Check 3 — tested-but-dead

`test_ctxcheck.py` was added and updated alongside `ctxcheck.py` in the same commits and targets the shipped module. No dead twin.

## Findings

None.

## Appendix — silent-catch census (in-window)

| File:line | Pattern | Classification |
|---|---|---|
| `ctxcheck.py:132` | `except (OSError, TimeoutExpired) → False` (git_ok) | justified sensor-degrade |
| `ctxcheck.py:144` | `except (OSError, TimeoutExpired, ValueError) → None` | justified sensor-degrade (mtime fallback) |
| `ctxcheck.py:232` | `except OSError as e` (reported) | justified-and-reported |
| `ctxcheck.py:378-383` | `except TimeoutExpired/OSError → FAIL` | justified — surfaced as FAIL |
| `ctxcheck.py:442` | `except TOMLDecodeError → stderr + return 2` | justified-and-reported |

# Anti-slop code audit — Claude-Tools

- **Scope:** FULL scan (no prior report). Repo `claude-tools` (consolidated toolkit: ctxcheck, ctxdex, local-audit, graphify, skillspector, tools).
- **Date:** 2026-09-13
- **Mode:** READ-ONLY. No target file was edited; the only write is this report.
- **Checks run:** (1) silent-catch census, (2) counter-integrity, (3) tested-but-dead.
- **Files inspected (all tracked code files in the brief):**
  - `ctxcheck/ctxcheck.py`, `ctxcheck/test_ctxcheck.py`
  - `ctxdex/ctxdex.py`, `ctxdex/test_ctxdex_gate.py`
  - `local-audit/local_audit.py`, `local-audit/slop_prepass.py`, `local-audit/test_local_audit.py`
  - `graphify/recall/measure_recall.py`, `graphify/recall/rerank_bm25.py`
  - `graphify/Check-GraphifyHealth.ps1`, `graphify/Refresh-Graphs.ps1`
  - `skillspector/Run-SkillSpector-Watch.ps1`, `skillspector/SkillTargets.ps1`, `skillspector/Update-Baselines.ps1`
  - `tools/pre_publish_check.py`, `tools/release-check.ps1`

## Severity summary

**CRITICAL 0, HIGH 0, MEDIUM 1, LOW 5** (plus 1 informational coverage note).

This is a notably clean, defensively-written codebase. The gate/watch tools (SkillSpector watch, ctxcheck, release-check, Check-GraphifyHealth, Update-Baselines) show deliberate, well-documented counter-integrity — INCONCLUSIVE outranking CLEAN, exit codes explicitly distrusted where they lie, every `continue` mapped to a tracked bucket. The findings below are edge-case silent skips, not systemic slop.

---

## Findings

### MEDIUM

**M1 — `Refresh-Graphs.ps1:178-180` — a target that produces no readable stats does not increment `$failed`, so the run reports success and falsely advances `last_success_at`.**
`VERIFIED` (code behavior) / `HYPOTHESIS` (real-world trigger).

When `graphify extract` returns 0 but `Get-GraphStats` returns `$null` (line 178), the code logs a `WARN` and leaves `$rec.ok` at its `$false` default, but **never increments `$failed`**. Consequently the run object's `exit_code` (line 203: `if ($failed){1}else{0}`) stays 0, `$lastSuccess` is set to the current run time (line 207), the script `exit 0` (line 224), and `last_success_at` in `health.json` advances as if the refresh fully succeeded. `Get-GraphStats` returns null whenever its Python one-liner fails — most plausibly if `python` (used only for stats + version) is unavailable while the `graphify` exe still runs, in which case *every* target fails to produce stats yet the run still reports success.

- **Blast radius / mitigation:** `Check-GraphifyHealth.ps1:65-69` independently raises a `target-failed` **error** alert on `-not $t.ok`, so the failure is *surfaced there*. But because `last_success_at` was advanced, the `stale` alert (`Check-GraphifyHealth.ps1:81`) will not fire, and the `refresh-failed` alert (`:62`, which keys on `last_run.exit_code`) will not fire either. Task Scheduler's `LastTaskResult` — which the script's own exit code is meant to feed (per the comment at `Refresh-Graphs.ps1` tail / `Check-GraphifyHealth.ps1:185-187`) — also reads success.
- **Contrast:** the sibling failure branches DO count correctly — missing graph (`:130 $failed++`) and non-zero extract (`:139 $failed++`). Only the null-stats branch is missed.
- **Verification step to promote to VERIFIED-triggered:** temporarily point `$env:PYTHON_EXE` at a non-Python executable (or a `graph.json` truncated to invalid JSON) and run `Refresh-Graphs.ps1`; confirm `health.json.last_run.exit_code == 0` and `last_success_at` advanced while a target shows `ok:false`. (Not run this session — READ-ONLY.)
- **Fix shape (not applied):** in the `else` at `:178`, increment `$failed` (and/or set an explicit failure flag) so the run exit code and `last_success_at` reflect the stats-read failure.

### LOW

**L1 — `ctxdex/ctxdex.py:231-234` — unreadable file in a directory index is silently dropped and not counted.**
`VERIFIED` (code read).
`except OSError: continue` skips a file that fails `read_text` with no message and without incrementing `rejected`. The final line prints `indexed N file(s)` and returns 0; a file that couldn't be read simply lowers the count with no notice. Rejected-by-secret-gate files ARE counted and reported (`:236-238,253-255`) — only the read-error path is silent. Impact is low (durable-recall index, not a gate), but a user believing content was indexed gets no signal it wasn't. Verification: index a directory containing one unreadable file; confirm no per-file notice and `rejected` stays 0.

**L2 — `tools/pre_publish_check.py:92-96` — publish-boundary content scan silently skips any tracked file it cannot open, and `errors="ignore"` drops undecodable bytes.**
`VERIFIED` (code read).
`except OSError: continue` (`:95`) means a tracked file that fails to open is neither scanned for private markers nor reported; `PASS: no private markers` (`:117`) can then print while a file went entirely unchecked. `errors="ignore"` (`:93`) additionally discards bytes that don't decode as UTF-8, so a marker straddling such bytes could be missed. This is a security-adjacent gate, which raises the stakes even though the probability is low (git-tracked text files rarely fail to open) and Gitleaks is a documented second layer (`:12`). Verification: mark a tracked file unreadable and run; confirm it is absent from output and the gate still passes. Fix shape: on `OSError`, emit a finding (fail-closed) rather than `continue`.

**L3 — `local-audit/slop_prepass.py:96-102` — the slop pre-pass silently drops any file it cannot read, and reports no files-scanned/skipped count.**
`VERIFIED` (code read).
`scan_file` returns `[]` on oversize (`:98-99`) or `OSError` (`:101-102`); `render_markdown` reports only `N candidate sites` (`:138-139`), never files-scanned or files-skipped. A file that errors on read contributes zero candidates and is invisible in the output — the exact "audit silently excludes an item" pattern this tool is built to detect, turned on itself. Low severity because the pre-pass is a candidate generator feeding a human/LLM classification lane, not a pass/fail gate (always `return 0`). Verification: run over a tree with one unreadable source file; confirm no skipped-files line. Fix shape: track and print a skipped-files count.

**L4 — `skillspector/SkillTargets.ps1:36,52 — `Get-ChildItem ... -ErrorAction SilentlyContinue` can silently shrink the reviewed skill set.**
`VERIFIED` (code read).
Both discovery enumerations suppress errors. A skill directory under a subtree that errors during enumeration (permissions, a transient lock, a reparse point) is silently omitted from `$targets`; the watch then scans fewer skills than exist and can report `CLEAN` over a skill it never enumerated. The recursive plugin-cache walk (`:52`) is the more exposed of the two. Note this is a real coverage gap distinct from the well-handled "newly-installed skill has no baseline → drift" case (`Run-SkillSpector-Watch.ps1:113-118`): that only catches skills that WERE enumerated. Verification: make one skill subdir unreadable and run `Get-SkillTargets`; confirm it is absent with no error surfaced. Fix shape: drop `SilentlyContinue` (or `-ErrorVariable` + surface a count), so an unreadable skill root is reported rather than dropped.

**L5 — `ctxdex/ctxdex.py:281-292, 296-308` — `except sqlite3.OperationalError` around both FTS queries can mask a genuine DB fault as "no results".**
`VERIFIED` (code read).
The porter query falls back to `porter_rows = []` (`:291-292`) and each trigram term `continue`s on `OperationalError` (`:307-308`). This is largely justified — FTS5 `MATCH` throws on odd tokenization and graceful degradation is reasonable for a read-only search — but a real fault (corrupt index, dropped virtual table) is indistinguishable from "no match" and surfaces to the user as `no results` (`:312-313`) with no diagnostic. Low severity (read path, non-gate, recoverable by re-index). Verification: drop `docs_fts` in a test DB and search; confirm silent `no results` rather than a surfaced error. Fix shape (optional): distinguish "no rows" from "query errored" in the message.

---

## Non-findings verified clean (worth recording)

- **`ctxcheck/ctxcheck.py` counter integrity — SOLID.** All eight checks funnel PASS/WARN/FAIL into one shared `Report`; `run()` aggregates via `rep.counts()` and exits 1 on any FAIL (or WARN under `--strict`) (`:476-489`). Every swallowed exception is turned into a visible FAIL/WARN result line (git probes `:132/:144` → downstream WARN; unreadable doc `:232` → WARN; command timeout/OSError `:378/:381` → FAIL; bad TOML `:442` → exit 2).
- **`skillspector/Run-SkillSpector-Watch.ps1` — EXEMPLARY.** Explicitly refuses to trust the scanner exit code (documented, dated verification at `:142-147`); every `continue` increments `inconclusive`/`drift`/`unreviewed`; `INCONCLUSIVE` outranks `DRIFT` and `CLEAN` (`:189-195`); `cleanCount++` fires only after a report is confirmed readable with zero issues (`:163-168`); result written to disk for unattended runs (`:217-227`).
- **`tools/release-check.ps1` — SOLID.** `Step` reads `$LASTEXITCODE` after each gate and accumulates `$script:fail` (`:15-19`); gitleaks scans the exact published tree via `git archive` with a 0-file abort guard (`:37-38`); temp cleanup `SilentlyContinue` is in a `finally` and cannot reset the native exit code.
- **`graphify/Check-GraphifyHealth.ps1` — SOLID.** Every `catch` raises an alert or logs (task-missing → error `:100`; PyPI down → cache fallback, and refuses to claim up-to-date on no evidence `:112-121`; toast/dashboard writes → `Log`, cosmetic). `errCount` drives status and exit (`:130-132, :187`).
- **`skillspector/Update-Baselines.ps1` — SOLID.** `$failed` counted, `exit 2` on any failure (`:63-66, :82`); reads `$proc.ExitCode` correctly.
- **`graphify/recall/measure_recall.py` — SOLID.** No swallows; a per-query non-zero `graphify` exit raises `RuntimeError` (`:76-80`) and ground-truth/graph mismatch raises `SystemExit` (`:142-147`) — failures abort loudly rather than skewing recall.
- **`graphify/recall/rerank_bm25.py` — clean** of swallow/counter patterns.
- **`local-audit/local_audit.py`** per-file audit errors are embedded verbatim in the report body (`**ERROR auditing this file:**`, `:174-175`) — surfaced, not hidden; always-0 exit is acceptable for a report-only lane.
- **`graphify/Refresh-Graphs.ps1` native-command handling — correct 5.1 pattern.** `Invoke-Logged` and `Get-GraphStats` flip `$ErrorActionPreference='Continue'` around native calls and read `$LASTEXITCODE` explicitly (`:63-72, :76-93`) — no try/catch-around-non-terminating trap. `catch {}` at `:97` is documented-intentional (absent/corrupt prior health → no drift calc).

## Tested-but-dead (check 3)

No green test over unshipped code found.

- **`test_ctxcheck.py`** invokes the shipped CLI end-to-end: `subprocess.run([sys.executable, CTXCHECK, ...])` where `CTXCHECK` = the real `ctxcheck.py` beside it (`:14, :33`). Exercises the production path (exit codes, ref/env/command/staleness checks). Real.
- **`test_ctxdex_gate.py`** invokes the shipped CLI end-to-end: `[sys.executable, CTXDEX, ...]` with `CTXDEX` = the real `ctxdex.py` (`:12, :26`), asserting the secret gate rejects each vector and clean files still index. Real.
- **`test_local_audit.py`** is a static policy guard, not a functional test: it asserts `local_audit.MODE == "census"`, that `CENSUS_ONLY` is embedded in `SYSTEM`, and that neither source defines a `--fix/--apply` flag or a repo-mutating git subcommand (`:38-68`). All three referenced symbols exist and are wired into the shipped `main` (`local_audit.py:41,43,49`). This is a legitimate invariant guard over shipped constants — **not** dead.
  - **INFORMATIONAL (coverage note, not a finding):** the local-audit *runtime* (`run_prepass`, `ollama_chat`, the `main` per-file loop) has no functional test — only the census guard and `slop_prepass --self-test`. Not a slop defect; noted for completeness.

## Silent-catch census (appendix)

| # | Site | Construct | Classification | Note |
|---|---|---|---|---|
| 1 | ctxcheck.py:132 | `except (OSError, TimeoutExpired)` → False | justified-and-handled | git commit probe; missing → downstream WARN |
| 2 | ctxcheck.py:144 | `except (OSError, TimeoutExpired, ValueError)` → None | justified-and-handled | git timestamp probe; falls back to mtime |
| 3 | ctxcheck.py:232 | `except OSError` → WARN | justified-and-logged | unreadable scan doc |
| 4 | ctxcheck.py:378,381 | `except TimeoutExpired/OSError` → FAIL | justified-and-logged | claimed-command run |
| 5 | ctxcheck.py:442 | `except TOMLDecodeError` → exit 2 | justified-and-logged | bad config |
| 6 | ctxdex.py:233 | `except OSError: continue` | **justified-but-silent (L1)** | index skip, not counted |
| 7 | ctxdex.py:291 | `except OperationalError` → [] | **justified-but-silent (L5)** | FTS porter query |
| 8 | ctxdex.py:307 | `except OperationalError: continue` | **justified-but-silent (L5)** | FTS trigram term |
| 9 | ctxdex/test_ctxdex_gate.py:125 | `except OSError: pass` | justified | test temp cleanup |
| 10 | local_audit.py:174 | `except (...) as e` → finding text | justified-and-logged | per-file error surfaced in report |
| 11 | slop_prepass.py:101 | `except OSError: return []` | **justified-but-silent (L3)** | file skipped, uncounted |
| 12 | pre_publish_check.py:95 | `except OSError: continue` | **swallows-a-real-failure risk (L2)** | publish gate skips unread file |
| 13 | pre_publish_check.py:93 | `errors="ignore"` | justified-but-silent (L2) | undecodable bytes dropped |
| 14 | Refresh-Graphs.ps1:97 | `catch {}` (prevHealth) | justified-and-documented | absent/corrupt → no drift calc |
| 15 | Refresh-Graphs.ps1:151-153 | cluster-only ≠0 → WARN, not counted | justified-and-documented | graph.json still fresh |
| 16 | Refresh-Graphs.ps1:178-180 | stats null → WARN, `$failed` not incremented | **swallows-a-real-failure (M1)** | run reports success, advances last_success_at |
| 17 | SkillTargets.ps1:36 | `Get-ChildItem -EA SilentlyContinue` | **justified-but-silent (L4)** | skills-root enumeration |
| 18 | SkillTargets.ps1:52 | `Get-ChildItem -Recurse -EA SilentlyContinue` | **justified-but-silent (L4)** | plugin-cache enumeration |
| 19 | Run-SkillSpector-Watch.ps1:101-103 | `catch` → inconclusive | justified-and-logged | plugin version map unreadable |
| 20 | Run-SkillSpector-Watch.ps1:132 | `Get-Content -EA SilentlyContinue`, null-guarded | justified | stderr read |
| 21 | Run-SkillSpector-Watch.ps1:159-162 | `catch` → inconclusive | justified-and-logged | unreadable report |
| 22 | Run-SkillSpector-Watch.ps1:225 | `catch` → Write-Host WARNING | justified-and-logged | run-log write |
| 23 | Run/Update/release temp `Remove-Item -EA SilentlyContinue` | (multiple) | justified | temp cleanup |
| 24 | Check-GraphifyHealth.ps1:52,99-101,112-121,160,182 | `catch` → alert or Log | justified-and-logged | every failure surfaced |

**Legend:** justified-and-handled/logged = failure is visibly reported or correctly converted to a result; justified-but-silent = defensible but the skip is invisible; swallows-a-real-failure = a genuine failure can pass unnoticed.

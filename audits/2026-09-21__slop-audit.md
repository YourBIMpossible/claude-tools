# Slop audit — F:\Claude-Tools — 2026-09-21

**Window:** incremental, commits since the prior report (`audits/2026-09-13__slop-audit.md`, commit `6d680e8`) → HEAD `8ccca10`.
**Checks:** silent-catch census · counter-integrity · tested-but-dead. Read-only; findings only, no fixes.

## Code in window

- `ctxcheck/ctxcheck.py` — add `_REV_RANGE_RE`; `looks_like_path()` now rejects git revision ranges (`origin/main..HEAD`, `abc123...feat/x`).
- `ctxcheck/test_ctxcheck.py` — fixture + assertion for the above.
- `evidence-relevance/measure_relevance.py` — **new** offline relevance-measurement tool (brief↔transcript join, shuffled baseline).
- `evidence-relevance/replay_relevance.py` — **new** offline replay harness (where an opened file falls out of the brief).
- `evidence-relevance/replay_rg.py` — **new** offline old-vs-new ripgrep collector benchmark.
- `evidence-relevance/compare_replays.py` — **new** cross-config replay comparison.

All four `evidence-relevance/*` files are offline analysis harnesses (read-only, never in the prompt path, `persist=False`), not production/prompt-path code.

## Verdict: LOW-only — 1 LOW, 0 MED, 0 HIGH/CRIT

### LOW-1 — uncounted malformed-line skip in transcript parse
- **file:** `evidence-relevance/measure_relevance.py:201`
- **status:** VERIFIED (read); severity HYPOTHESIS
- `build_turns()` streams each session `.jsonl` line and does `except json.JSONDecodeError: continue` with **no** `issues[...]` increment — unlike the sibling packet read (`load_packets`, `issues["unreadable_packet"] += 1`) and the subagent read (`issues["unreadable_subagent"] += 1`), which both count what they drop. A malformed/partial transcript line is therefore dropped silently; if it held a `tool_use`, the agent's opened-file set is under-counted, biasing every downstream `hit`/`precision`/`recall` metric with no trace in the emitted `join_issues`.
- **Why LOW:** offline measurement tool, not a shipped behavior; well-formed Claude Code transcripts are valid JSONL per line so the skip is expected to fire ~never in practice. It is a measurement-integrity blind spot, not a correctness bug in any user-facing path.
- **Verification step (to promote severity):** count how many lines this branch actually skips over the real packet corpus — add a throwaway `Counter` on that `continue` and run `measure_relevance.py --out /tmp`; if the count is 0 across the corpus, this is cosmetic. Fix (separate pass): mirror the siblings — `issues["unreadable_transcript_line"] += 1` before `continue`.

## Not findings (checked, cleared)

- **Counter-integrity — `measure_relevance.py` funnel: PASS.** Every `continue` in the scoring loop increments a `funnel[...]` bucket (`candidate_no_transcript_turn`, `candidate_turn_opened_no_repo_files`) before skipping; the success bucket `candidate_scored` is only reached after `score()` returns non-None. `candidate_brief_cited_no_files` is a non-terminal annotation (no `continue`), so it does not double-exclude. No success total silently omits failures.
- **`replay_rg.py` / `replay_relevance.py` — `next(glob(...))` without default** (`replay_rg.py:64`, `replay_relevance.py:162`): raises `StopIteration` if a packet's JSON is absent. This *fails loud* (uncaught, crashes the run with a traceback) — the opposite of the silent-success house defect — so it is out of scope for this audit. Noted only as a minor robustness nit for a future hardening pass (pass a default and skip with a counted issue).
- **Tested-but-dead — `ctxcheck` fix: refuted.** The new fixture line (`origin/main..HEAD`, `abc1234...feature/x`) drives the shipped `looks_like_path()` through the real `refs` check; asserted absent from messages. Verified green this run: `python ctxcheck/test_ctxcheck.py` → `49 passed, 0 failed` (exit 0).
- **Tested-but-dead — `evidence-relevance/*`:** no tests added; these are one-off analysis harnesses, so there is no green-over-dead-twin risk to flag.

## Appendix — silent-catch census (window)

| file:line | pattern | class | note |
|---|---|---|---|
| `evidence-relevance/measure_relevance.py:201` | `except json.JSONDecodeError: continue` (`build_turns`) | swallows-a-real-signal (LOW-1) | uncounted, unlike sibling reads; see LOW-1. |
| `evidence-relevance/measure_relevance.py:254` | `except (OSError, TimeoutExpired)` (`traffic_classes`) | justified-and-logged | prints `WARN ... to stderr`, returns `{}`. |
| `evidence-relevance/measure_relevance.py:274` | `except (OSError, json.JSONDecodeError)` (`load_packets`) | justified-and-counted | `issues["unreadable_packet"] += 1`. |
| `evidence-relevance/measure_relevance.py:302` | `except ValueError: return None` (`_ts`) | justified | timestamp parse; `None` handled downstream. |
| `evidence-relevance/measure_relevance.py:373` | `except (OSError, json.JSONDecodeError)` (subagent read) | justified-and-counted | `issues["unreadable_subagent"] += 1`. |
| `evidence-relevance/replay_relevance.py:573` | `except (OSError, TimeoutExpired): cache[key]=None` (`_uncapped`) | justified | `None` → `cap_exclusion="unknown"`, meaningfully distinct from confirmed/suspected. |

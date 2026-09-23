# Slop audit — claude-tools — 2026-09-21

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

**2026-09-21 closeout addendum:** `measure_relevance.py`, `replay_rg.py`, `replay_relevance.py` were subsequently patched (below) to close out this report's own findings, and `evidence-relevance/test_measure_relevance.py` was added (new file, 7 cases) to cover the two fixed paths.

## Verdict: CLEAN — 0 unresolved findings; 0 unowned robustness notes

All three items opened by the initial pass (LOW-1, plus the two `next(glob(...))` robustness notes) are **FIXED and validated** below, as part of the 2026-09-21 zero-residual closeout.

### LOW-1 — uncounted malformed-line skip in transcript parse — FIXED
- **file:** `evidence-relevance/measure_relevance.py` (`build_turns()`, was line 201)
- **fix:** added `issues["unreadable_transcript_line"] += 1` immediately before the `continue`, mirroring the sibling packet read (`issues["unreadable_packet"]`) and subagent read (`issues["unreadable_subagent"]`). The counter flows into `main()`'s aggregate `issues` via the existing `issues.update(t_issues)` merge — no new plumbing needed.
- **tests:** `evidence-relevance/test_measure_relevance.py::test_build_turns_counts_malformed_line_and_keeps_valid_data` — a 4-line synthetic transcript (hit line, valid tool-use, one malformed JSON line, a second valid tool-use after it) asserts: `issues["unreadable_transcript_line"] == 1` exactly; both valid tool-uses are still recorded, in order (`turn.opened == ["lib/foo.py", "lib/bar.py"]`); `score()` on the resulting turn returns `hit=True`, `recall=0.5` — i.e. the malformed line neither crashes the join nor silently inflates or deflates the real metrics.
- **real-corpus validation:** ran `measure_relevance.py --out <tmp>` against the live corpus (536 packets across BIMpossible/BIMpossible-AddIns/BIMpossible-Workspace/`bimpossible-next-49191a` worktrees) — `join_issues` in `results.json` has **no** `unreadable_transcript_line` key, i.e. **0 occurrences** across every real transcript line joined. This confirms the LOW-1 report's own prediction ("if the count is 0 across the corpus, this is cosmetic") — the fix is a correctness/observability guard for a case that has not yet fired, not a correction of a live miscount.

### Robustness note 1/2 — unguarded `next(glob(...))` in `replay_rg.py` — FIXED
- **file:** `evidence-relevance/replay_rg.py` (was line 64)
- **fix:** extracted a shared, tested helper `measure_relevance.load_packet_json(store_root, glob_pattern) -> dict | None` (returns `None`, never raises, when no file matches) and call sites in both replay tools now do `if raw is None: issues["missing_packet_json"] += 1; continue` instead of an unguarded `next()`. Summary JSON now carries `"issues": {...}` and `"complete": not issues`, so an aggregate with a skip is visibly marked incomplete rather than silently reported as a full run.
- **tests:** `test_measure_relevance.py::test_load_packet_json_present_and_absent` — asserts a present packet's JSON is read and parsed correctly, and an absent one returns `None` without raising `StopIteration`.
- **real-corpus validation:** iterated all 536 real packets through `load_packet_json` with `replay_rg.py`'s exact glob shape (`*{packet_id}*.json`) — **0 misses**. Confirms `load_packets()` (which enumerates packets by reading their JSON files off disk in the first place) cannot itself produce a packet whose JSON is later unresolvable under the current corpus; the guard is precautionary for a packet JSON deleted/moved between the two passes, not a live miss.

### Robustness note 2/2 — unguarded `next(glob(...))` in `replay_relevance.py` — FIXED
- **file:** `evidence-relevance/replay_relevance.py` (was line 162)
- **fix:** same shared `load_packet_json` helper, glob pattern `*{packet_id}.json` (matching this tool's original, narrower pattern). Same `issues["missing_packet_json"]` / `"complete"` wiring in the summary.
- **tests:** covered by the same `load_packet_json` unit test (the helper is shared, not duplicated).
- **real-corpus validation:** same 536-packet sweep with this tool's exact glob shape (`*{packet_id}.json`) — **0 misses**.

## Not findings (checked, cleared)

- **Counter-integrity — `measure_relevance.py` funnel: PASS.** Every `continue` in the scoring loop increments a `funnel[...]` bucket (`candidate_no_transcript_turn`, `candidate_turn_opened_no_repo_files`) before skipping; the success bucket `candidate_scored` is only reached after `score()` returns non-None. `candidate_brief_cited_no_files` is a non-terminal annotation (no `continue`), so it does not double-exclude. No success total silently omits failures. Re-verified after the fixes: real-corpus run reproduces the same funnel shape as the original pass (`packets 536`, `candidate_scored: 38`), i.e. the fixes changed 0 rows of real output.
- **Tested-but-dead — `ctxcheck` fix: refuted.** The new fixture line (`origin/main..HEAD`, `abc1234...feature/x`) drives the shipped `looks_like_path()` through the real `refs` check; asserted absent from messages. Verified green this run: `python ctxcheck/test_ctxcheck.py` → `49 passed, 0 failed` (exit 0).
- **Tested-but-dead — `evidence-relevance/*`:** now covered by `test_measure_relevance.py` (7 cases, see above) for the two fixed code paths; the remainder of these one-off analysis harnesses (scoring math, CLI wiring, ripgrep-collector replay) is unchanged and out of this closeout's scope.
- **"Stale #605 harness copies" (raised in the 2026-09-21 closeout instruction) — DISPROVEN.** Searched `git log --all --oneline` for `#605`/`#617` in Claude-Tools, Claude-Profile, and BIMpossible-Workspace, and `rg` for `measure_relevance|replay_rg|replay_relevance|evidence-relevance` across Claude-Profile, BIMpossible-Workspace, and BIMpossible-Site (excluding `node_modules`/`.git`): zero references to this evidence-relevance harness exist anywhere outside `evidence-relevance/`, and every `#605`/`#617` hit found (only in BIMpossible-Workspace's `.tools/state/queue.yaml` and ledgers) refers to unrelated BIMpossible backend/Autodesk-auth PRs (`fast-forwarded 39d47b7a -> 3a9dab64 (#617)`, `#605 deployed` re: R12 lifecycle+telemetry) — a different subject entirely. No stale copy or canonical replacement of this tooling exists to own or track.

## Appendix — silent-catch census (window)

| file:line | pattern | class | note |
|---|---|---|---|
| `evidence-relevance/measure_relevance.py` (`build_turns`) | `except json.JSONDecodeError: issues["unreadable_transcript_line"] += 1; continue` | justified-and-counted (was LOW-1, now fixed) | now mirrors sibling reads; validated 0 real occurrences, see LOW-1 above. |
| `evidence-relevance/measure_relevance.py:254` | `except (OSError, TimeoutExpired)` (`traffic_classes`) | justified-and-logged | prints `WARN ... to stderr`, returns `{}`. |
| `evidence-relevance/measure_relevance.py:274` | `except (OSError, json.JSONDecodeError)` (`load_packets`) | justified-and-counted | `issues["unreadable_packet"] += 1`. |
| `evidence-relevance/measure_relevance.py:302` | `except ValueError: return None` (`_ts`) | justified | timestamp parse; `None` handled downstream. |
| `evidence-relevance/measure_relevance.py:373` | `except (OSError, json.JSONDecodeError)` (subagent read) | justified-and-counted | `issues["unreadable_subagent"] += 1`. |
| `evidence-relevance/replay_rg.py` / `replay_relevance.py` (`load_packet_json`, both call sites) | `if not matches: return None` → caller does `issues["missing_packet_json"] += 1; continue` | justified-and-counted (was robustness notes, now fixed) | no longer an unguarded `next()`; validated 0 real misses across 536 packets. |
| `evidence-relevance/replay_relevance.py:573` | `except (OSError, TimeoutExpired): cache[key]=None` (`_uncapped`) | justified | `None` → `cap_exclusion="unknown"`, meaningfully distinct from confirmed/suspected. |

## Validation summary (2026-09-21 closeout)

- `python ctxcheck/test_ctxcheck.py` → `49 passed, 0 failed` (exit 0)
- `python evidence-relevance/test_measure_relevance.py` → `7 passed, 0 failed` (exit 0) — new test file, focused on the two fixed code paths
- `python -m py_compile evidence-relevance/measure_relevance.py evidence-relevance/replay_rg.py evidence-relevance/replay_relevance.py evidence-relevance/test_measure_relevance.py` → clean
- Real-corpus run: `python evidence-relevance/measure_relevance.py --out <tmp>` against 536 live packets → `join_issues` has no `unreadable_transcript_line` (0 occurrences); funnel/group output unchanged from the pre-fix baseline
- Real-corpus sweep: `load_packet_json` against all 536 packets under both replay tools' glob shapes → 0 misses either way

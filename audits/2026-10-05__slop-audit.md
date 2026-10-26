# Slop audit — Claude-Tools — 2026-10-05

**Mode:** incremental.
**Window:** `8bf7646..d6cec53`. This covers 12 commits after the 2026-09-28 report: 35 files changed, +2335/−293.
**Code in window:**
- `graphify/graphify_funnel.py` (new, 736 lines) and `graphify/test_graphify_funnel.py`
- remediation of the 2026-09-28 findings:
  - evidence-capture: maintenance, manifest scan, drain, isolation, store, audit, dryrun, desktop_host_probe
  - `evidence-archive/evacuate_worktree.py`, which now stages copies and links them
  - `graphify/Check-GraphifyHealth.ps1` and `GraphifyConfig.ps1`
  - `tools/pre_publish_check.py`, which now does a strict blob scan, and `tools/release-check.ps1`

**Result:** 0 CRITICAL · 0 HIGH · 2 MEDIUM · 5 LOW. All 22 prior findings (M1–M8, L1–L14) are fixed and none came back.

**How findings were labelled:**
- *VERIFIED (run)*: a probe was executed in a throwaway directory outside this repo. The probes ran against a `git show d6cec53:` copy of the module and the test harness's own `FunnelCase` fixtures.
- *VERIFIED (static)*: the full code path and its callers were traced, but the trigger was not reproduced.
- *HYPOTHESIS*: neither of the above.

The audit wrote nothing in this repo except this report, and it is not committed.

## MEDIUM

### M1 — The funnel's measurement stage reports PASS when no reranked record has a latency — VERIFIED (run)
- **Where:** `graphify/graphify_funnel.py:535-551`.
- **Defect:** `arm()` sets `n` from every record but builds the duration list `d` only from records whose `duration_ms` is an int or float. If no reranked record has a numeric duration, `median_ms` and `p90_ms` are both `None`. The check `(reranked["median_ms"] or 0) > LATENCY_MEDIAN_MS` turns `None` into 0, so the verdict is PASS.
- **Effect:** The verdict says the latency budget held when no latency was measured at all.
- **Probe:** 10 (`MIN_RERANKED`) reranked wrapper records were written:
  - with no `duration_ms`, the result was `PASS {'n': 10, 'median_ms': None, 'p90_ms': None}`;
  - with string durations (`"9000"`), the result was also PASS.
- **Expected:** INSUFFICIENT EVIDENCE when the timed count is below the minimum, or FAIL.

### M2 — The funnel ignores relative and scan-only target repos, so a missing expected graph never FAILs — VERIFIED (run)
- **Where:** `graphify/graphify_funnel.py:423`.
- **Defect:** The code is `targets = {norm(t["repo"]) for t in cfg["targets"] if t.get("repo")}`.
  - A relative `repo` is not resolved against the config folder. The example config's `_comment` and `GraphifyConfig.ps1` both say it is resolved there. The same file *does* resolve `skill_scripts` against `args.config.parent` (line ~710).
  - A target with no `repo`, which `GraphifyConfig.ps1` defaults to `scan`, is dropped.
- **Effect:** Sessions in those repos never land in `expected_graph_missing`, so stage 1a reads INSUFFICIENT instead of FAIL.
- **Probe:** One session ran in a repo that has no graph, and only the shape of its target entry changed:
  - with an absolute `repo` (control), the result was `FAIL ['s-other …\other']`;
  - with a relative `repo`, the result was `INSUFFICIENT EVIDENCE []`;
  - with a `scan`-only target, the result was `INSUFFICIENT EVIDENCE []`.
- **Related:** `query_log` at line ~711 is likewise used as `Path(cfg["query_log"])` without resolving it against the config folder (VERIFIED (static)). That feeds L1.

## LOW

| # | Where | Defect | Label |
|---|---|---|---|
| L1 | `graphify_funnel.py:349-351`, `:186-197` | When the query log is missing, `load_log` returns `[]` silently. `iter_json_lines` drops corrupt lines (`ValueError: continue`) and read errors (`OSError: return`) without counting them. Measurement then reads INSUFFICIENT, which looks like low volume rather than "no log". `render()` never prints the log path or whether it exists; that appears only in the JSON `query_log`. Execution FAILs only if wrapper calls were seen. Probe: a missing log gave `[]` and INSUFFICIENT for both execution and measurement. A one-line corrupt log gave `[]`. | VERIFIED (run) |
| L2 | `graphify_funnel.py:420-425`, `:634` | The `funnel` sub-keys are not validated, neither here nor in `GraphifyConfig.ps1`. An `exclude_sessions` typo counts smoke runs as organic. If `hint_live_since` does not parse, `hint_live` is `None`, but render still prints the raw string as if it were configured. Probe: `"2026-13-01"` printed `Hint live since: 2026-13-01`, with 1b INSUFFICIENT. | VERIFIED (run) |
| L3 | `graphify_funnel.py:313-317` | When a subagent's `.meta.json` is unreadable or invalid (`except (OSError, ValueError): pass`), `agent_type` is `None`. The unit then silently drops out of routing eligibility, with no count. | VERIFIED (static) |
| L4 | `graphify_funnel.py:331-334` | In `discover_sessions`, `stat()` raising OSError leads to `continue`, and an unreadable transcript yields no records. Either way the session disappears from every denominator with no count. | VERIFIED (static) |
| L5 | `evidence-archive/evacuate_worktree.py:191-201` | If `os.link` raises an OSError other than `FileExistsError` (for example on an archive volume without hard links, such as FAT or exFAT), the staged `.part` file is left in `archive/staging`. The run result is ERROR, so the failure is visible. The leftover file is not counted, and nothing reports it on later runs. The docstring calls it "inert", but it is not reclaimed. | HYPOTHESIS (verify: run with a staging dir on a no-hardlink volume, or monkeypatch `os.link` to raise `OSError(EXDEV)`) |

**Notes (not counted):**
- `graphify_funnel.main` returns 0 even when verdicts are FAIL. It is a report tool and no scheduler caller exists in the repo, but a future scheduled caller would need to read the JSON verdicts and not rely on the exit code.
- `Check-GraphifyHealth.ps1:255-266`: the public projection `graphify-health.js` is computed before the `public-rejected` or `write-failed` alerts are added. The public status can therefore lag one run behind `alerts.json`. This is inherent to the design, the run exits 1 through `$errCount`, and it is not silent.

## Prior findings — remediation check (VERIFIED (static) from the window diff)

| Prior | Status | How it was fixed |
|---|---|---|
| M1 | fixed | `decode()` accepts strict UTF-8, or a BOM-declared encoding. A NUL byte or a failed decode becomes an `undecodable-content` finding. |
| M2 | fixed | An unreadable blob becomes an `unreadable-blob` finding. |
| M3 | fixed | Index blobs and HEAD blobs are read via `git cat-file --batch`. HEAD-only content is reported as `path@HEAD`. |
| M4 | fixed | `episode_errors` and `busy_skipped` are counted. `maintain_findings()` judges both the backstop and `capture.py maintain` (exit 0/3/4). |
| M5 | fixed | `scan_manifests` returns `ManifestScan(manifests, unreadable)`. `audit.build_sheet` raises `AuditError`. `dryrun` reports `manifests_unreadable`. `desktop_host_probe` lists unreadable manifests as rows. |
| M6 | fixed | `drain_pending` returns `{drained, kept, errors}`. It restores failed records, retakes stale `.draining` orphans, and verifies the manifest by reading it back. |
| M7 | fixed | `stat_walk` and `git_status` raise. Errors from `state()` become failures. `check_isolation` raises on empty targets, watch or repos. |
| M8 | fixed | `review_pending` and `_iter_manifests` were deleted. |
| L1–L14 | fixed | The fixes include:<ul><li>the `GetFullPath` try</li><li>unknown keys set status to `invalid`</li><li>status is recomputed and `alerts.json` written after the dashboard writes</li><li>a `dashboard-dir-missing` alert</li><li>`lkg-unreadable` became a warn alert</li><li>release-check runs the 3 missing suites</li><li>`review_errors` and `meta_commit_failed` are counted</li><li>`_run(ok=…)`, the `_out` rc check, and a `CloneError` catch</li><li>unreadable records count as failed, with an isolation "not verified" line</li><li>a per-episode catch</li><li>`reconcile` is wired in at `pipeline.py:891`</li><li>dead helpers deleted (`git grep` finds no remaining references)</li><li>`bench_blind()` returns `None`, and `positive_controls_incomplete` was added</li></ul> |

## Clean areas
- `store.commit_meta` stages only changed paths, found via `diff --cached --name-only -z`, and checks the rc.
- In `run_maintain`, the reconcile path sets `changed = False` after reconcile writes `m` itself. This is sound and is not a lost write.
- `evacuate_worktree`: copies are staged, hash-verified and published with a single `os.link`. On `FileExistsError` it calls `_match_existing` and does not overwrite.
- `Check-GraphifyHealth.ps1`: the exit code comes from `$errCount`, and every new catch site adds an alert.

**Tested-but-dead:** none found.
- `test_graphify_funnel.py` imports the real module and drives `gf.main`.
- The new evidence-capture and evacuate tests call the shipped functions.
- `tools/test_pre_publish_check.py` exercises the real `main`.

## Silent-catch census (appendix)

| file:line | pattern | classification | note |
|---|---|---|---|
| graphify_funnel.py:192 | `except ValueError: continue` (JSON line) | justified-but-silent | Corrupt log or transcript lines are dropped uncounted (L1). |
| graphify_funnel.py:196 | `except OSError: return` | swallows-a-real-failure | An unreadable log or transcript looks identical to an empty one (L1, L4). |
| graphify_funnel.py:316 | `except (OSError, ValueError): pass` | justified-but-silent | Subagent type is lost and the unit becomes ineligible (L3). |
| graphify_funnel.py:333 | `except OSError: continue` | justified-but-silent | Session dropped from the denominators (L4). |
| graphify_funnel.py:350 | missing log leads to `return []` | swallows-a-real-failure | Not surfaced in the rendered report (L1). |
| graphify_funnel.py:410 | `except OSError: return None` | justified-but-silent | Same handling as the loader path. |
| graphify_funnel.py:702 | config unreadable leads to stderr and exit 2 | justified-and-logged | |
| evidence-capture drain_pending | per-record errors added to `errors[]` | justified-and-logged | Counted and returned. |
| evidence-capture isolation `state()` | errors become failures | justified-and-logged | |
| evidence-capture maintenance loop | `except Exception` adds to `episode_errors` | justified-and-logged | Judged by `maintain_findings()`. |
| Check-GraphifyHealth.ps1 lkg read | catch adds a warn alert | justified-and-logged | |
| GraphifyConfig.ps1 `GetFullPath` | catch sets status to `invalid` | justified-and-logged | |
| evacuate_worktree.py:201 | `except FileExistsError` calls `_match_existing` | justified-and-logged | Any other OSError gives an ERROR result (L5: the `.part` file is left behind). |
| pre_publish_check `decode` | `None` gives an `undecodable-content` finding | justified-and-logged | |
| pre_publish_check `tracked_blobs` | rev-parse rc sets `has_head` | justified | No HEAD yet is a legitimate state. |

## Disposition (2026-10-05)

| Finding | Disposition | Where |
|---|---|---|
| M1 | FIXED: latency uses only records with a numeric, finite, non-negative `duration_ms` (bool excluded); measurement is INSUFFICIENT EVIDENCE below `MIN_RERANKED` timed reranked records or when the log was not read completely. Untimed records are counted, never taken as 0 ms. Tests cover absent, null, string, bool, NaN, infinity and negative durations. | branch `claude/slop1005-graphify-funnel` |
| M2 | FIXED: `skill_scripts`, `query_log` and target paths resolve against the config file's folder (mirroring `Resolve-GraphifyConfigPath`); a target's repo defaults to its scan path, as in `Test-GraphifyConfigTargets`, so scan-only targets count in the expected-graph check. | same |
| L1 | FIXED: the query log status (`ok`, `not-configured`, `missing`, `not-a-file`, `unreadable`), its read error and its corrupt-line count are in the JSON `inputs` block and the rendered report. | same |
| L2 | FIXED: the config is validated before analysis; unknown `funnel` keys, wrong types, unparseable `hint_live_since`, placeholder and drive- or root-relative paths each fail with exit 2 and a listed error. `exclude_sessions` accepts the documented object form and a list of ids. | same |
| L3 | FIXED: missing, unreadable, invalid or type-less subagent metadata is counted per subagent and reported. | same |
| L4 | FIXED: unstatable, unreadable and undated transcripts and corrupt transcript lines are counted and reported. | same |
| L5 | NOT REPRODUCED at the remediation base: the outer `except OSError` in `evacuate_worktree` already removes the staged `.part` (PR #15, `04a7a3e`), and reports `staging_left` when that cleanup itself fails. Regression test `test_link_failure_leaves_no_part` (`os.link` raising `PermissionError`, `EXDEV` and `EINVAL`, each with and without a failing cleanup) passes, and fails when the cleanup call is removed. PENDING VERIFICATION: a run against a real no-hardlink volume. | same; `evacuate_worktree.py` unchanged |

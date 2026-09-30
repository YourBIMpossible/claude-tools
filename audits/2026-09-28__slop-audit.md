# Slop audit — Claude-Tools — 2026-09-28

**Mode:** incremental (scheduled `slop-audit-weekly`).
**Window:** `e2d9cb2..8bf7646` — 35 commits after the 2026-09-21 report.
**Code in window:**
- evidence-capture (new, about 10.6k lines)
- evidence-archive, evidence-episodes, evidence-footprints, evidence-twin (plus `lab/` fixtures)
- graphify config/health/refresh
- `tools/pre_publish_check.py` and `tools/release-check.ps1`
- comment-only diffs in ctxcheck, ctxdex, skillspector

**Result:** 0 CRITICAL · 0 HIGH · 8 MEDIUM · 14 LOW.

**How findings were labelled:**
- *VERIFIED (run)* — a probe was executed, in memory or in a throwaway scratch directory outside
  this repo.
- *VERIFIED (static)* — the full code path and its callers were traced, but the trigger was not
  reproduced.
- *HYPOTHESIS* — neither of the above.

Nothing in this repo was written by the audit.

Cross-repo: the profile repo's evacuation hook ↔ `evidence-archive/evacuate_worktree.py`
contract defect is reported in the profile repo's report of the same date (its M1). The
evacuator's own counters are sound.

## MEDIUM

### M1 — `pre_publish_check` passes UTF-16 files unscanned — VERIFIED (run)
`tools/pre_publish_check.py:219` reads as UTF-8 with `errors="ignore"`. In a UTF-16LE file
(the PowerShell 5.1 `Out-File` / `>` default), every ASCII character is followed by a NUL, so
no marker regex matches and the check prints PASS. **Ran:** `scan_line` finds 3 hits on a
path/home/email line, and 0 hits on the same line UTF-16-encoded. No tracked UTF-16 files
exist today. **Verify fix:** detect a BOM or NUL bytes, then decode as UTF-16 or fail the file.

### M2 — `pre_publish_check` skips unreadable tracked files without counting them — VERIFIED (static)
`tools/pre_publish_check.py:217-221` does `except OSError: continue`. A tracked file that was
deleted locally, a gitlink directory, or a Windows-locked file is dropped and does not block
PASS. **Verify:** delete a tracked file from the working tree only, run the check, and expect PASS.

### M3 — `pre_publish_check` scans the working tree, not the committed tree its docstring claims — VERIFIED (static)
`tools/pre_publish_check.py:2-4` vs `:219`. It reads working-tree bytes for the `ls-files`
paths. Locally, a leak that is committed but edited clean in the working tree passes
`release-check.ps1`. CI (fresh checkout) is unaffected, and the gitleaks step in the same
release-check already uses `git archive HEAD`.
**Verify:** commit a marker, clean it in the working tree, and run the check.

### M4 — The maintenance loop swallows per-episode errors, so the backstop reports ok — VERIFIED (static)
`evidence-capture/pipeline.py:855-868` (`run_maintain`). `except (OSError, ValueError,
KeyError)` writes a capture.log line and increments nothing. Busy-lock `continue`s are not
counted either. A manifest that throws on every pass is never finalized, yet
`backstop.py:67-81` reports `status=ok`, exit 0, indefinitely.
**Verify:** add a manifest missing `capture.phase`, run `backstop.run_backstop`, and expect `ok`.

### M5 — `iter_manifests` drops unreadable manifests with no log or count — VERIFIED (static)
`evidence-capture/store.py:443-452`. The twin in `review._iter_manifests` does log them. The
callers are maintenance (`pipeline.py:797,872`), the checkpoint `episodes_total`
(`report.py:114`), and audit sampling (`audit.py:131`). A truncated manifest vanishes from all
of them, and the checkpoint denominator undercounts silently.
**Verify:** corrupt one manifest, then call `write_checkpoint`.

### M6 — `drain_pending` deletes a pending record whose manifest write failed — VERIFIED (static)
`evidence-capture/pipeline.py:318-331`. The failure is logged, then `work.unlink()` runs
unconditionally. `pending_drained` counts only successes, and there is no error counter. The
failed capture never gets a `failed` manifest, so `capture_failures` undercounts permanently.
A second path: if `os.replace` succeeds but the read fails (`:314-315`), the `.draining` file
is orphaned and never retried.
**Verify:** monkeypatch `write_manifest` to raise `OSError` and drain.

### M7 — The isolation check passes without checking anything — VERIFIED (static)
`evidence-capture/isolation.py:99-125`, `check_isolation` at `:147-185`. Three ways it happens:
- `git_status` folds a git failure into the string `"<git status rc=N>"`, which compares equal
  before and after.
- `stat_walk` on a missing path returns the same sentinel both times.
- The CLI's `--watch` and `--repo` default to empty lists, so no state check runs.

A mistyped path, or none at all, yields `passed: true` for "no state changed".
**Verify:** `check_isolation(targets, watch=[Path("nope")], repos=[Path("nope")])` and expect
`passed=True`.

### M8 — `review_pending` is tested but has no production caller — VERIFIED (static)
`evidence-capture/review.py:537`. The docstring calls it the "next-start pass", and
`tests/test_review.py:192,395` assert on its counters, including `errors == 0`. Nothing in
production calls it. The shipped path is `run_maintain`'s inline loop, which lacks that error
accounting (M4, L7), so green tests vouch for a twin.
**Verify:** `grep -rn review_pending` outside `tests/`.

## LOW

| # | Where | Defect | Label |
|---|---|---|---|
| L1 | `graphify/GraphifyConfig.ps1:162` | `[IO.Path]::GetFullPath` sits outside any try. A `GRAPHIFY_CONFIG` containing `\|` or `"` throws under PS 5.1, which breaks the "never throws" contract. Refresh then dies without writing `config_error`, and there is no alert. | HYPOTHESIS; verify with `GRAPHIFY_CONFIG='a\|b'` under powershell.exe |
| L2 | `graphify/GraphifyConfig.ps1:172-196` | Unknown keys are ignored. A typo such as `pythonexe` silently falls back to PATH `python`, or publishes no dashboard, while status stays `ok`. This is the only real silent fallback against the "fail-closed" claim; everything else in the claim holds (`Refresh-Graphs.ps1:68-82` refuses unless status is `ok`). | VERIFIED (static) |
| L3 | `graphify/Check-GraphifyHealth.ps1:220-222` vs `:252,258` | `$errCount++` runs after `$status` is computed and `alerts.json` is written. `alerts.json` then says ok while the exit code is 1. | VERIFIED (static) |
| L4 | `graphify/Check-GraphifyHealth.ps1:260-262` | A missing dashboard directory is logged but not counted. If every configured directory is gone, the task still exits 0 and the panel freezes. | VERIFIED (static) |
| L5 | `graphify/Check-GraphifyHealth.ps1:96` | A corrupt `publish-settings.lkg.json` hits `catch {}` with no log line. | VERIFIED (static) |
| L6 | `tools/release-check.ps1:53-59` | The local release gate does not run `graphify/test_health_check.py`; CI does. | VERIFIED (static) |
| L7 | `evidence-capture/pipeline.py:821-826` | On the rejoin path the `review_episode` result is discarded. `reviewed++` runs but `review_errors` does not. It self-heals on the next pass. | VERIFIED (static) |
| L8 | `evidence-capture/pipeline.py:880` | The `False` return from `commit_meta` is ignored, so the backstop cannot see it. It only surfaces later, as `metadata_repo_clean=false`. | VERIFIED (static) |
| L9 | `evidence-capture/clone_builder.py:247,259,271,321,331-333` | Leak proofs P2/P3/P4/P6/P7 run git with `check=False` and never read the rc. A failing git gives empty stdout, which reads as "nothing leaked", so the proof passes. The caller is `evidence-twin/twin.py:200`. | HYPOTHESIS; verify with a PATH git shim that exits 128 |
| L10 | `evidence-capture/report.py:91-99` | `_isolation` skips unreadable or `passed`-less records, so a corrupt failing record leaves the `failed` count. It reads `meta/isolation/`, which no production code writes, so the checkpoint always reports 0/0. | VERIFIED (static) |
| L11 | `evidence-capture/pipeline.py:883` | Exception types outside the per-episode tuple (for example `TypeError`) abort the whole pass, including `expire()`. The failure is visible (backstop `fail`), but one bad manifest blocks retention. | VERIFIED (static) |
| L12 | `evidence-capture/linkage.py:120` | `reconcile` is tested (`tests/test_capture.py:392`) but has no production caller. The inline re-implementation at `pipeline.py:840-847` compares only `state` and misses a `packet_id` change. | VERIFIED (static) |
| L13 | `review.payload_snapshot`, `snapshot.snapshot_ok`, `isolation.standard_targets` | These have test-only callers: twin.py hard-codes the path, the CLI takes a `--targets` file, and `snapshot_ok` is only an assertion helper. | VERIFIED (static) |
| L14 | `evidence-twin/twin.py:594-595,612` | `bench_blind` is `False` ("not blind") when positive controls have no complete A/B pair, because `:594` silently `continue`s. The same object lists `missing_runs`, but the README reads `bench_blind` as the verdict. It should be `None` when unknown. | VERIFIED (run) — empty archive + one positive task → `bench_blind: False`, `lab: {}` |

## Clean areas

- `evidence-archive/evacuate_worktree.py`: every `continue` appends a result, and `BLOCKING` and
  unverified results feed both `safe_to_remove` and the exit code. A dry run never reports safe.
- `graphify/Refresh-Graphs.ps1`: every `continue` increments `$failed` first (`:153,162,205`),
  and `last_success_at` is preserved on failure.
- `evidence-episodes` counts `parse_errors`. `evidence-footprints` fails closed on git errors
  (verdict `None`, so the item is not eligible).
- Lab `verify.py` scripts exit 0 only on an explicit pass.
- Tested-but-dead, apart from M8/L12/L13: the graphify tests drive the real `.ps1` through
  pwsh/powershell.exe, `test_pre_publish_check.py` runs the real checker on a temp repo, and the
  evidence-* tests import the real modules. The research CLIs (episodes, footprints, twin) are
  manual by design.

## Silent-catch census (appendix)

| file:line | pattern | classification | note |
|---|---|---|---|
| tools/pre_publish_check.py:219 | `errors="ignore"` | swallows-a-real-failure | M1 |
| tools/pre_publish_check.py:220 | `except OSError: continue` | swallows-a-real-failure | M2 |
| tools/pre_publish_check.py:136,149 | missing config file → empty | justified-but-silent | only "0 private identifiers" is printed |
| tools/pre_publish_check.py:182 | subprocess `check=True` | justified | fails closed |
| graphify/GraphifyConfig.ps1:51,61,93 | try/catch with `-ErrorAction Stop` | justified-and-logged | returned as Errors |
| graphify/GraphifyConfig.ps1:162 | uncaught `GetFullPath` | real failure path | L1 |
| graphify/Refresh-Graphs.ps1:73,123 | `catch {}` on health.json read | justified-but-silent | an absent file means no prior record |
| graphify/Refresh-Graphs.ps1:~181 | git rev-parse rc unread | justified-but-silent | empty HEAD gives a misleading "older commit" alert |
| graphify/Check-GraphifyHealth.ps1:90 | catch → Log | justified-and-logged | |
| graphify/Check-GraphifyHealth.ps1:96 | `catch {}` | justified-but-silent | L5 |
| graphify/Check-GraphifyHealth.ps1:113-114 | catch → Add-Alert | justified-and-logged | |
| graphify/Check-GraphifyHealth.ps1:118-119 | python rc unread, `2>$null` | justified-but-silent | empty result is filtered downstream |
| graphify/Check-GraphifyHealth.ps1:258 | catch → Log, errCount++ | justified-and-logged | ordering is wrong: L3 |
| graphify/Check-GraphifyHealth.ps1:260-262 | missing dir, Log only | justified-but-silent | L4 |
| evidence-capture/pipeline.py:855 | except tuple → log only | swallows-a-real-failure | M4 |
| evidence-capture/pipeline.py:325,329 | except → pending file deleted | swallows-a-real-failure | M6 |
| evidence-capture/pipeline.py:314-315 | `except OSError: return []` | justified-but-silent | orphaned `.draining` file |
| evidence-capture/store.py:450 | `except (OSError, ValueError): continue` | swallows-a-real-failure | M5 |
| evidence-capture/pipeline.py:880 | `commit_meta` bool ignored | justified-but-silent | L8 |
| evidence-capture/pipeline.py:821 | review result discarded | swallows (temporary) | L7 |
| evidence-capture/isolation.py:125 | rc folded into an equal-comparing string | swallows-a-real-failure | M7 |
| evidence-capture/clone_builder.py:247/259/271/321/331/333 | `check=False`, rc unread | justified-but-silent | L9; fails toward pass |
| evidence-capture/report.py:84,96 | `except: continue` | justified-but-silent | L10 |
| evidence-capture/report.py:71 | `except OSError: pass` | justified-but-silent | size stat only |
| evidence-capture/report.py:307 | `except Exception` → None | justified-and-logged | empty repo has no HEAD |
| evidence-capture/pipeline.py:217,229-230 | `except OSError: pass` | justified-but-silent | last-resort writes |
| evidence-capture/pipeline.py:588 | `except Exception` → client problem | justified-and-logged | fails to untrusted |
| evidence-capture/pipeline.py:883 | `except Exception` → counts.error | justified-and-logged | L11 |
| evidence-capture/capture_start.py:22/40/43, capture_end.py:21/36/39 | `except Exception` | justified-and-logged | fail-open hooks; the import-failure path is silent |
| evidence-capture/backstop.py:103 | except → problems | justified-and-logged | |
| evidence-capture/review.py:108/113/290/566 | except → error field / log | justified-and-logged | privacy check fails closed |
| evidence-capture/review.py:597/608 | `except: continue` (seal) | justified-but-silent | fails closed |
| evidence-capture/linkage.py:53 | load_packet → None | justified-but-silent | retried later |
| evidence-capture/manifest.py:203 | → `boundary_file_unreadable` | justified-and-logged | |
| evidence-capture/client_identity.py:81-173 | OSError → None/problem | justified-and-logged | cache_store is silent |
| evidence-capture/snapshot.py:431-435 | typed excepts → snap failed | justified-and-logged | |
| evidence-capture/isolation.py:86 | whoami failure → pass | justified-but-silent | sid=None is visible |
| evidence-capture/store.py:285,300 | log_line / rmtree fallbacks | justified-but-silent | callers check rmtree |
| evidence-archive/evacuate_worktree.py:137 | decode/JSON → quarantine | justified-and-logged | |
| evidence-archive/evacuate_worktree.py:176,214 | OSError → ERROR | justified-and-logged | blocking |
| evidence-archive/evacuate_worktree.py:318 | except → exit 2 | justified-and-logged | |
| evidence-episodes/build_episodes.py:114 | git failure → "unknown" | justified-but-silent | provenance field only |
| evidence-episodes/build_episodes.py:226-231 | JSON errors → continue | justified-and-logged | counted in `parse_errors` |
| evidence-episodes/build_episodes.py:566 | `except Exception` | justified-and-logged | `join_reason=parse_failure` |
| evidence-footprints/footprints.py:326 | git ls-tree failure → None | justified-and-logged | fails closed |
| evidence-footprints/blind_sheets.py:112 | JSONDecodeError → continue | justified-but-silent | turn text may be short |
| evidence-twin/twin.py:110 | rmtree OSError → False | justified-and-logged | goes to `trace_left` |
| evidence-twin/twin.py:298 | taskkill rc unchecked | justified-but-silent | run is marked killed anyway |
| evidence-twin/twin.py:339 | hash OSError → None | justified-but-silent | `cli_sha` null |
| evidence-twin/twin.py:594 | unmatched A/B → continue | swallows (minor) | L14 |
| evidence-twin/replay_hook.py:22,33 | `except Exception: return 0` | justified-but-silent | a broken brief arm looks like arm B |


## Disposition ledger (2026-09-29)

| Finding | Disposition | Where |
|---|---|---|
| M1, M2, M3 | FIXED: the checker reads git blobs (index plus HEAD entries that differ), and BOM or undecodable text and unreadable blobs are counted findings | PR #12, merged `c71225c` |
| L1–L6 | FIXED: config path and unknown-key errors, dashboard-missing and write-failure alerts, logged corrupt LKG, and release-check runs the health, funnel and evacuation suites | PR #13, merged `5bbc724` |
| M4, M5, M6, M8, L7, L8, L11, L12 | FIXED (local): maintenance exit codes, counted unreadable manifests, atomic pending drain, re-join error counts, meta-commit failure fails the pass, relink via `linkage.reconcile`, `review_pending` deleted | `2c9c1ed` |
| M7, L9, L10, L13, L14 | FIXED (local): isolation fails closed, `_run` checks return codes, the checkpoint isolation reader is hardened, dead helpers are deleted, `bench_blind` is tri-state | `fa4e3c1` |

Related: evacuated copies are now published atomically via staging and link, for the profile repo's M1 contract (PR #14, merged `e06a9de`).

**Publication boundary.** The lane C and D fixes live in `evidence-capture/` and `evidence-twin/`. That code has never been pushed to this public repo. Both fixes are integrated on local `main` (`d6cec53`, 28 ahead / 0 behind `origin/main`), where every evidence suite and `tools/release-check.ps1`, including `pre_publish_check`, pass. The fixes are complete. Publishing them means publishing the whole unpushed evidence stack, so it waits on an owner decision.

**Re-check on changed paths:** all 28 original findings are gone (silent-catch census, counter-integrity and tested-but-dead re-run on the changed paths; 331 tests pass). The re-check found three new LOW gaps in the fixes themselves: N1, a failed staged write left a `.part` in `staging/` (claude-tools PR #15, CI green); N2, a SessionEnd `systemMessage` may never be shown; and N3, the hook's pre-check hashed every packet outside the time budget (both the profile repo's PR #18, 33/33 tests, the pre-change hook fails 5). Both PRs are open and await the owner's merge. Outside the audited list, `evidence-capture/pipeline.py` `record_pending` still swallows `OSError`; that code is local-only and is queued for the owner.

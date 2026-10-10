# evidence-twin

Replays a coding task twice, once with its context brief (arm A) and once without
(arm B), and measures how much work the agent needed before it first edited a target
file. It is the counterfactual half of the Evidence Compiler measurement plan; the
observational half is `../evidence-footprints`.

## Isolation

Every run gets a fresh repository the harness owns: a synthetic Lab fixture, or a
local clone at the recorded HEAD with its remote removed. It lives in a scratch
directory outside every real checkout and is deleted after the run. The CLI runs
with:

- `--setting-sources project` (the clone has no project settings) plus a generated
  `--settings` file: local tools allowed; web tools, network commands, `git push`,
  `git remote`, `git fetch`/`pull`/`clone` and package installs denied;
- the same `UserPromptSubmit` hook in both arms (`replay_hook.py`), which injects the
  archived brief through `additionalContext` in arm A — the production adapter's exact
  output — and nothing in arm B;
- `EVIDENCE_HOOK=0`, `--no-session-persistence`, `--strict-mcp-config`, stdin closed.
- an environment without any `CLAUDE*` or `ANTHROPIC*` variable. A harness started
  from inside a Claude Code session would otherwise hand the run that session's IDs,
  host-managed auth refresh, API base URL and effort. The run uses the CLI's own
  login. `meta.json` lists the dropped names (never values) and the CLI binary's sha256.

The CLI still creates an empty `~/.claude/projects/<slug>/memory` stub for the run's
directory. The harness removes it when it is empty and flags it in `meta.json`
(`trace_left`) when it is not. It also flags a real source checkout whose
`git status` changed during a run.

User-level skills and agents still load under `--setting-sources project`. They load
identically in both arms, and `meta.json` records their counts from the init event.

## Tokens and limits

Tokens are `input + cache_creation + cache_read + output`, deduplicated per message
ID, and include subagent messages. Tokens to target are the cumulative tokens through
the message holding the first main-thread edit of a target file. A run is killed at
the per-run cap (default 4M) or the wall timeout. A killed or unreached run is
censored at the cap.

A ledger keeps the total across batches. A run does not start when the total plus
one cap could cross the ceiling (default 150M). A run whose CLI never really
executed — no result event, or an errored result such as an expired login — is
kept as `<run_id>.invalid-N`, frees its run ID for a rerun, and stops the batch.

## Usage

```
python twin.py tasks-lab  --cases lab --golden <fixture dir> --out tasks.json
python twin.py tasks-real --episodes episodes.jsonl --packets <id>,<id> --out tasks.json
python twin.py plan   --tasks tasks.json --repeats 2 --seed <int> --out plan.json
python twin.py run    --tasks tasks.json --plan plan.json --archive <dir> --scratch <dir> \
                      --model <model id> --ledger ledger.json
python twin.py report --tasks tasks.json --plan plan.json --archive <dir> --seed <int> --out report.json
```

`run` resumes: runs with an archived `meta.json` are skipped. `--kind lab` runs only
the Lab controls, `--limit N` caps the runs in one batch, and
`--claude fake_claude.py` swaps in a zero-cost stand-in for dry runs.

The plan puts Lab controls first, shuffles (task, repeat) units with the seed and
randomizes which arm of each pair runs first.

Each archived run holds `stream.jsonl`, `diff.patch` (against the starting HEAD),
`stderr.txt` and `meta.json` (CLI version, model, init counts, tokens, target reach,
verify result, planted files touched, sha256 of stream and diff). `verify.py` runs in the
run's environment with a 120 s limit; a verify that hangs is recorded as unverified
(`verify_error`), and the run's spend still reaches the ledger.

## Lab controls

`lab/` holds five synthetic controls on a copy of a small golden fixture, each with a
`case.json`, a brief template (`{head}` is filled at run time), an `overlay/` and a
`verify.py`:

| Case | Family | Pass criterion |
|---|---|---|
| `pos_region_v2` | positive | A reaches the target 2/2 and B ≤ 1/2, or mean A tokens-to-target ≤ 0.5× B |
| `pos_engine` | positive | same |
| `neg_irrelevant` | negative | equal reach and 0.5 < A/B < 2; `null` (inconclusive) when every run in both arms is censored |
| `mis_old_copy_v2` | misleading | A opens the planted stale copy in ≥ 1 of 2 repeats |
| `reason_rule` | reasoning | exploratory, never pass/fail |

A case is never edited after its runs are seen. A changed case is a new case whose
`case.json` names the one it `supersedes` and why; the old case stays in `lab/` with its
runs but is no longer planned. `pos_region` asked for a per-tenant change the repo can only
make per region, and `mis_old_copy` named the target file in its prompt; both were
superseded after their first round.

If either positive control fails, `report.json` sets `bench_blind` to `true`: the bench
cannot see a brief that should help, and no real-prompt result is interpretable. It is
`false` only when every planned positive control ran all its repeats and passed. When no
positive control failed but one is missing runs (listed in
`positive_controls_incomplete`), or none is planned, it is `null`: unknown, which is not
a pass.

For real prompts the report gives, per prompt, the mean log ratio of A to B tokens to
target, and over prompts the median ratio with a seeded bootstrap 95% interval.

## Tests

```
python test_twin.py
```

Synthetic fixtures and `fake_claude.py` only: no model call, no network.

Synthetic example — contains no private repository data or production findings.

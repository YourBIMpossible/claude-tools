# evidence-capture

Prospective capture for the Evidence Compiler measurement plan. For each human prompt
in a repository that carries `.evidence-compiler/`, it records the checkout's exact
starting state, then joins the turn's transcript, links the Evidence Compiler packet
that was injected, screens the episode for replay exclusions and keeps a
privacy-reviewed payload for a later counterfactual replay (`../evidence-twin`).

**Nothing here registers a hook.** The start and end scripts exist and are tested, but
enabling capture (adding them to the client's hook settings) is a separate,
owner-approved step.

## Pieces

| Module | Role |
|---|---|
| `snapshot.py` | Starting-state snapshot inside a concurrent-change bracket; only git commands that leave `.git/index` byte-identical (tested) |
| `manifest.py` | Episode manifest v2, exclusion rules, funnel, pinned-client boundary |
| `linkage.py`, `join.py`, `screen.py` | Packet link, transcript join, dependency screen |
| `clone_builder.py`, `isolation.py` | Start-only replay clone with proofs P1–P10; filesystem-isolation check |
| `review.py` | Review states, privacy scope, payload, expiry, seals |
| `pipeline.py` | Start hook, end hook and maintenance |
| `capture_start.py`, `capture_end.py` | Fail-open hook wrappers: always exit 0, never print |
| `report.py` | Checkpoint report (JSON and markdown) |
| `audit.py` | Blind false-negative audit of the screen and first-batch admission gate |
| `dryrun.py` | The real hooks against synthetic fixtures |
| `backstop.py` | Scheduled maintenance and expiry backstop, independent of prompts and hooks |
| `desktop_boundary_test.py` | Pinned-client boundary tests in the desktop client's streaming mode (synthetic repository) |
| `capture.py` | Command line |

## Stores

Two directories, named by `EC_CAPTURE_META` and `EC_CAPTURE_CONTENT` or by
`evidence-capture.json` in the client's settings directory (`meta`, `content`,
`packet_dirs`, `seal_dirs`):

- **metadata**: its own git repository with no remote. Manifests, review records,
  audits, checkpoints. Content never enters it (`.gitignore` and `info/exclude`).
  `capture.log`, `store-check.json`, `maintenance.json`, `maintain.lock`, `open/` and
  `locks/` are local operational state and are never committed.
- **content**: snapshots and payloads, outside any repository and any sync root,
  marked `.git-blocked` and `NOSYNC`. Expires after 90 days unless sealed.

## Hooks

- **start** (`UserPromptSubmit`): writes the manifest, takes the snapshot, records an
  open pointer. Budget 2 s. It never reads packets or transcripts.
- **end** (`Stop`): joins the transcript, links the packet, screens, reviews and
  commits the episode. Budget 20 s.
- **maintenance** (launched detached by the start hook at most once a minute, or
  `capture.py maintain`): records pending failures, marks killed starts, re-joins
  episodes whose end hook never ran, re-links, reviews, expires and flags three
  consecutive capture failures per repository.

- **backstop** (`capture.py backstop`, for the operating system's scheduler): checks
  both stores, runs the same maintenance pass (waiting up to 5 minutes for a
  maintenance child holding the lock), verifies that nothing past expiry is left
  unsealed, and records the outcome in a status file beside the user config
  (`EC_CAPTURE_BACKSTOP_STATUS` overrides), in `capture.log`, on standard error and in
  the exit code (0 ok, 3 warning, 4 failure). Expiry therefore does not depend on
  future prompts or on a detached child surviving: a hook killed at its timeout takes
  its detached child with it. The checkpoint report notes a backstop that failed or has
  not succeeded for 48 hours. `--task-xml PATH` writes a Task Scheduler definition for
  review; nothing here registers it.

Every failure exits 0 with one log line. A failure that cannot be written to the
stores goes to `ec-capture-pending.jsonl` in the temp directory and becomes a failed
manifest at the next maintenance, so failures stay in the denominator.

Capture is off when `EVIDENCE_CAPTURE=0` or `EVIDENCE_HOOK=0` (the Twin sets both), when
the repository has no `.evidence-compiler/`, or when its config says `capture: false`.

Other environment: `EC_CAPTURE_TRANSCRIPTS` (transcript root; default the client's
projects directory), `EC_CAPTURE_PACKET_DIRS`, `EC_CAPTURE_SEALS`,
`EC_CAPTURE_MAINTAIN=0` (the start hook does not launch maintenance).

## Command line

```
python capture.py init
python capture.py maintain [--quiet]
python capture.py report
python capture.py fn-audit sheet | score K | amended K --note TEXT --tool-commit SHA
python capture.py admit EPISODE
python capture.py expire
python capture.py dry --out DIR [-n N]
python capture.py backstop [--health | --task-xml PATH]
python desktop_boundary_test.py --cli EXE --out DIR [--runs N] [--turns N] [--cleanup-transcripts]
```

`desktop_boundary_test.py` drives the CLI the way the desktop client does (streaming
JSON input and output, initialize handshake, project and inline hook sources) against
a disposable repository. It makes small model calls, so it is not free. It writes
results only to `--out` and never writes a trusted boundary file: accepting a client
identity stays the owner's decision.

`dry` refuses a directory inside a git repository, any path with a `scratchpad`
segment, and a non-empty directory it did not create.

## Tests

```
cd tests
python test_capture.py
python test_clone.py
python test_review.py
python test_hooks.py
python test_backstop.py
```

All fixtures are synthetic and live in temporary directories. Hook subprocesses run
with `HOME`, `USERPROFILE` and `TEMP` pointed into the test directory.

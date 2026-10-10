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
| `client_identity.py` | The client a hook runs under: executable SHA-256, invocation mode |
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
| `desktop_host_probe.py` | Probe hooks and analysis for the desktop app itself (`claude-desktop` mode), run from a disposable repository |
| `capture.py` | Command line |

## Stores

Two directories, named by `EC_CAPTURE_META` and `EC_CAPTURE_CONTENT` or by
`evidence-capture.json` in the client's settings directory (`meta`, `content`,
`packet_dirs`, `seal_dirs`):

- **metadata**: its own git repository with no remote. Manifests, review records,
  audits, checkpoints. Content never enters it (`.gitignore` and `info/exclude`).
  `capture.log`, `store-check.json`, `maintenance.json`, `maintain.lock`,
  `identity-cache.json`, `open/` and `locks/` are local operational state and are never
  committed.
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
`EC_CAPTURE_MAINTAIN=0` (the start hook does not launch maintenance),
`EC_CAPTURE_BOUNDARY_DIRS` (boundary record directories, replacing `boundary/`).

## Client identity and the boundary

An episode is replay-eligible only if its start snapshot provably preceded every tool
call, and that ordering was measured for one client, not assumed. A client is identified
by three things:

- **executable**: SHA-256 of the image of the process named by `CLAUDE_PID` (cross-checked
  against `CLAUDE_CODE_EXECPATH`);
- **version**: the CLI version recorded in the transcript;
- **mode**: `CLAUDE_CODE_ENTRYPOINT` (`unset` when absent). One executable runs in several
  modes, each with its own hook path: the desktop client sets `claude-desktop`, a bare
  stream-json process sets `sdk-cli`.

The host's version (`CLAUDE_CODE_DESKTOP_APP_VERSION`) is recorded, not matched. No
command line, argument value, token or socket is read. The start hook only observes
(a process query and a `stat`). The end hook or maintenance hashes the executable once per
path, size and modification time, via `identity-cache.json`. Manifests hold a hash of the
path, never the path.

A record `boundary/<version>--<mode>.json` holds `identity {sha256, cli_version, mode}`,
`accepted {at, by}`, `relied_upon` and `results`. An episode's boundary is trusted only
when:

- its client matches the record's hash, version and mode;
- the record is accepted;
- every relied-upon result passed.

Anything else fails closed for replay eligibility and is reported as `cli_untested`.
Capture, the hooks and the client carry on unchanged. The reasons are:

| Reason | Meaning |
|---|---|
| `boundary_untested` | no record for this version |
| `mode_untested` | records exist for this version, but not for this mode |
| `identity_untested` | only a version-only record, predating identity |
| `identity_changed` | the record names another executable |
| `identity_unknown`, `identity_unverified:<problem>` | the client could not be identified or hashed |
| `boundary_unaccepted` | a candidate without owner acceptance |
| `boundary_record_mismatch`, `boundary_file_unreadable`, `boundary_failed:<keys>` | the record is inconsistent, unreadable or failed |

Maintenance re-evaluates every untrusted terminal episode. An acceptance placed later
therefore takes effect without rerunning capture, and so does a hash cached later. The
scheduled backstop has no client process, so it relies on that cache.

### After identity drift

A client update changes the executable hash, and the version usually changes with it. A
new invocation path changes the mode. Either way, episodes from the new identity stay
untrusted until its boundary is tested and accepted:

1. **Headless modes (`sdk-cli`).** Run `desktop_boundary_test.py --cli <new executable>
   --out <dedicated dir> --cleanup-transcripts`. It removes inherited `CLAUDE*` and
   `DESKTOP_*` variables, and the probe hooks record the identity they ran under. It
   writes the full result and a candidate `<version>--<mode>.json` with `accepted: null`,
   and exits non-zero when a relied-upon property or the identity check failed.
2. **The desktop client (`claude-desktop`).** The harness cannot stand in for the desktop
   host, because its settings payload and initialize request are not reproducible here.
   Test that mode in the desktop app itself with `desktop_host_probe.py`:
   - `setup --out <dir>` builds a disposable repository (outside every repository and
     sync root) whose committed `.claude/settings.json` registers the probe hooks, plus
     synthetic stores;
   - open that repository in the desktop app and send the synthetic prompts **one at a
     time, waiting for each turn to finish** — a prompt queued behind a running turn is
     folded into it, and the event windows then overlap;
   - `arm --a-sleep 6` before one prompt and `disarm` after it, for the timeout property;
   - `analyse --out <dir> --cli <executable>` builds the same result and candidate the
     harness does, from the event log and the synthetic stores.
3. **Acceptance.** The owner reviews the candidate, adds `accepted: {"at": <UTC>, "by":
   <name>}`, and places it in `boundary/` or in a directory named by
   `EC_CAPTURE_BOUNDARY_DIRS`. Nothing here writes an accepted record.
4. **Re-evaluation.** The next maintenance or backstop pass re-evaluates held episodes of
   that identity. Episodes whose own start showed the boundary did not hold stay
   untrusted: `bracket_dirty`, `lock_present` and `hook_killed` override the record.

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
python isolation.py --targets FILE --watch DIR [--watch DIR ...] --repo DIR [--repo DIR ...] \n    [--probe-prefix CMD] --out META/isolation/NAME.json
python desktop_boundary_test.py --cli EXE --out DIR [--runs N] [--turns N] [--cleanup-transcripts]
```

`desktop_boundary_test.py` drives the CLI the way the desktop client does (streaming
JSON input and output, initialize handshake, project and inline hook sources) against
a disposable repository. It makes small model calls, so it is not free. It writes
results and an unaccepted candidate record only to `--out`, and refuses an `--out` inside
this package or any repository. Accepting a client identity stays the owner's decision.
`boundary_test.py` (print mode) writes version-only records, which are never trusted.

`isolation.py` needs at least one `--watch` store and one `--repo`; a store or repository
it cannot read fails the check. Records written to `META/isolation/` are what the
checkpoint counts; an unreadable record counts as failed, and no record reads as not
verified.

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
python test_identity.py
python tests/test_host_probe.py
```

All fixtures are synthetic and live in temporary directories. Hook subprocesses run
with `HOME`, `USERPROFILE` and `TEMP` pointed into the test directory.

## Running the tests

The runner contract is one script per suite: `python <test file>` exits non-zero on any failure.
The platform-neutral suites run in CI (`gates`, Ubuntu) and in `tools/release-check.ps1`:
`tests/test_capture.py`, `tests/test_locks.py`, `tests/test_identity.py`, `tests/test_review.py`,
`tests/test_host_probe.py`, plus `evidence-footprints/` and `evidence-twin/`.

Three suites are pinned to the Windows host and are run locally before a change to capture,
hooks, backstop or clone code lands:

```
python evidence-capture/tests/test_hooks.py
python evidence-capture/tests/test_backstop.py
python evidence-capture/tests/test_clone.py
```

On Linux they fail at the reviewed baseline too (the `icacls` isolation proof; privacy-screen
`deny_glob` exclusions whose Linux cause is not yet diagnosed), so CI does not carry them.

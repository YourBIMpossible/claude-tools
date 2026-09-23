# SkillSpector watch

Drift detection for installed Claude Code skills and plugins.
Set up 2026-07-25. Scanner: NVIDIA SkillSpector v2.4.4, pinned at commit
`fd25398d7aa99353d86237b9c260759351f0e644` in `.\src`.

> **Setup:** `.\src` (the upstream SkillSpector project) is not vendored in this
> repo. Clone it and check out the pinned commit before first use:
> ```
> git clone https://github.com/NVIDIA/skillspector.git src
> git -C src checkout fd25398d7aa99353d86237b9c260759351f0e644
> ```
> Then create its venv per the upstream README under `.\src`.

## Usage

```powershell
.\Run-SkillSpector-Watch.ps1          # static scan, ~1 min, no API usage
.\Run-SkillSpector-Watch.ps1 -Llm     # adds semantic pass, burns Claude subscription usage
.\Update-Baselines.ps1                # accept current findings as the new normal
```

Exit codes: `0` clean, `1` drift (new findings, or a newly installed skill with
no baseline), `2` inconclusive (scan could not be trusted -- never reported as
clean).

## What it is actually good for

**Change detection, not threat detection.** It answers "did anything about my
installed skills change since I last reviewed them", and nothing stronger.

186 findings across 39 skills were reviewed by hand on 2026-07-25 and accepted
into `.\baselines\`. All of them were false positives or known-benign. Two
observations were worth keeping:

- `superpowers/brainstorming` starts a backgrounded node web server
  (`nohup ... &` in `start-server.sh`). Loopback-bound (`BIND_HOST=127.0.0.1`)
  by default. Benign, but real.
- The `graphify` skill auto-installs the PyPI package `graphifyy` unpinned and
  with `--upgrade`, silently on first run. The package is legitimate
  (Graphify-Labs, matches the skill's own upstream repo), but a maintainer
  compromise would execute automatically. This baseline is the mitigation:
  a changed skill shows up as drift.

## Measured limitations (do not skip this)

These were verified empirically on this machine, not inferred.

**1. The 0-100 risk score is not usable.** A fixture containing SSH/AWS
credential exfiltration, `curl | bash`, base64 `exec()`, an API-key beacon, a
`crontab` persistence line, and a `nohup` command loop scored **10/100 LOW with
1 finding**. The legitimate `ui-ux-pro-max` skill scored **100/100 CRITICAL
with 55 findings** because it ships large CSV reference corpora. The score
tracks volume of prose, not danger. Read findings; ignore the score.

**2. Static detection of genuinely malicious content is weak.** That same
fixture -- six distinct malicious behaviours -- produced exactly one finding
(a YARA hack-tool match). Do not treat a clean result as evidence a skill is
safe. It is only evidence that nothing *changed*.

**3. `--baseline` is silently ignored under `--recursive`.** A recursive scan
of 17 skills with a valid baseline reported `suppressed=0` for every skill and
kept all 119 findings. The flags do not compose, and nothing warns you.

**4. A non-recursive scan of a parent directory does not scan the skills
inside it.** It analyses the directory as one skill. A malicious skill planted
in the personal Claude Code skills directory was not detected and the run reported CLEAN. This is why
`SkillTargets.ps1` enumerates skills and scans each one individually.

**5. The exit code is severity-thresholded, not finding-based.** A skill with
3 MEDIUM findings exits 0. A skill with a HIGH YARA match exits 0 if its
overall score lands LOW. `Run-SkillSpector-Watch.ps1` therefore counts findings
in the JSON report and uses the exit code only to detect a crash.

**6. The LLM stage is flaky on Windows.** The `claude_cli` provider runs each
call in a `TemporaryDirectory` whose cleanup races the child process, producing
`[WinError 32]`. On one large skill all 17 batches failed; SkillSpector kept
the findings "unfiltered" and still exited normally -- i.e. it silently
degrades to static-only. The watch treats any batch failure as INCONCLUSIVE.

## Scheduling

Registered 2026-07-25 as scheduled task **`SkillSpector-Watch`** -- weekly,
Sunday 03:00, 2-hour limit, runs whether or not on battery. Verified by
triggering it manually: `LastTaskResult = 0`.

```powershell
Get-ScheduledTaskInfo -TaskName SkillSpector-Watch    # NextRunTime / LastTaskResult
Start-ScheduledTask   -TaskName SkillSpector-Watch    # run it now
```

Because nobody watches a 3am console, every run leaves evidence:

- `reports\history.log` -- one line per run, appended. This is the file to
  check: a silent multi-week INCONCLUSIVE streak is visible at a glance.
- `reports\last-run.log` -- full summary of the most recent run.
- `reports\task-console.log` -- raw console capture of the scheduled run only.
- `reports\<timestamp>\` -- per-skill JSON reports.

Two bugs found while wiring this up, in case they resurface:

- Under `powershell.exe -File`, `$PSScriptRoot` evaluated **empty** inside this
  script's param defaults, so `$ReportDir` became `\reports` and the first
  scheduled run wrote its output to a `reports` folder at the drive root. Both scripts now resolve
  their own directory in the body via `$MyInvocation.MyCommand.Definition`.
  If you add a script here, do the same -- do not use `$PSScriptRoot` in a
  param default.
- `Start-Process -ArgumentList` joins arguments on spaces **without quoting**,
  so `--reason "some text"` reached the CLI as extra positional arguments and
  every baseline generation exited 2. Arguments are quoted explicitly now.

## Layout

```
src\          pinned SkillSpector clone (v2.4.4)
venv\         isolated Python 3.14 install; nothing global, nothing on PATH
baselines\    one YAML per skill, grouped by scope (skills\, plugin-<name>\)
reports\      one timestamped folder per run
```

Regenerating baselines after a plugin upgrade is expected: plugin cache paths
embed a version segment, but baselines are keyed on plugin name and findings
are recorded relative to the skill root, so a version bump alone does not
invalidate them.

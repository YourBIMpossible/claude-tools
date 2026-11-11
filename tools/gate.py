#!/usr/bin/env python3
"""The local gate for this repository: the pre-push hook runs it, CI runs it, and a push goes out only on GATE PASS.

It runs, from the repository root, everything the CI workflows check:

  1. the public-boundary check (tools/pre_publish_check.py), the machine-paths guard (self-test, then scan) and the
     slop_prepass self-test;
  2. every test suite in TEST_SUITES, each a plain script that exits non-zero on a failure;
  3. the gitleaks secret scan of the tracked tree, with the repo's .gitleaks.toml (CI's last step).

CI's two jobs (.github/workflows/ci.yml, manual dispatch only: Linux, and Windows PowerShell 5.1) run this gate, so
TEST_SUITES is the one list of what CI checks: a suite added to TEST_SUITES is checked on the pushing machine and in CI
with no second list to update. The automatic PR check (secret-scan.yml) runs only the gate's boundary check and secret
scan. Every tracked test_*.py must be in TEST_SUITES (tools/test_gate.py fails otherwise), so no suite goes unrun.
A suite this machine cannot run is left out with a line saying why, never silently: the Windows-only suites off
Windows, the PowerShell suites where that shell is missing, a suite whose package is not installed (CI installs it). On Windows the PowerShell suites run twice, under pwsh 7
and under Windows PowerShell 5.1 (the shell the scheduled tasks use), as CI's two jobs do between them.

The steps run in parallel, each with its own temporary directory, and their output is buffered: a passing step prints
one line, a failing step prints all of its output (`--verbose` prints every step's output). Every step runs even when
another fails, so one run shows every problem. The last line of output is the verdict (`GATE PASS ...` or
`GATE FAIL ...`), and the exit status is 0 only for a pass. The pass line is read alone, so it says what it does not
cover: `GATE PASS: 29 of 29 steps passed; 4 suites not run here (listed above)` when this machine left suites out, and
`... (publication scans only, no test suite)` for `--scans-only`.

gitleaks is found through $GITLEAKS_BIN, then PATH, then <home>/.claude/bin (where the profile's tools/git_gate.py installs
it), then this repo's bin/ (bin/README.md). Missing gitleaks fails the gate: the secret scan is not optional.

Standard library only.

Usage:
  python tools/gate.py              # the whole gate, from anywhere inside the repository
  python tools/gate.py --verbose    # the same, printing every step's output (what CI runs)
  python tools/gate.py --root DIR   # another checkout of this repository
  python tools/gate.py --scans-only --root DIR   # boundary, machine paths and secrets only (see scan_steps)
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
GITLEAKS_PIN = "8.18.4"  # the version ci.yml installs and bin/README.md pins
# (suite, what it needs). "any": every machine. "windows": Windows only (it drives wscript.exe).
# "powershell": a PowerShell host, set through GRAPHIFY_TEST_SHELL (pwsh 7 by default).
# "evidence_compiler": that package importable (ci.yml installs it at EVIDENCE_COMPILER_PIN).
TEST_SUITES = (
    ("tools/test_pre_publish_check.py", "any"),
    ("tools/test_pre_push_hook.py", "any"),
    ("tools/test_gate.py", "any"),
    ("local-audit/test_local_audit.py", "any"),
    ("ctxcheck/test_ctxcheck.py", "any"),
    ("ctxdex/test_ctxdex_gate.py", "any"),
    ("graphify/test_refresh_graphs.py", "powershell"),
    ("graphify/test_health_check.py", "powershell"),
    ("graphify/test_graphify_funnel.py", "any"),
    ("evidence-relevance/test_measure_relevance.py", "any"),
    ("evidence-archive/test_evacuate_worktree.py", "any"),
    ("evidence-capture/tests/test_capture.py", "any"),
    ("evidence-capture/tests/test_locks.py", "any"),
    ("evidence-capture/tests/test_identity.py", "any"),
    ("evidence-capture/tests/test_review.py", "any"),
    ("evidence-capture/tests/test_snapshot.py", "any"),
    ("evidence-capture/tests/test_causal.py", "any"),
    ("evidence-capture/tests/test_host_probe.py", "any"),
    ("evidence-capture/tests/test_backstop.py", "any"),
    ("evidence-capture/tests/test_causal_hooks.py", "any"),
    ("evidence-capture/tests/test_clone.py", "any"),
    ("evidence-capture/tests/test_hooks.py", "any"),
    ("evidence-capture/tests/test_hygiene.py", "any"),
    ("evidence-episodes/test_build_episodes.py", "evidence_compiler"),
    ("evidence-footprints/test_footprints.py", "any"),
    ("evidence-footprints/test_blind_sheets.py", "any"),
    ("evidence-twin/test_twin.py", "any"),
    ("scheduling/test_run_hidden.py", "windows"),
)
GUARD = ".github/scripts/check_machine_paths.py"
EVIDENCE_COMPILER_PIN = "1cdc625c4c18ea95994eaf424b039b06e9387d6c"  # the commit ci.yml installs
EVIDENCE_COMPILER_SOURCE = "git+https://github.com/YourBIMpossible/evidence-compiler@" + EVIDENCE_COMPILER_PIN


@dataclass
class Result:
    label: str
    ok: bool
    output: str
    seconds: float
    status: str


@dataclass(frozen=True)
class Step:
    label: str
    argv: list[str]
    env: tuple[tuple[str, str], ...] = ()


def powershell_hosts(windows: bool, which: Callable[[str], str | None] = shutil.which) -> list[tuple[str, str]]:
    """(label suffix, GRAPHIFY_TEST_SHELL value) for each PowerShell host this machine has and CI runs under."""
    wanted = [("pwsh 7", "pwsh")] + ([("PowerShell 5.1", "powershell.exe")] if windows else [])
    return [(name, shell) for name, shell in wanted if which(shell)]


def importable(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def default_steps(root: Path, python: str = sys.executable, windows: bool = os.name == "nt",
                  which: Callable[[str], str | None] = shutil.which,
                  has_module: Callable[[str], bool] = importable) -> tuple[list[Step], list[str]]:
    """(the steps to run, a line for each suite left out on this machine and why)."""
    steps = [
        Step("public-boundary check", [python, "tools/pre_publish_check.py"]),
        Step("machine-paths guard self-test", [python, GUARD, "--self-test"]),
        Step("machine-paths guard scan", [python, GUARD]),
        Step("slop_prepass self-test", [python, "local-audit/slop_prepass.py", "--self-test"]),
    ]
    skipped = []
    hosts = powershell_hosts(windows, which)
    for suite, needs in TEST_SUITES:
        if needs == "windows" and not windows:
            skipped.append(f"not run here (Windows only): {suite}")
        elif needs == "powershell":
            if not hosts:
                skipped.append(f"not run here (no pwsh on PATH): {suite}")
            for name, shell in hosts:
                steps.append(Step(f"{suite} ({name})", [python, suite], (("GRAPHIFY_TEST_SHELL", shell),)))
        elif needs == "evidence_compiler" and not has_module(needs):
            skipped.append(f"not run here (evidence_compiler not installed; pip install \"{EVIDENCE_COMPILER_SOURCE}\"):"
                           f" {suite}")
        else:
            steps.append(Step(suite, [python, suite]))
    steps.append(Step("gitleaks secret scan", [python, "tools/gate.py", "--secret-scan", "--root", str(root)]))
    return steps, skipped


def scan_steps(root: Path, python: str = sys.executable) -> list[Step]:
    """The checks that decide whether a commit's content may be published: the public-boundary check, the
    machine-paths scan and the secret scan. No test suite: a red test in an earlier commit is not a leak.

    The pre-push hook runs these (`--scans-only`) on every pushed commit before the tip, because every pushed commit
    stays in the public history. The secret scan step runs THIS file, so an earlier commit whose own gate predates
    `--scans-only` is still scanned; the two scanners are the scanned commit's own, as in the full gate."""
    return [
        Step("public-boundary check", [python, "tools/pre_publish_check.py"]),
        Step("machine-paths guard scan", [python, GUARD]),
        Step("gitleaks secret scan", [python, str(Path(__file__).resolve()), "--secret-scan", "--root", str(root)]),
    ]


def find_gitleaks(root: Path, environ: dict | None = None,
                  which: Callable[[str], str | None] = shutil.which) -> str | None:
    environ = os.environ if environ is None else environ
    if environ.get("GITLEAKS_BIN"):
        return environ["GITLEAKS_BIN"]
    for name in ("gitleaks", "gitleaks.exe"):
        found = which(name)
        if found:
            return found
    for folder in (Path.home() / ".claude" / "bin", root / "bin"):
        for name in ("gitleaks.exe", "gitleaks"):
            if (folder / name).is_file():
                return str(folder / name)
    return None


def tracked_files(root: Path) -> list[str] | None:
    """The tracked paths, or None when root is not a git work tree (then the folder itself is scanned)."""
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def secret_scan(root: Path, out: TextIO) -> int:
    """gitleaks over the tracked tree, as CI scans its fresh checkout: ignored and untracked files are not published."""
    gitleaks = find_gitleaks(root)
    if not gitleaks:
        print(f"gitleaks not found ($GITLEAKS_BIN, PATH, <home>/.claude/bin, bin/). Install {GITLEAKS_PIN}: run the "
              "profile's `python tools/git_gate.py`, or follow bin/README.md. The secret scan is required.", file=out)
        return 1
    files = tracked_files(root)
    with tempfile.TemporaryDirectory(prefix="gate-tree-") as tree:
        source = root
        if files is not None:
            source = Path(tree)
            for rel in files:
                src = root / rel
                if src.is_file():  # a deleted-but-staged path, or a submodule, has nothing to scan
                    (source / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, source / rel)
        argv = [gitleaks, "detect", "--source", str(source), "--no-git", "--redact", "--no-banner"]
        if (root / ".gitleaks.toml").is_file():
            argv += ["--config", str(root / ".gitleaks.toml")]
        try:
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"gitleaks could not start ({gitleaks}): {exc}", file=out)
            return 1
    out.write(proc.stdout)
    return proc.returncode


def run_step(step: Step, cwd: Path, scratch: Path) -> Result:
    """Run one step to completion with its output captured. A step that cannot start is a failure, not a crash."""
    started = time.monotonic()
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               TMPDIR=str(scratch), TMP=str(scratch), TEMP=str(scratch), **dict(step.env))
    try:
        proc = subprocess.run(step.argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        return Result(step.label, False, f"could not start: {exc}\n", time.monotonic() - started,
                      "FAILED (could not start)")
    status = "ok" if proc.returncode == 0 else f"FAILED (exit {proc.returncode})"
    return Result(step.label, proc.returncode == 0, proc.stdout, time.monotonic() - started, status)


def run_steps(steps: list[Step], cwd: Path, out: TextIO, verbose: bool = False,
              runner: Callable[[Step, Path, Path], Result] = run_step) -> tuple[list[str], int]:
    """(labels of the failed steps, number of steps run). Every step runs; results print in step order."""
    if not steps:
        return [], 0
    with tempfile.TemporaryDirectory(prefix="gate-") as scratch_root, \
            ThreadPoolExecutor(max_workers=min(8, len(steps), os.cpu_count() or 1)) as pool:
        futures = [pool.submit(runner, step, cwd, Path(scratch_root) / str(i)) for i, step in enumerate(steps)]
        failed = []
        for future in futures:
            result = future.result()
            if verbose or not result.ok:
                print(f"==> {result.label}", file=out)
                text = result.output
                out.write(text if not text or text.endswith("\n") else text + "\n")
            print(f"<== {result.label}: {result.status} in {result.seconds:.1f}s", file=out, flush=True)
            if not result.ok:
                failed.append(result.label)
    return failed, len(steps)


def main(argv: list[str] | None = None, steps: list[Step] | None = None, out: TextIO | None = None,
         skipped: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: this clone)")
    ap.add_argument("--verbose", action="store_true", help="print every step's output, not only a failing step's")
    ap.add_argument("--secret-scan", action="store_true", help="run only the gitleaks step (the gate runs it so)")
    ap.add_argument("--scans-only", action="store_true",
                    help="run only the publication scans (boundary, machine paths, secrets) and no test suite; "
                         "the pre-push hook runs this on each pushed commit before the tip")
    args = ap.parse_args(argv)
    out = out or sys.stdout
    for stream in (sys.stdout, sys.stderr):  # step output is relayed as text; never die on a console codec
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    root = args.root.resolve()
    if args.secret_scan:
        return secret_scan(root, out)
    scope = ""
    if steps is None and args.scans_only:
        steps, skipped = scan_steps(root), []
        scope = " (publication scans only, no test suite)"
    elif steps is None:
        steps, skipped = default_steps(root)
    skipped = skipped or []
    for line in skipped:
        print(line, file=out)
    failed, total = run_steps(steps, root, out, verbose=args.verbose)
    if total == 0:
        print("GATE FAIL: no steps ran, so nothing was checked", file=out, flush=True)
        return 1
    if failed:
        print(f"GATE FAIL: {len(failed)} of {total} steps failed: {', '.join(failed)}", file=out, flush=True)
        return 1
    # The verdict line is read alone, so it carries what it does not cover: the suites this machine left out.
    if skipped:
        scope += f"; {len(skipped)} suite{'' if len(skipped) == 1 else 's'} not run here (listed above)"
    print(f"GATE PASS: {total} of {total} steps passed{scope}", file=out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

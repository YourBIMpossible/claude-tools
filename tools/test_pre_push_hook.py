#!/usr/bin/env python3
"""Tests for .githooks/pre-push (the local run of the gate, tools/gate.py, on every pushed commit).

No framework, plain checks (ctxdex-suite style). Each case builds a throwaway repo holding this checkout's hook, gate
and scanners, with a passing stub at every TEST_SUITES path (the real suites are the gate's own steps in this repo;
here they stand in, so one can be made to fail), activates the hook through core.hooksPath, and runs a real
`git push` to a local bare remote, so git itself invokes the hook (under Git for Windows' sh on Windows). The secret
scan uses the real gitleaks, which this machine must have (the gate needs it). Synthetic content only; violating
literals are assembled at runtime so this file itself stays clean.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = ".githooks/pre-push"
GATE = "tools/gate.py"
MACHINE_PATHS = ".github/scripts/check_machine_paths.py"
PRE_PUBLISH = "tools/pre_publish_check.py"
COPIED = (HOOK, GATE, MACHINE_PATHS, PRE_PUBLISH, "local-audit/slop_prepass.py", ".gitleaks.toml")
BS = "\\"
DRIVE = "Q:"                                       # synthetic drive letter
PRIVATE_EMAIL = "dev" + "@" + "corp-internal.net"  # synthetic, caught by the public-boundary check only
FAKE_TOKEN = "ghp_" + "a1B2" * 9                   # GitHub classic PAT shape, synthetic
STUB_OK = "print('stub suite ok')\n"
STUB_FAIL = "import sys\nprint('stub suite failed on purpose')\nsys.exit(1)\n"
# The hook's environment: this process's, minus any bypass a caller has set.
ENV = {k: v for k, v in os.environ.items() if not k.startswith("BIMP_")}

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED += 1
        FAILURES.append(name)
        print(f"FAIL  {name}\n{detail}")


def load_gate():
    spec = importlib.util.spec_from_file_location("tools_gate_for_hook_test", REPO / GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(root: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    r = subprocess.run(list(args), cwd=root, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env={**ENV, **(env or {})}, stdin=subprocess.DEVNULL)
    return r.returncode, (r.stdout + r.stderr).replace("\r\n", "\n")


def git(root: Path, *args: str) -> str:
    rc, out = run(root, "git", *args)
    if rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({rc}):\n{out}")
    return out


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def commit(root: Path, rel: str, text: str, msg: str) -> None:
    write(root, rel, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)


def make_repo(tmp: Path, suites: list[str]) -> Path:
    """A repo with the hook, the gate, both scanners and a stub per suite, a bare origin, and core.hooksPath set."""
    src = tmp / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.name", "Hook Test")
    git(src, "config", "user.email", "hook-test@example.com")
    git(src, "config", "core.hooksPath", ".githooks")
    for rel in COPIED:
        if (REPO / rel).is_file():
            dest = src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / rel, dest)
    hook = src / HOOK
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    for suite in suites:
        write(src, suite, STUB_OK)
    git(tmp, "init", "-q", "--bare", "origin.git")
    git(src, "remote", "add", "origin", str(tmp / "origin.git"))
    commit(src, "README.md", "Reports go to {reports}/x.md.\n", "clean base")
    return src


def remote_has(src: Path, branch: str) -> bool:
    rc, _ = run(src.parent, "git", "--git-dir", "origin.git", "rev-parse", "-q", "--verify",
                f"refs/heads/{branch}")
    return rc == 0


def scanner_report(src: Path, rel: str) -> str:
    """What CI prints for this scanner on the current (fully committed) tree."""
    _, out = run(src, sys.executable, rel)
    return out.strip()


def verdict(out: str) -> str:
    lines = [ln for ln in out.splitlines() if ln.startswith("GATE ")]
    return lines[-1] if lines else ""


def branch(src: Path, name: str) -> None:
    git(src, "switch", "-q", "main")
    git(src, "switch", "-q", "-c", name)


def main() -> int:
    if shutil.which("git") is None:
        print("FAIL: git not on PATH; the hook cannot be tested")
        return 1
    gate = load_gate()
    if gate.find_gitleaks(REPO) is None:
        print("FAIL: gitleaks not found; the gate (and so this test) needs it. Install it with the profile's "
              "tools/git_gate.py, or follow bin/README.md.")
        return 1
    suites = [s for s, _ in gate.TEST_SUITES]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = make_repo(tmp, suites)

        print("clean push")
        rc, out = run(src, "git", "push", "-q", "origin", "main")
        check("clean push succeeds", rc == 0, out)
        check("clean push reached the remote", remote_has(src, "main"))
        check("the gate ran and passed", "pre-push: running the gate on" in out
              and verdict(out).startswith("GATE PASS") and "pre-push: gate passed" in out, out)
        check("every suite and scanner ran", all(f"<== {label}: ok" in out for label in
              ("public-boundary check", "machine-paths guard self-test", "machine-paths guard scan",
               "gitleaks secret scan", "ctxcheck/test_ctxcheck.py")), out)

        print("drive path in a commit")
        branch(src, "drive")
        commit(src, "docs/notes.md", f"See {DRIVE}{BS}work{BS}repo{BS}notes.md for details.\n", "add drive path")
        machine_report = scanner_report(src, MACHINE_PATHS)
        publish_report = scanner_report(src, PRE_PUBLISH)
        rc, out = run(src, "git", "push", "-q", "origin", "drive")
        check("drive-path push is blocked", rc != 0, out)
        check("blocked branch never reached the remote", not remote_has(src, "drive"))
        check("machine-paths report is CI's, verbatim", "drive-path" in machine_report
              and machine_report in out, f"expected:\n{machine_report}\n--- got:\n{out}")
        check("public-boundary report is CI's, verbatim", "windows-drive-path" in publish_report
              and publish_report in out, f"expected:\n{publish_report}\n--- got:\n{out}")
        check("the verdict names the failing steps", verdict(out).startswith("GATE FAIL")
              and "machine-paths guard scan" in verdict(out) and "public-boundary check" in verdict(out), out)
        check("hook names the block and the allowlists", "pre-push: BLOCKED" in out
              and ".machine-paths-allow.json" in out, out)

        print("fix uncommitted only")
        write(src, "docs/notes.md", "See {repos}/repo/notes.md for details.\n")
        rc, out = run(src, "git", "push", "-q", "origin", "drive")
        check("a working-copy fix does not unblock the pushed commit", rc != 0 and not remote_has(src, "drive"), out)
        git(src, "checkout", "-q", "--", "docs/notes.md")

        print("bypass")
        rc, out = run(src, "git", "push", "-q", "origin", "drive", env={"BIMP_PREPUSH_BYPASS": "hook test"})
        log = Path(git(src, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()) / "prepush-bypass.log"
        check("bypass lets the push through", rc == 0 and remote_has(src, "drive"), out)
        check("bypass is logged with its reason", log.is_file() and "hook test" in log.read_text(encoding="utf-8"))
        check("bypass is announced", "gate BYPASSED (hook test)" in out, out)

        print("private marker only")
        branch(src, "marker")
        commit(src, "docs/contact.md", f"Ask {PRIVATE_EMAIL} for access.\n", "add private email")
        rc, out = run(src, "git", "push", "-q", "origin", "marker")
        check("private-marker push is blocked", rc != 0 and not remote_has(src, "marker"), out)
        check("only the public-boundary check failed", "[email-address]" in out
              and verdict(out).startswith("GATE FAIL: 1 of ")
              and verdict(out).endswith("steps failed: public-boundary check"), out)

        print("failing test suite")
        branch(src, "redtest")
        commit(src, "ctxcheck/test_ctxcheck.py", STUB_FAIL, "break a suite")
        rc, out = run(src, "git", "push", "-q", "origin", "redtest")
        check("a failing suite blocks the push", rc != 0 and not remote_has(src, "redtest"), out)
        check("the failing suite is named and its output shown", "ctxcheck/test_ctxcheck.py" in verdict(out)
              and "stub suite failed on purpose" in out, out)

        print("secret in a commit")
        branch(src, "secret")
        commit(src, "config/settings.txt", f"token = {FAKE_TOKEN}\n", "add a secret")
        rc, out = run(src, "git", "push", "-q", "origin", "secret")
        check("a committed secret blocks the push", rc != 0 and not remote_has(src, "secret"), out)
        check("the secret scan is the failing step", "gitleaks secret scan" in verdict(out), out)
        check("the secret is never printed", FAKE_TOKEN not in out, out)

        print("commit without the gate")
        branch(src, "nogate")
        git(src, "rm", "-q", GATE)
        git(src, "commit", "-q", "-m", "drop the gate")
        rc, out = run(src, "git", "push", "-q", "origin", "nogate")
        check("a commit without the gate is blocked", rc != 0 and not remote_has(src, "nogate"), out)
        check("the block names why", "has no tools/gate.py" in out, out)

        print("dirty working copy, clean commit")
        branch(src, "dirty")
        commit(src, "docs/clean.md", "Nothing machine-specific.\n", "clean change")
        write(src, "docs/scratch.md", f"local only {DRIVE}{BS}scratch\n")
        git(src, "add", "docs/scratch.md")  # staged but not committed
        rc, out = run(src, "git", "push", "-q", "origin", "dirty")
        check("staged machine path does not block a clean commit", rc == 0 and remote_has(src, "dirty"), out)
        git(src, "rm", "-q", "--cached", "docs/scratch.md")

        print("deletion")
        rc, out = run(src, "git", "push", "-q", "origin", ":dirty")
        check("branch deletion is not checked and succeeds", rc == 0 and not remote_has(src, "dirty")
              and "running the gate" not in out, out)

    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILURES:
        print("failed: " + ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

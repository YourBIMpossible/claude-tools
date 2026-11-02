#!/usr/bin/env python3
"""Tests for .githooks/pre-push (the local run of the CI path scanners).

No framework, plain checks (ctxdex-suite style). Each case builds a throwaway repo
holding this checkout's hook and scanners, activates the hook through
core.hooksPath, and runs a real `git push` to a local bare remote, so git itself
invokes the hook (under Git for Windows' sh on Windows). Synthetic content only;
violating literals are assembled at runtime so this file itself stays clean.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = ".githooks/pre-push"
MACHINE_PATHS = ".github/scripts/check_machine_paths.py"
PRE_PUBLISH = "tools/pre_publish_check.py"
BS = "\\"
DRIVE = "Q:"                                 # synthetic drive letter
PRIVATE_EMAIL = "dev" + "@" + "corp-internal.net"  # synthetic, caught by pre-publish only

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


def run(root: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    r = subprocess.run(list(args), cwd=root, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env={**os.environ, **(env or {})})
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


def make_repo(tmp: Path) -> Path:
    """A repo with the hook and both scanners, a bare origin, and core.hooksPath set."""
    src = tmp / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.name", "Hook Test")
    git(src, "config", "user.email", "hook-test@example.com")
    git(src, "config", "core.hooksPath", ".githooks")
    for rel in (HOOK, MACHINE_PATHS, PRE_PUBLISH):
        dest = src / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, dest)
    hook = src / HOOK
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
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


def main() -> int:
    if shutil.which("git") is None:
        print("SKIP: git not on PATH")
        return 0
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src = make_repo(tmp)

        print("clean push")
        rc, out = run(src, "git", "push", "-q", "origin", "main")
        check("clean push succeeds", rc == 0, out)
        check("clean push reached the remote", remote_has(src, "main"))
        check("both scanners ran and passed", "PASS: no machine-specific absolute paths." in out
              and "PASS: no forbidden files or private markers" in out and "self-test:" in out, out)

        print("drive path in a commit")
        git(src, "switch", "-q", "-c", "drive")
        commit(src, "docs/notes.md", f"See {DRIVE}{BS}work{BS}repo{BS}notes.md for details.\n", "add drive path")
        machine_report = scanner_report(src, MACHINE_PATHS)
        publish_report = scanner_report(src, PRE_PUBLISH)
        rc, out = run(src, "git", "push", "-q", "origin", "drive")
        check("drive-path push is blocked", rc != 0, out)
        check("blocked branch never reached the remote", not remote_has(src, "drive"))
        check("machine-paths report is CI's, verbatim", "drive-path" in machine_report
              and machine_report in out, f"expected:\n{machine_report}\n--- got:\n{out}")
        check("pre-publish report is CI's, verbatim", "windows-drive-path" in publish_report
              and publish_report in out, f"expected:\n{publish_report}\n--- got:\n{out}")
        check("hook names the block and the bypass", "pre-push: BLOCKED" in out
              and "BIMP_PATHS_BYPASS" in out, out)

        print("fix uncommitted only")
        write(src, "docs/notes.md", "See {repos}/repo/notes.md for details.\n")
        rc, out = run(src, "git", "push", "-q", "origin", "drive")
        check("a working-copy fix does not unblock the pushed commit", rc != 0, out)
        git(src, "checkout", "-q", "--", "docs/notes.md")

        print("bypass")
        rc, out = run(src, "git", "push", "-q", "origin", "drive", env={"BIMP_PATHS_BYPASS": "hook test"})
        log = Path(git(src, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()) / "paths-bypass.log"
        check("bypass lets the push through", rc == 0 and remote_has(src, "drive"), out)
        check("bypass is logged with its reason", log.is_file() and "hook test" in log.read_text(encoding="utf-8"))

        print("private marker only")
        git(src, "switch", "-q", "main")
        git(src, "switch", "-q", "-c", "marker")
        commit(src, "docs/contact.md", f"Ask {PRIVATE_EMAIL} for access.\n", "add private email")
        rc, out = run(src, "git", "push", "-q", "origin", "marker")
        check("pre-publish marker push is blocked", rc != 0 and not remote_has(src, "marker"), out)
        check("only the pre-publish check failed", "[email-address]" in out
              and "PASS: no machine-specific absolute paths." in out, out)

        print("dirty working copy, clean commit")
        git(src, "switch", "-q", "main")
        git(src, "switch", "-q", "-c", "dirty")
        commit(src, "docs/clean.md", "Nothing machine-specific.\n", "clean change")
        write(src, "docs/scratch.md", f"local only {DRIVE}{BS}scratch\n")
        git(src, "add", "docs/scratch.md")  # staged but not committed
        rc, out = run(src, "git", "push", "-q", "origin", "dirty")
        check("staged machine path does not block a clean commit", rc == 0 and remote_has(src, "dirty"), out)
        git(src, "rm", "-q", "--cached", "docs/scratch.md")

        print("deletion")
        rc, out = run(src, "git", "push", "-q", "origin", ":dirty")
        check("branch deletion is not scanned and succeeds", rc == 0 and not remote_has(src, "dirty")
              and "scanning" not in out, out)

    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILURES:
        print("failed: " + ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

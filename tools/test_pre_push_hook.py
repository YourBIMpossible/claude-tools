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

import hashlib
import importlib.util
import json
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
EXCEPTIONS = "tools/public-boundary-exceptions.json"
MACHINE_ALLOW = ".machine-paths-allow.json"
COPIED = (HOOK, GATE, MACHINE_PATHS, PRE_PUBLISH, "local-audit/slop_prepass.py", ".gitleaks.toml")
BS = "\\"
DRIVE = "Q:"                                       # synthetic drive letter
PRIVATE_EMAIL = "dev" + "@" + "corp-internal.net"  # synthetic, caught by the public-boundary check only
FAKE_TOKEN = "ghp_" + "a1B2" * 9                   # GitHub classic PAT shape, synthetic
STUB_OK = "print('stub suite ok')\n"
STUB_FAIL = "import sys\nprint('stub suite failed on purpose')\nsys.exit(1)\n"
# Fails when a `git -c` given to the push leaks through the hook into the gate's processes.
STUB_NO_PUSH_CONFIG = "import os, sys\nsys.exit('GIT_CONFIG_PARAMETERS' in os.environ)\n"
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


def run_no_gitleaks(root: Path, home: Path, *args: str) -> tuple[int, str]:
    """`run` as on a machine with no gitleaks installed: none named by GITLEAKS_BIN, none on PATH, none under the
    home folder (`home` is an empty one). The only place left to look is bin/ of a checkout."""
    env = {k: v for k, v in ENV.items() if k.upper() != "GITLEAKS_BIN"}
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[path_key] = os.pathsep.join(
        d for d in env.get(path_key, "").split(os.pathsep)
        if d and not any((Path(d) / name).is_file() for name in ("gitleaks", "gitleaks.exe")))
    env["HOME"] = env["USERPROFILE"] = str(home)
    r = subprocess.run(list(args), cwd=root, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, stdin=subprocess.DEVNULL)
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


def line_exception(path: str, rule: str, line: str) -> dict[str, str]:
    """A public-boundary exception for one line, as tools/public-boundary-exceptions.json holds it."""
    return {"path": path, "rule": rule, "line_sha256": hashlib.sha256(line.strip().encode("utf-8")).hexdigest(),
            "reason": "granted by the commit that needs it"}


def head(src: Path) -> str:
    return git(src, "rev-parse", "HEAD").strip()[:12]


def failed_steps(out: str, short: str) -> str:
    """The GATE FAIL line of the scan the hook attributes to commit `short` ('' when that scan did not fail)."""
    lines = out.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("pre-push: FAILED") and f"[{short}, " in ln:
            verdicts = [x for x in lines[:i] if x.startswith("GATE ")]
            return verdicts[-1] if verdicts else ""
    return ""


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

        print("git -c on the push")
        branch(src, "gitc")
        commit(src, "ctxcheck/test_ctxcheck.py", STUB_NO_PUSH_CONFIG, "probe for push-time config")
        rc, out = run(src, "git", "-c", "gate.probe=1", "push", "-q", "origin", "gitc")
        check("a `git -c` on the push does not reach the gate's suites", rc == 0 and remote_has(src, "gitc")
              and verdict(out).startswith("GATE PASS"), out)

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

        # SA09-1: every pushed commit is published, not only the tip. The tip runs the whole gate; each earlier
        # commit new to the remote runs the publication scans.
        print("secret added, then removed, in the pushed range")
        branch(src, "history")
        commit(src, "config/settings.txt", f"token = {FAKE_TOKEN}\n", "add a secret")
        leaked = git(src, "rev-parse", "HEAD").strip()[:12]
        git(src, "rm", "-q", "config/settings.txt")
        git(src, "commit", "-q", "-m", "remove the secret")
        clean_tip = git(src, "rev-parse", "HEAD").strip()[:12]
        rc, out = run(src, "git", "push", "-q", "origin", "history")
        check("a secret in an earlier pushed commit blocks the push", rc != 0 and not remote_has(src, "history"), out)
        check("the tip itself is clean and passes the whole gate",
              f"pre-push: running the gate on {clean_tip} " in out and "GATE PASS" in out, out)
        check("the earlier commit is scanned, and named as the one that failed",
              f"pre-push: scanning {leaked} " in out and f"[{leaked}, " in out
              and "gitleaks secret scan" in out, out)
        check("the earlier commit's secret is never printed", FAKE_TOKEN not in out, out)

        print("machine path added, then removed, in the pushed range")
        branch(src, "pathhistory")
        commit(src, "docs/notes.md", f"See {DRIVE}{BS}work{BS}repo{BS}notes.md for details.\n", "add drive path")
        commit(src, "docs/notes.md", "See {repos}/repo/notes.md for details.\n", "fix the path")
        rc, out = run(src, "git", "push", "-q", "origin", "pathhistory")
        check("a machine path in an earlier pushed commit blocks the push",
              rc != 0 and not remote_has(src, "pathhistory") and "machine-paths guard scan" in out, out)

        print("several clean commits")
        branch(src, "several")
        commit(src, "docs/one.md", "First.\n", "one")
        first = git(src, "rev-parse", "HEAD").strip()[:12]
        commit(src, "docs/two.md", "Second.\n", "two")
        second = git(src, "rev-parse", "HEAD").strip()[:12]
        commit(src, "docs/three.md", "Third.\n", "three")
        rc, out = run(src, "git", "push", "-q", "origin", "several")
        check("several clean commits push", rc == 0 and remote_has(src, "several"), out)
        check("each earlier commit is scanned once, oldest first",
              out.count("pre-push: scanning ") == 2
              and out.index(f"pre-push: scanning {first} ") < out.index(f"pre-push: scanning {second} "), out)
        check("only the tip runs the test suites", out.count("pre-push: running the gate on ") == 1
              and out.count("publication scans only") == 2, out)
        commit(src, "docs/four.md", "Fourth.\n", "four")
        rc, out = run(src, "git", "push", "-q", "origin", "several")
        check("a later push scans nothing the remote already has",
              rc == 0 and "pre-push: scanning " not in out, out)

        # PR 50 review F1: an earlier commit is scanned with the TIP's scanners and allowlists, never its own.
        # Scanned with its own, a commit could relax the scan for itself and a later commit could tidy up after it.
        print("earlier commit excepts its own private marker")
        branch(src, "selfexcept")
        contact = f"Ask {PRIVATE_EMAIL} for access."
        write(src, EXCEPTIONS, json.dumps([line_exception("docs/contact.md", "email-address", contact)]))
        commit(src, "docs/contact.md", contact + "\n", "add a private email and except it")
        widened = head(src)
        git(src, "rm", "-q", "docs/contact.md", EXCEPTIONS)
        git(src, "commit", "-q", "-m", "remove the email and the exception")
        rc, out = run(src, "git", "push", "-q", "origin", "selfexcept")
        check("an earlier commit that excepts its own private marker is refused",
              rc != 0 and not remote_has(src, "selfexcept"), out)
        check("the refusal is the public-boundary check on that commit",
              "public-boundary check" in failed_steps(out, widened), out)

        print("earlier commit allowlists its own secret")
        branch(src, "selfsecret")
        write(src, ".gitleaks.toml", "[extend]\nuseDefault = true\n\n[allowlist]\n"
              "description = \"self-granted\"\npaths = ['''config[/\\\\]settings\\.txt''']\n")
        commit(src, "config/settings.txt", f"token = {FAKE_TOKEN}\n", "add a secret and allowlist its file")
        widened = head(src)
        git(src, "checkout", "-q", "main", "--", ".gitleaks.toml")
        git(src, "rm", "-q", "config/settings.txt")
        git(src, "commit", "-q", "-m", "remove the secret, restore the config")
        rc, out = run(src, "git", "push", "-q", "origin", "selfsecret")
        check("an earlier commit that allowlists its own secret is refused",
              rc != 0 and not remote_has(src, "selfsecret"), out)
        check("the refusal is the secret scan on that commit",
              "gitleaks secret scan" in failed_steps(out, widened) and FAKE_TOKEN not in out, out)

        print("earlier commit allowlists its own machine path")
        branch(src, "selfpath")
        note = f"See {DRIVE}{BS}work{BS}repo{BS}notes.md for details."
        write(src, MACHINE_ALLOW, json.dumps([{"path": "docs/notes.md", "reason": "self-granted"}]))
        write(src, EXCEPTIONS, json.dumps([line_exception("docs/notes.md", "windows-drive-path", note)]))
        commit(src, "docs/notes.md", note + "\n", "add a drive path and allowlist it twice")
        widened = head(src)
        git(src, "rm", "-q", "docs/notes.md", MACHINE_ALLOW, EXCEPTIONS)
        git(src, "commit", "-q", "-m", "remove the path and both entries")
        rc, out = run(src, "git", "push", "-q", "origin", "selfpath")
        check("an earlier commit that allowlists its own machine path is refused",
              rc != 0 and not remote_has(src, "selfpath"), out)
        check("both path scans fail on that commit", "machine-paths guard scan" in failed_steps(out, widened)
              and "public-boundary check" in failed_steps(out, widened), out)

        print("earlier commit replaces a scanner")
        branch(src, "selfscanner")
        write(src, PRE_PUBLISH, "print('PASS: nothing looked at')\n")
        commit(src, "docs/contact.md", contact + "\n", "add a private email and gut the scanner")
        widened = head(src)
        git(src, "checkout", "-q", "main", "--", PRE_PUBLISH)
        git(src, "rm", "-q", "docs/contact.md")
        git(src, "commit", "-q", "-m", "remove the email, restore the scanner")
        rc, out = run(src, "git", "push", "-q", "origin", "selfscanner")
        check("an earlier commit that replaces a scanner is refused",
              rc != 0 and not remote_has(src, "selfscanner"), out)
        check("the tip's scanner is the one that ran on it",
              "public-boundary check" in failed_steps(out, widened) and "[email-address]" in out, out)

        # Review N1: where no gitleaks is installed, the last place the gate looks is bin/ of a checkout. For an
        # earlier commit that must be the TIP's bin/: a commit that ships its own "gitleaks" would scan itself clean.
        print("earlier commit ships its own gitleaks")
        branch(src, "selfbinary")
        shipped = src / "bin" / ("gitleaks.exe" if os.name == "nt" else "gitleaks")
        shipped.parent.mkdir(exist_ok=True)
        if os.name == "nt":
            shutil.copyfile(os.environ["ComSpec"], shipped)  # cmd.exe: ignores the arguments, exits 0 at end of input
        else:
            shipped.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shipped.chmod(shipped.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        commit(src, "config/settings.txt", f"token = {FAKE_TOKEN}\n", "add a secret and a scanner that finds nothing")
        widened = head(src)
        git(src, "rm", "-q", "-r", "config/settings.txt", "bin")
        git(src, "commit", "-q", "-m", "remove the secret and the scanner")
        empty_home = tmp / "empty-home"
        empty_home.mkdir()
        rc, out = run_no_gitleaks(src, empty_home, "git", "push", "-q", "origin", "selfbinary")
        check("an earlier commit that ships its own gitleaks is refused",
              rc != 0 and not remote_has(src, "selfbinary"), out)
        # The decisive check: that commit's secret scan must not pass. With no gitleaks in the tip's bin/ either, the
        # scan fails closed ("gitleaks not found") instead of running the scanner the commit brought along.
        check("its secret scan fails closed; the scanner it ships is not the one used",
              "gitleaks secret scan" in failed_steps(out, widened) and FAKE_TOKEN not in out, out)

        print("allowlist entries that stay to the tip")
        branch(src, "keptentry")
        commit(src, "docs/one.md", "First.\n", "a clean commit before the entry exists")
        write(src, EXCEPTIONS, json.dumps([line_exception("docs/contact.md", "email-address", contact)]))
        commit(src, "docs/contact.md", contact + "\n", "add a fixture with its exception")
        commit(src, "docs/two.md", "Second.\n", "a clean commit after it")
        rc, out = run(src, "git", "push", "-q", "origin", "keptentry")
        check("a range whose exception is still in the tip pushes", rc == 0 and remote_has(src, "keptentry"), out)
        check("the tip's entry does not fail the commit that predates what it covers",
              out.count("pre-push: scanning ") == 2 and "pre-push: FAILED" not in out, out)

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

#!/usr/bin/env python3
"""Tests for tools/gate.py (the one command the pre-push hook and both CI jobs run).

No framework, plain checks (ctxdex-suite style). They pin what the gate runs (so a step cannot be dropped or narrowed
quietly, and no tracked suite goes unrun), what it leaves out per platform and why, and how it fails (a failing or
unstartable step fails the gate, every step still runs, a failing step's output is shown, the last line is the
verdict, missing gitleaks is a failure). They never run the real default steps: that would start this suite inside
itself. Synthetic content only; the secret is assembled at runtime so this file itself stays clean.
"""
from __future__ import annotations

import importlib.util
import io
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "tools" / "gate.py"
FAKE_TOKEN = "ghp_" + "a1B2" * 9  # GitHub classic PAT shape, synthetic

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


def workflow_code(text: str) -> str:
    """A workflow with its comments stripped, so a commented-out trigger neither passes nor fails a check."""
    return "\n".join(re.sub(r"(^|\s)#.*", "", line).rstrip() for line in text.splitlines())


def workflow_events(code: str) -> set:
    """The events under a workflow's top-level `on:`, inline (`on: [push, pull_request]`) or as a block. Anything it
    cannot read comes back empty, which no posture check accepts."""
    match = re.search(r"^on:[ \t]*(.*)\n((?:(?:[ \t]+.*)?\n)*)", code + "\n", re.M)
    if not match:
        return set()
    inline, block = match.groups()
    return set(re.findall(r"\w+", inline)) | set(re.findall(r"^  (\w+)\s*:", block, re.M))


def workflow_jobs(code: str) -> dict:
    """Each job under `jobs:`, by id, as its own block of code."""
    match = re.search(r"^jobs:[ \t]*\n((?:(?:[ \t]+.*)?\n)*)", code + "\n", re.M)
    parts = re.split(r"^  ([\w-]+):[ \t]*\n", match.group(1), flags=re.M) if match else []
    return dict(zip(parts[1::2], parts[2::2]))


def pull_request_block(code: str) -> str:
    """The body of the `pull_request:` event under `on:`, empty when there is none."""
    match = re.search(r"^on:[ \t]*\n(?:(?:[ \t]+.*)?\n)*?  pull_request:[ \t]*\n((?:(?:    .*)?\n)*)", code + "\n", re.M)
    return match.group(1) if match else ""


def posture_problems(code: str) -> list:
    """Why an automatic workflow breaks the cost posture: it must fire once per PR and skip drafts in every job, on
    Linux only."""
    problems = []
    if "    types: [opened, reopened, ready_for_review]" not in pull_request_block(code):
        problems.append("pull_request is not limited to opened, reopened, ready_for_review")
    if "synchronize" in code:
        problems.append("names synchronize")
    jobs = workflow_jobs(code)
    if not jobs:
        problems.append("no jobs read")
    for job, body in jobs.items():
        if not re.search(r"^    if:.*github\.event\.pull_request\.draft == false", body, re.M):
            problems.append(f"job {job} runs on drafts")
        if re.findall(r"^    runs-on:\s*(\S.*)", body, re.M) != ["ubuntu-latest"]:
            problems.append(f"job {job} is not ubuntu-latest only")
    return problems


# An automatic workflow whose second job forgot the draft skip and runs on Windows: the posture check must name both.
SECOND_JOB = (
    "on:\n  pull_request:\n    types: [opened, reopened, ready_for_review]\njobs:\n"
    "  first:\n    if: ${{ github.event.pull_request.draft == false }}\n    runs-on: ubuntu-latest\n"
    "  second:\n    runs-on: windows-latest\n")


def load():
    spec = importlib.util.spec_from_file_location("tools_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def run_main(gate, steps, *extra) -> tuple[int, str]:
    out = io.StringIO()
    code = gate.main(["--root", str(REPO), *extra], steps=steps, out=out)
    return code, out.getvalue()


def lines_of(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def which_only(*names: str):
    return lambda name: f"/stub/{name}" if name in names else None


def installed(module: str) -> bool:
    return True


def main() -> int:
    gate = load()

    print("what the gate runs")
    # Tracked plus not-yet-added files, so a new suite is caught before its first commit too.
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, text=True).stdout.split()
    suites_on_disk = sorted(p for p in tracked if Path(p).name.startswith("test_") and p.endswith(".py"))
    listed = sorted(s for s, _ in gate.TEST_SUITES)
    check("every tracked test_*.py is a gate suite (none goes unrun)", suites_on_disk == listed,
          f"not in TEST_SUITES: {sorted(set(suites_on_disk) - set(listed))}\n"
          f"listed but not tracked: {sorted(set(listed) - set(suites_on_disk))}")
    check("the suite list has no duplicates", len(listed) == len(set(listed)), str(listed))
    win, win_skipped = gate.default_steps(REPO, "python", windows=True, which=which_only("pwsh", "powershell.exe"),
                                           has_module=installed)
    labels = [s.label for s in win]
    check("Windows with both shells and every package runs every suite, nothing left out", not win_skipped, str(win_skipped))
    for name in ("public-boundary check", "machine-paths guard self-test", "machine-paths guard scan",
                 "slop_prepass self-test", "gitleaks secret scan"):
        check(f"step present: {name}", name in labels, str(labels))
    check("the scanners and self-tests run exactly as CI ran them", [s.argv for s in win[:4]] == [
        ["python", "tools/pre_publish_check.py"],
        ["python", ".github/scripts/check_machine_paths.py", "--self-test"],
        ["python", ".github/scripts/check_machine_paths.py"],
        ["python", "local-audit/slop_prepass.py", "--self-test"]], str([s.argv for s in win[:4]]))
    check("the secret scan is the last step", labels[-1] == "gitleaks secret scan", str(labels))
    shells = {s.label: dict(s.env).get("GRAPHIFY_TEST_SHELL") for s in win if "graphify/test_health_check" in s.label}
    check("Windows runs the PowerShell suites under pwsh 7 and 5.1", sorted(shells.values()) ==
          ["powershell.exe", "pwsh"], str(shells))
    check("the Windows-only suite runs on Windows", "scheduling/test_run_hidden.py" in labels, str(labels))
    for step in win:
        for part in step.argv[1:]:
            if part.endswith(".py"):
                check(f"{step.label}: {part} exists", (REPO / part).is_file())

    lin, lin_skipped = gate.default_steps(REPO, "python", windows=False, which=which_only("pwsh"), has_module=installed)
    lin_labels = [s.label for s in lin]
    check("Linux leaves out only the Windows-only suite, and says so",
          lin_skipped == ["not run here (Windows only): scheduling/test_run_hidden.py"], str(lin_skipped))
    check("Linux runs the PowerShell suites once, under pwsh",
          [s.label for s in lin if "graphify/test_refresh_graphs" in s.label] ==
          ["graphify/test_refresh_graphs.py (pwsh 7)"], str(lin_labels))
    _, bare_skipped = gate.default_steps(REPO, "python", windows=False, which=which_only(), has_module=installed)
    check("no pwsh: the PowerShell suites are left out with a line naming why",
          sum("no pwsh" in ln for ln in bare_skipped) == 2, str(bare_skipped))
    no_pkg, no_pkg_skipped = gate.default_steps(REPO, "python", windows=True,
                                                which=which_only("pwsh", "powershell.exe"),
                                                has_module=lambda module: module != "evidence_compiler")
    check("evidence_compiler missing: only its suite is left out, with the pinned install to run",
          no_pkg_skipped == ["not run here (evidence_compiler not installed; pip install "
                             f"\"{gate.EVIDENCE_COMPILER_SOURCE}\"): evidence-episodes/test_build_episodes.py"]
          and "evidence-episodes/test_build_episodes.py" not in [s.label for s in no_pkg], str(no_pkg_skipped))

    print("CI runs this gate")
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    check("both CI jobs run `python tools/gate.py --verbose`", workflow.count("python tools/gate.py --verbose") == 2)
    check("CI runs on Linux and Windows", "ubuntu-latest" in workflow and "windows-latest" in workflow)
    check("both CI jobs install evidence-compiler at the gate's pin, so its suite runs in CI",
          workflow.count(f'python -m pip install "{gate.EVIDENCE_COMPILER_SOURCE}"') == 2)

    print("CI cost posture")
    workflows = REPO / ".github" / "workflows"
    check("the posture parser reads inline and block triggers and ignores comments",
          workflow_events(workflow_code("on: [push, pull_request]\njobs:\n")) == {"push", "pull_request"}
          and workflow_events(workflow_code("on:\n  # push:\n  pull_request:\n    types: [opened]\njobs:\n"))
          == {"pull_request"} and workflow_events(workflow_code('"on":\n  push:\n')) == set())
    check("the posture check refuses a second job without the draft skip, and a Windows job",
          posture_problems(SECOND_JOB) == ["job second runs on drafts", "job second is not ubuntu-latest only"],
          str(posture_problems(SECOND_JOB)))
    check("the full gate (ci.yml) is workflow_dispatch only",
          workflow_events(workflow_code(workflow)) == {"workflow_dispatch"}, str(workflow_events(workflow_code(workflow))))
    scan = (workflows / "secret-scan.yml").read_text(encoding="utf-8")
    check("the automatic check runs the boundary check and gitleaks with the gate's config",
          "python tools/pre_publish_check.py" in scan and "--no-git" in scan and "--config .gitleaks.toml" in scan
          and f'GITLEAKS_VERSION: "{gate.GITLEAKS_PIN}"' in scan)
    automatic = {}
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        code = workflow_code(path.read_text(encoding="utf-8"))
        events = workflow_events(code)
        check(f"{path.name}: fires only on pull_request or workflow_dispatch",
              bool(events) and events <= {"pull_request", "workflow_dispatch"}, str(events))
        if events != {"workflow_dispatch"}:
            automatic[path.name] = code
    check("the automatic workflows are exactly machine-paths.yml and secret-scan.yml",
          set(automatic) == {"machine-paths.yml", "secret-scan.yml"}, str(sorted(automatic)))
    for name, code in sorted(automatic.items()):
        check(f"{name}: fires once per PR (no synchronize), skips drafts in every job, Linux only",
              not posture_problems(code), str(posture_problems(code)))

    print("verdicts")
    code, text = run_main(gate, [gate.Step("one", py("print('one ran')")), gate.Step("two", py("print('two ran')"))])
    lines = lines_of(text)
    check("all passing is a pass, and the last line says so", code == 0 and lines[-1].startswith("GATE PASS"), text)
    check("a passing step prints one line, not its output", "one ran" not in text
          and lines[0].startswith("<== one: ok") and lines[1].startswith("<== two: ok"), text)
    code, text = run_main(gate, [gate.Step("one", py("print('one ran')"))], "--verbose")
    check("--verbose prints every step's output", code == 0 and "one ran" in text, text)
    code, text = run_main(gate, [gate.Step("first", py("print('first ran')")),
                                 gate.Step("broken", py("import sys; print('broken ran'); sys.exit(3)")),
                                 gate.Step("last", py("print('last ran')"))])
    lines = lines_of(text)
    check("a failing step fails the gate and is named", code == 1 and lines[-1].startswith("GATE FAIL")
          and "broken" in lines[-1] and "first" not in lines[-1], text)
    check("a failing step's output is shown", "broken ran" in text, text)
    check("steps after a failure still run", "<== last: ok" in text, text)
    code, text = run_main(gate, [gate.Step("missing program", ["this-program-does-not-exist-anywhere"]),
                                 gate.Step("fine", py("print('fine ran')"))])
    check("a step that cannot start fails the gate instead of crashing it", code == 1
          and "could not start" in text and "<== fine: ok" in text, text)
    code, text = run_main(gate, [])
    check("no steps is not a pass", code == 1 and "GATE FAIL" in lines_of(text)[-1], text)
    code, text = run_main(gate, [gate.Step("env", py("import os, sys; sys.exit(os.environ.get('GRAPHIFY_TEST_SHELL') "
                                                    "!= 'powershell.exe')"), (("GRAPHIFY_TEST_SHELL", "powershell.exe"),))])
    check("a step's own environment reaches it", code == 0, text)

    print("the verdict counts what was left out (SA09-2)")
    one = [gate.Step("one", py("print('one ran')"))]
    left_out = ["not run here (Windows only): a/test_a.py", "not run here (no pwsh on PATH): b/test_b.py"]
    out = io.StringIO()
    code = gate.main(["--root", str(REPO)], steps=one, out=out, skipped=left_out)
    text = out.getvalue()
    check("a pass with suites left out says how many, in the verdict line", code == 0
          and lines_of(text)[-1] == "GATE PASS: 1 of 1 steps passed; 2 suites not run here (listed above)", text)
    check("each left-out suite is still listed before the verdict", all(ln in lines_of(text)[:-1] for ln in left_out),
          text)
    out = io.StringIO()
    code = gate.main(["--root", str(REPO)], steps=one, out=out, skipped=left_out[:1])
    check("one left-out suite reads as one", code == 0
          and lines_of(out.getvalue())[-1] == "GATE PASS: 1 of 1 steps passed; 1 suite not run here (listed above)",
          out.getvalue())
    code, text = run_main(gate, one)
    check("a pass with nothing left out keeps the plain verdict",
          code == 0 and lines_of(text)[-1] == "GATE PASS: 1 of 1 steps passed", text)

    print("publication scans only (SA09-1)")
    scans = gate.scan_steps(REPO, "python")
    check("the publication scans are the boundary check, the machine-paths scan and the secret scan",
          [s.label for s in scans] == ["public-boundary check", "machine-paths guard scan", "gitleaks secret scan"],
          str([s.label for s in scans]))
    check("the secret scan step runs this gate, not the scanned commit's copy",
          Path(scans[-1].argv[1]).resolve() == (REPO / "tools" / "gate.py").resolve()
          and scans[-1].argv[2:] == ["--secret-scan", "--root", str(REPO)], str(scans[-1].argv))
    saved_scan_steps = gate.scan_steps
    gate.scan_steps = lambda root: [gate.Step("stub scan", py("print('stub scan ran')"))]
    try:
        out = io.StringIO()
        code = gate.main(["--root", str(REPO), "--scans-only"], out=out)
    finally:
        gate.scan_steps = saved_scan_steps
    text = out.getvalue()
    check("--scans-only runs the scans and no suite, and its verdict says so", code == 0
          and lines_of(text) == ["<== stub scan: ok in " + lines_of(text)[0].rsplit(" in ", 1)[-1],
                                 "GATE PASS: 1 of 1 steps passed (publication scans only, no test suite)"], text)

    print("secret scan")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check("no gitleaks anywhere is found as none", gate.find_gitleaks(root, environ={}, which=which_only()) is None
              or (Path.home() / ".claude" / "bin").is_dir())
        check("GITLEAKS_BIN wins", gate.find_gitleaks(root, environ={"GITLEAKS_BIN": "x-gl"}, which=which_only("gitleaks"))
              == "x-gl")
        (root / "bin").mkdir()
        (root / "bin" / "gitleaks.exe").write_text("", encoding="utf-8")
        found = gate.find_gitleaks(root, environ={}, which=which_only())
        check("the repo's bin/ is the last place looked", found is not None, str(found))
        out = io.StringIO()
        saved = gate.find_gitleaks
        gate.find_gitleaks = lambda r, environ=None, which=None: None
        try:
            rc = gate.secret_scan(root, out)
        finally:
            gate.find_gitleaks = saved
        check("missing gitleaks fails the scan and names the fix", rc == 1 and "git_gate.py" in out.getvalue()
              and "required" in out.getvalue(), out.getvalue())

    gitleaks = gate.find_gitleaks(REPO)
    check("gitleaks is installed here (the gate needs it)", gitleaks is not None,
          "install it: the profile's tools/git_gate.py, or bin/README.md")
    if gitleaks and shutil.which("git"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (root / "ignored.txt").write_text(f"token = {FAKE_TOKEN}\n", encoding="utf-8")
            (root / "clean.txt").write_text("nothing here\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", ".gitignore", "clean.txt"], check=True)
            out = io.StringIO()
            rc = gate.secret_scan(root, out)
            check("an ignored file's secret is not scanned (it is never published)", rc == 0, out.getvalue())
            (root / "leak.txt").write_text(f"token = {FAKE_TOKEN}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "leak.txt"], check=True)
            out = io.StringIO()
            rc = gate.secret_scan(root, out)
            check("a tracked file's secret fails the scan, redacted", rc != 0 and FAKE_TOKEN not in out.getvalue(),
                  out.getvalue())

    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILURES:
        print("failed: " + ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

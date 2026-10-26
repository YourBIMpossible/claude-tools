#!/usr/bin/env python3
"""Tests for scheduling/run-hidden.js: the hidden launcher for scheduled-task actions.

Windows only (it drives the real wscript.exe); on any other platform it prints SKIP and exits 0.
Run: python scheduling/test_run_hidden.py

What is proved, each against the real launcher:
  * the child's exit code passes through (0, a small non-zero, 255, and an NTSTATUS-sized code);
  * every argument arrives exactly as given: spaces, a trailing backslash, an empty string, a lone percent sign,
    a tab. Two documented limits of Windows Script Host are stated rather than hidden: an embedded double
    quote cannot cross wscript.exe's own command-line parser (explained in the test), and a %NAME% naming a SET
    environment variable is expanded by WScript.Shell.Run (pinned by a check; an unset name and a lone % stay
    literal);
  * a command that cannot be started exits 127, and no arguments at all exits 2 (never 0);
  * the child's console window is NOT visible (window style 0). The control run starts the same probe in
    a fresh console and must see a visible window; if the machine cannot show one (a service session)
    the no-window test reports SKIP rather than a false pass.

Nothing here registers, runs or reads a scheduled task.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "run-hidden.js"
WSCRIPT = (Path(os.environ["SystemRoot"]) / "System32" / "wscript.exe") if os.environ.get("SystemRoot") else Path("wscript.exe")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" -- {detail}" if detail and not cond else ""))


def skip(name, why):
    print(f"SKIP: {name} -- {why}")


def python_exe():
    exe = Path(sys.executable)
    return exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe


def launch(*argv, timeout=60):
    """wscript //B //Nologo run-hidden.js <argv...>, the way a scheduled-task action runs it."""
    return subprocess.run([str(WSCRIPT), "//B", "//Nologo", str(LAUNCHER), *map(str, argv)],
                          capture_output=True, text=True, timeout=timeout)


def main():
    if sys.platform != "win32":
        print("SKIP: run-hidden.js is a Windows Script Host launcher; nothing to run on this platform")
        return 0
    check("the launcher file exists", LAUNCHER.is_file(), str(LAUNCHER))
    check("wscript.exe exists", WSCRIPT.is_file(), str(WSCRIPT))
    if not LAUNCHER.is_file() or not WSCRIPT.is_file():
        return 1
    py = python_exe()
    tmp = Path(tempfile.mkdtemp(prefix="run hidden "))  # a space in the folder name on purpose
    try:
        # -- exit codes pass through
        for code in (0, 7, 255):
            p = launch(py, "-c", f"import sys; sys.exit({code})")
            check(f"exit code {code} passes through", p.returncode == code, f"rc={p.returncode} err={p.stderr[:200]}")
        p = launch(py, "-c", "import ctypes; ctypes.windll.kernel32.ExitProcess(0xC000013A)")
        check("an NTSTATUS-sized exit code (0xC000013A, the B14 Windows Terminal kill) passes through",
              p.returncode in (0xC000013A, 0xC000013A - (1 << 32)), f"rc={p.returncode}")

        # -- arguments arrive exactly. The command line is written the way a task's <Arguments> element is:
        #    each argument in plain double quotes. (wscript.exe has its own, simpler parser: double quotes group
        #    and are removed, a backslash is literal, so an embedded double quote cannot be sent through
        #    wscript's command line at all; put such text in a file or an environment variable instead.)
        probe = tmp / "argv dump.py"
        probe.write_text("import json, sys\njson.dump(sys.argv[2:], open(sys.argv[1], 'w', encoding='utf-8'))\n",
                         encoding="utf-8")
        out = tmp / "argv out.json"
        tricky = ["plain", "with space", "folder with space\\sub dir\\", "", "100%", "a\\\\b c", "-c", "tab\there"]
        line = " ".join(f'"{a}"' for a in [WSCRIPT, "//B", "//Nologo", LAUNCHER, py, probe, out, *tricky])
        p = subprocess.run(line, capture_output=True, text=True, timeout=60)
        got = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
        check("arguments (spaces, trailing backslash, empty, a lone %, a lone -c) arrive exactly", got == tricky,
              f"rc={p.returncode} got={got!r}")

        # -- documented limit: WScript.Shell.Run expands %NAME% when NAME is a set environment variable
        out2 = tmp / "percent out.json"
        pline = " ".join(f'"{a}"' for a in [WSCRIPT, "//B", "//Nologo", LAUNCHER, py, probe, out2,
                                            "%RUN_HIDDEN_SET_VAR%", "%RUN_HIDDEN_UNSET_VAR%", "100%"])
        penv = {k: v for k, v in os.environ.items() if k != "RUN_HIDDEN_UNSET_VAR"}
        penv["RUN_HIDDEN_SET_VAR"] = "expanded value"
        subprocess.run(pline, capture_output=True, text=True, timeout=60, env=penv)
        got2 = json.loads(out2.read_text(encoding="utf-8")) if out2.is_file() else None
        check("a %NAME% naming a set variable is expanded; an unset name and a lone % stay literal (documented limit)",
              got2 == ["expanded value", "%RUN_HIDDEN_UNSET_VAR%", "100%"], f"got={got2!r}")

        # -- failure modes are never silent successes
        p = launch(str(tmp / "no such program.exe"))
        check("a program that cannot be started exits 127", p.returncode == 127, f"rc={p.returncode}")
        p = subprocess.run([str(WSCRIPT), "//B", "//Nologo", str(LAUNCHER)], capture_output=True, text=True, timeout=60)
        check("no arguments exits 2", p.returncode == 2, f"rc={p.returncode}")

        # -- no visible window
        cprobe = tmp / "console probe.py"
        cprobe.write_text(
            "import ctypes, json, sys\n"
            "h = ctypes.windll.kernel32.GetConsoleWindow()\n"
            "json.dump({'console': bool(h), 'visible': bool(h and ctypes.windll.user32.IsWindowVisible(h))},"
            " open(sys.argv[1], 'w'))\n", encoding="utf-8")
        ctl = tmp / "control.json"
        subprocess.run([str(py), str(cprobe), str(ctl)], timeout=60, creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        control = json.loads(ctl.read_text()) if ctl.is_file() else {}
        hidden = tmp / "hidden.json"
        launch(py, cprobe, hidden)
        seen = json.loads(hidden.read_text()) if hidden.is_file() else None
        check("the probe ran through the launcher", seen is not None)
        if not control.get("visible"):
            skip("no visible console window", "the control (a fresh console) was not visible here, so this machine "
                 "cannot show the difference (a service or headless session)")
        else:
            check("the child's console window is not visible (window style 0)",
                  seen is not None and seen.get("visible") is False, f"seen={seen} control={control}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

# scheduling: run a scheduled task without a window

`run-hidden.js` is a tiny Windows Script Host launcher for the **action** of a Task Scheduler task. It starts a
program with window style 0 (hidden), waits for it, and exits with the program's own exit code.

## Why

A task whose action is `powershell.exe` or `python.exe` gets a console. On Windows 11 that console is handed to
Windows Terminal, so even `-WindowStyle Hidden` flashes a window and the console can land in a Windows Terminal
window. A windowless parent (`pythonw.exe`) does not help when it starts console children (`git`, `attrib`): each one
opens its own console. `WScript.Shell.Run(..., 0, true)` creates the whole tree hidden, so there is nothing to hand
off or show. The exit code is passed through, so Task Scheduler's `LastTaskResult` (and anything that reads it, such
as the graphify health check) stays truthful.

Modelled on `aep-run-hidden.js` in the zen-toolchain repository (a one-script launcher written for a fixed `pwsh`
and one `.ps1`); this one runs any program.

## Usage

The task action is the launcher, not the program:

    Command:    wscript.exe
    Arguments:  //B //Nologo "<claude-tools>\scheduling\run-hidden.js" <program> [arguments...]

Put each argument in plain double quotes (paths with spaces). `<claude-tools>` is the clone's folder; use the full
path in the task. The working directory of the task is kept.

Exit codes: the program's own; `2` = no program given; `127` = the program could not be started.

Limits, both of Windows Script Host and not of the launcher:

- An embedded double quote cannot be sent through `wscript.exe`'s own command line (it treats a double quote as a
  grouping character and a backslash as literal). Put such text in a file or an environment variable. For the same
  reason a program that builds the Arguments string with MSVCRT-style escaping (Python's list quoting does) sends
  `Dir\\` for `Dir\`: write the plain-quoted string a task action holds.
- `WScript.Shell.Run` expands `%NAME%` when `NAME` is a set environment variable (an unset name and a lone `%` stay
  literal). Do not pass a literal `%NAME%` that must survive.

## The tasks of this repository

- **Evidence capture backstop:** `python capture.py backstop --task-xml PATH` writes a ready definition (review only,
  nothing registers it). The action is `wscript.exe //B //Nologo <run-hidden.js> python.exe capture.py backstop`.
  Use `python.exe`, not `pythonw.exe`: the `git` and `attrib` children then share the one hidden console.
- **Graphify weekly refresh and health check:** action `wscript.exe`, arguments
  `//B //Nologo "<claude-tools>\scheduling\run-hidden.js" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<claude-tools>\graphify\Refresh-Graphs.ps1"`
  (and `Check-GraphifyHealth.ps1` for the health check). Keep each task in the signed-in user's session: the health
  check shows a balloon toast, which needs it.

## Tests

`python scheduling/test_run_hidden.py` (Windows only; it prints SKIP elsewhere): exit codes pass through (0, 7, 255
and an NTSTATUS-sized one), arguments arrive intact (spaces, trailing backslash, empty, a lone percent, tab; the percent limit above is pinned), a program that
cannot start exits 127, and the child's console window is not visible (with a control that starts the same probe in a
visible console, so the test cannot pass on a machine that cannot show a window).

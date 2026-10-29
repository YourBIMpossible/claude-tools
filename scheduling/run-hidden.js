// Runs a command with NO window, waits for it, and exits with the command's own exit code.
//
// Why: a scheduled task whose action is powershell.exe or python.exe gets a console, and on Windows 11 that
// console is handed to Windows Terminal (the default-terminal handoff), so even "-WindowStyle Hidden" flashes a
// window, and a windowless parent (pythonw.exe) still flashes one console per child it starts. WScript.Shell.Run
// with window style 0 creates the child's console hidden, so there is nothing to hand off and nothing to show.
// The exit code is passed through so Task Scheduler's LastTaskResult (and anything that reads it) stays truthful.
//
// Modelled on aep-run-hidden.js in the zen-toolchain repository (B14, 2026-10-19: a Windows Terminal close killed
// a hidden pwsh task with 0xC000013A). That one is fixed to one pwsh and one .ps1; this one runs any program.
//
// Usage (the task action):
//   wscript.exe //B //Nologo <path>\run-hidden.js <program> [arguments...]
// Every argument is quoted for the child (paths with spaces, quotes and trailing backslashes arrive intact).
// Exit codes: the child's own; 2 = no program given; 127 = the program could not be started.
// The working directory is the one the task gives wscript.exe.
(function () {
    var args = WScript.Arguments;
    if (args.length < 1) { WScript.Quit(2); }

    // CommandLineToArgvW-compatible quoting: backslashes are literal unless they precede a double quote (or the
    // closing quote), where each one is doubled; an embedded double quote is escaped with a backslash.
    function repeat(ch, n) { return new Array(n + 1).join(ch); }
    function quote(arg) {
        var out = '"', slashes = 0, i, c;
        for (i = 0; i < arg.length; i++) {
            c = arg.charAt(i);
            if (c === '\\') {
                slashes++;
            } else if (c === '"') {
                out += repeat('\\', slashes * 2) + '\\"';
                slashes = 0;
            } else {
                out += repeat('\\', slashes) + c;
                slashes = 0;
            }
        }
        return out + repeat('\\', slashes * 2) + '"';
    }

    var parts = [], i;
    for (i = 0; i < args.length; i++) { parts.push(quote(args.Item(i))); }

    var code;
    try {
        // 0 = hidden window; true = wait for the child and return its exit code.
        code = new ActiveXObject("WScript.Shell").Run(parts.join(" "), 0, true);
    } catch (e) {
        WScript.Quit(127);
    }
    WScript.Quit(code);
}());

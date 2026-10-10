"""Deterministic path extraction from Bash and PowerShell command strings.

``parse_command(text)`` splits a command into segments (``;``, ``|``, ``&&``,
``||``, ``&``, newlines, outside quotes and parentheses), tokenizes each one,
and classifies it by its verb:

  read     cat, head, sed, Get-Content, ...   positional arguments are paths
  search   grep, rg, Select-String, find, ... first positional is the query
  git      show/diff/log/blame read paths; add/rm/mv/checkout write them
  write    cp, mv, rm, Set-Content, ...       positional arguments are paths
  exec     python, node, pwsh, a script path  path-like arguments are paths
  nav      cd, Set-Location                   the argument is a directory
  assign   ``$x = 'literal'``                 a named string, not a touch
  nonfile  echo, gh, Select-Object, ...        no file roles
  comment  a ``#`` line

Command substitutions (``$(...)``, a leading ``(...)``) are parsed as commands
of their own. Heredoc and here-string bodies are dropped from segmentation;
quoted literals in them that look like paths are reported with role ``script``.
Redirect targets are reported with role ``write`` (``>``) or ``read`` (``<``).

A segment with a path-like token and an unknown verb, or a segment that fails
to tokenize, is a *miss*: its paths cannot be attributed to a role. Misses are
returned, never guessed at, so the caller can publish the miss rate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

READ = {
    "cat", "type", "get-content", "gc", "head", "tail", "less", "more", "wc", "sed", "awk",
    "jq", "diff", "cmp", "sha256sum", "md5sum", "sha1sum", "file", "stat", "xxd", "od", "nl",
    "sort", "uniq", "cut", "tr", "bat", "test-path", "get-item", "gi", "get-filehash",
    "import-csv", "resolve-path", "du", "realpath", "readlink", "iconv", "strings", "hexdump",
    "column", "comm", "join", "paste", "fold", "tac", "get-itemproperty",
}
SEARCH = {
    "grep", "egrep", "fgrep", "rg", "select-string", "sls", "findstr", "find", "fd",
    "get-childitem", "gci", "ls", "dir", "tree", "locate",
}
WRITE = {
    "cp", "mv", "rm", "mkdir", "touch", "set-content", "out-file", "add-content", "new-item",
    "remove-item", "copy-item", "move-item", "rename-item", "ni", "rmdir", "tee", "tee-object",
    "ln", "chmod", "rd", "del", "ri", "md", "export-csv", "unzip", "tar", "7z",
    "expand-archive", "compress-archive", "install",
}
EXEC = {
    "python", "python3", "py", "node", "npx", "npm", "pnpm", "yarn", "pytest", "pwsh",
    "powershell", "bash", "sh", "dotnet", "uv", "uvx", "pip", "pip3", "ruff", "mypy", "tsc",
    "cargo", "go", "make", "msbuild", "deno", "bun", "java", "javac", "gradle", "mvn", "cmake",
    "evidence", "claude", "code", "start", "start-process", "invoke-expression", "iex", "cmd",
    "black", "isort", "pyright", "eslint", "prettier", "vitest", "jest", "playwright", "docker",
    "docker-compose", "gitleaks", "semgrep", "invoke-pester", "kubectl", "terraform", "wsl",
    "graphify", "sqlite3", "psql", "timeout", "xargs", "env", "watch", "coverage", "tox", "nox",
    "pre-commit", "hatch", "poetry", "dotnet-format", "alembic", "uvicorn", "flask", "django-admin",
}
NAV = {"cd", "set-location", "sl", "pushd", "popd", "push-location", "pop-location", "chdir"}
NONFILE = {
    "echo", "printf", "write-host", "write-output", "write-error", "write-warning", "true",
    "false", "exit", "sleep", "start-sleep", "date", "get-date", "export", "set", "unset", "gh",
    "curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "select-object",
    "select", "sort-object", "measure-object", "measure", "format-table", "ft", "format-list",
    "fl", "where-object", "where", "foreach-object", "%", "?", "out-null", "out-string",
    "return", "break", "continue", ":", "wait", "read", "local", "declare", "pwd",
    "get-location", "clear", "cls", "which", "get-command", "gcm", "done", "fi", "esac", "}",
    ")", "{", "(", "else", "then", "do", "if", "elif", "for", "foreach", "while", "until",
    "case", "function", "try", "catch", "finally", "param", "trap", "shift", "wait-process",
    "stop-process", "get-process", "ps", "kill", "taskkill", "tasklist", "whoami", "hostname",
    "uname", "id", "setx", "set-variable", "get-variable", "new-object", "add-type",
    "get-member", "gm", "group-object", "compare-object", "convertto-json", "convertfrom-json",
    "convertto-csv", "join-path", "split-path", "set-strictmode", "get-service",
    "get-ciminstance", "get-wmiobject", "get-host", "get-help", "history", "alias", "source",
    "eval", "seq", "yes", "expr", "let", "bc", "printenv", "get-random", "base64", "openssl",
    "ssh", "scp", "netstat", "ping", "nslookup", "ipconfig", "reg", "sc", "net", "winget",
    "choco", "scoop", "brew", "apt", "apt-get", "systemctl", "service", "get-acl", "icacls",
    "attrib", "powercfg", "wmic", "schtasks", "get-scheduledtask", "register-scheduledtask",
    "get-psdrive", "get-volume", "get-disk", "get-counter", "get-eventlog", "get-winevent",
    "get-nettcpconnection", "test-netconnection", "resolve-dnsname", "get-uptime",
    "get-computerinfo", "[", "[[", "test", "read-host", "get-clipboard", "set-clipboard",
    "out-host", "out-default", "write-progress", "write-verbose", "write-debug",
    "write-information", "throw", "get-error", "get-job", "receive-job", "start-job",
    "stop-job", "remove-job", "wait-job", "get-alias", "get-module", "import-module",
    "install-module", "get-executionpolicy", "set-executionpolicy", "get-culture",
    "get-uiculture", "get-timezone", "tzutil", "chcp", "mode", "title", "ver", "vol",
    "systeminfo", "whereis", "nproc", "free", "df", "top", "htop", "uptime", "lsof",
    "basename", "dirname", "new-guid", "get-filehash-", "wait-event", "register-objectevent",
}
PREFIX = {"sudo", "time", "command", "builtin", "&", ".", "!", "nohup", "exec", "then", "do",
          "else", "{", "}", "(", "!"}
SCRIPT_EXT = (".py", ".ps1", ".sh", ".js", ".mjs", ".cjs", ".ts", ".bat", ".cmd", ".exe")
GIT_READ = {"show", "diff", "log", "blame", "cat-file", "ls-files", "ls-tree", "grep",
            "check-ignore", "annotate", "whatchanged", "difftool", "range-diff", "check-attr"}
GIT_WRITE = {"add", "rm", "mv", "checkout", "restore", "apply", "am", "update-index"}
GIT_NONFILE = {
    "status", "commit", "fetch", "push", "pull", "worktree", "branch", "rev-parse", "merge-base",
    "stash", "remote", "config", "switch", "tag", "reset", "cherry-pick", "rebase", "merge",
    "clone", "init", "describe", "reflog", "shortlog", "count-objects", "gc", "fsck", "notes",
    "submodule", "sparse-checkout", "hash-object", "for-each-ref", "symbolic-ref", "update-ref",
    "ls-remote", "name-rev", "rev-list", "cherry", "revert", "bisect", "clean", "prune",
    "maintenance", "version", "help", "lfs", "credential", "var", "verify-commit", "mergetool",
    "format-patch", "request-pull", "send-email", "archive", "bundle", "filter-branch", "replace",
    "rerere", "stage", "check-ref-format", "show-ref", "show-branch", "merge-tree", "patch-id",
    "fetch-pack", "--version", "-v", "",
}

# Single-letter flags that take a value, per verb. Case matters (grep -A vs -a).
SHORT_VALUE = {
    "grep": set("efmABCdD"), "egrep": set("efmABCdD"), "fgrep": set("efmABCdD"),
    "rg": set("efgtTmABCMjdrE"), "head": set("nc"), "tail": set("nc"), "sed": set("ef"),
    "awk": set("fFv"), "cut": set("dfcb"), "sort": set("kto"), "diff": set("UxX"),
    "xargs": set("InLPd"), "git": set("nSGLUCc"), "python": set("mcW"), "python3": set("mcW"),
    "py": set("mcW"), "node": set("er"), "pytest": set("kmpc"), "jq": set(), "wc": set(),
    "tree": set("LPI"), "du": set("d"), "ls": set(), "uniq": set("fs"), "tar": set("fC"),
    "unzip": set("d"), "docker": set("fvewp"), "npm": set(), "npx": set("p"), "bash": set("c"),
    "sh": set("c"), "cp": set("t"), "mv": set("t"), "ln": set("t"), "timeout": set("sk"),
    "curl": set("oHdXuA"), "fd": set("etdE"),
}
LONG_VALUE = {
    "--glob", "--type", "--type-not", "--max-count", "--context", "--after-context",
    "--before-context", "--regexp", "--file", "--max-depth", "--maxdepth", "--include",
    "--exclude", "--exclude-dir", "--format", "--pretty", "--since", "--until", "--author",
    "--grep", "--encoding", "--prefix", "--sort", "--replace", "--max-columns", "--threads",
    "--output", "--config", "--source", "--jq", "--body-file", "--title", "--body", "--base",
    "--head", "--diff-filter", "--date", "--word-diff", "--stat-width", "--abbrev", "--depth",
    "--branch", "--message", "--rootdir", "--cov", "--tb", "--deselect", "--ignore",
}
# PowerShell parameters (lowercased) that take a value; others are switches.
PS_VALUE = {
    "-path", "-literalpath", "-destination", "-filter", "-include", "-exclude", "-pattern",
    "-encoding", "-totalcount", "-tail", "-head", "-first", "-last", "-skip", "-context",
    "-depth", "-value", "-itemtype", "-newname", "-delimiter", "-property", "-expandproperty",
    "-erroraction", "-executionpolicy", "-file", "-command", "-readcount", "-attributes",
    "-argumentlist", "-workingdirectory", "-outfile", "-inputobject", "-message", "-script",
    "-output", "-show", "-configuration", "-childpath", "-parent", "-leaf", "-algorithm",
    "-index", "-unique-", "-warningaction", "-verbosity", "-framework", "-logger",
}
FIND_VALUE = {"-name", "-iname", "-path", "-ipath", "-type", "-regex", "-newer", "-mtime",
              "-size", "-maxdepth", "-mindepth", "-perm", "-user", "-not"}
PATH_FLAGS = {"-path", "-literalpath", "-destination", "-file", "-outfile", "-script", "-f",
              "--file", "-t", "-d", "-childpath"}
QUERY_FLAGS = {"-e", "--regexp", "-pattern", "-filter", "-include", "-name", "-iname", "-regex",
               "--grep", "-S", "-G"}

PATHLIKE_RE = re.compile(
    r"^(?:[a-zA-Z]:)?[\w.@~$(){}+,=-]*(?:/[\w.@~$(){}+,=*?\[\]-]+)+/?$"  # contains a slash
    r"|^[\w@~+,=-][\w.@~+,=-]*\.[A-Za-z][A-Za-z0-9]{0,7}$"             # bare name.ext
)
NOT_PATH_RE = re.compile(r"^(?:-|\$\(|@|\d+(?:\.\d+)*$)|://")
EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
DOMAIN_TLDS = {"com", "org", "net", "io", "dev", "ai", "app", "co", "gov", "edu"}
HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
HERESTRING_OPEN_RE = re.compile(r"@(['\"])\s*$")
LITERAL_RE = re.compile(r"(['\"])([^'\"\n]{2,200})\1")
REDIRECT_RE = re.compile(r"^(\d|\*)?(>>?|<)(&\d)?(.*)$")
SUB = "__SUB__"
BODY = "__BODY__"
BLOCK = "__BLOCK__"
# ``$name = rhs`` (also ``+=`` etc. and the glued ``$name='x'`` form); ``==`` is not PowerShell.
ASSIGN_RE = re.compile(r"^\s*\$[A-Za-z_][\w:.]*\s*[+*/-]?=(?!=)\s*(.*)$", re.S)
# A right-hand side that is a value or expression rather than a command.
VALUE_START_RE = re.compile(r"^(?:['\"$@\[(\d-]|__SUB__|__BLOCK__|\{|true\b|false\b|null\b)", re.I)
# A PowerShell loop header ``$item in <expr>`` (the inside of ``foreach (...)``).
FOREACH_HEADER_RE = re.compile(r"^\s*\$[A-Za-z_]\w*\s+in\s+(.*)$", re.I | re.S)
# Control keywords whose header or body was replaced by SUB/BLOCK with no space between.
CONTROL = {"foreach", "for", "while", "if", "elseif", "switch", "catch", "until"}


@dataclass
class PathUse:
    path: str
    role: str  # read | search | write | exec | nav | script
    verb: str


@dataclass
class Segment:
    text: str
    verb: str | None
    kind: str
    paths: list[PathUse] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    miss_reason: str | None = None
    has_pathlike: bool = False


def is_pathlike(tok: str) -> bool:
    if not tok or NOT_PATH_RE.search(tok) or SUB in tok or BODY in tok:
        return False
    if tok.startswith("$") and "/" not in tok:
        return False
    if not PATHLIKE_RE.match(tok):
        return False
    return "/" in tok or tok.rsplit(".", 1)[-1].lower() not in DOMAIN_TLDS


REV_RANGE_RE = re.compile(r"[^/]\.\.\.?[^/]|[^/]\.\.$")


def is_filelike(path: str) -> bool:
    """A concrete file name: has an extension; no glob, list, variable or git range syntax."""
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return (bool(EXT_RE.search(last)) and not any(ch in path for ch in "*?{}$,")
            and not any(ch in last for ch in "[]")
            and not REV_RANGE_RE.search(path))


def strip_bodies(text: str) -> tuple[str, list[str]]:
    """Drop heredoc and here-string bodies; return (command text, body lines)."""
    out: list[str] = []
    bodies: list[str] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = HEREDOC_RE.search(line)
        h = HERESTRING_OPEN_RE.search(line)
        if m:
            out.append(line[:m.start()] + BODY + line[m.end():])
            i += 1
            while i < len(lines) and lines[i].strip() != m.group(2):
                bodies.append(lines[i])
                i += 1
        elif h:
            close = h.group(1) + "@"
            head = line[:h.start()] + BODY
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(close):
                bodies.append(lines[i])
                i += 1
            out.append(head + (lines[i].lstrip()[2:] if i < len(lines) else ""))
        else:
            out.append(line)
        i += 1
    return "\n".join(out), bodies


def _scan(text: str):
    """Yield (index, char, in_quote) with escapes consumed; the final state is the quote left open."""
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
                yield i, ch, True
            elif ch in "\\`" and quote == '"' and i + 1 < n:
                yield i, ch, True
                i += 1
                yield i, text[i], True
            else:
                yield i, ch, True
        elif ch in "'\"":
            quote = ch
            yield i, ch, True
        elif ch in "\\`" and i + 1 < n and text[i + 1] not in "\n":
            yield i, ch, False
            i += 1
            yield i, text[i], True
        else:
            yield i, ch, False
        i += 1
    yield n, "", quote is not None


def extract_subs(text: str) -> tuple[str, list[str]]:
    """Replace ``$(...)``, ``@(...)`` and parenthesized groups with SUB; return the inner commands."""
    out: list[str] = []
    inner: list[str] = []
    depth = 0
    start = 0
    prev = ""
    for i, ch, quoted in _scan(text):
        if not ch:
            break
        if not quoted and ch == "(":
            if depth == 0:
                start = i + 1
                if prev and prev in "$@" and out:
                    out.pop()
            depth += 1
        elif not quoted and ch == ")" and depth:
            depth -= 1
            if depth == 0:
                inner.append(text[start:i])
                out.append(SUB)
        elif depth == 0:
            out.append(ch)
        prev = ch
    if depth:  # unbalanced: keep the text as is
        return text, []
    return "".join(out), inner


def extract_blocks(text: str) -> tuple[str, list[str], bool]:
    """Replace unquoted ``{ ... }`` script blocks with BLOCK; return their inner commands.

    Only a brace that opens a token is a block: ``${var}``, ``@{...}`` hashtables, brace
    expansion (``f{1,2}``) and ``find -exec {}`` are left as text. Unbalanced braces leave
    the text unchanged and report ``balanced=False`` so the caller records a miss."""
    out: list[str] = []
    inner: list[str] = []
    depth = 0
    start = 0
    for i, ch, quoted in _scan(text):
        if not ch:
            break
        if quoted:
            if depth == 0:
                out.append(ch)
            continue
        if ch == "{":
            if depth:
                depth += 1
                continue
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if (not prev or prev.isspace() or prev in ");|&_") and nxt != "}":
                depth = 1
                start = i + 1
                continue
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                inner.append(text[start:i])
                out.append(f" {BLOCK} ")
            continue
        if depth == 0:
            out.append(ch)
    if depth:
        return text, [], False
    return "".join(out), inner, True


def split_segments(text: str) -> tuple[list[str], bool]:
    """Split on unquoted separators. Returns (segments, quotes balanced)."""
    segs: list[str] = []
    buf: list[str] = []
    balanced = True
    events = list(_scan(text))
    k = 0
    while k < len(events):
        i, ch, quoted = events[k]
        if not ch:
            balanced = not quoted
            break
        nxt = events[k + 1][1] if k + 1 < len(events) else ""
        if not quoted and ch == "\\" and nxt == "\n":
            buf.append(" ")
            k += 2
            continue
        if not quoted and (ch in ";\n|&"):
            prev = text[i - 1] if i else ""
            if ch == "&" and (prev in "<>" or nxt == ">"):
                buf.append(ch)
            elif ch == "&" and not "".join(buf).strip() and nxt != "&":
                buf.append(ch)  # PowerShell call operator
            else:
                segs.append("".join(buf))
                buf = []
                if nxt == ch and ch in "|&":
                    k += 1
        else:
            buf.append(ch)
        k += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()], balanced


def tokenize(seg: str) -> list[str] | None:
    """Whitespace tokens with quotes removed and backslashes as '/'; None if unbalanced."""
    toks: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    started = False
    for ch in seg:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
            continue
        if ch in "'\"":
            quote = ch
            started = True
        elif ch.isspace():
            if started or buf:
                toks.append("".join(buf))
            buf, started = [], False
        else:
            buf.append(ch)
    if quote:
        return None
    if started or buf:
        toks.append("".join(buf))
    return [t.replace("\\", "/") for t in toks]


def _verb_name(tok: str) -> str:
    base = tok.rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def flags_and_positionals(verb: str, args: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """(positional args, [(flag, value)]); value-taking flags consume the next token."""
    pos: list[str] = []
    flagged: list[tuple[str, str]] = []
    short = SHORT_VALUE.get(verb, set())
    i = 0
    after_dd = False
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if after_dd or not a.startswith("-") or len(a) == 1 or a[1:2].isdigit():
            pos.append(a)
        elif a == "--":
            after_dd = True
        elif a.startswith("--"):
            flag, eq, inline = a.partition("=")
            if eq:
                flagged.append((flag, inline))
            elif flag in LONG_VALUE and nxt is not None:
                flagged.append((flag, nxt))
                i += 1
            else:
                flagged.append((flag, ""))
        elif len(a) == 2:  # -x
            if a[1] in short and nxt is not None:
                flagged.append((a, nxt))
                i += 1
            else:
                flagged.append((a, ""))
        else:  # -word: PowerShell parameter, find predicate, or bundled short flags
            low = a.lower().rstrip(":")
            if verb == "find" and low in FIND_VALUE and nxt is not None:
                flagged.append((low, nxt))
                i += 1
            elif low in PS_VALUE and nxt is not None:
                flagged.append((low, nxt))
                i += 1
            elif short and a[1:].isalpha() and a[-1] in short and nxt is not None:
                flagged.append(("-" + a[-1], nxt))  # grep -rn PATTERN: last letter takes the value
                i += 1
            else:
                flagged.append((low, ""))
        i += 1
    return pos, flagged


FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
FIND_OUTFILE = {"-fprint", "-fprint0", "-fprintf", "-fls"}  # create or truncate their argument
FIND_EXEC_END = {";", "\\;", "+"}


def _split_find_exec(args: list[str]) -> tuple[list[str], list[list[str]], list[str], bool]:
    """``find`` arguments without its write actions: each ``-exec``-style action's own
    command (``{}`` dropped), the files ``-fprint``-style actions write, and whether
    ``-delete`` removes what was found. None of these are searched paths."""
    outer: list[str] = []
    nested: list[list[str]] = []
    outfiles: list[str] = []
    delete = False
    i = 0
    while i < len(args):
        low = args[i].lower()
        if low == "-delete":
            delete = True
        elif low in FIND_OUTFILE and i + 1 < len(args):
            outfiles.append(args[i + 1])
            i += 1
            if low == "-fprintf":
                i += 1  # the format
        elif low in FIND_EXEC:
            cmd: list[str] = []
            i += 1
            while i < len(args) and args[i] not in FIND_EXEC_END:
                if args[i] != "{}":
                    cmd.append(args[i])
                i += 1
            if cmd:
                nested.append(cmd)
        else:
            outer.append(args[i])
        i += 1
    return outer, nested, outfiles, delete


def _requote(tok: str) -> str:
    return "'" + tok.replace("'", "'\\''") + "'" if re.search(r"[\s'\"]", tok) else tok


def _sed_in_place(flagged: list[tuple[str, str]]) -> bool:
    """``-i``, ``-i.bak``, ``--in-place[=SUF]`` or a bundle such as ``-ni``."""
    return any(fl == "--in-place" or (fl.startswith("-") and not fl.startswith("--")
                                      and fl[1:2].isalpha() and "i" in fl[1:].split(".", 1)[0])
               for fl, _ in flagged)


def _clean_arg(a: str) -> str | None:
    a = a.strip().rstrip(",;")
    if not a or a in {"-", ".", "..", "/", "~", "*"} or SUB in a or BODY in a:
        return None
    if BLOCK in a:
        return None
    if a.startswith("$") and "/" not in a:
        return None
    if a.startswith(("{", "[", "@")) or "://" in a:
        return None
    return a


def parse_segment(seg: str) -> Segment:
    if seg.lstrip().startswith("#"):
        return Segment(seg, None, "comment")
    m = ASSIGN_RE.match(seg)
    if m:
        rhs = m.group(1).strip()
        if not rhs or VALUE_START_RE.match(rhs):
            # a value or expression: a named string, not a touch (substitutions parse separately)
            return Segment(seg, None, "assign", has_pathlike=any(is_pathlike(t) for t in (tokenize(rhs) or [])))
        inner = parse_segment(rhs)  # ``$x = <command>``: the command's own roles apply
        inner.text = seg
        return inner
    h = FOREACH_HEADER_RE.match(seg)
    if h:
        inner = parse_segment(h.group(1))
        inner.text = seg
        return inner
    toks = tokenize(seg)
    if toks is None:
        return Segment(seg, None, "miss", miss_reason="unbalanced_quote",
                       has_pathlike=any(is_pathlike(t) for t in seg.split()))
    words: list[str] = []
    redirects: list[PathUse] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        m = REDIRECT_RE.match(t)
        if m and not t.startswith("->"):
            target = m.group(4)
            if not m.group(3) and not target and i + 1 < len(toks):
                target = toks[i + 1]
                i += 1
            c = _clean_arg(target) if target and not m.group(3) else None
            if c and c.lower() not in {"/dev/null", "nul", "$null"}:
                redirects.append(PathUse(c, "read" if m.group(2) == "<" else "write", ">"))
        else:
            words.append(t)
        i += 1
    # PowerShell assignment: $x = <command or value>
    assigned = False
    if len(words) >= 2 and words[0].startswith("$") and words[1] in {"=", "+=", "-="}:
        words = words[2:]
        assigned = True
    while words and (words[0].lower() in PREFIX or re.match(r"^[A-Za-z_]\w*=", words[0])):
        if re.match(r"^[A-Za-z_]\w*=", words[0]):
            assigned = True
        words = words[1:]
    if not words:
        return Segment(seg, None, "empty", paths=redirects, has_pathlike=bool(redirects))
    verb = _verb_name(words[0])
    glued = re.sub(r"(?:__sub__|__block__)+$", "", verb)
    if glued != verb and glued in CONTROL:
        verb = glued
    args = words[1:]
    nested: list[list[str]] = []
    find_outfiles: list[str] = []
    find_delete = False
    if verb == "find":
        args, nested, find_outfiles, find_delete = _split_find_exec(args)
    has_path = bool(redirects) or any(is_pathlike(a) for a in words)
    s = Segment(seg, verb, "nonfile", has_pathlike=has_path)
    s.paths.extend(redirects)
    if assigned and (len(words) == 1 or words[0].startswith(("'", '"', SUB))) and verb not in EXEC | READ | SEARCH:
        s.kind = "assign"
        return s
    pos, flagged = flags_and_positionals(verb, args)

    def add(paths: list[str], role: str) -> None:
        for p in paths:
            c = _clean_arg(p)
            if c:
                s.paths.append(PathUse(c, role, verb))

    if verb.startswith((SUB.lower(), BODY.lower(), BLOCK.lower())):
        s.kind = "nonfile"
        return s
    if verb in READ:
        role = "write" if verb == "sed" and _sed_in_place(flagged) else "read"
        s.kind = role
        for fl, val in flagged:
            if fl in PATH_FLAGS and val:
                add([val], "read")
        if verb in {"sed", "awk"} and not any(fl in {"-e", "-f"} for fl, _ in flagged) and pos:
            pos = pos[1:]  # the script
        if verb in {"cut", "tr", "sort", "uniq", "column", "join", "paste", "comm", "fold", "jq"}:
            pos = [p for p in pos if is_pathlike(p)]
        add(pos, role)
    elif verb in SEARCH:
        s.kind = "search"
        s.queries.extend(v for fl, v in flagged if fl in QUERY_FLAGS and v)
        for fl, val in flagged:
            if fl in {"-path", "-literalpath"} and val and verb != "find":
                add([val], "search")
        if verb in {"grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls"}:
            if not any(fl in {"-e", "--regexp", "-pattern", "-f", "--file"} for fl, _ in flagged) and pos:
                s.queries.append(pos[0])
                pos = pos[1:]
        add(pos, "search")
    elif verb == "git":
        s.kind = "git"
        j = 0
        while j < len(args) and args[j].startswith("-"):
            j += 2 if args[j] in {"-C", "-c", "--git-dir", "--work-tree"} else 1
        sub = args[j].lower() if j < len(args) else ""
        rest = args[j + 1:]
        role = "read" if sub in GIT_READ else "write" if sub in GIT_WRITE else None
        if role is None:
            if sub in GIT_NONFILE:
                return s
            s.kind = "miss"
            s.miss_reason = f"git {sub}"
            return s
        rpos, rflag = flags_and_positionals("git", rest)
        s.queries.extend(v for fl, v in rflag if fl in QUERY_FLAGS and v)
        after_dd = rest[rest.index("--") + 1:] if "--" in rest else []
        paths: list[str] = []
        for n, a in enumerate(rpos):
            if n >= len(rpos) - len(after_dd):
                paths.append(a)
            elif ":" in a and not re.match(r"^[a-zA-Z]:/", a):
                p = a.partition(":")[2]
                if p:
                    paths.append(p)
            elif sub == "grep" and n == 0 and not any(fl == "-e" for fl, _ in rflag):
                s.queries.append(a)
            elif (is_pathlike(a) and (is_filelike(a) or role == "write")
                  and not re.match(r"^[0-9a-f]{7,40}$", a)):
                paths.append(a)
        add(paths, role)
    elif verb in WRITE:
        s.kind = "write"
        for fl, val in flagged:
            if fl in PATH_FLAGS and val:
                add([val], "write")
        add(pos, "write")
    elif verb in EXEC or verb.endswith(SCRIPT_EXT) or verb.startswith("$"):
        s.kind = "exec"
        if verb.endswith(SCRIPT_EXT):
            add([words[0]], "exec")
        for fl, val in flagged:
            if fl in PATH_FLAGS and val:
                add([val], "exec")
            elif fl in {"-c", "-e", "-command"} and val:
                add([lit for _, lit in LITERAL_RE.findall(val) if is_pathlike(lit)], "script")
        add([a for a in pos if is_pathlike(a)], "exec")
    elif verb in NAV:
        s.kind = "nav"
        add(pos[:1], "nav")
    elif verb in NONFILE:
        s.kind = "nonfile"
    elif s.has_pathlike:
        s.kind = "miss"
        s.miss_reason = f"unknown verb {verb}"
    roots = [u for u in s.paths if u.role == "search"]
    for cmd in nested:  # find -exec: the action's own roles (a nested rm is a write)
        inner = parse_segment(" ".join(_requote(c) for c in cmd))
        s.paths.extend(inner.paths)
        if inner.kind in {"read", "write", "exec"}:  # the action runs on what find found under its roots
            s.paths.extend(PathUse(u.path, inner.kind, inner.verb or verb) for u in roots)
            s.kind = inner.kind
        if inner.kind == "miss" and s.kind != "miss":
            s.kind, s.miss_reason = "miss", f"find action: {inner.miss_reason}"
    if find_delete or find_outfiles:
        if find_delete:  # -delete removes what was found under the roots
            s.paths.extend(PathUse(u.path, "write", verb) for u in roots)
        add(find_outfiles, "write")
        if s.kind != "miss":
            s.kind = "write"
    return s


def parse_command(text: str) -> tuple[list[Segment], list[PathUse]]:
    """Segments of a shell command (substitutions included), plus heredoc/here-string literals."""
    body_free, bodies = strip_bodies(text)
    out: list[Segment] = []
    queue = [body_free]
    while queue:
        chunk = queue.pop(0)
        flat, inner = extract_subs(chunk)
        queue.extend(inner)
        flat, blocks, blocks_ok = extract_blocks(flat)
        queue.extend(blocks)
        if not blocks_ok:
            out.append(Segment(flat, None, "miss", miss_reason="unbalanced_block", has_pathlike=True))
        segs, balanced = split_segments(flat)
        parsed = [parse_segment(s) for s in segs]
        if not balanced and parsed and parsed[-1].kind != "miss":
            last = parsed[-1]
            parsed[-1] = Segment(last.text, last.verb, "miss", last.paths, last.queries,
                                 "unbalanced_command", True)
        out.extend(parsed)
    script = [PathUse(lit, "script", "heredoc") for line in bodies
              for _, lit in LITERAL_RE.findall(line) if is_pathlike(lit)]
    return out, script

"""Weekly graphify adoption funnel: availability -> routing -> execution -> measurement.

A query log that works proves nothing about adoption: "zero logged queries" can mean no
demand, no graph found, the skill never selected, or exploration sent to a subagent that
never heard of graphify. This joins Claude Code session transcripts to the graphify query
log so each boundary gets its own verdict (PASS / FAIL / INSUFFICIENT EVIDENCE) with counts
and the specific units behind a failure.

Stages
  1. Availability  sessions by starting repo; graph resolved (cwd walk, repo subfolders,
                   worktree -> main checkout) and, once the hint hook is live, whether the
                   <graphify-graph> hint was actually injected.
  2. Routing       graph-backed exploration units (main threads and subagents) and whether
                   each made a graphify call. Eligibility is a HEURISTIC over tool counts and
                   is labelled as such; a graph-backed exploration unit with no graphify call
                   is a routing FAIL to investigate, never a reason to wait for volume.
  3. Execution     graphify calls found in transcripts vs completed calls vs matched
                   query-log records.
  4. Measurement   organic records only (matched to organic transcript calls): stock vs
                   reranked counts, latency, zero-node results; sample thresholds apply here
                   and only here.

Organic vs test: sessions listed in the config's funnel.exclude_sessions (smoke/acceptance
runs), sessions whose cwd sits in the system temp dir, and log records that match no
organic transcript call are all kept out of stages 2-4; unmatched records are reported as
"unattributed".

Config (graphify.local.json beside this script, or --config), validated before any analysis:
  "skill_scripts": folder holding the skill's resolve_graph.py        (required)
  "query_log":     query log path (default: env GRAPHIFY_QUERY_LOG)
  "funnel": {"hint_live_since": "YYYY-MM-DDT00:00:00Z", "exclude_temp_cwd": true,
             "exclude_sessions": {"<session-id>": "<reason>"} or ["<session-id>", ...]}
  "targets": [{"scan": ..., "repo": ...}]   repos that are expected to have a graph
Relative config paths resolve against the config file's folder, as in GraphifyConfig.ps1.

Input problems (missing/unreadable/corrupt query log, unreadable or undated transcripts,
missing subagent metadata) are counted in the JSON "inputs" block and listed in the report;
they are never read as low volume.

Run: python graphify_funnel.py [--days 7] [--out report.md] [--json summary.json]
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import math
import os
import re
import shlex
import statistics
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

PASS, FAIL, INSUFFICIENT = "PASS", "FAIL", "INSUFFICIENT EVIDENCE"

# A call is an INVOCATION, not a mention: reading, grepping, diffing, editing or committing
# the wrapper's source names query_reranked.py too, so the wrapper only counts when a Python
# interpreter in command position runs it as its script (not `-m <module>` or `-c <code>`
# with the path as an argument), and bare graphify only counts in command position (start
# of the command, after a shell separator, or as a path ending in graphify[.exe]).
# Detection runs on code_part(command): heredoc/here-string bodies and multi-line quoted
# strings (commit messages, prose) are removed first, so their line starts are not commands,
# and separators inside single-line quoted strings (grep patterns like "a\|b") are blanked.
# Path components must end in / or \, which keeps test_query_reranked.py out; a quoted path
# may contain spaces.
_CMD_POS = r"(?:^|[;&|(\n]|\$\()\s*(?:[A-Za-z_]\w*=\S*\s+)*"   # separator, VAR=value prefixes
_ARG = r"(?:\"[^\"\n]*[\\/]|'[^'\n]*[\\/]|[\"']?[^\s\"';|&]*[\\/]|[\"'])?"
WRAPPER_RE = re.compile(
    _CMD_POS + _ARG + r"(?:python[\d.]*|py)(?:\.exe)?[\"']?"      # interpreter
    r"(?:\s+(?!-[mc])-\S+(?:\s+[A-Za-z_]\w*(?:=\S*)?)?)*\s+"       # options, e.g. -3, -X utf8
    + _ARG + r"query_reranked\.py",
    re.I | re.M)
BARE_CLI_RE = re.compile(
    _CMD_POS + _ARG + r"graphify(?:\.exe)?[\"']?\s+(?:query|path|explain)\b",
    re.I | re.M)
GRAPHIFY_CALL_RE = re.compile(f"{WRAPPER_RE.pattern}|{BARE_CLI_RE.pattern}", re.I | re.M)
_HEREDOC_RE = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n.*?^[ \t]*\2[ \t]*$", re.S | re.M)
_HERESTRING_RE = re.compile(r"@(['\"])[ \t]*\n.*?\n\1@", re.S)
# The same heredoc, with the rest of its opening line kept: `cat <<'EOF' | grep -c x` runs that grep.
_HEREDOC_TAIL_RE = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1(?P<tail>[^\n]*)\n.*?^[ \t]*\2[ \t]*$", re.S | re.M)


_QUOTED_SEPARATORS = str.maketrans(";&|()", "     ")
# The end of a line that passes the next quoted argument to a shell as its script. The words between
# the shell and its -c cannot hold a command separator, so `bash build.sh; python -c "..."` is not bash's
# script (the -c belongs to python).
_SHELL_SCRIPT_ARG_RE = re.compile(
    r"(?:^|[\s;&|(/\\])(?:(?P<posix>bash|sh|zsh|dash)(?:\.exe)?[\"']?(?:\s+[^\s\"';&|]+)*?\s+-\w*c\w*"
    r"|(?P<ps>pwsh|powershell)(?:\.exe)?[\"']?(?:\s+[^\s\"';&|]+)*?\s+-c(?:ommand)?"
    r"|(?P<cmd>cmd)(?:\.exe)?[\"']?\s+/[ck])\s*$", re.I)
_SHELLS = {"bash": "Bash", "sh": "Bash", "zsh": "Bash", "dash": "Bash", "cmd": "Bash",
           "pwsh": "PowerShell", "powershell": "PowerShell"}


def _blank_quoted_code(command: str, escape: str = "\\",
                       scripts: list[tuple[str, str]] | None = None) -> str:
    """Replace each quoted string that spans a newline with "", and blank shell separators
    (;&|()) inside single-line quoted strings, so a search pattern such as a\\|graphify query
    in quotes gives `graphify` no command position. A double-quoted string containing $( keeps
    all its separators, since that substitution runs a real command (so a pattern string that
    also contains $( can still read as a call). A quoted
    `bash -c`, `pwsh -Command` or `cmd /c` argument, on one line or several, is a script the shell
    runs: it is handed to `scripts` (when given) so it can be read as commands, while the string
    itself stays an argument here.

    A regex would pair one line's closing quote with the next line's opening quote and blank
    the real command between them, so quotes are paired by walking the string: `escape`
    (backslash for a POSIX shell, backtick for PowerShell) escapes the next character inside
    "..." and outside quotes, and a `#` comment runs to end of line and is blanked, so an
    apostrophe in it opens nothing and a command named in it is no invocation or lookup.
    An unclosed quote leaves the rest as is.
    """
    out: list[str] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "#" and (i == 0 or command[i - 1] in " \t\n;"):
            end = command.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif ch == escape and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
        elif ch in "\"'":
            j = i + 1
            while j < n and command[j] != ch:
                j += 2 if ch == '"' and command[j] == escape else 1
            if j >= n:
                out.append(command[i:])
                break
            span = command[i:j + 1]
            if scripts is not None:
                head = "".join(out)
                m = _SHELL_SCRIPT_ARG_RE.search(head[head.rfind("\n") + 1:])
                if m:
                    scripts.append((span[1:-1], "PowerShell" if m.group("ps") else "Bash"))
            if "\n" in span:
                span = '""'
            elif not (ch == '"' and "$(" in span):
                span = span.translate(_QUOTED_SEPARATORS)
            out.append(span)
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def code_part(command: str, tool: str = "Bash", keep_heredoc_tail: bool = False) -> str:
    """The command with heredoc/here-string bodies and multi-line quoted strings blanked, and
    shell separators inside single-line quoted strings neutralised. The rest of a heredoc's opening
    line goes with the body unless `keep_heredoc_tail` (the search count keeps it: `| grep x` runs)."""
    if keep_heredoc_tail:
        command = _HEREDOC_TAIL_RE.sub(lambda m: "<<HEREDOC" + m.group("tail"), command)
    else:
        command = _HEREDOC_RE.sub("<<HEREDOC", command)
    command = _HERESTRING_RE.sub("@''@", command)
    return _blank_quoted_code(command, "`" if tool == "PowerShell" else "\\")


def _shell_named_in(segment: str) -> str | None:
    """The tool syntax of the first shell named by any word of a command segment, so a shell
    run through a prefix (`sudo bash`, `FOO=1 bash`, `ssh host bash`) is found too."""
    for word in segment.split():
        name = re.split(r"[\\/]", word.strip("\"'"))[-1].lower().removesuffix(".exe")
        if name in _SHELLS:
            return _SHELLS[name]
    return None


def shell_script_parts(command: str, tool: str = "Bash") -> list[str]:
    """The code_part of each script the command hands to a shell, which code_part blanks: a
    heredoc whose command names a shell (`bash <<'EOF'`, `sudo bash <<'EOF'`) or whose
    opening line pipes it to one (`cat <<'EOF' | bash`), and a multi-line quoted argument of
    `bash -c`, `pwsh -Command` or `cmd /c`. A heredoc fed to anything else (`cat > f`, a
    commit message) is data and is not returned. Nested scripts are included."""
    scripts: list[tuple[str, str]] = []

    def heredoc(m: re.Match) -> str:
        line = m.string[:m.start()]
        body = m.group(0)
        shell = _shell_named_in(re.split(r"[;&|(]", line[line.rfind("\n") + 1:])[-1])
        for piped in body[:body.find("\n")].split("|")[1:]:
            shell = shell or _shell_named_in(re.split(r"[;&(]", piped)[0])
        if shell:
            scripts.append((body[body.find("\n") + 1:body.rfind("\n")], shell))
        return "<<HEREDOC"

    command = _HEREDOC_RE.sub(heredoc, command)
    command = _HERESTRING_RE.sub("@''@", command)
    _blank_quoted_code(command, "`" if tool == "PowerShell" else "\\", scripts)
    parts: list[str] = []
    for body, shell in scripts:
        parts += [code_part(body, shell), *shell_script_parts(body, shell)]
    return parts


ALT_LOG_RE = re.compile(r"GRAPHIFY_QUERY_LOG\W{0,3}[=:]")   # redirected log = a test run, not organic use
QUESTION_RE = re.compile(r"(?:query_reranked\.py[\"']?|\s(?:query|explain))\s+\"([^\"]+)\"")
# A shell search is a search tool in COMMAND position, not a word of an argument (`echo "use grep here"`,
# `Write-Host 'use Select-String'`, a commit message). Command positions: the start of a segment (after
# ; & | ( ` or a newline), after `if`/`then`/`do`/`{` and the like, after a VAR=value prefix or an
# assignment (`$r = `), after a wrapper (sudo, env, xargs, timeout, time, nohup, watch, taskset, numactl,
# flock, nsenter, gdb --args, ...) and its
# options, behind `git`'s global options, and anywhere after a runner that takes a whole command line
# (ssh, docker, kubectl, wsl, adb, su, eval, chroot, ...), which this cannot parse. (`find` is a search
# command of its own, see SHELL_SEARCH_CMDS, so its -exec needs no rule.)
SHELL_SEARCH_TOOLS = {"grep", "rg", "findstr", "select-string"}
_SEARCH_KEYWORDS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{"}
_SEARCH_WRAPPER_VALUE_FLAGS = {          # wrapper -> its options that take a separate value
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-R", "-T"},
    "doas": {"-u", "-C"}, "env": {"-u", "-C", "-S"},
    "xargs": {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a"},
    "timeout": {"-s", "-k"}, "nice": {"-n"}, "ionice": {"-c", "-n", "-p", "-P", "-u"},
    "stdbuf": {"-i", "-o", "-e"}, "nohup": set(), "command": set(), "exec": {"-a"}, "builtin": set(),
    "time": {"-f", "-o"}, "watch": {"-n"}, "setsid": set(), "unbuffer": set(), "busybox": set(),
    "parallel": {"-j", "-n", "-a", "-N"}, "strace": {"-o", "-e", "-p", "-s", "-u"},
    "taskset": {"-c", "--cpu-list"}, "chronic": set(), "caffeinate": {"-t", "-w"}, "valgrind": set(),
    "numactl": {"-N", "-m", "-C", "-i", "-p", "--cpunodebind", "--membind", "--physcpubind", "--interleave",
                "--preferred"},
    "flock": {"-w", "-E", "--timeout", "--wait", "--conflict-exit-code"},
    "ltrace": {"-o", "-e", "-p", "-s", "-u", "-n", "-l", "-a", "-A", "-F", "-x", "-w"},
    "nsenter": {"-t", "--target", "-S", "--setuid", "-G", "--setgid"},
    "gdb": {"-ex", "-iex", "-x", "-ix", "-d", "-cd", "-p", "-pid", "-c", "-core", "-s", "-e", "-se", "-tty",
            "-b", "-l", "-D", "-directory"}}      # gdb: the program follows --args
_SEARCH_RUNNERS = {"ssh", "wsl", "docker", "podman", "kubectl", "adb", "su", "script", "eval", "chroot"}
_GIT_VALUE_FLAGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
WRAPPER_FAILURE_RE = re.compile(r"graphify query failed|Traceback \(most recent call last\)")

SHELL_TOOLS = {"Bash", "PowerShell"}
SEARCH_TOOLS = {"Grep", "Glob"}
AGENT_TOOLS = {"Agent", "Task"}
EXPLORE_AGENT_TYPES = {"Explore", "general-purpose", "Plan"}   # spawn statistics only
# Built-in subagent types without a shell; custom ones are judged by their definition.
NO_SHELL_AGENTS = {"claude-code-guide", "statusline-setup"}

# Shell commands read from a Bash/PowerShell call, per command segment (lowercased).
SHELL_CD_CMDS = {"cd", "pushd", "chdir", "set-location", "sl", "push-location"}
SHELL_READ_CMDS = {"cat", "head", "tail", "sed", "less", "more", "nl", "get-content", "gc", "type",
                   "git show", "git blame"}
SHELL_SEARCH_CMDS = {"grep", "egrep", "fgrep", "rg", "findstr", "select-string", "sls", "find", "fd",
                     "get-childitem", "gci", "git grep", "git ls-files"}
# The first positional argument of these is a pattern or script, not a path.
SHELL_PATTERN_FIRST = {"grep", "egrep", "fgrep", "rg", "findstr", "sed", "git grep"}
SHELL_PATTERN_OPTS = {"-e", "--regexp", "-f", "--file", "-pattern"}
# Options whose value is not a path. Short options are case-sensitive (grep -A vs -a);
# longer ones (PowerShell, find) are compared lowercased.
SHELL_VALUE_OPTS = {"-e", "--regexp", "-g", "--glob", "--include", "--exclude", "--exclude-dir",
                    "-t", "--type", "-A", "-B", "-C", "-m", "--max-count", "-pattern", "-filter",
                    "-include", "-exclude", "-name", "-iname", "-encoding"}
_SEGMENT_RE = re.compile(r"(&&|\|\||[;|&\n()])")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PATHLIKE_RE = re.compile(r"^[\w.~\-\\/: ]+$")
_GLOB_CHARS = "*?[{"

# Routing heuristic thresholds (tool counts are a proxy for "cross-file exploration").
SHELL_TOOLS = ("Bash", "PowerShell")
AGENT_DEF_MAX_BYTES = 256 << 10
_LIST_SPLIT_RE = re.compile(r",(?![^()]*\))")  # commas outside Bash(...) patterns
SUBAGENT_MIN_LOOKUPS = 3      # searches + reads for a subagent of any type
MAIN_MIN_SEARCHES = 6         # searches for a main thread
MAIN_MIN_FILES = 3            # distinct files read by that main thread
MATCH_SLACK = timedelta(seconds=5)
BYPASS_SHOWN = 25             # markdown cap; the JSON summary keeps every unit

# Measurement thresholds agreed when the reranker shipped.
MIN_RERANKED = 10
LATENCY_MEDIAN_MS = 2000.0
LATENCY_P90_MS = 3000.0


# --------------------------------------------------------------------------- model

@dataclass
class Call:
    unit: "Unit"
    tool_use_id: str
    ts: datetime | None
    command: str
    question: str | None
    via_wrapper: bool
    alt_log: bool = False
    result_ts: datetime | None = None
    is_error: bool | None = None
    output: str = ""
    record: dict | None = None
    origin_session: str | None = None   # the transcript record's sessionId (kept on copies)
    copy: bool = False                  # a continued/forked transcript's copy of another call

    @property
    def status(self) -> str:
        if self.alt_log:
            return "test-alt-log"
        if self.is_error is None and not self.output:
            return "no-result"
        if WRAPPER_FAILURE_RE.search(self.output):
            return "wrapper-error"
        if self.is_error:
            return "shell-error"
        return "ok"


@dataclass(frozen=True)
class Lookup:
    """One search or read tool use and the paths it touched."""
    tool_use_id: str
    search: bool
    files: frozenset[str] = frozenset()          # Read targets, shell read arguments
    search_paths: frozenset[str] = frozenset()   # Grep/Glob paths, shell search paths


@dataclass
class Unit:
    session: "Session"
    unit_id: str                  # "main", an agent id, or "sidechain"
    agent_type: str | None
    lookups: list[Lookup] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    skill_selected: bool = False
    spawned: list[dict] = field(default_factory=list)   # Agent calls made by this unit
    cwd: str | None = None              # first cwd in this unit's own records
    resolved: dict | None = None        # the graph for that cwd (set by analyse)
    repo: str | None = None
    routing_copies: set[str] = field(default_factory=set)   # tool_use ids routing counts elsewhere

    def _kept(self) -> list[Lookup]:
        return [lk for lk in self.lookups if lk.tool_use_id not in self.routing_copies]

    @property
    def searches(self) -> int:
        return sum(1 for lk in self._kept() if lk.search)

    @property
    def reads(self) -> int:
        return sum(1 for lk in self._kept() if not lk.search)

    @property
    def files(self) -> set[str]:
        return {f for lk in self._kept() for f in lk.files}

    @property
    def search_paths(self) -> set[str]:
        return {p for lk in self._kept() for p in lk.search_paths}

    def routing_calls(self) -> list[Call]:
        """Real (not redirected-log) calls that routing credits to this unit."""
        return [c for c in self.calls if not c.alt_log and c.tool_use_id not in self.routing_copies]

    @property
    def label(self) -> str:
        who = "main" if self.unit_id == "main" else f"{self.agent_type or 'subagent'}:{self.unit_id}"
        return f"{self.session.session_id[:8]}/{who}"

    def covered(self, in_scope: Callable[[str], bool]) -> int:
        """Distinct files read or paths searched inside the graph's coverage."""
        return sum(1 for f in self.files | self.search_paths if in_scope(f))

    def enough_lookups(self) -> bool:
        if self.unit_id == "main":
            return self.searches >= MAIN_MIN_SEARCHES and len(self.files) >= MAIN_MIN_FILES
        return self.searches + self.reads >= SUBAGENT_MIN_LOOKUPS

    def is_exploration(self, in_scope: Callable[[str], bool]) -> bool:
        """Heuristic eligibility: enough lookups, and they land in code the graph covers."""
        need = MAIN_MIN_FILES if self.unit_id == "main" else 1
        return self.enough_lookups() and self.covered(in_scope) >= need


@dataclass
class Session:
    session_id: str
    path: Path
    cwd: str | None = None
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    hint_seen: bool = False
    subagent_hints: int = 0
    units: dict[str, Unit] = field(default_factory=dict)
    seen_tool_ids: set[str] = field(default_factory=set)
    resolved: dict | None = None
    repo: str | None = None
    excluded: str | None = None
    read_errors: list[str] = field(default_factory=list)     # "<file>: <error>", main or subagent
    corrupt_lines: int = 0                                    # undecodable / non-object JSONL lines
    meta_problems: list[str] = field(default_factory=list)   # "<agent file>: <problem>"

    def unit(self, unit_id: str, agent_type: str | None = None) -> Unit:
        if unit_id not in self.units:
            self.units[unit_id] = Unit(self, unit_id, agent_type)
        return self.units[unit_id]


@dataclass
class SessionScan:
    """Sessions in the window, plus every transcript that could not be placed in it."""
    sessions: list[Session]
    projects_missing: bool = False
    unstatable: list[str] = field(default_factory=list)     # "<file>: <error>"
    undated: list[Session] = field(default_factory=list)    # no timestamp at all (incl. unreadable)


@dataclass
class QueryLog:
    path: Path | None
    status: str              # ok | not-configured | missing | not-a-file | unreadable
    records: list[dict] = field(default_factory=list)
    corrupt_lines: int = 0
    error: str | None = None
    undated_records: int = 0     # query records whose ts is missing or unparseable


@dataclass
class ReadStats:
    corrupt_lines: int = 0
    error: str | None = None


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class FunnelConfig:
    skill_scripts: Path
    query_log: Path | None
    targets: tuple[str, ...]              # absolute repo paths (repo, else scan)
    hint_live_since: datetime | None
    hint_live_since_raw: str | None
    exclude_temp_cwd: bool
    exclude_sessions: dict[str, str]      # session id -> reason


# --------------------------------------------------------------------------- helpers

def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def norm_path(p: str) -> str:
    """Normalise a transcript path; MSYS-style /<letter>/x becomes a drive path on Windows."""
    m = re.match(r"^/([a-zA-Z])(/.*)?$", p)
    if m and os.name == "nt":
        p = f"{m.group(1)}:{m.group(2) or '/'}"
    return norm(p)


def iter_json_lines(path: Path, stats: ReadStats) -> Iterable[dict]:
    """Yield the JSON objects in a JSONL file. Corrupt lines (invalid UTF-8, invalid JSON, or
    not an object) and read errors go to `stats`."""
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw.decode("utf-8"))
                except ValueError:       # UnicodeDecodeError is a ValueError
                    stats.corrupt_lines += 1
                    continue
                if isinstance(rec, dict):
                    yield rec
                else:
                    stats.corrupt_lines += 1
    except OSError as exc:
        stats.error = f"{type(exc).__name__}: {exc}"


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[k]


def load_resolver(skill_scripts: Path):
    path = skill_scripts / "resolve_graph.py"
    spec = importlib.util.spec_from_file_location("graphify_resolve_graph", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"resolver not found: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod     # dataclasses look the module up while executing it
    spec.loader.exec_module(mod)
    return mod


def norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


# --------------------------------------------------------------------------- config

FUNNEL_KEYS = {"hint_live_since", "exclude_temp_cwd", "exclude_sessions"}
PLACEHOLDER_RE = re.compile(r"<[^<>]*>")   # template value such as "<path-to-repo>"


def resolve_config_path(value: Any, base: Path, field_name: str, errors: list[str]) -> Path | None:
    """Mirror of GraphifyConfig.ps1 Resolve-GraphifyConfigPath: a config-file path resolves
    against the config file's folder and becomes absolute; drive-relative ("C:x") and
    root-relative ("\\x") forms are rejected because they depend on the current drive."""
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field_name} must be a non-empty string.")
        return None
    if PLACEHOLDER_RE.search(value):
        errors.append(f"{field_name} still holds a template placeholder.")
        return None
    p = Path(value)
    if p.is_absolute():
        return Path(os.path.normpath(p))
    if p.drive or p.root:
        errors.append(f"{field_name} is drive- or root-relative; use an absolute path "
                      "or one relative to the config file.")
        return None
    return Path(os.path.normpath(base / p))


def _parse_funnel(raw: Any, errors: list[str]) -> tuple[datetime | None, str | None, bool, dict[str, str]]:
    hint, hint_raw, temp_rule, excludes = None, None, True, {}
    if raw is None:
        return hint, hint_raw, temp_rule, excludes
    if not isinstance(raw, dict):
        errors.append("funnel must be a JSON object.")
        return hint, hint_raw, temp_rule, excludes
    for key in sorted(set(raw) - FUNNEL_KEYS):
        errors.append(f"funnel.{key} is not a known key (expected one of {sorted(FUNNEL_KEYS)}).")
    if "hint_live_since" in raw:
        v = raw["hint_live_since"]
        hint = parse_ts(v)
        if hint is None:
            errors.append(f"funnel.hint_live_since must be an ISO-8601 timestamp, got {v!r}.")
        else:
            hint_raw = v
    if "exclude_temp_cwd" in raw:
        if isinstance(raw["exclude_temp_cwd"], bool):
            temp_rule = raw["exclude_temp_cwd"]
        else:
            errors.append(f"funnel.exclude_temp_cwd must be true or false, got {raw['exclude_temp_cwd']!r}.")
    if "exclude_sessions" in raw:
        v = raw["exclude_sessions"]
        # Documented form is {session-id: reason}; a bare list of ids is accepted too.
        pairs = (list(v.items()) if isinstance(v, dict)
                 else [(sid, "listed in funnel.exclude_sessions") for sid in v] if isinstance(v, list)
                 else None)
        if pairs is None:
            errors.append("funnel.exclude_sessions must be an object {session-id: reason} "
                          "or a list of session ids.")
        else:
            for sid, reason in pairs:
                if not isinstance(sid, str) or not sid.strip():
                    errors.append(f"funnel.exclude_sessions holds a non-string or empty session id: {sid!r}.")
                elif not isinstance(reason, str) or not reason.strip():
                    errors.append(f"funnel.exclude_sessions[{sid!r}] needs a non-empty string reason.")
                else:
                    excludes[sid] = reason
    return hint, hint_raw, temp_rule, excludes


def _parse_targets(raw: Any, base: Path, errors: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append("targets must be a JSON array.")
        return ()
    out: list[str] = []
    for i, t in enumerate(raw):
        f = f"targets[{i}]"
        if not isinstance(t, dict):
            errors.append(f"{f} must be a JSON object.")
            continue
        # GraphifyConfig.ps1 Test-GraphifyConfigTargets: "repo" is optional and defaults to
        # "scan" ($repoIn = if ($t.repo) { $t.repo } else { $t.scan }); both resolve against
        # the config folder. A scan-only target therefore still names a graph-expected repo.
        if t.get("repo"):
            p = resolve_config_path(t["repo"], base, f"{f}.repo", errors)
        elif t.get("scan"):
            p = resolve_config_path(t["scan"], base, f"{f}.scan", errors)
        else:
            errors.append(f"{f} needs a scan or repo path.")
            continue
        if p is not None:
            out.append(str(p))
    return tuple(out)


def parse_config(raw: Any, base: Path) -> FunnelConfig:
    """Validate every value the funnel reads; raise ConfigError listing all problems."""
    if not isinstance(raw, dict):
        raise ConfigError(["config must be a JSON object."])
    errors: list[str] = []
    scripts = None
    if "skill_scripts" not in raw:
        errors.append("skill_scripts is missing (folder holding resolve_graph.py).")
    else:
        scripts = resolve_config_path(raw["skill_scripts"], base, "skill_scripts", errors)
    log = (resolve_config_path(raw["query_log"], base, "query_log", errors)
           if "query_log" in raw else None)
    targets = _parse_targets(raw.get("targets"), base, errors)
    hint, hint_raw, temp_rule, excludes = _parse_funnel(raw.get("funnel"), errors)
    if errors or scripts is None:
        raise ConfigError(errors)
    return FunnelConfig(scripts, log, targets, hint, hint_raw, temp_rule, excludes)


# --------------------------------------------------------------------------- transcripts

def _ingest(session: Session, unit: Unit, path: Path, pending: dict[str, Call],
            track_sidechain: bool) -> None:
    stats = ReadStats()
    _ingest_records(session, unit, iter_json_lines(path, stats), pending, track_sidechain)
    session.corrupt_lines += stats.corrupt_lines
    if stats.error:
        session.read_errors.append(f"{path}: {stats.error}")


def _ingest_records(session: Session, unit: Unit, records: Iterable[dict],
                    pending: dict[str, Call], track_sidechain: bool) -> None:
    for rec in records:
        ts = parse_ts(rec.get("timestamp"))
        if ts:
            session.first_ts = min(filter(None, [session.first_ts, ts]))
            session.last_ts = max(filter(None, [session.last_ts, ts]))
        cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
        if session.cwd is None and cwd:
            session.cwd = cwd
        target = unit
        if track_sidechain and rec.get("isSidechain"):
            target = session.unit("sidechain", "subagent")
        if target.cwd is None and cwd:
            target.cwd = cwd

        att = rec.get("attachment")
        if isinstance(att, dict) and att.get("type") == "hook_additional_context":
            if "<graphify-graph>" in json.dumps(att.get("content"), ensure_ascii=False):
                if target.unit_id == "main":
                    session.hint_seen = True
                else:
                    session.subagent_hints += 1

        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_id = str(block.get("id", ""))
                if tool_id and tool_id in session.seen_tool_ids:
                    continue    # resumed/forked transcripts repeat earlier records
                session.seen_tool_ids.add(tool_id)
                origin = rec.get("sessionId")
                _tool_use(target, block, ts, pending, origin if isinstance(origin, str) else None,
                          cwd or target.cwd)
            elif block.get("type") == "tool_result":
                call = pending.pop(block.get("tool_use_id", ""), None)
                if call is not None:
                    call.result_ts = ts
                    call.is_error = bool(block.get("is_error"))
                    call.output = result_text(block.get("content"))[:4000]


def _tool_use(unit: Unit, block: dict, ts: datetime | None, pending: dict[str, Call],
              origin_session: str | None = None, cwd: str | None = None) -> None:
    name = block.get("name", "")
    tool_id = str(block.get("id", ""))
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name in SEARCH_TOOLS:
        path = inp.get("path")
        unit.lookups.append(Lookup(tool_id, True, search_paths=frozenset(
            [norm_path(path)] if isinstance(path, str) and path else [])))
    elif name == "Read":
        path = inp.get("file_path")
        unit.lookups.append(Lookup(tool_id, False, files=frozenset(
            [norm_path(path)] if isinstance(path, str) else [])))
    elif name == "Skill" and str(inp.get("skill", "")).split(":")[-1] == "graphify":
        unit.skill_selected = True
    elif name in AGENT_TOOLS:
        prompt = str(inp.get("prompt", ""))
        unit.spawned.append({"type": inp.get("subagent_type") or "general-purpose",
                             "passed_graph": bool(GRAPHIFY_CALL_RE.search(prompt)
                                                  or "graphify" in prompt.lower())})
    elif name in SHELL_TOOLS:
        cmd = str(inp.get("command", ""))
        code = code_part(cmd, name)
        if GRAPHIFY_CALL_RE.search(code):
            m = QUESTION_RE.search(cmd)
            call = Call(unit, tool_id, ts, cmd, m.group(1) if m else None,
                        bool(WRAPPER_RE.search(code)), bool(ALT_LOG_RE.search(cmd)),
                        origin_session=origin_session)
            unit.calls.append(call)
            pending[call.tool_use_id] = call
        else:
            lk = shell_lookups(cmd, name, cwd)
            tails = code_part(cmd, name, keep_heredoc_tail=True)
            searched = (any(shell_searches(part, name) for part in [tails, *shell_script_parts(cmd, name)])
                        or lk.searched)
            if searched or lk.read:          # paths only come with a search or a read
                unit.lookups.append(Lookup(tool_id, searched, frozenset(lk.read_paths),
                                           frozenset(lk.search_paths)))


@dataclass
class ShellLookups:
    searched: bool = False
    read: bool = False
    search_paths: set[str] = field(default_factory=set)
    read_paths: set[str] = field(default_factory=set)


def _shell_words(segment: str, tool: str) -> list[str]:
    try:
        lexer = shlex.shlex(segment, posix=tool != "PowerShell", punctuation_chars=False)
        lexer.whitespace_split = True
        if tool == "PowerShell":
            lexer.escape = ""     # backslashes are path separators there, and ` is not modelled
        words = list(lexer)
    except ValueError:
        words = segment.split()
    return [w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in "\"'" else w for w in words]


def _tool_name(word: str) -> str:
    """A command word as a bare lower-case name: quotes off, `.exe` off (so `rg.exe` is `rg`)."""
    return word.strip("\"'").lower().removesuffix(".exe")


def _segment_searches(words: list[str]) -> bool:
    """Whether a search tool is run as a command in one command segment (see SHELL_SEARCH_TOOLS)."""
    want, runner, i, n = True, False, 0, len(words)
    while i < n:
        word = words[i]
        name = _tool_name(word)
        i += 1
        if runner and not want:
            first = word.split(None, 1)[0] if word.strip() else ""
            if _tool_name(first) in SHELL_SEARCH_TOOLS:
                return True                     # `ssh host grep ...`, `ssh host "grep ..."`
        if not want:
            want = word == "=" or word.endswith("{")      # `$r = <cmd>`, a block
            continue
        if re.match(r"[A-Za-z_]\w*=", word) or name in _SEARCH_KEYWORDS:
            continue                            # VAR=value prefix, `then`, `do`, `{`
        if name in SHELL_SEARCH_TOOLS:
            return True
        if name == "git":
            while i < n and words[i].startswith("-"):      # git's global options, then the subcommand
                i += 2 if words[i] in _GIT_VALUE_FLAGS else 1
            if i < n and words[i].lower() == "grep":
                return True
            want = False
        elif name in _SEARCH_WRAPPER_VALUE_FLAGS:
            value_flags = _SEARCH_WRAPPER_VALUE_FLAGS[name]
            first_flag = i
            while i < n and words[i].startswith("-"):
                i += 2 if words[i] in value_flags else 1
            if name == "command" and {"-v", "-V"} & set(words[first_flag:i]):
                want = False                    # `command -v rg` looks a tool up, it does not run it
            if name == "timeout" and i < n and re.match(r"\d[\w.]*$", words[i]):
                i += 1                          # timeout's duration
            elif (name == "taskset" and i < n and not {"-c", "--cpu-list"} & set(words[first_flag:i])
                  and re.match(r"(0x)?[0-9a-fA-F][0-9a-fA-F,-]*$", words[i])):
                i += 1                          # taskset's CPU mask, when no -c list was given
            elif name == "flock" and i < n:
                i += 1                          # flock's lock file or directory
        elif name in _SEARCH_RUNNERS:
            runner, want = True, False
        else:
            want = False
    return False


def shell_searches(text: str, tool: str = "Bash") -> bool:
    """Whether the (already blanked, see code_part) shell text runs grep, rg, findstr or Select-String
    as a command. A tool name that is only a word of an argument or a string does not count."""
    if tool != "PowerShell":
        text = text.replace("`", ";")           # `cmd` substitution starts a command
    parts = _SEGMENT_RE.split(text)
    # a segment cut at find's escaped `\;` ends in a lone backslash, which would make the lexer fail
    return any(_segment_searches(_shell_words(parts[i].rstrip().removesuffix("\\"), tool))
               for i in range(0, len(parts), 2))


def _shell_path(arg: str, base: str | None) -> str | None:
    """A normalised path for a command argument that names a file or folder, else None.

    Accepts absolute paths (a drive letter, a Git Bash drive prefix, a home tilde) and relative ones that contain a
    separator or end in a file extension; a git `<ref>:<path>` keeps the path; a glob is cut
    back to the folder before its first wildcard. Relative paths need `base` (the cd target,
    else the record's cwd).
    """
    if not arg or arg.startswith("-") or "://" in arg or "$" in arg:
        return None
    if not _DRIVE_RE.match(arg) and ":" in arg:
        arg = arg.rsplit(":", 1)[1]
    cut = min((arg.find(c) for c in _GLOB_CHARS if c in arg), default=-1)
    if cut >= 0:
        arg = arg[:max(arg.rfind("/", 0, cut), arg.rfind("\\", 0, cut)) + 1]
    if not arg or not _PATHLIKE_RE.match(arg) or arg in (".", ".."):
        return None
    if arg.startswith("~"):
        arg = os.path.expanduser(arg)
    absolute = bool(_DRIVE_RE.match(arg)) or arg.startswith("/")
    if not absolute:
        if not (re.search(r"[\\/]", arg) or re.search(r"\.\w{1,8}$", arg)) or not base:
            return None
        arg = os.path.join(base, arg)
    return norm_path(arg.rstrip("/\\") or arg)


def shell_lookups(command: str, tool: str, cwd: str | None) -> ShellLookups:
    """Searches and reads in a shell command, with the paths they touched.

    Splits the command (heredoc bodies and quoted separators already blanked) into
    segments, follows `cd`/`Set-Location`/`git -C` so relative arguments resolve against
    where the command ran, and keeps only path-like arguments of search and read commands.
    A search with no path argument after a `cd` searched the cd target. Commands it does
    not know (python, git log, ...) contribute nothing.
    """
    out = ShellLookups()
    base = norm_path(cwd) if cwd else None
    parts = _SEGMENT_RE.split(code_part(command, tool))
    for i in range(0, len(parts), 2):
        piped = i > 0 and parts[i - 1] == "|"
        words = _shell_words(parts[i], tool)
        while words and re.match(r"^\w+=", words[0]):
            words.pop(0)                  # VAR=value prefixes
        if not words:
            continue
        head, args, here = words[0].lower(), words[1:], base
        if head.endswith(".exe"):
            head = head[:-4]
        if head == "git":
            while len(args) >= 2 and args[0] == "-C":
                here = _shell_path(args[1], here) or here
                args = args[2:]
            if args:
                head, args = f"git {args[0].lower()}", args[1:]
        if head in SHELL_CD_CMDS:
            target = next((a for a in args if not a.startswith("-")), None)
            dest = _shell_path(target, base) if target else None
            if dest:
                base = dest
            continue
        is_read, is_search = head in SHELL_READ_CMDS, head in SHELL_SEARCH_CMDS
        if not (is_read or is_search):
            continue
        positional: list[str] = []
        pattern_given, skip = False, False
        for a in args:
            if skip:
                skip = False
                continue
            opt = a.split("=", 1)[0]
            opt = opt if len(opt) <= 2 else opt.lower()
            if a.startswith("-"):
                pattern_given |= opt in SHELL_PATTERN_OPTS
                skip = opt in SHELL_VALUE_OPTS and "=" not in a
                continue
            positional.append(a)
        if head in SHELL_PATTERN_FIRST and not pattern_given:
            positional = positional[1:]
        found = {p for p in (_shell_path(a, here) for a in positional) if p}
        if is_search:
            out.searched = True
            if not found and here and here != (norm_path(cwd) if cwd else None) and not piped:
                found = {here}
            out.search_paths |= found
        else:
            out.read = True
            out.read_paths |= found
    return out


def load_session(path: Path) -> Session:
    session = Session(path.stem, path)
    pending: dict[str, Call] = {}
    _ingest(session, session.unit("main"), path, pending, track_sidechain=True)
    sub_dir = path.with_suffix("") / "subagents"
    if sub_dir.is_dir():
        for sub in sorted(sub_dir.glob("agent-*.jsonl")):
            agent_type, problem = read_agent_type(sub.with_suffix(".meta.json"))
            if problem:
                # The unit stays in the session; with no type it is judged shell-capable: say so.
                session.meta_problems.append(f"{sub.name}: {problem}")
            unit = session.unit(sub.stem.removeprefix("agent-"), agent_type)
            _ingest(session, unit, sub, pending, track_sidechain=False)
    return session


def read_agent_type(meta_path: Path) -> tuple[str | None, str | None]:
    """(agentType, problem) from a subagent's .meta.json; problem is None when it was read."""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "metadata missing"
    except OSError as exc:
        return None, f"metadata unreadable ({type(exc).__name__})"
    except ValueError:
        return None, "metadata is not valid JSON"
    agent_type = meta.get("agentType") if isinstance(meta, dict) else None
    if not isinstance(agent_type, str) or not agent_type:
        return None, "metadata has no agentType"
    return agent_type, None


def discover_sessions(projects: Path, since: datetime, until: datetime) -> SessionScan:
    # A session moved between project folders leaves a copy in each; keep the largest.
    best: dict[str, tuple[int, Path]] = {}
    floor = since.timestamp()
    if not projects.is_dir():
        return SessionScan([], projects_missing=True)
    scan = SessionScan([])
    for proj in sorted(p for p in projects.iterdir() if p.is_dir()):
        for path in proj.glob("*.jsonl"):
            try:
                st = path.stat()
            except OSError as exc:
                scan.unstatable.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if st.st_mtime < floor:
                continue
            if path.stem not in best or st.st_size > best[path.stem][0]:
                best[path.stem] = (st.st_size, path)
    for _, path in sorted(best.values(), key=lambda v: str(v[1])):
        s = load_session(path)
        if not (s.first_ts and s.last_ts):
            scan.undated.append(s)      # unreadable, or no timestamped record: not placeable
        elif s.last_ts >= since and s.first_ts <= until:
            scan.sessions.append(s)
    return scan


# --------------------------------------------------------------------------- analysis

def load_log(path: Path | None, since: datetime, until: datetime) -> QueryLog:
    if path is None:
        return QueryLog(None, "not-configured")
    if not path.exists():
        return QueryLog(path, "missing")
    if not path.is_file():
        return QueryLog(path, "not-a-file")
    stats = ReadStats()
    recs, undated = [], 0
    for r in iter_json_lines(path, stats):
        if r.get("kind") != "query":
            continue
        ts = parse_ts(r.get("ts"))
        if ts is None:
            undated += 1          # cannot be placed in any window: reported, never dropped silently
        elif since - MATCH_SLACK <= ts <= until + MATCH_SLACK:
            r["_ts"] = ts
            recs.append(r)
    return QueryLog(path, "unreadable" if stats.error else "ok", recs,
                    stats.corrupt_lines, stats.error, undated)


def finite_duration(value: Any) -> float | None:
    """A usable latency: int or float, not bool (an int subclass), finite, >= 0. Else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def mark_copied_calls(sessions: list[Session]) -> int:
    """Mark all but one Call per tool_use id across sessions as copies; return the count.

    A continued or forked session starts a new transcript (new session id) holding a copy of
    the earlier history, so one real invocation shows up in every copy, yet the query log has
    one record for it. The copy kept is the one in the session named by the record's own
    sessionId (copies keep the originating id); without that, the earliest-starting session.
    Copies leave execution and measurement; routing dedupes on its own terms
    (mark_routing_copies).
    """
    def rank(c: Call) -> tuple:
        s = c.unit.session
        return (c.origin_session != s.session_id,
                s.first_ts or datetime.max.replace(tzinfo=timezone.utc), s.session_id)

    by_id: dict[str, list[Call]] = {}
    for s in sessions:
        for u in s.units.values():
            for c in u.calls:
                if c.tool_use_id:
                    by_id.setdefault(c.tool_use_id, []).append(c)
    marked = 0
    for copies in by_id.values():
        for c in sorted(copies, key=rank)[1:]:
            c.copy = True
            marked += 1
    return marked


def mark_routing_copies(units: list[Unit]) -> int:
    """Count each lookup and call once in routing; return how many copies were set aside.

    A continued or forked transcript repeats the earlier history under the same tool_use
    ids, so without this one exploration shows up as one unit per transcript. Each id stays
    with the holder whose session ran latest: the continuation carries the whole conversation
    plus what it did after, so its own graphify call covers the lookups it copied. The other
    holders drop it from routing (execution keeps its own owner rule, mark_copied_calls).
    """
    def latest(u: Unit) -> tuple:
        return (u.session.last_ts or datetime.min.replace(tzinfo=timezone.utc), u.session.session_id)

    holders: dict[str, list[Unit]] = {}
    for u in units:
        for tid in {lk.tool_use_id for lk in u.lookups} | {c.tool_use_id for c in u.calls}:
            if tid:
                holders.setdefault(tid, []).append(u)
    marked = 0
    for tid, us in holders.items():
        if len(us) > 1:
            keep = max(us, key=latest)
            for u in us:
                if u is not keep:
                    u.routing_copies.add(tid)
                    marked += 1
    return marked


def match_records(calls: list[Call], records: list[dict]) -> None:
    free = list(records)
    for call in sorted(calls, key=lambda c: c.ts or datetime.min.replace(tzinfo=timezone.utc)):
        if call.status != "ok" or call.ts is None:
            continue
        hi = (call.result_ts or call.ts) + MATCH_SLACK
        lo = call.ts - MATCH_SLACK
        cands = [r for r in free if lo <= r["_ts"] <= hi]
        if call.question:
            same = [r for r in cands if r.get("question") == call.question]
            cands = same or cands
        if cands:
            call.record = cands[0]
            free.remove(cands[0])


def coverage(session: Session) -> Callable[[str], bool]:
    return coverage_for(session.resolved, session.repo)


def coverage_for(resolved: dict | None, repo: str | None) -> Callable[[str], bool]:
    """Path predicate for the code a resolved graph covers.

    Covers the graph root, the local mapping of it, and the same relative
    folder inside any worktree lane under the repo, so units from worktrees
    that were deleted since (they now resolve to the main checkout) still count.
    """
    r = resolved or {}
    roots = {norm(r["graph_root"]), norm(r["local_root"])} if r else set()
    patterns = []
    if r and repo:
        repo = norm(repo)
        graph_root = norm(r["graph_root"])
        rel = os.path.relpath(graph_root, repo) if graph_root.startswith(repo) else None
        if rel is not None and not rel.startswith(".."):
            sep = re.escape(os.sep)   # paths are already norm()-ed to os.sep
            lane = re.escape(os.path.join(repo, ".claude", "worktrees")) + sep + f"[^{sep}]+"
            tail = "" if rel == "." else re.escape(os.sep + rel)
            patterns.append(re.compile(lane + tail + f"(?:{sep}|$)"))
    prefixes = tuple(x.rstrip(os.sep) + os.sep for x in roots)

    def in_scope(path: str) -> bool:
        return (path.startswith(prefixes) or path + os.sep in prefixes
                or any(p.match(path) for p in patterns))
    return in_scope


def repo_identity(path: str, resolver) -> str | None:
    """Main-checkout root of the git repo holding `path` (a worktree maps to its main checkout)."""
    try:
        top = resolver._git_toplevel(Path(path).resolve())
    except OSError:
        return None
    if top is None:
        return None
    main = resolver._main_checkout(top)
    return str(main or top)


def repo_of(session: Session, resolver) -> str | None:
    return repo_identity(session.cwd, resolver) if session.cwd else None


# --------------------------------------------------------------------------- agent definitions
# Ported from the profile's graphify hint hook (the hook may not import from here, and
# this repo cannot import the hook): keep the two in step. Definitions are read as they are
# now, not as they were when the session ran.

def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _scalar_or_list(value: str) -> str | list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [_unquote(v) for v in _LIST_SPLIT_RE.split(value[1:-1]) if v.strip()]
    return _unquote(value)


def parse_frontmatter(text: str) -> dict[str, str | list[str]] | None:
    """Top-level keys of a `---` frontmatter block; None if there is none or it is unterminated."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    out: dict[str, str | list[str]] = {}
    key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        item = re.match(r"\s*-\s+(.*)$", line)
        if item and key is not None and isinstance(out.get(key), list):
            out[key].append(_unquote(item.group(1)))  # type: ignore[union-attr]
            continue
        m = re.match(r"([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            key, value = m.group(1), m.group(2)
            out[key] = _scalar_or_list(value) if value.strip() else []
    return None


def _tool_names(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    items = value if isinstance(value, list) else _LIST_SPLIT_RE.split(value)
    return [t.strip() for t in items if t.strip()]


def find_agent_definition(agent_type: str, cwd: str | None,
                          user_agents: Path | None) -> dict[str, str | list[str]] | None:
    """Frontmatter of the agent file whose `name` (or file stem) is agent_type.

    Searched in .claude/agents from cwd upward (nearest wins), then in user_agents.
    """
    dirs: list[Path] = []
    if cwd:
        here = Path(cwd)
        dirs = [d / ".claude" / "agents" for d in (here, *here.parents)]
    if user_agents is not None:
        dirs.append(user_agents)
    for d in dirs:
        try:
            paths = sorted(d.glob("*.md")) if d.is_dir() else []
        except OSError:
            continue
        for path in paths:
            try:
                with open(path, "rb") as f:
                    text = f.read(AGENT_DEF_MAX_BYTES).decode("utf-8", errors="replace")
            except OSError:
                continue
            fm = parse_frontmatter(text)
            if fm is None:
                continue
            name = fm.get("name")
            if (name if isinstance(name, str) and name else path.stem) == agent_type:
                return fm
    return None


def agent_has_shell(agent_type: str | None, cwd: str | None,
                    user_agents: Path | None) -> bool | None:
    """False when the agent provably has no Bash/PowerShell; None when its definition is unknown.

    A `tools` field absent or `*` means every tool; a pattern grant such as Bash(git:*)
    counts as a shell; only a bare entry in disallowedTools removes one.
    """
    if not agent_type:
        return None
    if agent_type in NO_SHELL_AGENTS:
        return False
    if ":" in agent_type:
        return None
    fm = find_agent_definition(agent_type, cwd, user_agents)
    if fm is None:
        return None
    tools = _tool_names(fm.get("tools"))
    denied = set(_tool_names(fm.get("disallowedTools")) or [])
    if not tools or "*" in tools:
        granted = set(SHELL_TOOLS)
    else:
        granted = {t.split("(", 1)[0].strip() for t in tools} & set(SHELL_TOOLS)
    return bool(granted - denied)


def input_problems(inputs: dict) -> list[str]:
    """One line per kind of unusable input; empty when every input was read cleanly."""
    q, out = inputs["query_log"], []
    if q["status"] == "not-configured":
        out.append("query log not configured (no query_log, --log or GRAPHIFY_QUERY_LOG): "
                   "execution and measurement have no log evidence")
    elif q["status"] != "ok":
        out.append(f"query log {q['status']}: {q['path']}"
                   + (f" ({q['error']})" if q["error"] else "")
                   + ": execution and measurement have no complete log evidence")
    if q["corrupt_lines"]:
        out.append(f"query log has {q['corrupt_lines']} corrupt line(s), skipped")
    if q["undated_records"]:
        out.append(f"query log has {q['undated_records']} query record(s) without a usable ts, skipped")
    if inputs["projects_dir_missing"]:
        out.append("transcript projects folder missing: no sessions could be read")
    for key, text in (("transcripts_unstatable", "transcript(s) could not be stat'ed"),
                      ("transcripts_unreadable", "transcript file(s) could not be read"),
                      ("transcripts_undated", "transcript(s) had no timestamped record and were left out"),
                      ("subagent_metadata_problems", "subagent(s) without readable metadata "
                                                     "(type unknown, judged shell-capable)"),
                      ("sessions_without_cwd", "session(s) without a cwd (repo and graph unknown)")):
        if inputs[key]:
            out.append(f"{len(inputs[key])} {text}")
    if inputs["transcript_corrupt_lines"]:
        out.append(f"{inputs['transcript_corrupt_lines']} corrupt transcript line(s), skipped")
    return out


def analyse(scan: SessionScan, log: QueryLog, resolver, cfg: FunnelConfig,
            since: datetime, until: datetime, user_agents: Path | None = None) -> dict:
    if user_agents is None:
        user_agents = Path.home() / ".claude" / "agents"
    sessions, records = scan.sessions, log.records
    excludes = cfg.exclude_sessions
    hint_live = cfg.hint_live_since
    # A target path (repo, else scan) may sit inside a repo or a worktree lane: compare it by
    # the same identity as a session's repo; a path outside any git repo is kept verbatim.
    targets = {norm(repo_identity(t, resolver) or t) for t in cfg.targets}
    # Headless/throwaway runs start in the temp dir; on unless the config turns it off.
    tmp = norm(tempfile.gettempdir()) if cfg.exclude_temp_cwd else None

    for s in sessions:
        if s.session_id in excludes:
            s.excluded = str(excludes[s.session_id])
        elif tmp and s.cwd and norm(s.cwd).startswith(tmp):
            s.excluded = "cwd in system temp dir"
        if s.cwd:
            r = resolver.resolve(s.cwd)
            s.resolved = dataclasses.asdict(r) if dataclasses.is_dataclass(r) else r
        s.repo = repo_of(s, resolver)

    organic = [s for s in sessions if not s.excluded]
    copied_calls = mark_copied_calls(sessions)
    all_calls = [c for s in sessions for u in s.units.values() for c in u.calls if not c.copy]
    match_records(all_calls, records)

    # A session is kept whole when it overlaps the window, but the log is windowed per
    # record, so execution and measurement judge only calls made inside the window (same
    # slack as the log); an undated call stays in. The wrapper logs when the call ends, so
    # a call is placed by its result time when it has one. Records of boundary calls are
    # still matched above, so they never read as unattributed.
    def in_window(c: Call) -> bool:
        t = c.result_ts or c.ts
        return t is None or since - MATCH_SLACK <= t <= until + MATCH_SLACK

    outside_window = sum(1 for s in organic for u in s.units.values() for c in u.calls
                         if not c.alt_log and not c.copy and not in_window(c))
    organic_calls = [c for s in organic for u in s.units.values() for c in u.calls
                     if not c.alt_log and not c.copy and in_window(c)]
    alt_log_calls = sum(1 for s in organic for u in s.units.values() for c in u.calls
                        if c.alt_log and not c.copy and in_window(c))

    # ---- stage 1: availability
    def post_hint(s: Session) -> bool:
        return bool(hint_live and s.first_ts and s.first_ts >= hint_live)

    by_repo: dict[str, dict[str, int]] = {}
    missing_expected, hint_missing = [], []
    for s in organic:
        key = s.repo or (s.cwd or "<unknown cwd>")
        row = by_repo.setdefault(key, {"sessions": 0, "graph": 0, "graph_pre": 0, "hinted_pre": 0,
                                       "graph_post": 0, "hinted_post": 0})
        row["sessions"] += 1
        if s.resolved:
            row["graph"] += 1
            phase = "post" if post_hint(s) else "pre"
            row[f"graph_{phase}"] += 1
            if s.hint_seen:
                row[f"hinted_{phase}"] += 1
        if s.repo and norm(s.repo) in targets and not s.resolved:
            missing_expected.append(s)
        if s.resolved and post_hint(s) and not s.hint_seen:
            hint_missing.append(s)
    graph_backed = [s for s in organic if s.resolved]
    graph_post = [s for s in graph_backed if post_hint(s)]
    # 1a: did a graph resolve where one is expected?
    if missing_expected:
        v1a = FAIL
    elif graph_backed:
        v1a = PASS
    else:
        v1a = INSUFFICIENT
    # 1b: did the hint reach graph-backed sessions that started after it went live?
    # Pre-hint sessions (and hints they saw on resume) are no evidence either way.
    if hint_missing:
        v1b = FAIL
    elif graph_post:
        v1b = PASS
    else:
        v1b = INSUFFICIENT
    v1 = FAIL if FAIL in (v1a, v1b) else PASS if (v1a, v1b) == (PASS, PASS) else INSUFFICIENT

    # ---- stage 2: routing
    # Each unit is judged against the graph of its own cwd: a subagent may run elsewhere
    # than its session started, and a graph-less session may spawn one inside a graph.
    graph_cache: dict[str, tuple[dict | None, str | None]] = {}
    for s in organic:
        for u in s.units.values():
            u.cwd = u.cwd or s.cwd
            if not u.cwd:
                continue
            if u.cwd not in graph_cache:
                r = resolver.resolve(u.cwd)
                graph_cache[u.cwd] = (dataclasses.asdict(r) if dataclasses.is_dataclass(r) else r,
                                      repo_identity(u.cwd, resolver))
            u.resolved, u.repo = graph_cache[u.cwd]
    routing_copies = mark_routing_copies([u for s in organic for u in s.units.values()])
    units = [u for s in organic for u in s.units.values() if u.resolved]
    scope_of = {id(u): coverage_for(u.resolved, u.repo) for u in units}
    eligible = [u for u in units if u.is_exploration(scope_of[id(u)])]
    # A subagent without a shell cannot run graphify: its lookups show the graph was
    # unusable to it, not that it was bypassed.
    unusable = [u for u in eligible if u.unit_id != "main"
                and agent_has_shell(u.agent_type, u.cwd, user_agents) is False]
    exploration = [u for u in eligible if u not in unusable]
    no_path_evidence = [u for u in units if u.enough_lookups() and not (u.files | u.search_paths)]
    used = [u for u in exploration if u.routing_calls()]
    bypassed = sorted((u for u in exploration if not u.routing_calls()),
                      key=lambda u: -(u.searches + u.reads))
    other_callers = [u for u in units if u.routing_calls() and u not in eligible]
    after_hint = [u for u in exploration if post_hint(u.session)]
    spawns = [sp for u in units for sp in u.spawned if sp["type"] in EXPLORE_AGENT_TYPES]
    if bypassed:
        v2 = FAIL
    elif exploration:
        v2 = PASS
    else:
        v2 = INSUFFICIENT

    # ---- stage 3: execution
    by_status: dict[str, int] = {}
    for c in organic_calls:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    ok_calls = [c for c in organic_calls if c.status == "ok"]
    unlogged = [c for c in ok_calls if c.via_wrapper and c.record is None]
    wrapper_errors = [c for c in organic_calls if c.status == "wrapper-error"]
    def recovered(c: Call) -> bool:
        return any(o.status == "ok" and o.ts and c.ts and o.ts >= c.ts
                   for o in c.unit.calls if not o.copy)

    shell_errors = [c for c in organic_calls if c.status == "shell-error"]
    unrecovered = [c for c in shell_errors if not recovered(c)]
    recovered_errors = [c for c in shell_errors if recovered(c)]
    bare = [c for c in organic_calls if not c.via_wrapper]
    if unlogged or wrapper_errors or unrecovered:
        v3 = FAIL
    elif organic_calls:
        v3 = PASS
    else:
        v3 = INSUFFICIENT

    # ---- stage 4: measurement
    organic_recs = [c.record for c in organic_calls if c.record is not None]
    matched_ids = {id(c.record) for c in all_calls if c.record is not None}
    unattributed = [r for r in records if id(r) not in matched_ids]
    excluded_recs = [c.record for s in sessions if s.excluded
                     for u in s.units.values() for c in u.calls if c.record is not None]

    def arm(pred) -> dict:
        rs = [r for r in organic_recs if pred(r)]
        # A record without a usable duration is untimed: it never counts as 0 ms.
        d = [v for v in (finite_duration(r.get("duration_ms")) for r in rs) if v is not None]
        return {"n": len(rs), "timed": len(d), "untimed": len(rs) - len(d),
                "zero_nodes": sum(1 for r in rs if r.get("nodes_returned") == 0),
                "median_ms": statistics.median(d) if d else None, "p90_ms": percentile(d, 90)}

    # Only wrapper records carry an explicit rerank stamp, and both arms are wrapper wall
    # time. A record without the field is a bare CLI call with traversal-only timing: it is
    # reported on its own, never folded into the stock arm.
    reranked = arm(lambda r: r.get("rerank") is True)
    stock = arm(lambda r: r.get("rerank") is False)
    unlabelled = arm(lambda r: "rerank" not in r)
    # Latency is judged on timed records only, and an incompletely read log is no evidence.
    if log.status != "ok" or reranked["timed"] < MIN_RERANKED:
        v4 = INSUFFICIENT
    elif reranked["median_ms"] > LATENCY_MEDIAN_MS or reranked["p90_ms"] > LATENCY_P90_MS:
        v4 = FAIL
    else:
        v4 = PASS

    # ---- inputs: what could not be read is reported, never taken as low volume
    seen = sessions + scan.undated
    unreadable = [e for s in seen for e in s.read_errors]
    meta = [f"{s.session_id[:8]} {p}" for s in sessions for p in s.meta_problems]
    no_cwd = [s.session_id[:8] for s in sessions if not s.cwd]
    inputs = {
        "query_log": {"path": str(log.path) if log.path else None, "status": log.status,
                      "error": log.error, "corrupt_lines": log.corrupt_lines,
                      "undated_records": log.undated_records,
                      "records_in_window": len(records)},
        "projects_dir_missing": scan.projects_missing,
        "transcripts_unstatable": scan.unstatable,
        "transcripts_unreadable": unreadable,
        "transcripts_undated": [str(s.path) for s in scan.undated],
        "transcript_corrupt_lines": sum(s.corrupt_lines for s in seen),
        "subagent_metadata_problems": meta,
        "sessions_without_cwd": no_cwd,
    }
    inputs["problems"] = input_problems(inputs)

    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "inputs": inputs,
        "sessions": {"total": len(sessions), "organic": len(organic),
                     "excluded": [{"session": s.session_id, "reason": s.excluded}
                                  for s in sessions if s.excluded]},
        "availability": {
            "verdict": v1, "graph_resolution": v1a, "hint_delivery": v1b,
            "by_repo": by_repo, "graph_backed": len(graph_backed),
            "graph_backed_pre_hint": len(graph_backed) - len(graph_post),
            "graph_backed_post_hint": len(graph_post),
            "hinted_post_hint": sum(1 for s in graph_post if s.hint_seen),
            "hint_live_since": cfg.hint_live_since_raw,
            "expected_graph_missing": [f"{s.session_id[:8]} {s.cwd}" for s in missing_expected],
            "hint_missing": [f"{s.session_id[:8]} {s.cwd}" for s in hint_missing],
            "subagent_hints": sum(s.subagent_hints for s in graph_backed)},
        "routing": {
            "verdict": v2, "eligibility": "heuristic",
            "graph_backed_units": len(units), "exploration_units": len(exploration),
            "used_graphify": [u.label for u in used],
            "after_hint_live": {"exploration_units": len(after_hint),
                                "used_graphify": sum(1 for u in after_hint if u.routing_calls()),
                                "bypassed": [f"{u.label} searches={u.searches} reads={u.reads} "
                                             f"transcript={u.session.path}"
                                             for u in after_hint if not u.routing_calls()]},
            "bypassed": [f"{u.label} searches={u.searches} reads={u.reads} files={len(u.files)} "
                         f"in_graph={u.covered(scope_of[id(u)])} "
                         f"cwd={u.cwd}" for u in bypassed],
            "copied_tool_uses_excluded": routing_copies,
            "graph_unusable_units": len(unusable),
            "graph_unusable": [f"{u.label} searches={u.searches} reads={u.reads} "
                               f"in_graph={u.covered(scope_of[id(u)])} cwd={u.cwd}"
                               for u in unusable],
            "non_exploration_callers": len(other_callers),
            "skill_selected_units": sum(1 for u in units if u.skill_selected),
            "explore_spawns": len(spawns),
            "explore_spawns_passed_graph": sum(1 for sp in spawns if sp["passed_graph"]),
            "lookup_units_without_path_evidence": len(no_path_evidence),
            "eligibility_unknown_sessions": len({u.session.session_id for u in units}
                                                - {u.session.session_id for u in eligible})},
        "execution": {
            "verdict": v3, "calls": len(organic_calls), "by_status": by_status,
            "logged": sum(1 for c in ok_calls if c.record is not None),
            "unlogged_ok_wrapper_calls": [c.unit.label for c in unlogged],
            "copied_calls_excluded": copied_calls,
            "calls_outside_window_excluded": outside_window,
            "wrapper_errors": [f"{c.unit.label}: {c.output[:160]!r}" for c in wrapper_errors],
            "unrecovered_shell_errors": [f"{c.unit.label}: {c.output[:160]!r}" for c in unrecovered],
            "recovered_shell_errors": [f"{c.unit.label}: {c.command[:160]!r} -> {c.output[:160]!r}"
                                       for c in recovered_errors],
            "bare_cli_calls": len(bare), "test_alt_log_calls": alt_log_calls},
        "measurement": {
            "verdict": v4, "organic_records": len(organic_recs), "reranked": reranked,
            "log_status": log.status,
            "stock": stock, "unlabelled_bare_cli": unlabelled, "min_reranked": MIN_RERANKED,
            "latency_ceiling_ms": {"median": LATENCY_MEDIAN_MS, "p90": LATENCY_P90_MS},
            "excluded_session_records": len(excluded_recs),
            "unattributed_records": len(unattributed)},
    }


# --------------------------------------------------------------------------- report

def _fmt_ms(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}"


def render(summary: dict) -> str:
    a, r, e, m = (summary[k] for k in ("availability", "routing", "execution", "measurement"))
    w, s, inp = summary["window"], summary["sessions"], summary["inputs"]
    q = inp["query_log"]
    L = [f"# Graphify adoption funnel {w['since'][:10]} -> {w['until'][:10]}", "",
         f"Sessions in window: {s['total']} ({s['organic']} organic, {len(s['excluded'])} excluded as tests/smoke).",
         f"Query log: {q['path'] or 'not configured'} ({q['status']}, {q['records_in_window']} query records "
         f"in window).", ""]
    if inp["problems"]:
        L += ["**Input problems (verdicts below are missing this evidence; this is not low volume):**",
              *[f"- {x}" for x in inp["problems"]], ""]
    L += ["| Stage | Verdict | Evidence |", "|---|---|---|",
         f"| 1a Graph resolution | {a['graph_resolution']} | {a['graph_backed']}/{s['organic']} organic sessions "
         f"resolved a graph; {len(a['expected_graph_missing'])} in graph-expected repos without one |",
         f"| 1b Hint delivery | {a['hint_delivery']} | {a['hinted_post_hint']}/{a['graph_backed_post_hint']} graph-backed "
         f"sessions started after go-live saw the hint ({a['graph_backed_pre_hint']} pre-hint sessions are no evidence) |",
         f"| 2 Routing | {r['verdict']} | after go-live: {r['after_hint_live']['used_graphify']}/"
         f"{r['after_hint_live']['exploration_units']} exploration units queried graphify; whole window: "
         f"{len(r['used_graphify'])}/{r['exploration_units']} (heuristic), {len(r['bypassed'])} bypassed |",
         f"| 3 Execution | {e['verdict']} | {e['calls']} calls, {e['by_status']}, {e['logged']} logged |",
         f"| 4 Measurement | {m['verdict']} | "
         + ("" if m["log_status"] == "ok" else f"query log {m['log_status']}; ")
         + f"{m['organic_records']} organic records: {m['reranked']['n']} reranked "
         f"({m['reranked']['timed']} timed), {m['stock']['n']} stock, "
         f"{m['unlabelled_bare_cli']['n']} unlabelled bare CLI |", ""]
    L += ["## 1 Availability", "",
          "Graph resolution and hint delivery are separate claims: only graph-backed sessions that started "
          "after the hint went live show whether the hook delivers it.", "",
          "| Starting repo | Sessions | Graph resolved | Pre-hint: graph / hint seen | Post-hint: graph / hint seen |",
          "|---|---|---|---|---|"]
    for repo, row in sorted(a["by_repo"].items(), key=lambda kv: -kv[1]["sessions"]):
        L.append(f"| {repo} | {row['sessions']} | {row['graph']} | {row['graph_pre']} / {row['hinted_pre']} | "
                 f"{row['graph_post']} / {row['hinted_post']} |")
    L += ["", f"Hint live since: {a['hint_live_since'] or 'not configured'}; subagent hints injected: {a['subagent_hints']}."]
    for title, items in (("Graph-expected repo, no graph resolved (graph missing or resolver failing)", a["expected_graph_missing"]),
                         ("Graph resolved but no hint injected (hook failure)", a["hint_missing"])):
        if items:
            L += ["", f"**{title}:**", *[f"- {x}" for x in items]]
    L += ["", "## 2 Routing", "",
          f"Eligibility is a heuristic (a subagent of any type with >= {SUBAGENT_MIN_LOOKUPS} lookups, or a main "
          f"thread with >= {MAIN_MIN_SEARCHES} searches over >= {MAIN_MIN_FILES} files; shell searches and reads "
          "count with the paths they name). Each unit is judged against the graph of its own cwd, and "
          "only lookups inside that graph's coverage count. "
          f"{r['eligibility_unknown_sessions']} sessions with graph-backed units had no exploration unit by this rule: "
          "their eligibility is unknown, not zero demand.", "",
          f"- Graph-backed units: {r['graph_backed_units']}; exploration units: {r['exploration_units']}; "
          f"lookups/calls repeated by continued/forked transcripts, counted once: "
          f"{r['copied_tool_uses_excluded']}",
          f"- Subagents without a shell (graph unusable, not bypasses): {r['graph_unusable_units']}",
          f"- Units with enough lookups but no file/path evidence (eligibility unknown): "
          f"{r['lookup_units_without_path_evidence']}",
          f"- Queried graphify: {len(r['used_graphify'])} {r['used_graphify'] or ''}",
          f"- **Decisive line — after the hint went live: {r['after_hint_live']['used_graphify']}/"
          f"{r['after_hint_live']['exploration_units']} graph-backed exploration units queried graphify**",
          f"- Graphify calls from non-exploration units: {r['non_exploration_callers']}",
          f"- Skill tool selected graphify: {r['skill_selected_units']} units",
          f"- Explore-type subagents spawned: {r['explore_spawns']} ({r['explore_spawns_passed_graph']} prompts carried the graph/command)"]
    if r["after_hint_live"]["bypassed"]:
        L += ["", "**Bypassed after go-live (open these transcripts first):**",
              *[f"- {x}" for x in r["after_hint_live"]["bypassed"][:BYPASS_SHOWN]]]
    if r["graph_unusable"]:
        L += ["", "**Graph unusable (subagent definition grants no Bash/PowerShell):**",
              *[f"- {x}" for x in r["graph_unusable"][:BYPASS_SHOWN]]]
    if r["bypassed"]:
        shown = r["bypassed"][:BYPASS_SHOWN]
        L += ["", f"**Bypassed (routing failures to investigate this week; largest {len(shown)} of "
                  f"{len(r['bypassed'])}, full list in the JSON summary):**", *[f"- {x}" for x in shown]]
    L += ["", "## 3 Execution", "",
          f"- Calls: {e['calls']} by status {e['by_status']}; logged: {e['logged']}; bare CLI (outside wrapper): {e['bare_cli_calls']}; "
          f"copies from continued/forked transcripts excluded: {e['copied_calls_excluded']}; "
          f"calls made outside the window (in sessions overlapping it) excluded: "
          f"{e['calls_outside_window_excluded']}",
          f"- Test runs against a redirected GRAPHIFY_QUERY_LOG (not counted): {e['test_alt_log_calls']}"]
    for title, key in (("Completed wrapper calls with no log record", "unlogged_ok_wrapper_calls"),
                       ("Wrapper/query errors", "wrapper_errors"),
                       ("Shell errors never followed by a successful call", "unrecovered_shell_errors"),
                       ("Shell errors recovered by a later call (kept as evidence)", "recovered_shell_errors")):
        if e[key]:
            L += ["", f"**{title}:**", *[f"- {x}" for x in e[key]]]
    rr, st, ul = m["reranked"], m["stock"], m["unlabelled_bare_cli"]
    L += ["", "## 4 Measurement (organic only)", "",
          "| Arm | Records | Timed | Zero-node | Median ms | p90 ms |", "|---|---|---|---|---|---|",
          *[f"| {name} | {x['n']} | {x['timed']} | {x['zero_nodes']} | {_fmt_ms(x['median_ms'])} | "
            f"{_fmt_ms(x['p90_ms'])} |"
            for name, x in (("reranked", rr), ("stock (wrapper, rerank=false)", st),
                            ("unlabelled bare CLI (not an arm)", ul))], "",
          "Latency uses timed records only (a numeric, finite duration_ms); an untimed record "
          f"never counts as 0 ms. The latency verdict needs >= {m['min_reranked']} timed reranked records.", "",
          "Reranked and stock are both wrapper wall time. Bare-CLI records carry no `rerank` stamp and "
          "traversal-only timing, so they sit outside both arms. "
          f"Reranked latency ceiling: median {m['latency_ceiling_ms']['median']:.0f} / p90 {m['latency_ceiling_ms']['p90']:.0f} ms; "
          f"quality verdicts need >= {m['min_reranked']} reranked records. Excluded-session records: "
          f"{m['excluded_session_records']}; unattributed records (manual/test/unmatched): {m['unattributed_records']}.",
          "", "Latency and quality verdicts use organic records only; smoke and test runs never count toward them."]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=Path(__file__).with_name("graphify.local.json"))
    ap.add_argument("--projects", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--until", help="ISO timestamp (default: now)")
    ap.add_argument("--out", type=Path, help="write the markdown report here")
    ap.add_argument("--json", type=Path, help="write the JSON summary here")
    args = ap.parse_args(argv)

    try:
        raw = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"config unreadable: {args.config}: {exc}", file=sys.stderr)
        return 2
    try:
        cfg = parse_config(raw, args.config.resolve().parent)
    except ConfigError as exc:
        print(f"config invalid: {args.config}:", *[f"  - {x}" for x in exc.errors], sep="\n", file=sys.stderr)
        return 2
    resolver = load_resolver(cfg.skill_scripts)
    # Precedence: --log (relative to cwd, like any CLI path), then the config value (already
    # resolved against the config folder), then the env override used verbatim.
    env_log = os.environ.get("GRAPHIFY_QUERY_LOG")
    log_path = args.log or cfg.query_log or (Path(env_log) if env_log else None)
    until = parse_ts(args.until) if args.until else datetime.now(timezone.utc)
    if until is None:
        print(f"bad --until: {args.until}", file=sys.stderr)
        return 2
    since = until - timedelta(days=args.days)

    scan = discover_sessions(args.projects, since, until)
    summary = analyse(scan, load_log(log_path, since, until), resolver, cfg, since, until)
    report = render(summary)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    if not args.out:
        sys.stdout.buffer.write(report.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

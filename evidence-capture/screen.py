#!/usr/bin/env python3
"""Dependency screen: observed versus required, with ``observation_sufficient`` (plan §5.2).

``observed`` counts what the episode's tool calls touched, category by category.
``required`` is ``yes`` when the prompt names the dependency or the episode's edits
provably reuse content read in that category; ``no`` only when nothing was observed, the
prompt has no such reference and the observation was sufficient; otherwise ``unknown``.
A ``no`` means "nothing was detected", never "nothing was needed".

Shell commands go through ``shell_paths.parse_command``; any segment it cannot parse
makes the observation insufficient for the whole episode.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evidence-footprints"))
from shell_paths import parse_command  # noqa: E402

from common import norm_path, path_sha256  # noqa: E402
from manifest import DEP_CATEGORIES  # noqa: E402

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Glob", "Grep", "LSP"}
SHELL_TOOLS = {"Bash", "PowerShell"}
DELEGATE_TOOLS = {"Agent", "Task"}
SESSION_TOOLS = {"SendMessage", "ListAgents", "EnterWorktree", "ExitWorktree", "Workflow"}
NETWORK_TOOLS = {"WebFetch", "WebSearch"}
OUTWARD_TOOL_RE = re.compile(r"(?:^|__)(send_message|reply|forward|create_pr|publish|deploy|post_|send_)", re.I)
REMOTE_TOOL_RE = re.compile(r"(?:^|__)(github|gh_|jira|linear|slack|gmail|search_threads|get_thread|get_message|fetch)", re.I)
MIN_LINE = 16

GIT_LIVE = {"fetch", "pull", "ls-remote", "remote", "clone", "submodule"}
GIT_OUTWARD = {"push"}
GH_OUTWARD_RE = re.compile(r"\b(?:pr|issue|release)\s+(?:create|comment|merge|close|edit|review)\b|\bapi\b.*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--method)", re.I)
NET_VERBS = {"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "ssh", "scp", "rsync", "nc",
             "ping", "nslookup", "pip", "pip3", "npm", "npx", "pnpm", "yarn", "uv", "uvx", "cargo", "go", "dotnet",
             "winget", "choco", "docker", "kubectl", "terraform"}
DEPLOY_VERBS = {"deploy", "kubectl", "terraform", "vercel", "netlify", "az", "aws", "gcloud", "twine", "nuget"}
LOCAL_HOST_RE = re.compile(r"(?:https?://)?(?:localhost|127\.0\.0\.1|\[::1\]|[\w.-]+\.(?:localhost|test))(?::\d+)?\b", re.I)
URL_RE = re.compile(r"https?://[^\s'\")\]]+", re.I)
ORIGIN_REF_RE = re.compile(r"\b(?:origin|upstream)/[\w./-]+")
WORKTREE_SEG_RE = re.compile(r"/\.claude/worktrees/([^/]+)")
ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[^\\\s]+\\|/(?:home|Users|mnt|tmp|var|etc|opt)/)[^\s'\"`<>|]+")

PROMPT_REQUIRED_RE = {
    "live_remote": re.compile(r"\b(?:origin|upstream)/[\w./-]+|\b(?:fetch|pull)\b.*\b(?:origin|remote|upstream)\b|\bls-remote\b|\bPR\s*#?\d+|\bpull request\s*#?\d+|\bissue\s*#?\d+|\bgh\s+(?:pr|issue|api|run)\b", re.I),
    "outward_action": re.compile(r"\bpush(?:ed|ing)?\b|\bopen (?:a |the )?(?:pr|pull request)\b|\bcreate (?:a |the )?(?:pr|pull request|issue|release)\b|\bdeploy\b|\bsend (?:a |an |the )?(?:message|email|mail|reply)\b|\breply to\b|\bpublish\b", re.I),
    "other_session": re.compile(r"\bother session\b|\bthe (?:\w+ )?session(?:'s)? (?:work|worktree|branch)\b|\bsubagents?\b|\bteammates?\b|\bsend ?message\b|\bfan ?out\b", re.I),
    "network": re.compile(r"\bhttps?://|\bdownload\b|\bweb ?search\b|\bfetch the (?:page|url|docs)\b|\blook(?: it)? up online\b", re.I),
    "attachment": re.compile(r"\battached\b|\bthe attachment\b|\bthe (?:screenshot|image|pasted (?:text|content))\b|\bthis (?:screenshot|image)\b|\[Image", re.I),
    "external_path": ABS_PATH_RE,
    "external_repository": re.compile(r"\b(?:the |in )?other repo(?:sitory)?\b|\bfrom (?:the )?[\w-]+ repo\b", re.I),
    "memory_or_scratch": re.compile(r"\byour memory\b|\bremember(?:ed)? (?:from|that)\b|\bscratchpad\b|\btask output\b|\bMEMORY\.md\b", re.I),
}
# "the pasted text" names a dependency only when the paste is not inline in the prompt.
INLINE_PASTE_RE = re.compile(r"<pasted_content\b", re.I)
PASTE_PHRASE_RE = re.compile(r"\bthe pasted (?:text|content)\b", re.I)
DEICTIC_RE = re.compile(r"\b(?:as (?:above|before|discussed|we said|I said)|continue|carry on|go ahead|proceed|the plan we|that file|those files|same as|again|the previous|earlier (?:you|we)|like (?:before|last time)|now do|next,? do|also do|the other one|this one)\b", re.I)
NEXT_STEP_RE = re.compile(r"^\s*(?:yes|ok|okay|approved?|proceed|go|do it|continue|next|same)\b[.!]?\s*$", re.I)


class RepoIndex:
    """Cheap, cached "which repository owns this path" lookup by walking up for ``.git``."""

    def __init__(self, repo_root: Path) -> None:
        self.root = norm_path(repo_root).lower()
        self.main_root = WORKTREE_SEG_RE.sub("", self.root) if "/.claude/worktrees/" in self.root else self.root
        self._cache: dict[str, str | None] = {}

    def owner(self, path: str) -> str | None:
        p = norm_path(path).lower()
        if p in self._cache:
            return self._cache[p]
        cur = Path(p)
        found: str | None = None
        try:
            for cand in (cur, *cur.parents):
                if (cand / ".git").exists():
                    found = norm_path(cand).lower()
                    break
        except OSError:
            found = None
        self._cache[p] = found
        return found

    def inside(self, path: str) -> bool:
        p = norm_path(path).lower()
        return p == self.root or p.startswith(self.root + "/")

    def relative(self, path: str) -> str | None:
        p = norm_path(path)
        low = p.lower()
        if low.startswith(self.root + "/"):
            return p[len(self.root) + 1:]
        return None


def _home_markers() -> list[str]:
    home = norm_path(Path.home()).lower()
    return [home + "/.claude/projects", home + "/.claude/memory", home + "/.claude/todos", home + "/.claude/tasks",
            home + "/appdata/local/temp/claude", "/tmp/claude"]


def _resolve_dots(p: str) -> str:
    """Collapse ``.`` and ``..`` segments in one pass; a ``..`` above the anchor is
    dropped (it cannot climb past a drive, the root or a UNC ``//server/share``)."""
    if p.startswith("//"):
        parts = p[2:].split("/", 2)
        head, sep, rest = "//" + "/".join(parts[:2]), "/", (parts[2] if len(parts) > 2 else "")
        if len(parts) < 3:
            return head
    else:
        head, sep, rest = p.partition("/")
    out: list[str] = []
    for seg in rest.split("/"):
        if seg == ".":
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return head + sep + "/".join(out)


def classify_path(path: str, idx: RepoIndex, cwd: str | None, markers: list[str]) -> str | None:
    """Category of one path use, or None when it is inside the repository."""
    raw = path.strip().strip("'\"")
    if not raw:
        return None
    if raw.startswith("~"):
        raw = norm_path(Path.home()) + raw[1:]
    raw = raw.replace("\\", "/")
    ext = re.match(r"^//[?.]/(unc/)?", raw, re.IGNORECASE)  # Win32 extended-length / device prefixes, UNC form included
    if ext:
        raw = ("//" if ext.group(1) else "") + raw[ext.end():]
    base = cwd or idx.root
    rel_drive = re.match(r"^([A-Za-z]):(?!/)", raw)  # C:x is relative to that drive's own cwd
    if rel_drive:
        if base[:2].lower() != rel_drive.group(1).lower() + ":":
            return "external_path"  # another drive's cwd is unknown, and the repo is not on it
        raw = f"{base}/{raw[2:]}"
    elif not re.match(r"^(?:[A-Za-z]:/|/)", raw):
        raw = f"{base}/{raw}"
    p = norm_path(raw).lower()
    p = _resolve_dots(p)
    if any(p == m or p.startswith(m + "/") for m in markers):
        return "memory_or_scratch"
    # A lane's worktree lives inside the main checkout; it is another session's, not this repo's.
    if p.startswith(idx.root + "/.claude/worktrees/"):
        return "other_session"
    if p == idx.root or p.startswith(idx.root + "/"):
        return None
    if "/.claude/worktrees/" in p and p.startswith(idx.main_root + "/"):
        return "other_session"
    owner = idx.owner(p)
    if owner and owner != idx.root:
        return "external_repository"
    return "external_path"


GLOB_META_RE = re.compile(r"[*?\[{]")


def _glob_anchor(pattern: str, base: str | None) -> str:
    """The directory a Glob pattern searches from: its literal leading segments, resolved
    against the call's ``path`` when the pattern is relative. An absolute pattern (or one
    climbing with ``..``) ignores ``path``, so it is the pattern that must be classified."""
    pat = pattern.strip().strip("'\"").replace("\\", "/")
    literal: list[str] = []
    for seg in pat.split("/"):
        if GLOB_META_RE.search(seg):
            break
        literal.append(seg)
    anchor = "/".join(literal)
    if re.match(r"^(?:[A-Za-z]:|/|~)", anchor) or anchor.startswith("//"):
        return anchor
    if base:
        return f"{base}/{anchor}" if anchor else base
    return anchor or "."


def _lines(text: str | None) -> set[str]:
    if not text:
        return set()
    return {ln.strip() for ln in text.splitlines() if len(ln.strip()) >= MIN_LINE}


def _edit_content(inp: dict[str, Any]) -> str:
    parts = [inp.get("content"), inp.get("new_string"), inp.get("new_source")]
    for e in inp.get("edits") or []:
        if isinstance(e, dict):
            parts.append(e.get("new_string"))
    return "\n".join(str(p) for p in parts if isinstance(p, str))


def _shell_observations(cmd: str, idx: RepoIndex, cwd: str | None, markers: list[str]) -> tuple[dict[str, list[str]], bool]:
    """Categories observed in one shell command, and whether it parsed without a miss."""
    obs: dict[str, list[str]] = {c: [] for c in DEP_CATEGORIES}
    segments, uses = parse_command(cmd)
    clean = True
    for seg in segments:
        if seg.kind == "miss":
            clean = False
        verb = (seg.verb or "").lower()
        text = seg.text
        if verb == "git":
            sub = re.match(r"\s*git\s+(?:-C\s+\S+\s+|-c\s+\S+\s+|--\S+\s+)*(\S+)", text)
            g = sub.group(1).lower() if sub else ""
            if g in GIT_OUTWARD:
                obs["outward_action"].append(f"git {g}")
            elif g in GIT_LIVE and not (g == "remote" and re.search(r"\bremote\s+(?:-v|show)\b", text)):
                obs["live_remote"].append(f"git {g}")
            if ORIGIN_REF_RE.search(text):
                obs["live_remote"].append("origin/* ref")
            if g == "worktree" and re.search(r"\bworktree\s+add\b", text):
                obs["other_session"].append("git worktree add")
        elif verb == "gh":
            if GH_OUTWARD_RE.search(text):
                obs["outward_action"].append("gh write")
            else:
                obs["live_remote"].append("gh read")
        elif verb in DEPLOY_VERBS and verb not in ("kubectl", "terraform"):
            obs["outward_action"].append(verb)
        elif verb in NET_VERBS:
            urls = URL_RE.findall(text)
            if urls and all(LOCAL_HOST_RE.match(u) for u in urls):
                pass
            elif verb in ("pip", "pip3", "npm", "npx", "pnpm", "yarn", "uv", "uvx", "cargo", "go", "dotnet", "winget", "choco") \
                    and not re.search(r"\b(?:install|add|update|upgrade|download|fetch|sync|publish|get)\b", text):
                pass
            else:
                obs["network"].append(verb)
        elif verb == "claude" or verb == "evidence":
            obs["other_session"].append(verb)
        for use in seg.paths:
            cat = classify_path(use.path, idx, cwd, markers)
            if cat:
                obs[cat].append(f"{use.role}:{use.path[:80]}")
    for use in uses:
        cat = classify_path(use.path, idx, cwd, markers)
        if cat:
            obs[cat].append(f"{use.role}:{use.path[:80]}")
    return obs, clean


def screen(m: dict[str, Any], obs: dict[str, Any], repo_root: Path, cwd: str | None = None,
           untracked_paths: Iterable[str] = (), complete: bool = True) -> dict[str, Any]:
    """Fill deps, edited_files, prior_context and observation in ``m`` from ``obs`` (join output)."""
    idx = RepoIndex(repo_root)
    markers = _home_markers()
    turn = obs["turn"]
    prompt = turn["prompt_text"] or ""
    untracked = {norm_path(p).lower() for p in untracked_paths}
    observed: dict[str, list[str]] = {c: [] for c in DEP_CATEGORIES}
    read_lines: dict[str, set[str]] = {c: set() for c in DEP_CATEGORIES}
    edit_lines: set[str] = set()
    edited: list[str] = []
    causes = list(obs.get("causes", []))
    if not complete:
        causes.append("transcript_partial")
    first_edit_seen = False

    for call in obs["tool_calls"]:
        name, inp = call["name"], call["input"]
        cats_here: list[str] = []
        # A delegated call resolves relative paths against its own agent's cwd.
        call_cwd = (call.get("cwd") or cwd) if call.get("source", "main") != "main" else cwd
        if name in SHELL_TOOLS:
            cmd = str(inp.get("command") or "")
            sobs, clean = _shell_observations(cmd, idx, call_cwd, markers)
            if not clean:
                causes.append("shell_path_miss")
            for c, items in sobs.items():
                if items:
                    observed[c].extend(items)
                    cats_here.append(c)
            if re.search(r"\b(?:git\s+(?:add|commit|apply|checkout|restore|mv|rm)|>\s*\S|tee\s|sed\s+-i|Set-Content|Out-File)\b", cmd):
                first_edit_seen = True
        elif name in EDIT_TOOLS or name in READ_TOOLS:
            p = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
            if isinstance(p, str):
                cat = classify_path(p, idx, call_cwd, markers)
                if cat:
                    observed[cat].append(f"{name}:{p[:80]}")
                    cats_here.append(cat)
                elif name in EDIT_TOOLS:
                    rel = idx.relative(p) or idx.relative(f"{call_cwd or idx.root}/{p}")
                    if rel and rel not in edited:
                        edited.append(rel)
                    first_edit_seen = True
                elif name in READ_TOOLS and not first_edit_seen:
                    rel = idx.relative(p)
                    if rel and rel.lower() in untracked:
                        observed["untracked_input"].append(f"{name}:{rel[:80]}")
                        cats_here.append("untracked_input")
            if name == "Glob" and isinstance(inp.get("pattern"), str):
                anchor = _glob_anchor(inp["pattern"], p if isinstance(p, str) else None)
                cat = classify_path(anchor, idx, call_cwd, markers) if anchor else None
                if cat:
                    observed[cat].append(f"{name}:{inp['pattern'][:80]}")
                    cats_here.append(cat)
            if name in EDIT_TOOLS:
                edit_lines |= _lines(_edit_content(inp))
        elif name in DELEGATE_TOOLS or name in SESSION_TOOLS:
            observed["other_session"].append(name)
            cats_here.append("other_session")
        elif name in NETWORK_TOOLS:
            observed["network"].append(name)
            cats_here.append("network")
        elif OUTWARD_TOOL_RE.search(name):
            observed["outward_action"].append(name)
            cats_here.append("outward_action")
        elif REMOTE_TOOL_RE.search(name) or name.startswith("mcp__"):
            observed["live_remote" if REMOTE_TOOL_RE.search(name) else "network"].append(name)
            cats_here.append("live_remote" if REMOTE_TOOL_RE.search(name) else "network")
        for c in set(cats_here):
            read_lines[c] |= _lines(call.get("result_text"))

    if m["prompt"].get("has_attachment"):
        observed["attachment"].append("prompt attachment")
    sufficient = complete and not causes and not m["prompt"].get("has_attachment")
    for c in DEP_CATEGORIES:
        if c == "prior_conversation":
            continue
        ev: list[dict[str, str]] = []
        named = PROMPT_REQUIRED_RE.get(c) and PROMPT_REQUIRED_RE[c].search(
            PASTE_PHRASE_RE.sub("", prompt) if c == "attachment" and INLINE_PASTE_RE.search(prompt) else prompt)
        if named:
            ev.append({"kind": "prompt_names_dependency", "detail": c})
        overlap = edit_lines & read_lines[c]
        if overlap:
            ev.append({"kind": "edit_depends_on_read", "detail": f"{len(overlap)} shared lines"})
        if c == "attachment" and m["prompt"].get("has_attachment"):
            ev.append({"kind": "prompt_attachment", "detail": "attachment present"})
        if ev:
            required = "yes"
        elif not observed[c] and sufficient:
            required = "no"
        else:
            required = "unknown"
        m["deps"][c] = {"observed": len(observed[c]), "required": required,
                        "evidence": ev + [{"kind": "observed", "detail": d} for d in observed[c][:20]],
                        "observation_sufficient": sufficient}

    # prior conversation
    pc_ev: list[dict[str, str]] = []
    index = m["prompt"].get("index_in_session")
    if index == 0:
        pc_ev.append({"kind": "first_prompt", "detail": "index_in_session=0"})
    else:
        earlier_paths = {norm_path(p).lower() for p in turn.get("earlier_edit_paths", [])}
        # A named path matches an earlier artifact only on whole segments: ``a.py`` names
        # ``.../a.py`` and ``.../src/a.py``, never ``.../data.py``.
        named = [p for p in re.findall(r"[\w./\\-]+\.[A-Za-z0-9]{1,8}\b", prompt)
                 if any(ep == norm_path(p).lower() or ep.endswith("/" + re.sub(r"^(?:\.{1,2}/)+", "", norm_path(p).lower()))
                        for ep in earlier_paths)]
        if named:
            pc_ev.append({"kind": "prompt_names_earlier_artifact", "detail": f"{len(named)} path(s)"})
        earlier_lines: set[str] = set()
        for t in turn.get("earlier_result_text", []):
            earlier_lines |= _lines(t)
        dep = edit_lines & earlier_lines
        if dep:
            pc_ev.append({"kind": "edit_depends_on_earlier_output", "detail": f"{len(dep)} shared lines"})
        if DEICTIC_RE.search(prompt) or NEXT_STEP_RE.match(prompt):
            pc_ev.append({"kind": "prompt_reference", "detail": "deictic phrase"})
    kinds = {e["kind"] for e in pc_ev}
    if kinds & {"prompt_names_earlier_artifact", "edit_depends_on_earlier_output"}:
        pc_required = "yes"
    elif "first_prompt" in kinds and complete:
        pc_required = "no"
    elif "prompt_reference" in kinds or not complete or not sufficient:
        pc_required = "unknown"
    else:
        pc_required = "no"  # hash-anchored, no overlap, sufficient observation
    m["prior_context"].update(prior_conversation_required=pc_required, prior_context_evidence=pc_ev)
    m["deps"]["prior_conversation"] = {"observed": 1 if index else 0, "required": pc_required,
                                       "evidence": pc_ev, "observation_sufficient": sufficient}
    m["edited_files"] = [{"path_sha256": path_sha256(p)} for p in edited]
    m["observation"] = {"sufficient": sufficient, "causes": sorted(set(causes))}
    m["task_class"]["mechanical"] = _task_class(edited, obs["tool_calls"])
    return m


def _task_class(edited: list[str], calls: list[dict[str, Any]]) -> str:
    if not edited:
        return "investigation" if calls else "other"
    docs = sum(1 for p in edited if p.lower().endswith((".md", ".rst", ".txt")))
    if docs == len(edited):
        return "docs_change"
    if any(c["name"] in SHELL_TOOLS and re.search(r"\b(?:git push|deploy|release|publish)\b", str(c["input"].get("command", "")))
           for c in calls):
        return "ops_or_release"
    return "code_change"

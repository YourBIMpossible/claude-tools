#!/usr/bin/env python3
"""Join a capture episode to its transcript turn (plan §5.1).

A lean transcript reader with the same event rules as ``build_episodes.py`` (a queued
enqueue is a hash-only candidate; a queued command attachment is a prompt; a
``hook_additional_context`` carrying ``<context_brief packet_id=...>`` is a brief; a user
entry with an ``origin`` dict is a prompt of that kind, a meta entry without one is a
candidate, anything else is a human prompt). Nothing here imports the Evidence Compiler.

The turn is the prompt whose hash equals the manifest's ``prompt.sha256``; when several
prompts share the hash the one nearest ``started_at`` wins, deterministically. The
episode ends at the next delivered human prompt (``ended_by = prompt``) or at the
transcript's end (``stop`` / ``session_end``). Task notifications, peer messages, meta and
hook prompts, and prompts queued mid-turn are continuations, never a boundary.

Delegated work is part of the turn: every Agent/Task call (and every SendMessage that
resumes a known subagent) is joined to its ``subagents/agent-<id>.jsonl`` transcript,
recursively for nested agents. A delegation whose transcript is not preserved leaves the
observation insufficient (``missing_subagent_transcript``); it is never treated as clean.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common import parse_iso, sha256_text

BRIEF_TAG_RE = re.compile(r'<context_brief packet_id="(ep_[0-9a-f]{8,64})"')
TASK_NOTIFICATION_RE = re.compile(r"^\s*<task-notification>", re.I)
TRUNCATED_RE = re.compile(r"\[(?:output |result )?truncated|… \(\d+ more|\(truncated\)|<truncated", re.I)
ATTACHMENT_BLOCKS = {"image", "document", "file"}
# Content the captured prompt does not contain: media, attached files and paste placeholders.
# An inline ``<pasted_content>`` block is part of the prompt text itself, so it is not one.
PASTED_RE = re.compile(r"<attached_file|\[Image #?\d+\]|\[Pasted text", re.I)
HUMAN_ORIGINS = {"user", "human"}
CONTINUATION_ORIGINS = {"queued_command:prompt", "task_notification", "hook", "meta", "peer"}
DELEGATE_CALLS = ("Agent", "Task")
AGENT_ID_RE = re.compile(r"\bagentId:\s*([A-Za-z0-9_-]{6,64})")
# Envelopes the transcript wraps a hook prompt in; the hook received exactly ``hook``.
PEER_ENVELOPE_RE = re.compile(
    r"^(?:Another Claude session sent a message:\n)?(?P<hook><(agent-message|cross-session-message)\b[^>]*>\n.*\n</\2>)"
    r"(?:\n\n[\s\S]*)?$", re.S)
COMMAND_ENVELOPE_RE = re.compile(
    r"^<command-message>[^<]*</command-message>\n<command-name>(?P<name>/[^<\s]+)</command-name>"
    r"(?:\n<command-args>(?P<args>[\s\S]*?)</command-args>)?\s*$")


def normalize_origin(origin: Any, is_meta: bool, text: str, command_mode: str | None = None,
                     queued: bool = False) -> str:
    """One origin label from the transcript's own fields, most specific first.

    ``origin.kind`` wins (human/user -> ``human``, ``task-notification`` -> ``task_notification``,
    peer, ...); then a queued command's ``commandMode``; then a ``<task-notification>`` body;
    then the meta flag. A queued prompt with none of these is a legacy record whose sender is
    unknown and keeps ``queued_command:prompt``; an unqueued plain prompt is human."""
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if isinstance(kind, str) and kind:
        k = kind.replace("-", "_").lower()
        return "human" if k in HUMAN_ORIGINS else k
    if command_mode and command_mode != "prompt":
        return command_mode.replace("-", "_").lower()
    if TASK_NOTIFICATION_RE.search(text):
        return "task_notification"
    if is_meta:
        return "meta"
    return "queued_command:prompt" if queued else "human"


@dataclass
class Event:
    idx: int
    kind: str  # prompt | candidate | brief | assistant | tool_result | version
    ts: str | None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Transcript:
    path: Path
    events: list[Event]
    decode_errors: int
    versions: list[str]
    session_id: str | None


def _prompt_text(content: Any) -> tuple[str | None, bool]:
    """(prompt text, has_attachment) of a user entry; text None when not a prompt."""
    if isinstance(content, str):
        return content, bool(PASTED_RE.search(content))
    if not isinstance(content, list):
        return None, False
    texts: list[str] = []
    attach = False
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "tool_result":
            return None, False
        if t == "text":
            texts.append(str(block.get("text") or ""))
        elif t in ATTACHMENT_BLOCKS:
            attach = True
    text = "\n".join(texts) if texts else None
    if text is not None and PASTED_RE.search(text):
        attach = True
    return text, attach


def _result_text(block: dict[str, Any]) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(str(b.get("text") or "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_transcript(path: Path, sidechain: bool = False) -> Transcript:
    """Events of one transcript. The main transcript skips ``isSidechain`` entries (they
    belong to subagent files); a subagent transcript (``sidechain=True``) keeps them."""
    events: list[Event] = []
    errors = 0
    versions: list[str] = []
    session: str | None = None
    with path.open(encoding="utf-8", errors="surrogateescape") as fh:
        for idx, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            if not isinstance(entry, dict):
                errors += 1
                continue
            v = entry.get("version")
            if isinstance(v, str) and v not in versions:
                versions.append(v)
            if session is None and isinstance(entry.get("sessionId"), str):
                session = entry["sessionId"]
            if entry.get("isSidechain") and not sidechain:
                continue
            ts = entry.get("timestamp")
            etype = entry.get("type")
            if etype == "queue-operation":
                text, _ = _prompt_text(entry.get("content"))
                if entry.get("operation") == "enqueue" and text is not None:
                    events.append(Event(idx, "candidate", ts, {"sha256": sha256_text(text), "origin": "queue_enqueue"}))
                continue
            if etype == "attachment":
                att = entry.get("attachment") or {}
                if att.get("type") == "queued_command":
                    text, attach = _prompt_text(att.get("prompt"))
                    if text is not None:
                        origin = normalize_origin(att.get("origin"), bool(att.get("isMeta") or entry.get("isMeta")),
                                                  text, att.get("commandMode"), queued=True)
                        events.append(Event(idx, "prompt", ts, {
                            "sha256": sha256_text(text), "text": text, "uuid": entry.get("uuid"),
                            "origin": origin, "attachment": attach, "queued": True}))
                    continue
                if att.get("type") != "hook_additional_context":
                    continue
                content = att.get("content")
                for part in (content if isinstance(content, list) else [content]):
                    if not isinstance(part, str):
                        continue
                    for m in BRIEF_TAG_RE.finditer(part):
                        events.append(Event(idx, "brief", ts, {"packet_id": m.group(1), "sha256": sha256_text(part),
                                                               "len": len(part), "text": part}))
                continue
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            if etype == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            text = _result_text(block)
                            tur = entry.get("toolUseResult")
                            agent = tur.get("agentId") if isinstance(tur, dict) else None
                            if not isinstance(agent, str):
                                am = AGENT_ID_RE.search(text)
                                agent = am.group(1) if am else None
                            events.append(Event(idx, "tool_result", ts, {
                                "tool_use_id": block.get("tool_use_id"), "is_error": bool(block.get("is_error")),
                                "text": text, "truncated": bool(TRUNCATED_RE.search(text)), "agent_id": agent}))
                if entry.get("isCompactSummary"):
                    continue
                text, attach = _prompt_text(content)
                if text is None:
                    continue
                origin = entry.get("origin")
                o = normalize_origin(origin, bool(entry.get("isMeta")), text)
                kind = "prompt" if isinstance(origin, dict) or not entry.get("isMeta") else "candidate"
                events.append(Event(idx, kind, ts, {"sha256": sha256_text(text), "text": text,
                                                    "uuid": entry.get("uuid"), "origin": o, "attachment": attach}))
            elif etype == "assistant":
                tools = [{"id": b.get("id"), "name": b.get("name"), "input": b.get("input")}
                         for b in (msg.get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_use"]
                events.append(Event(idx, "assistant", ts, {"message_id": msg.get("id"), "tools": tools,
                                                           "cwd": entry.get("cwd")}))
    return Transcript(path, events, errors, versions, session)


def transcript_for(transcript_path: str | None, project_dir: Path | None, session_id: str) -> Path | None:
    """The transcript path the hook reported, else ``<project_dir>/<session_id>.jsonl``."""
    if transcript_path and Path(transcript_path).is_file():
        return Path(transcript_path)
    if project_dir is not None:
        cand = project_dir / f"{session_id}.jsonl"
        if cand.is_file():
            return cand
    return None


def subagent_transcripts(transcript: Path) -> list[Path]:
    d = transcript.with_suffix("") / "subagents"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


class SubagentIndex:
    """The session's preserved subagent transcripts, by agent id and by spawning tool-use id.

    ``agent-<id>.jsonl`` names the agent; its ``agent-<id>.meta.json`` ``toolUseId`` names the
    Agent call that spawned it. Parsed lazily, once."""

    def __init__(self, transcript: Path) -> None:
        self.by_agent: dict[str, Path] = {}
        self.by_tool_use: dict[str, Path] = {}
        self._parsed: dict[Path, Transcript] = {}
        for p in subagent_transcripts(transcript):
            self.by_agent[agent_id_of(p)] = p
            meta = p.with_suffix(".meta.json")
            try:
                tool_use = json.loads(meta.read_text(encoding="utf-8")).get("toolUseId") if meta.is_file() else None
            except (OSError, ValueError, AttributeError):
                tool_use = None
            if isinstance(tool_use, str):
                self.by_tool_use[tool_use] = p

    def get(self, path: Path) -> Transcript:
        if path not in self._parsed:
            self._parsed[path] = parse_transcript(path, sidechain=True)
        return self._parsed[path]


def agent_id_of(path: Path) -> str:
    return path.stem[len("agent-"):] if path.stem.startswith("agent-") else path.stem


@dataclass
class Delegation:
    """Subagent activity referenced from inside a turn's bounds: each joined transcript with
    the events kept for this turn."""
    transcripts: list[tuple[Transcript, list[Event]]] = field(default_factory=list)
    unresolved: int = 0
    beyond_turn: int = 0


def _in_window(ev: Event, lo: Any, hi: Any) -> bool:
    """Missing timestamps are kept: over-inclusion can only add observations."""
    ts = parse_iso(ev.ts)
    return ts is None or ((lo is None or ts >= lo) and (hi is None or ts <= hi))


def referenced_subagents(tr: Transcript, start: int, end: int, index: SubagentIndex) -> Delegation:
    """Join every delegation inside ``[start, end)`` to its preserved subagent transcript.

    An Agent/Task call resolves by the meta file's ``toolUseId``, else by the ``agentId`` its
    result reports; a SendMessage resolves when ``to`` is a known agent id. Subagent events
    are kept from the referencing call to the turn's end (to the transcript's end when the
    turn ran to ``stop``); nested delegations inside them resolve the same way. Each
    Agent/Task call with no preserved transcript counts as unresolved; an agent still active
    after the turn ended, and never referenced again, counts as ``beyond_turn``."""
    out = Delegation()
    hi = parse_iso(tr.events[end].ts) if end < len(tr.events) else None
    results = {ev.data["tool_use_id"]: ev.data for ev in tr.events[start:end] if ev.kind == "tool_result"}
    later_refs = {t["input"].get("to") for ev in tr.events[end:] if ev.kind == "assistant"
                  for t in ev.data["tools"] if t["name"] == "SendMessage" and isinstance(t["input"], dict)}
    later_refs |= {ev.data.get("agent_id") for ev in tr.events[end:] if ev.kind == "tool_result"}
    seen: set[Path] = set()
    pending: list[tuple[list[Event], dict[str, dict[str, Any]]]] = [(tr.events[start:end], results)]
    while pending:
        events, res = pending.pop(0)
        for ev in events:
            if ev.kind != "assistant":
                continue
            for tool in ev.data["tools"]:
                inp = tool["input"] if isinstance(tool["input"], dict) else {}
                path: Path | None = None
                if tool["name"] in DELEGATE_CALLS:
                    path = index.by_tool_use.get(tool["id"])
                    if path is None:
                        agent = (res.get(tool["id"]) or {}).get("agent_id")
                        path = index.by_agent.get(agent) if agent else None
                    if path is None:
                        out.unresolved += 1
                        continue
                elif tool["name"] == "SendMessage" and isinstance(inp.get("to"), str):
                    path = index.by_agent.get(inp["to"])
                if path is None or path in seen:
                    continue
                seen.add(path)
                sub = index.get(path)
                lo = parse_iso(ev.ts)
                kept = [e for e in sub.events if _in_window(e, lo, hi)]
                if hi is not None and agent_id_of(path) not in later_refs and any(
                        (t := parse_iso(e.ts)) is not None and t > hi for e in sub.events):
                    out.beyond_turn += 1
                out.transcripts.append((sub, kept))
                pending.append((kept, {e.data["tool_use_id"]: e.data for e in sub.events if e.kind == "tool_result"}))
    return out


CONTAINED_WINDOW_S = 120
# The hook's ``started_at`` and the transcript's delivery timestamp are the same moment, a
# hook's run time apart. An identical prompt further away is an earlier turn, not this one.
EXACT_WINDOW_S = 600


def envelope_payloads(text: str) -> list[str]:
    """What the hook received for a recognized transcript envelope; [] for anything else.

    A peer message is wrapped in an optional header line and a trailing note; the hook got
    the ``<agent-message>``/``<cross-session-message>`` element. A slash command is
    expanded to ``<command-message>``/``<command-name>``/``<command-args>``; the hook got
    ``/name`` or ``/name args``. Arbitrary text is never searched."""
    m = PEER_ENVELOPE_RE.match(text)
    if m:
        return [m.group("hook")]
    m = COMMAND_ENVELOPE_RE.match(text)
    if m:
        name, args = m.group("name"), (m.group("args") or "").strip()
        return [f"{name} {args}", name] if args else [name]
    return []


def find_turn(tr: Transcript, prompt_sha256: str, started_at: str,
              prompt_len: int | None = None, misses: list[str] | None = None) -> tuple[int, str] | None:
    """(index, match) of the event carrying the hook's prompt, nearest to ``started_at``.

    ``exact``: a prompt (or meta prompt) event whose whole text has the hook's hash, within
    EXACT_WINDOW_S of ``started_at`` (an event without a timestamp is kept, as elsewhere:
    nothing is known about its distance). Queue enqueue records carry no turn and are
    never matched.
    ``contained``: the transcript wrapped the hook's prompt in a recognized envelope
    (:func:`envelope_payloads`); the payload must have the hook's exact length and hash,
    lie within CONTAINED_WINDOW_S, and be the only such event. Two or more is ambiguous and
    nothing is joined (``misses`` gets ``prompt_ambiguous``)."""
    start = parse_iso(started_at)

    def dist(ev: Event) -> float:
        ts = parse_iso(ev.ts)
        return abs((ts - start).total_seconds()) if ts and start else float("inf")

    def near(ev: Event, window: float) -> bool:
        return parse_iso(ev.ts) is None or start is None or dist(ev) <= window

    for kinds in (("prompt",), ("candidate",)):
        hits = [(dist(ev), i) for i, ev in enumerate(tr.events)
                if ev.kind in kinds and "text" in ev.data and ev.data["sha256"] == prompt_sha256
                and near(ev, EXACT_WINDOW_S)]
        if hits:
            return min(hits)[1], "exact"
    if not prompt_len:
        return None
    hits = [i for i, ev in enumerate(tr.events)
            if ev.kind in ("prompt", "candidate") and isinstance(ev.data.get("text"), str)
            and dist(ev) <= CONTAINED_WINDOW_S
            and any(len(p) == prompt_len and sha256_text(p) == prompt_sha256
                    for p in envelope_payloads(ev.data["text"]))]
    if len(hits) > 1:
        if misses is not None:
            misses.append("prompt_ambiguous")
        return None
    return (hits[0], "contained") if hits else None


def is_boundary(ev: Event) -> bool:
    """A human prompt delivered between turns ends the episode; nothing else does."""
    return ev.kind == "prompt" and not ev.data.get("queued") and ev.data["origin"] not in CONTINUATION_ORIGINS


def window_end(tr: Transcript, start: int) -> tuple[int, str]:
    for j in range(start + 1, len(tr.events)):
        if is_boundary(tr.events[j]):
            return j, "prompt"
    return len(tr.events), "stop"


def observe(tr: Transcript, start: int, end: int, delegation: Delegation | None = None) -> dict[str, Any]:
    """Tool calls with results inside the bounds (delegated ones included), plus the
    observation-quality facts."""
    delegation = delegation or Delegation()
    results = {ev.data["tool_use_id"]: ev.data for ev in tr.events[start:end] if ev.kind == "tool_result"}
    for sub, _ in delegation.transcripts:
        results.update({ev.data["tool_use_id"]: ev.data for ev in sub.events if ev.kind == "tool_result"})
    calls: list[dict[str, Any]] = []
    continuations: list[dict[str, Any]] = []
    briefs: list[dict[str, Any]] = []
    assistant_turns = 0

    def add(events: list[Event], source: str) -> None:
        for ev in events:
            if ev.kind != "assistant":
                continue
            for tool in ev.data["tools"]:
                r = results.get(tool["id"])
                calls.append({"seq": len(calls), "source": source, "tool_use_id": tool["id"], "name": tool["name"],
                              "input": tool["input"] if isinstance(tool["input"], dict) else {},
                              "timestamp": ev.ts, "cwd": ev.data.get("cwd"), "is_error": r["is_error"] if r else None,
                              "result_text": r["text"] if r else None,
                              "result_truncated": r["truncated"] if r else None, "result_missing": r is None})

    for ev in tr.events[start + 1:end]:
        if ev.kind == "assistant":
            assistant_turns += 1
        elif ev.kind == "prompt":
            continuations.append({"origin": ev.data["origin"], "sha256": ev.data["sha256"],
                                  "queued": bool(ev.data.get("queued"))})
        elif ev.kind == "candidate" and ev.data.get("origin") == "task_notification":
            continuations.append({"origin": "task_notification", "sha256": ev.data["sha256"]})
        elif ev.kind == "brief":
            briefs.append(ev.data)
    add(tr.events[start:end], "main")
    for sub, kept in delegation.transcripts:
        add(kept, sub.path.stem)
    causes: list[str] = []
    if any(c["result_truncated"] for c in calls):
        causes.append("truncated_result")
    if any(c["result_missing"] for c in calls):
        causes.append("missing_result")
    if delegation.unresolved:
        causes.append("missing_subagent_transcript")
    if delegation.beyond_turn:
        causes.append("subagent_beyond_turn")
    first = tr.events[start].data if start < len(tr.events) else {}
    if any(c["origin"] in HUMAN_ORIGINS and c["queued"] for c in continuations) or (
            first.get("queued") and first.get("origin") in HUMAN_ORIGINS):
        causes.append("interleaved_human_prompt")
    if any(sub.decode_errors for sub, _ in delegation.transcripts):
        causes.append("decode_errors")
    if assistant_turns == 0:
        causes.append("no_response")
    if tr.decode_errors:
        causes.append("decode_errors")
    return {"tool_calls": calls, "continuations": continuations, "briefs": briefs,
            "assistant_turns": assistant_turns, "causes": causes, "turn_end_ts": tr.events[end - 1].ts if end > start else None}


def join(m: dict[str, Any], transcript: Path, misses: list[str] | None = None) -> dict[str, Any] | None:
    """Locate the manifest's turn in ``transcript``; None when the prompt is not there.

    Fills prompt.uuid/origin/index_in_session/has_attachment, prior_context facts, brief
    hashes for the linked packet (if any brief event carries the same packet id, or the
    only brief inside the bounds) and returns the observation dict for the screen.
    """
    tr = parse_transcript(transcript)
    found = find_turn(tr, m["prompt"]["sha256"], m["started_at"], m["prompt"].get("len"), misses)
    if found is None:
        return None
    start, match = found
    ev = tr.events[start]
    end, ended_by = window_end(tr, start)
    delegation = referenced_subagents(tr, start, end, SubagentIndex(transcript))
    obs = observe(tr, start, end, delegation)
    human_before = sum(1 for e in tr.events[:start] if e.kind == "prompt" and e.data["origin"] in HUMAN_ORIGINS)
    m["prompt_uuid"] = ev.data.get("uuid")
    m["prompt"].update(origin=ev.data["origin"], index_in_session=human_before, has_attachment=bool(ev.data.get("attachment")))
    m["prior_context"]["has_prior_conversation"] = human_before > 0
    m["ended_by"] = m.get("ended_by") or ended_by
    if tr.versions:
        m["boundary"]["cli_version"] = tr.versions[-1]
    brief = None
    for b in obs["briefs"]:
        if m["link"].get("packet_id") and b["packet_id"] == m["link"]["packet_id"]:
            brief = b
    if brief is None and len(obs["briefs"]) == 1:
        brief = obs["briefs"][0]
    if brief is not None:
        m["link"].update(brief_sha256=brief["sha256"], brief_len=brief["len"], brief_empty=brief["len"] == 0)
    obs["turn"] = {"start": start, "end": end, "prompt_text": ev.data["text"],
                   "brief_text": brief["text"] if brief is not None else None,
                   "earlier_result_text": [e.data["text"] for e in tr.events[:start] if e.kind == "tool_result"],
                   "earlier_prompt_text": [e.data["text"] for e in tr.events[:start] if e.kind == "prompt"],
                   "earlier_edit_paths": _edit_paths(tr.events[:start])}
    obs["subagent_count"] = len(delegation.transcripts)
    obs["subagent_unresolved"] = delegation.unresolved
    obs["prompt_match"] = match
    return obs


def _edit_paths(events: list[Event]) -> list[str]:
    out: list[str] = []
    for ev in events:
        if ev.kind != "assistant":
            continue
        for t in ev.data["tools"]:
            inp = t.get("input") if isinstance(t.get("input"), dict) else {}
            p = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
            if t["name"] in ("Edit", "Write", "MultiEdit", "NotebookEdit") and isinstance(p, str):
                out.append(p)
    return out

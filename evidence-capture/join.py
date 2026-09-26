#!/usr/bin/env python3
"""Join a capture episode to its transcript turn (plan §5.1).

A lean transcript reader with the same event rules as ``build_episodes.py`` (a queued
enqueue is a hash-only candidate; a queued command attachment is a prompt; a
``hook_additional_context`` carrying ``<context_brief packet_id=...>`` is a brief; a user
entry with an ``origin`` dict is a prompt of that kind, a meta entry without one is a
candidate, anything else is a human prompt). Nothing here imports the Evidence Compiler.

The turn is the prompt whose hash equals the manifest's ``prompt.sha256``; when several
prompts share the hash the one nearest ``started_at`` wins, deterministically. The
episode ends at the next delivered prompt (``ended_by = prompt``) or at the transcript's
end (``stop`` / ``session_end``). Task notifications and hook prompts inside the bounds
are continuations, never a boundary.
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
PASTED_RE = re.compile(r"<pasted_content|<attached_file|\[Image #?\d+\]|\[Pasted text", re.I)
HUMAN_ORIGINS = {"user", "human"}
CONTINUATION_ORIGINS = {"queued_command:prompt", "task_notification", "hook", "meta"}


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


def parse_transcript(path: Path) -> Transcript:
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
            if entry.get("isSidechain"):
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
                        events.append(Event(idx, "prompt", ts, {
                            "sha256": sha256_text(text), "text": text, "uuid": entry.get("uuid"),
                            "origin": f"queued_command:{att.get('commandMode') or 'prompt'}", "attachment": attach}))
                    continue
                if att.get("type") != "hook_additional_context":
                    continue
                content = att.get("content")
                for part in (content if isinstance(content, list) else [content]):
                    if not isinstance(part, str):
                        continue
                    for m in BRIEF_TAG_RE.finditer(part):
                        events.append(Event(idx, "brief", ts, {"packet_id": m.group(1), "sha256": sha256_text(part),
                                                               "len": len(part)}))
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
                            events.append(Event(idx, "tool_result", ts, {
                                "tool_use_id": block.get("tool_use_id"), "is_error": bool(block.get("is_error")),
                                "text": text, "truncated": bool(TRUNCATED_RE.search(text))}))
                if entry.get("isCompactSummary"):
                    continue
                text, attach = _prompt_text(content)
                if text is None:
                    continue
                origin = entry.get("origin")
                if isinstance(origin, dict):
                    kind = "prompt"
                    o = str(origin.get("kind"))
                elif entry.get("isMeta"):
                    kind = "candidate"
                    o = "task_notification" if TASK_NOTIFICATION_RE.search(text) else "meta"
                else:
                    kind, o = "prompt", "user"
                events.append(Event(idx, kind, ts, {"sha256": sha256_text(text), "text": text,
                                                    "uuid": entry.get("uuid"), "origin": o, "attachment": attach}))
            elif etype == "assistant":
                tools = [{"id": b.get("id"), "name": b.get("name"), "input": b.get("input")}
                         for b in (msg.get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_use"]
                events.append(Event(idx, "assistant", ts, {"message_id": msg.get("id"), "tools": tools}))
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


def find_turn(tr: Transcript, prompt_sha256: str, started_at: str) -> int | None:
    """Index of the prompt event with this hash, nearest to ``started_at``."""
    start = parse_iso(started_at)
    best: tuple[float, int] | None = None
    for i, ev in enumerate(tr.events):
        if ev.kind != "prompt" or ev.data["sha256"] != prompt_sha256:
            continue
        ts = parse_iso(ev.ts)
        dist = abs((ts - start).total_seconds()) if ts and start else float("inf")
        if best is None or dist < best[0]:
            best = (dist, i)
    return best[1] if best else None


def window_end(tr: Transcript, start: int) -> tuple[int, str]:
    for j in range(start + 1, len(tr.events)):
        ev = tr.events[j]
        if ev.kind == "prompt" and ev.data["origin"] not in CONTINUATION_ORIGINS:
            return j, "prompt"
    return len(tr.events), "stop"


def observe(tr: Transcript, start: int, end: int, subagents: list[Transcript]) -> dict[str, Any]:
    """Tool calls with results inside the bounds, plus the observation-quality facts."""
    results = {ev.data["tool_use_id"]: ev.data for ev in tr.events[start:end] if ev.kind == "tool_result"}
    for sub in subagents:
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
                              "timestamp": ev.ts, "is_error": r["is_error"] if r else None,
                              "result_text": r["text"] if r else None,
                              "result_truncated": r["truncated"] if r else None, "result_missing": r is None})

    for ev in tr.events[start + 1:end]:
        if ev.kind == "assistant":
            assistant_turns += 1
        elif ev.kind == "prompt":
            continuations.append({"origin": ev.data["origin"], "sha256": ev.data["sha256"]})
        elif ev.kind == "candidate" and ev.data.get("origin") == "task_notification":
            continuations.append({"origin": "task_notification", "sha256": ev.data["sha256"]})
        elif ev.kind == "brief":
            briefs.append(ev.data)
    add(tr.events[start:end], "main")
    for sub in subagents:
        add(sub.events, sub.path.stem)
    agent_calls = sum(1 for c in calls if c["name"] in ("Agent", "Task") and c["source"] == "main")
    causes: list[str] = []
    if any(c["result_truncated"] for c in calls):
        causes.append("truncated_result")
    if any(c["result_missing"] for c in calls):
        causes.append("missing_result")
    if agent_calls and not subagents:
        causes.append("missing_subagent_transcript")
    if assistant_turns == 0:
        causes.append("no_response")
    if tr.decode_errors:
        causes.append("decode_errors")
    return {"tool_calls": calls, "continuations": continuations, "briefs": briefs,
            "assistant_turns": assistant_turns, "causes": causes, "turn_end_ts": tr.events[end - 1].ts if end > start else None}


def join(m: dict[str, Any], transcript: Path) -> dict[str, Any] | None:
    """Locate the manifest's turn in ``transcript``; None when the prompt is not there.

    Fills prompt.uuid/origin/index_in_session/has_attachment, prior_context facts, brief
    hashes for the linked packet (if any brief event carries the same packet id, or the
    only brief inside the bounds) and returns the observation dict for the screen.
    """
    tr = parse_transcript(transcript)
    start = find_turn(tr, m["prompt"]["sha256"], m["started_at"])
    if start is None:
        return None
    ev = tr.events[start]
    end, ended_by = window_end(tr, start)
    subs = [parse_transcript(p) for p in subagent_transcripts(transcript)]
    obs = observe(tr, start, end, subs)
    human_before = sum(1 for e in tr.events[:start] if e.kind == "prompt" and e.data["origin"] in HUMAN_ORIGINS)
    m["prompt_uuid"] = ev.data.get("uuid")
    m["prompt"].update(origin=ev.data["origin"], index_in_session=human_before, has_attachment=bool(ev.data["attachment"]))
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
                   "earlier_result_text": [e.data["text"] for e in tr.events[:start] if e.kind == "tool_result"],
                   "earlier_prompt_text": [e.data["text"] for e in tr.events[:start] if e.kind == "prompt"],
                   "earlier_edit_paths": _edit_paths(tr.events[:start])}
    obs["subagent_count"] = len(subs)
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

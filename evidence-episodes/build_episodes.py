#!/usr/bin/env python3
"""Deterministic episode builder for Evidence Compiler measurement.

Joins persisted EvidencePackets to the Claude Code session transcripts that
received them and writes one factual row per ``packet_id``:

  identity   packet, session, repo, HEAD, traffic class (Evidence Compiler's own
             ``classify_traffic``), cohort membership
  retrieved  what the compiler found, selected and omitted, and why
  injected   the exact brief text found in the transcript, and whether it equals
             ``render_brief(packet)``
  prompt     the full prompt text, checked against the packet's prompt hash
  observed   every main-thread tool call until the next prompt, with full arguments
  cost       token usage per assistant message, deduplicated by ``message.id``

Rows hold facts only. No relevance, hit, coverage, or usefulness is computed here.

Provenance rules:
  * The key is ``packet_id``; every copy of a packet across ``--source`` trees is
    listed in ``sources`` with its sha256. The row is built from the first source
    given on the command line that holds the packet.
  * Copies that differ in sha256 produce a ``sha_conflict`` row.
  * Packets named in a cohort manifest's lost list get a row with ``missing`` set.

Every row carries exactly one ``join_reason``:
  joined, missing_packet, missing_transcript, brief_not_found,
  ambiguous_transcript_session, sha_conflict, parse_failure, other

Output is byte-deterministic for identical inputs: rows sorted by ``packet_id``,
canonical JSON, paths relative to their named roots, no build timestamps.

Read-only on every input. Requires the ``evidence_compiler`` package importable.

Usage:
    python build_episodes.py --source w3=<packets-dir> --source live=<packets-dir> \\
        --transcripts w3=<dir> [--transcripts live=<dir> ...] \\
        [--cohort w3=<manifest.json>] --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import evidence_compiler
    from evidence_compiler.packet import EvidencePacket
    from evidence_compiler.rendering import render_brief
    from evidence_compiler.review import classify_traffic
    from evidence_compiler.scoping import prompt_hash
except ImportError as exc:  # pragma: no cover - environment guard
    sys.exit(f"build_episodes: evidence_compiler is not importable ({exc}); "
             "put its src/ on PYTHONPATH")

SCHEMA_VERSION = 1
JOIN_REASONS = ("joined", "missing_packet", "missing_transcript", "brief_not_found",
                "ambiguous_transcript_session", "sha_conflict", "parse_failure", "other")
PACKET_NAME_RE = re.compile(r"(ep_[0-9a-f]{8,64})\.json$")
PACKET_ID_RE = re.compile(r"ep_[0-9a-f]{8,64}")
BRIEF_TAG_RE = re.compile(r'<context_brief packet_id="(ep_[0-9a-f]{8,64})"')
SESSION_FILE_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# A prompt found by hash alone must sit this close to the packet's creation time.
PROMPT_TIME_TOLERANCE_S = 120.0
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
              "output_tokens")


# ---------------------------------------------------------------- helpers

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # packets write naive UTC in some versions
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def tree_digest(entries: list[tuple[str, str]]) -> str:
    """sha256 over sorted ``relpath\\tsha256`` lines: one hash for a whole input tree."""
    body = "".join(f"{rel}\t{sha}\n" for rel, sha in sorted(entries))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def git_head(path: Path) -> str:
    try:
        res = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return res.stdout.strip() if res.returncode == 0 else "unknown"


# ---------------------------------------------------------------- packets

@dataclass
class PacketCopy:
    source: str
    rel: str
    path: Path
    sha256: str


def discover_packets(sources: list[tuple[str, Path]]) -> tuple[dict[str, list[PacketCopy]], dict[str, list[tuple[str, str]]], Counter]:
    copies: dict[str, list[PacketCopy]] = defaultdict(list)
    trees: dict[str, list[tuple[str, str]]] = {}
    issues: Counter = Counter()
    for name, root in sources:
        entries: list[tuple[str, str]] = []
        for path in sorted(root.rglob("*.json")):
            m = PACKET_NAME_RE.search(path.name)
            if not m:
                issues[f"{name}:unrecognized_file"] += 1
                continue
            rel = path.relative_to(root).as_posix()
            sha = sha256_file(path)
            entries.append((rel, sha))
            copies[m.group(1)].append(PacketCopy(name, rel, path, sha))
        trees[name] = entries
    return copies, trees, issues


# ---------------------------------------------------------------- transcripts

@dataclass
class Event:
    idx: int
    # prompt:    a delivered prompt; it ends the previous turn
    # candidate: text that may be the hook's prompt but does not end a turn
    #            (a queue enqueue, a meta entry without origin); matched by hash only
    # brief | assistant | tool_result
    kind: str
    ts: str | None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Transcript:
    root: str
    rel: str
    session: str
    sha256: str
    events: list[Event]
    parse_errors: int
    sidechain_entries: int


SLASH_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
SLASH_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def prompt_forms(text: str) -> list[str]:
    """Texts the hook may have received for a stored prompt.

    A slash command is stored as ``<command-name>``/``<command-args>`` markup;
    the hook received the typed form, ``/name args``.
    """
    forms = [text]
    name = SLASH_NAME_RE.search(text)
    if name:
        args = SLASH_ARGS_RE.search(text)
        typed = f"{name.group(1)} {args.group(1)}".strip() if args else name.group(1).strip()
        if typed not in forms:
            forms.append(typed)
    return forms


def _prompt_event(idx: int, kind: str, ts: str | None, text: str, uuid: Any, origin: str) -> Event:
    return Event(idx, kind, ts, {"text": text, "forms": prompt_forms(text), "uuid": uuid,
                                 "origin": origin})


def _prompt_text(content: Any) -> str | None:
    """Prompt text of a user entry, or None when the entry is not a prompt."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
    return "\n".join(texts) if texts else None


def parse_transcript(root_name: str, root: Path, path: Path) -> Transcript:
    events: list[Event] = []
    errors = 0
    sidechain = 0
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
            if entry.get("isSidechain"):
                sidechain += 1
                continue
            ts = entry.get("timestamp")
            etype = entry.get("type")
            if etype == "queue-operation":
                text = _prompt_text(entry.get("content"))
                if entry.get("operation") == "enqueue" and text is not None:
                    events.append(_prompt_event(idx, "candidate", ts, text, None, "queue_enqueue"))
                continue
            if etype == "attachment":
                att = entry.get("attachment") or {}
                text = _prompt_text(att.get("prompt")) if att.get("type") == "queued_command" else None
                if text is not None:
                    # A prompt queued while the agent worked, delivered mid-turn.
                    events.append(_prompt_event(idx, "prompt", ts, text, entry.get("uuid"),
                                                f"queued_command:{att.get('commandMode') or 'prompt'}"))
                    continue
                if att.get("type") != "hook_additional_context":
                    continue
                content = att.get("content")
                parts = content if isinstance(content, list) else [content]
                for part in parts:
                    if not isinstance(part, str):
                        continue
                    for m in BRIEF_TAG_RE.finditer(part):
                        events.append(Event(idx, "brief", ts, {
                            "packet_id": m.group(1), "text": part,
                            "session_id": entry.get("sessionId"),
                        }))
                continue
            msg = entry.get("message")
            if not isinstance(msg, dict):
                continue
            if etype == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            events.append(Event(idx, "tool_result", ts, {
                                "tool_use_id": block.get("tool_use_id"),
                                "is_error": bool(block.get("is_error")),
                            }))
                if entry.get("isCompactSummary"):
                    continue
                # Meta entries are mostly harness text (skill bodies, reminders): they
                # end a turn only when they carry an ``origin`` (a peer session or the
                # system); otherwise they are hash-only candidates.
                origin = entry.get("origin")
                text = _prompt_text(content)
                if text is None:
                    continue
                if isinstance(origin, dict):
                    events.append(_prompt_event(idx, "prompt", ts, text, entry.get("uuid"),
                                                str(origin.get("kind"))))
                elif entry.get("isMeta"):
                    events.append(_prompt_event(idx, "candidate", ts, text, entry.get("uuid"), "meta"))
                else:
                    events.append(_prompt_event(idx, "prompt", ts, text, entry.get("uuid"), "user"))
            elif etype == "assistant":
                tools = []
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tools.append({"id": block.get("id"), "name": block.get("name"),
                                      "input": block.get("input")})
                events.append(Event(idx, "assistant", ts, {
                    "message_id": msg.get("id"), "model": msg.get("model"),
                    "usage": msg.get("usage") if isinstance(msg.get("usage"), dict) else None,
                    "tools": tools,
                }))
    return Transcript(root_name, path.relative_to(root).as_posix(), path.stem,
                      sha256_file(path), events, errors, sidechain)


def discover_transcripts(roots: list[tuple[str, Path]]) -> list[Transcript]:
    """Session transcripts are ``<root>/<project>/<session-uuid>.jsonl``; subagent files are not."""
    out: list[Transcript] = []
    for name, root in roots:
        for path in sorted(root.glob("*/*.jsonl")):
            if SESSION_FILE_RE.match(path.stem):
                out.append(parse_transcript(name, root, path))
    return out


# ---------------------------------------------------------------- episode window

def window_end(tr: Transcript, start: int, pid: str, other_starts: set[int]) -> tuple[int, str]:
    """(end, ended_by) of the turn starting at ``start``.

    The turn ends at the next delivered prompt, another packet's brief, or another
    packet's turn start. Prompts delivered together (a batch of queued prompts)
    leave every turn but the last empty; ``ended_by`` records that fact.
    """
    for pos in range(start + 1, len(tr.events)):
        ev = tr.events[pos]
        if ev.kind == "prompt":
            return pos, "prompt"
        if ev.kind == "brief" and ev.data["packet_id"] != pid:
            return pos, "brief"
        if pos in other_starts:
            return pos, "packet_start"
    return len(tr.events), "end_of_transcript"


def observe(tr: Transcript, start: int, end: int) -> dict[str, Any]:
    errors = {ev.data["tool_use_id"]: ev.data["is_error"]
              for ev in tr.events[start:end] if ev.kind == "tool_result"}
    calls: list[dict[str, Any]] = []
    usage_by_msg: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    models: set[str] = set()
    anonymous = 0
    last_ts = tr.events[start].ts
    for ev in tr.events[start:end]:
        if ev.ts:
            last_ts = ev.ts
        if ev.kind != "assistant":
            continue
        for tool in ev.data["tools"]:
            calls.append({
                "seq": len(calls), "tool_use_id": tool["id"], "name": tool["name"],
                "input": tool["input"], "timestamp": ev.ts,
                "is_error": errors.get(tool["id"]),
            })
        mid = ev.data["message_id"]
        if ev.data["model"]:
            models.add(ev.data["model"])
        if not mid:
            anonymous += 1
            continue
        if mid not in usage_by_msg:
            order.append(mid)
        if ev.data["usage"] is not None:
            usage_by_msg[mid] = ev.data["usage"]  # the last chunk carries the final usage
    totals = {k: 0 for k in USAGE_KEYS}
    missing_usage = 0
    for mid in order:
        usage = usage_by_msg.get(mid)
        if usage is None:
            missing_usage += 1
            continue
        for k in USAGE_KEYS:
            v = usage.get(k)
            if isinstance(v, int):
                totals[k] += v
    return {
        "tool_calls": calls,
        "cost": {
            "assistant_messages": len(order), "messages_without_id": anonymous,
            "messages_without_usage": missing_usage, "models": sorted(models), **totals,
            "total_tokens": sum(totals.values()),
        },
        "turn_end_timestamp": last_ts,
    }


# ---------------------------------------------------------------- cohort manifests

@dataclass
class Cohort:
    name: str
    commit: str
    members: set[str]
    recorded_traffic: dict[str, str]
    lost: dict[str, dict[str, Any]]
    late: set[str]


def load_cohort(name: str, path: Path) -> Cohort:
    data = json.loads(path.read_text(encoding="utf-8"))
    recorded = {p["packet_id"]: p.get("traffic") for p in data.get("packets") or []}
    lost = {p["packet_id"]: p for p in (data.get("lost_packets") or {}).get("packets") or []}
    recon = data.get("cohort_reconciliation") or {}
    late: set[str] = set(PACKET_ID_RE.findall(canonical(recon.get("extra_packet") or {})))
    late.update(PACKET_ID_RE.findall(canonical(recon.get("other_late_non_candidate") or [])))
    cohort_rec = data.get("cohort") or {}
    commit = str(cohort_rec.get("evidence_compiler_commit") or cohort_rec.get("commit") or "unknown")
    return Cohort(name, commit, set(recorded) | set(lost), recorded, lost, late)


# ---------------------------------------------------------------- rows

def retrieved_section(raw: dict[str, Any]) -> dict[str, Any]:
    items = []
    for ev in raw.get("evidence") or []:
        claim = ev.get("source_claim") or {}
        assess = ev.get("compiler_assessment") or {}
        items.append({
            "id": ev.get("id"), "kind": claim.get("kind"),
            "collector": (ev.get("provenance") or {}).get("collector"),
            "statement": claim.get("statement"), "references": claim.get("references"),
            "authority": ev.get("authority"), "freshness": ev.get("freshness"),
            "status": ev.get("status"), "selected": assess.get("selected"),
            "final_score": assess.get("final_score"),
            "selected_because": assess.get("selected_because"),
            "omitted_because": assess.get("omitted_because"),
        })
    return {
        "scope": raw.get("scope"), "budget": raw.get("budget"),
        "collectors_run": raw.get("collectors_run"),
        "negative_evidence": raw.get("negative_evidence"), "items": items,
    }


def _pick_transcript(pid: str, session_id: str | None, by_pid: dict[str, list[tuple[Transcript, int]]],
                     by_session: dict[str, list[Transcript]],
                     root_rank: dict[str, int]) -> tuple[str, Transcript | None, int | None, str]:
    """(status, transcript, brief position, detail); status is found | none | ambiguous.

    ``root_rank`` is each transcript root's position on the command line: when a
    file is in several roots, the earliest root wins, never the alphabetically first."""
    hits = by_pid.get(pid, [])
    if hits:
        # A resumed or forked session copies history into a new file; the brief
        # belongs to the file of the session that received it.
        own = [(t, pos) for t, pos in hits
               if t.session == (session_id or t.events[pos].data.get("session_id"))]
        pool = own or hits
        first = min((t.root for t, _ in pool), key=root_rank.__getitem__)
        pool = [(t, pos) for t, pos in pool if t.root == first]  # first-priority root wins
        files = sorted({(t.root, t.rel) for t, _ in pool})
        if len(files) > 1:
            return "ambiguous", None, None, "packet_id in " + ", ".join(f"{r}:{p}" for r, p in files)
        tr, pos = min(pool, key=lambda x: x[1])
        return "found", tr, pos, ""
    if session_id and session_id in by_session:
        cands = by_session[session_id]
        first = min((t.root for t in cands), key=root_rank.__getitem__)
        cands = [t for t in cands if t.root == first]
        if len(cands) > 1:
            return "ambiguous", None, None, "session in " + ", ".join(sorted(t.rel for t in cands))
        return "found", cands[0], None, ""
    return "none", None, None, ""


def _matching_form(ev: Event, want_hash: str | None) -> str | None:
    if not want_hash:
        return None
    return next((f for f in ev.data["forms"] if prompt_hash(f) == want_hash), None)


def _prompt_before(tr: Transcript, brief_pos: int, pid: str,
                   want_hash: str | None) -> tuple[int | None, str | None]:
    """(position, matched text) of the prompt that triggered the brief at ``brief_pos``.

    Searches back to the previous packet's brief. The nearest prompt or candidate
    whose hash equals the packet's wins. Failing that, the nearest delivered prompt
    is returned without matched text, and the row reports the mismatch.
    """
    nearest: int | None = None
    for pos in range(brief_pos - 1, -1, -1):
        ev = tr.events[pos]
        if ev.kind == "brief" and ev.data["packet_id"] != pid:
            break
        if ev.kind not in ("prompt", "candidate"):
            continue
        form = _matching_form(ev, want_hash)
        if form is not None:
            return pos, form
        if nearest is None and ev.kind == "prompt":
            nearest = pos
    return nearest, None


def _anchor_by_prompt(tr: Transcript, want_hash: str | None,
                      created: datetime | None) -> tuple[int | None, str | None, str]:
    """(position, matched text, why) of the packet's prompt when no brief was injected.

    Matches by hash within ``PROMPT_TIME_TOLERANCE_S`` of the packet's creation.
    A delivered prompt outranks a candidate (a queued prompt is enqueued, then
    delivered). Entries sharing one timestamp record a single delivery (a copy
    re-written by the harness).
    """
    if not want_hash:
        return None, None, "no prompt hash"
    by_kind: dict[str, dict[str | None, list[tuple[int, str]]]] = {
        "prompt": defaultdict(list), "candidate": defaultdict(list)}
    for pos, ev in enumerate(tr.events):
        if ev.kind not in by_kind:
            continue
        form = _matching_form(ev, want_hash)
        if form is None:
            continue
        t = parse_ts(ev.ts)
        if created is not None and (t is None or abs((t - created).total_seconds()) > PROMPT_TIME_TOLERANCE_S):
            continue
        by_kind[ev.kind][ev.ts].append((pos, form))
    by_ts = by_kind["prompt"] or by_kind["candidate"]
    if len(by_ts) == 1:
        return (*next(iter(by_ts.values()))[0], "")
    if by_ts or created is None:
        return None, None, f"{len(by_ts)} prompt matches"
    # No stored form hashes to the packet's prompt (the app rewrote it, e.g. a
    # paste wrapper): fall back to the last delivered prompt before creation.
    # The row then reports ``hash_matches_packet: false``.
    last: int | None = None
    for pos, ev in enumerate(tr.events):
        t = parse_ts(ev.ts)
        if ev.kind != "prompt" or t is None or t > created:
            continue
        if (created - t).total_seconds() <= PROMPT_TIME_TOLERANCE_S:
            last = pos
    if last is None:
        return None, None, "0 prompt matches, no prompt before creation"
    return last, None, ""


def build_row(pid: str, copies: list[PacketCopy], cohorts: list[Cohort],
              by_pid: dict[str, list[tuple[Transcript, int]]],
              by_session: dict[str, list[Transcript]], root_rank: dict[str, int]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "packet_id": pid,
        "sources": [{"source": c.source, "path": c.rel, "sha256": c.sha256} for c in copies],
        "missing": {}, "join_reason": "other", "join_detail": "",
    }
    cohort = next((c for c in cohorts if pid in c.members), None)
    row["cohort"] = cohort.name if cohort else None
    row["cohort_status"] = (None if cohort is None else "lost" if pid in cohort.lost
                            else "late" if pid in cohort.late else "member")
    row["compiler_commit"] = cohort.commit if cohort else "unknown"
    lost_rec = cohort.lost.get(pid) if cohort else None
    row["recorded_transcripts"] = sorted(lost_rec.get("transcripts") or []) if lost_rec else None
    row["recorded_traffic"] = (cohort.recorded_traffic.get(pid) or (cohort.lost.get(pid) or {}).get("traffic")) if cohort else None

    reason: str | None = None
    detail = ""
    packet: EvidencePacket | None = None
    raw: dict[str, Any] | None = None
    if not copies:
        reason = "missing_packet"
        row["missing"]["packet"] = True
    elif len({c.sha256 for c in copies}) > 1:
        reason = "sha_conflict"
        detail = "copies differ: " + ", ".join(f"{c.source}={c.sha256[:12]}" for c in copies)
    else:
        try:
            raw = json.loads(copies[0].path.read_text(encoding="utf-8"))
            packet = EvidencePacket.from_dict(raw)
        except Exception as exc:  # any malformed packet is data, never a crash
            reason = "parse_failure"
            detail = f"{type(exc).__name__}: {exc}"[:300]
            packet = None

    ident = (raw or {}).get("identity") or {}
    task = (raw or {}).get("task") or {}
    session_id = ident.get("session_id")
    row["identity"] = {
        "session_id": session_id, "turn_id": ident.get("turn_id"),
        "repository_root": ident.get("repository_root"), "worktree_id": ident.get("worktree_id"),
        "head": ident.get("head"), "branch": ident.get("branch"),
        "head_state": ident.get("head_state"),
        "created_at": (raw or {}).get("created_at") or ((cohort.lost.get(pid) or {}).get("created_at") if cohort else None),
        "source_kind": task.get("source_kind"), "intent": task.get("intent"),
        "extracted_symbols": task.get("extracted_symbols"),
    }
    row["traffic"] = classify_traffic(packet) if packet is not None else None
    row["retrieved"] = retrieved_section(raw) if packet is not None else None

    rendered = render_brief(packet).text if packet is not None else None
    status, tr, brief_pos, tdetail = _pick_transcript(pid, session_id, by_pid, by_session, root_rank)
    anchor_pos: int | None = None
    matched: str | None = None
    injected: dict[str, Any] = {"rendered_nonempty": None if rendered is None else bool(rendered),
                                "found": False, "text": None, "matches_render": None}
    if tr is not None:
        row["transcript"] = {"source": tr.root, "path": tr.rel, "sha256": tr.sha256,
                             "parse_errors": tr.parse_errors}
        if brief_pos is not None:
            text = tr.events[brief_pos].data["text"]
            injected.update(found=True, text=text,
                            matches_render=None if rendered is None else text == rendered)
            anchor_pos, matched = _prompt_before(tr, brief_pos, pid, task.get("raw_prompt_hash"))
            if anchor_pos is None:
                row["missing"]["prompt"] = True
        elif packet is not None:
            anchor_pos, matched, why = _anchor_by_prompt(tr, task.get("raw_prompt_hash"),
                                                         parse_ts(raw.get("created_at")))
            if anchor_pos is None:
                row["missing"]["prompt"] = True
                detail = detail or f"prompt not located: {why}"
    else:
        row["transcript"] = None
        row["missing"]["transcript"] = True
    row["injected"] = injected

    if tr is not None and anchor_pos is not None:
        anchor = tr.events[anchor_pos]
        stored = anchor.data["text"]
        row["prompt"] = {
            "text": matched if matched is not None else stored,
            "transcript_text": stored if matched is not None and matched != stored else None,
            "timestamp": anchor.ts, "uuid": anchor.data.get("uuid"),
            "origin": anchor.data.get("origin"),
            # hash: a stored form hashes to the packet's prompt; otherwise the prompt
            # is the nearest one before the brief (brief_order) or before creation (time)
            "anchored_by": ("hash" if matched is not None
                            else "brief_order" if brief_pos is not None else "time"),
            "hash_matches_packet": None if not task.get("raw_prompt_hash") else matched is not None,
            # delivered prompts earlier in this transcript, by origin (standalone checks)
            "prior_prompts": dict(sorted(Counter(
                str(ev.data.get("origin")) for ev in tr.events[:anchor_pos] if ev.kind == "prompt").items())),
        }
        # The hook answers at submission, so the brief marks where the turn's
        # response begins; a queued prompt can sit well before its delivery.
        # The window's end depends on every row's start, so build() fills it in.
        row["_turn"] = (tr, brief_pos if brief_pos is not None else anchor_pos)
        row["observed"] = None
    else:
        row["prompt"] = None
        row["observed"] = None
        row["missing"]["observed"] = True

    if reason is None:
        if status == "ambiguous":
            reason, detail = "ambiguous_transcript_session", tdetail
        elif status == "none":
            reason = "missing_transcript"
            detail = "no session id in packet" if not session_id else "no transcript for session"
        elif rendered and not injected["found"]:
            reason = "brief_not_found"
        elif injected["found"] or rendered == "":
            reason = "joined"
            if anchor_pos is None:
                reason, detail = "other", detail or "prompt not located"
    elif reason == "missing_packet" and status == "ambiguous":
        detail = tdetail
    row["join_reason"] = reason
    row["join_detail"] = detail
    return row


# ---------------------------------------------------------------- summary

def summarize(rows: list[dict[str, Any]], cohorts: list[Cohort], issues: Counter,
              transcripts: list[Transcript]) -> dict[str, Any]:
    def reasons(sel):
        c = Counter(r["join_reason"] for r in sel)
        return {k: c.get(k, 0) for k in JOIN_REASONS}

    def brief_rate(sel):
        eligible = [r for r in sel if r["injected"]["rendered_nonempty"]]
        found = sum(1 for r in eligible if r["injected"]["found"])
        exact = sum(1 for r in eligible if r["injected"]["matches_render"])
        return {"eligible": len(eligible), "found": found, "exact_render_match": exact,
                "rate": round(found / len(eligible), 4) if eligible else None}

    def prompts(sel):
        with_prompt = [r for r in sel if r["prompt"]]
        return {
            "located": len(with_prompt),
            "anchored_by": dict(sorted(Counter(r["prompt"]["anchored_by"] for r in with_prompt).items())),
            "hash_mismatch": sorted(r["packet_id"] for r in with_prompt
                                    if r["prompt"]["hash_matches_packet"] is False),
            "turn_ended_by": dict(sorted(Counter(r["observed"]["ended_by"] for r in with_prompt).items())),
            "turns_without_assistant": sum(1 for r in with_prompt
                                           if not r["observed"]["cost"]["assistant_messages"]),
        }

    out: dict[str, Any] = {
        "rows": len(rows),
        "join_reasons": reasons(rows),
        "brief_found": brief_rate(rows),
        "prompts": prompts(rows),
        "sha_conflicts": sorted(r["packet_id"] for r in rows if r["join_reason"] == "sha_conflict"),
        "discovery_issues": dict(sorted(issues.items())),
        "transcripts": {"files": len(transcripts),
                        "parse_errors": sum(t.parse_errors for t in transcripts)},
        "by_source": {}, "cohorts": {},
    }
    sources = sorted({s["source"] for r in rows for s in r["sources"]})
    for name in sources:
        held = [r for r in rows if any(s["source"] == name for s in r["sources"])]
        out["by_source"][name] = {"packets": len(held)}
    for c in cohorts:
        members = [r for r in rows if r["cohort"] == c.name]
        present = [r for r in members if r["sources"]]
        traffic = Counter(r["traffic"] or "unclassified" for r in present)
        disagreements = sorted(r["packet_id"] for r in present
                               if r["traffic"] and r["recorded_traffic"] and r["traffic"] != r["recorded_traffic"])
        sessions = {r["identity"]["session_id"] for r in present if r["identity"]["session_id"]}
        joined_sessions = {r["identity"]["session_id"] for r in present
                           if r["identity"]["session_id"] and r["transcript"]}
        cand = [r for r in present if r["traffic"] == "candidate"]
        out["cohorts"][c.name] = {
            "compiler_commit": c.commit,
            "packets": len(present),
            "sessions": len(sessions),
            "sessions_with_transcript": len(joined_sessions),
            "candidate_packets": len(cand),
            "candidate_sessions": len({r["identity"]["session_id"] for r in cand}),
            "traffic": dict(sorted(traffic.items())),
            "traffic_disagreements_with_manifest": disagreements,
            "lost_rows": sum(1 for r in members if r["cohort_status"] == "lost"),
            "late_rows": sorted(r["packet_id"] for r in members if r["cohort_status"] == "late"),
            "join_reasons": reasons(members),
            "brief_found": brief_rate(members),
            "brief_found_candidates": brief_rate(cand),
            "prompts": prompts(members),
        }
    return out


# ---------------------------------------------------------------- main

def build(sources: list[tuple[str, Path]], transcript_roots: list[tuple[str, Path]],
          cohort_specs: list[tuple[str, Path]], out: Path) -> dict[str, Any]:
    copies, trees, issues = discover_packets(sources)
    cohorts = [load_cohort(name, path) for name, path in cohort_specs]
    for c in cohorts:
        for pid in c.lost:
            copies.setdefault(pid, [])
    order = {name: i for i, (name, _) in enumerate(sources)}
    for pid in copies:
        copies[pid].sort(key=lambda c: (order[c.source], c.rel))

    transcripts = discover_transcripts(transcript_roots)
    root_rank = {name: i for i, (name, _) in enumerate(transcript_roots)}
    by_pid: dict[str, list[tuple[Transcript, int]]] = defaultdict(list)
    by_session: dict[str, list[Transcript]] = defaultdict(list)
    for tr in transcripts:
        by_session[tr.session].append(tr)
        for pos, ev in enumerate(tr.events):
            if ev.kind == "brief":
                by_pid[ev.data["packet_id"]].append((tr, pos))

    rows = [build_row(pid, copies[pid], cohorts, by_pid, by_session, root_rank) for pid in sorted(copies)]
    starts: dict[int, set[int]] = defaultdict(set)
    for row in rows:
        if "_turn" in row:
            tr, start = row["_turn"]
            starts[id(tr)].add(start)
    for row in rows:
        turn = row.pop("_turn", None)
        if turn is None:
            continue
        tr, start = turn
        end, ended_by = window_end(tr, start, row["packet_id"], starts[id(tr)] - {start})
        row["observed"] = {**observe(tr, start, end), "ended_by": ended_by}
    summary = summarize(rows, cohorts, issues, transcripts)

    out.mkdir(parents=True, exist_ok=True)
    with (out / "episodes.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(canonical(row) + "\n")
    here = Path(__file__).resolve()
    build_record = {
        "schema_version": SCHEMA_VERSION,
        "builder": {"file": here.name, "sha256": sha256_file(here), "git_head": git_head(here.parent)},
        "evidence_compiler_version": evidence_compiler.__version__,
        "inputs": {
            "sources": [{"name": n, "files": len(trees[n]), "tree_sha256": tree_digest(trees[n])}
                        for n, _ in sources],
            "transcripts": [{"name": n, "files": len([t for t in transcripts if t.root == n]),
                             "tree_sha256": tree_digest([(t.rel, t.sha256) for t in transcripts if t.root == n])}
                            for n, _ in transcript_roots],
            "cohorts": [{"name": n, "sha256": sha256_file(p)} for n, p in cohort_specs],
        },
        "episodes_sha256": sha256_file(out / "episodes.jsonl"),
        "summary": summary,
    }
    (out / "build.json").write_text(json.dumps(build_record, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8", newline="\n")
    return build_record


def _named(spec: str, flag: str) -> tuple[str, Path]:
    name, sep, path = spec.partition("=")
    if not sep or not name or not path:
        raise argparse.ArgumentTypeError(f"{flag} expects NAME=PATH, got {spec!r}")
    return name, Path(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", action="append", default=[], required=True,
                    help="NAME=DIR of packet JSON files (recursive); earlier sources take precedence")
    ap.add_argument("--transcripts", action="append", default=[],
                    help="[NAME=]DIR laid out <project>/<session>.jsonl; earlier roots take precedence")
    ap.add_argument("--cohort", action="append", default=[],
                    help="NAME=MANIFEST.json listing cohort packets and lost packets")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    sources = [_named(s, "--source") for s in args.source]
    roots = [_named(s, "--transcripts") if "=" in s else (f"t{i}", Path(s))
             for i, s in enumerate(args.transcripts)]
    cohorts = [_named(s, "--cohort") for s in args.cohort]
    names = [n for n, _ in sources]
    if len(set(names)) != len(names):
        ap.error("source names must be unique")
    for _, p in sources + roots + cohorts:
        if not p.exists():
            ap.error(f"input not found: {p}")
    record = build(sources, roots, cohorts, args.out)
    print(json.dumps({"episodes_sha256": record["episodes_sha256"],
                      "rows": record["summary"]["rows"],
                      "join_reasons": record["summary"]["join_reasons"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

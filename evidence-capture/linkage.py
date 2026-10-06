#!/usr/bin/env python3
"""Deferred, deterministic linkage of a capture manifest to an Evidence Compiler packet (plan §2).

Identity: ``session_id`` equal, ``prompt_sha256`` equal to the packet's prompt hash,
packet ``created_at`` inside the episode bounds and ``repo.head`` equal. All four must
select exactly one packet. Anything else is ``none`` (retried at every capture start and
in the daily backstop), ``ambiguous`` (final) or ``mismatch`` (final). Capture never reads
a packet at start time; packets are read here, afterwards, from the repository's own
``.evidence-compiler/packets/`` and any extra directories the store configuration names.

The episode's lower bound is the capture's ``started_at`` less ``HOOK_START_SKEW_S``: the
capture hook and the Evidence Compiler hook are started concurrently for the same prompt
(boundary test b), so the packet's ``created_at`` may precede the capture's own clock
reading by the difference in interpreter start-up. The session, prompt-hash and head
equalities still have to hold; a second identical prompt inside the skew is ``ambiguous``.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from common import iso, now_utc, parse_iso
from manifest import FINAL_LINK_STATES, refresh
from store import Stores, log_line, write_manifest

PACKET_GLOB = "*ep_*.json"  # EC archive names <id>.json; repo stores name <ts>_<id>.json
HOOK_START_SKEW_S = 10


def packet_dirs(repo_root: Path | None, stores: Stores, session_roots: list[Path] | None = None) -> list[Path]:
    """The capture's checkout store, then the session's own checkout stores (EC writes to the
    session project root, which can differ from a worktree the prompt ran in), then archives."""
    dirs: list[Path] = []
    for r in [repo_root, *(session_roots or [])]:
        if r is not None and (d := r / ".evidence-compiler" / "packets") not in dirs:
            dirs.append(d)
    dirs.extend(Path(p) for p in stores.packet_dirs)
    return dirs


def _field(d: dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def load_packet(path: Path) -> dict[str, Any] | None:
    """The identity fields of one packet file; None when unreadable or not a packet."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("packet_id"), str):
        return None
    prompt_hash = _field(raw, "correlation", "prompt_hash") or _field(raw, "task", "raw_prompt_hash")
    return {
        "packet_id": raw["packet_id"], "created_at": raw.get("created_at"),
        "session_id": _field(raw, "identity", "session_id"), "head": _field(raw, "identity", "head"),
        "head_state": _field(raw, "identity", "head_state"), "prompt_hash": prompt_hash,
        "path": str(path),
    }


def load_packets(dirs: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob(PACKET_GLOB)):
            pk = load_packet(p)
            if pk and pk["packet_id"] not in seen:
                seen.add(pk["packet_id"])
                out.append(pk)
    return out


def resolve_link(m: dict[str, Any], packets: list[dict[str, Any]], now: str | None = None) -> dict[str, Any]:
    """The link block for manifest ``m`` given the packets visible now. Pure."""
    now = now or iso(now_utc())
    prev = m["link"]
    if prev.get("state") in FINAL_LINK_STATES:
        return dict(prev)  # final states are never recomputed
    start = parse_iso(m["started_at"])
    if start is not None:
        start -= timedelta(seconds=HOOK_START_SKEW_S)
    end = parse_iso(m.get("ended_at")) or parse_iso(now)
    session = m["session_id"]
    want = m["prompt"]["sha256"]
    same = [p for p in packets if p["session_id"] == session and p["prompt_hash"] == want]
    in_bounds = []
    for p in same:
        created = parse_iso(p["created_at"])
        if created is not None and start is not None and end is not None and start <= created <= end:
            in_bounds.append(p)
    link = {"state": "none", "packet_id": None, "brief_sha256": prev.get("brief_sha256"),
            "brief_len": prev.get("brief_len"), "brief_empty": prev.get("brief_empty"),
            "matched_on": [], "reason": None, "checked_at": now}
    if not in_bounds:
        link["reason"] = "no packet with this session and prompt hash inside the episode bounds"
        return link
    if len(in_bounds) > 1:
        link.update(state="ambiguous", matched_on=["session_id", "prompt_sha256", "created_at"],
                    reason=f"{len(in_bounds)} packets match: " + ",".join(sorted(p["packet_id"] for p in in_bounds)))
        return link
    pk = in_bounds[0]
    head = m["repo"].get("head")
    if pk["head"] != head or head is None:
        link.update(state="mismatch", packet_id=pk["packet_id"],
                    matched_on=["session_id", "prompt_sha256", "created_at"],
                    reason=f"head differs: packet={pk['head']} ({pk.get('head_state')}) capture={head}")
        return link
    link.update(state="linked", packet_id=pk["packet_id"],
                matched_on=["session_id", "prompt_sha256", "created_at", "head"])
    return link


def reconcile(stores: Stores, manifests: list[dict[str, Any]], repo_root_by_episode: dict[str, Path] | None = None,
              extra_packets: list[dict[str, Any]] | None = None, *, now: str | None = None,
              rules_for: Callable[[str], dict[str, Any] | None] | None = None,
              session_roots: list[Path] | None = None) -> dict[str, int]:
    """Re-run linkage for every non-final manifest; writes only when the link changed (its
    state or its packet). A changed manifest has its exclusions and funnel recomputed first,
    with the owner's rules from ``rules_for(episode_id)`` when given. Maintenance calls this
    for each terminal episode, so this is the one place a link is re-resolved."""
    counts = {"checked": 0, "linked": 0, "ambiguous": 0, "mismatch": 0, "none": 0, "final_skipped": 0, "changed": 0}
    cache: dict[str, list[dict[str, Any]]] = {}
    for m in manifests:
        if m["link"].get("state") in FINAL_LINK_STATES:
            counts["final_skipped"] += 1
            continue
        counts["checked"] += 1
        root = (repo_root_by_episode or {}).get(m["episode_id"])
        key = str(root) if root else ""
        if key not in cache:
            cache[key] = load_packets(packet_dirs(root, stores, session_roots)) + list(extra_packets or [])
        new = resolve_link(m, cache[key], now)
        counts[new["state"]] += 1
        if new["state"] != m["link"].get("state") or new.get("packet_id") != m["link"].get("packet_id"):
            m["link"] = new
            refresh(m, rules_for(m["episode_id"]) if rules_for else None)
            write_manifest(stores, m)
            counts["changed"] += 1
            log_line(stores, f"link {m['episode_id']} -> {new['state']} {new.get('packet_id') or ''}")
    return counts

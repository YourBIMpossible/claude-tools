#!/usr/bin/env python3
"""Manifest schema v2, exclusion rules X1–X7 and the funnel (plan §4.1, §5.2, §5.4).

A manifest holds identifiers, hashes, categories, states and reasons. Never file
contents, prompt text or brief text. Rules and funnel stages are pure functions of the
manifest so they can be recomputed identically at any time.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common import SCHEMA_VERSION

DEP_CATEGORIES = ("prior_conversation", "attachment", "external_path", "external_repository",
                  "untracked_input", "memory_or_scratch", "live_remote", "outward_action",
                  "other_session", "network")
X3_CATEGORIES = ("live_remote", "outward_action", "other_session", "network")
X4_CATEGORIES = ("attachment", "external_path", "external_repository", "memory_or_scratch")
RULES = ("X1", "X2", "X3", "X4", "X5", "X6", "X7")
RULE_TEXT = {
    "X1": "the episode is not a human task",
    "X2": "no edit to a repository file",
    "X3": "live_remote, outward_action, other_session or network is required",
    "X4": "attachment, external_path, external_repository or memory_or_scratch is required",
    "X5": "prior conversation is required",
    "X6": "the start state cannot be rebuilt (snapshot failed or incomplete)",
    "X7": "capture failed or was incomplete",
}
OWNER_RESOLVABLE = ("X1", "X2", "X3", "X4", "X5")  # never X6 or X7
FUNNEL = ("packet_linked", "start_snapshot_complete", "dependency_screen_clear", "privacy_cleared",
          "payload_materialized", "start_state_reconstructed", "replay_verified")
LINK_STATES = ("linked", "none", "ambiguous", "mismatch")
FINAL_LINK_STATES = ("linked", "ambiguous", "mismatch")
TRI = ("yes", "no", "unknown")
HUMAN_ORIGINS = ("user", "human")


def new_manifest(*, episode_id: str, session_id: str, started_at: str, prompt_sha256: str, prompt_len: int,
                 tool_commit: str | None, tool_sha256: str | None, cwd_sha256: str) -> dict[str, Any]:
    return {
        "capture_schema": SCHEMA_VERSION,
        "episode_id": episode_id, "session_id": session_id, "prompt_uuid": None,
        "started_at": started_at, "ended_at": None, "ended_by": None,
        "cwd_sha256": cwd_sha256,
        "repo": {"root_sha256": None, "main_root_sha256": None, "worktree_id": None, "head": None,
                 "branch": None, "detached": None},
        "start_snapshot": {"state": "failed", "reason": "not_taken"},
        "boundary": {"cli_version": None, "trusted": False, "reason": "cli_unknown"},
        "link": {"state": "none", "packet_id": None, "brief_sha256": None, "brief_len": None,
                 "brief_empty": None, "matched_on": [], "reason": None, "checked_at": None},
        "prompt": {"sha256": prompt_sha256, "len": prompt_len, "origin": None, "index_in_session": None,
                   "has_attachment": None},
        "prior_context": {"has_prior_conversation": None, "prior_conversation_required": "unknown",
                          "prior_context_evidence": []},
        "deps": {c: {"observed": 0, "required": "unknown", "evidence": [], "observation_sufficient": False}
                 for c in DEP_CATEGORIES},
        "observation": {"sufficient": False, "causes": []},
        "task_class": {"mechanical": None, "owner_override": None},
        "edited_files": [], "outcome_commit": None,
        "exclusions": [], "funnel": {k: False for k in FUNNEL},
        "review": {"state": None, "reason": None, "reviewed_at": None},
        "capture": {"tool_commit": tool_commit, "tool_sha256": tool_sha256, "status": "partial",
                    "errors": [], "start_ms": None, "end_ms": None},
    }


def _tri_any_yes_all_no(states: list[str]) -> str:
    if any(s == "yes" for s in states):
        return "yes"
    if all(s == "no" for s in states):
        return "no"
    return "unknown"


def evaluate_rules(m: dict[str, Any], owner: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """X1–X7 as ``{rule, state, evidence}``; ``owner`` may resolve ``unknown`` on X1–X5 only."""
    deps = m["deps"]
    sufficient = bool(m.get("observation", {}).get("sufficient"))
    out: list[dict[str, Any]] = []

    origin = m["prompt"].get("origin")
    if origin is None:
        out.append({"rule": "X1", "state": "unknown", "evidence": "prompt origin not joined"})
    elif origin in HUMAN_ORIGINS:
        out.append({"rule": "X1", "state": "no", "evidence": f"origin={origin}"})
    else:
        out.append({"rule": "X1", "state": "yes", "evidence": f"origin={origin}"})

    if m["edited_files"]:
        out.append({"rule": "X2", "state": "no", "evidence": f"{len(m['edited_files'])} repository file(s) edited"})
    elif sufficient:
        out.append({"rule": "X2", "state": "yes", "evidence": "no repository edit observed in a complete transcript"})
    else:
        out.append({"rule": "X2", "state": "unknown", "evidence": "no edit observed; observation insufficient"})

    for rule, cats in (("X3", X3_CATEGORIES), ("X4", X4_CATEGORIES)):
        states = [deps[c]["required"] for c in cats]
        state = _tri_any_yes_all_no(states)
        named = [c for c in cats if deps[c]["required"] == state] if state != "no" else list(cats)
        out.append({"rule": rule, "state": state, "evidence": ",".join(f"{c}={deps[c]['required']}" for c in named)})

    pc = m["prior_context"]["prior_conversation_required"]
    out.append({"rule": "X5", "state": pc,
                "evidence": ";".join(e["kind"] for e in m["prior_context"]["prior_context_evidence"]) or "no evidence"})

    ss = m["start_snapshot"]
    if ss.get("state") == "complete":
        out.append({"rule": "X6", "state": "no", "evidence": "snapshot complete and verified"})
    else:
        out.append({"rule": "X6", "state": "yes", "evidence": f"snapshot {ss.get('state')}: {ss.get('reason')}"})

    cs = m["capture"]["status"]
    out.append({"rule": "X7", "state": {"complete": "no", "failed": "yes"}.get(cs, "unknown"),
                "evidence": f"capture.status={cs}"})

    if owner:
        for entry in out:
            r = entry["rule"]
            if r in owner and r in OWNER_RESOLVABLE and entry["state"] == "unknown" and owner[r].get("state") in ("yes", "no"):
                entry["state"] = owner[r]["state"]
                entry["evidence"] += f"; owner: {owner[r].get('basis', '')}"[:300]
                entry["owner_resolved"] = True
    return out


def rule_state(exclusions: list[dict[str, Any]], rule: str) -> str:
    for e in exclusions:
        if e["rule"] == rule:
            return e["state"]
    return "unknown"


def compute_funnel(m: dict[str, Any]) -> dict[str, bool]:
    """Stages from the manifest's own fields; later stages keep their recorded values only
    when every earlier stage holds (monotone by construction)."""
    ex = m["exclusions"]
    f = {k: False for k in FUNNEL}
    f["packet_linked"] = m["link"]["state"] == "linked"
    f["start_snapshot_complete"] = (m["start_snapshot"].get("state") == "complete"
                                    and bool(m.get("boundary", {}).get("trusted")))
    f["dependency_screen_clear"] = all(rule_state(ex, r) == "no" for r in ("X1", "X2", "X3", "X4", "X5"))
    prev = f["packet_linked"] and f["start_snapshot_complete"] and f["dependency_screen_clear"]
    rec = m.get("funnel") or {}
    for stage in ("privacy_cleared", "payload_materialized", "start_state_reconstructed", "replay_verified"):
        f[stage] = bool(prev and rec.get(stage))
        prev = prev and f[stage]
    return f


def validate_funnel(f: dict[str, Any]) -> list[str]:
    """Violations of monotonicity: a later stage true while an earlier one is false."""
    bad = []
    seen_false = None
    for stage in FUNNEL:
        v = bool(f.get(stage))
        if seen_false and v:
            bad.append(f"{stage} true after {seen_false} false")
        if not v and seen_false is None:
            seen_false = stage
    return bad


def provisional_candidate(f: dict[str, Any]) -> bool:
    return all(bool(f.get(s)) for s in FUNNEL[:4])


def load_boundary(cli_version: str | None, search_dirs: list[Path]) -> dict[str, Any]:
    """The boundary block for a CLI version from the first ``<dir>/<version>.json`` found.

    Trusted only when that file exists, was produced by the ordering tests and every
    relied-upon property passed. Unknown or untested versions are never trusted.
    """
    if not cli_version:
        return {"cli_version": None, "trusted": False, "reason": "cli_unknown"}
    for d in search_dirs:
        p = d / f"{cli_version}.json"
        if not p.is_file():
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"cli_version": cli_version, "trusted": False, "reason": "boundary_file_unreadable"}
        required = rec.get("relied_upon") or []
        results = rec.get("results") or {}
        failed = [k for k in required if not results.get(k, {}).get("passed")]
        if failed:
            return {"cli_version": cli_version, "trusted": False, "reason": "boundary_failed:" + ",".join(failed)}
        return {"cli_version": cli_version, "trusted": True, "reason": None, "recorded_at": rec.get("recorded_at")}
    return {"cli_version": cli_version, "trusted": False, "reason": "boundary_untested"}


def refresh(m: dict[str, Any], owner: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute exclusions and funnel in place; returns the manifest."""
    m["exclusions"] = evaluate_rules(m, owner)
    m["funnel"] = compute_funnel(m)
    return m

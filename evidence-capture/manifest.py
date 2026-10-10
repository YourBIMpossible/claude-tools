#!/usr/bin/env python3
"""Manifest schema v2, exclusion rules X1–X7 and the funnel (plan §4.1, §5.2, §5.4).

A manifest holds identifiers, hashes, categories, states and reasons. Never file
contents, prompt text or brief text. Rules and funnel stages are pure functions of the
manifest so they can be recomputed identically at any time.
"""
from __future__ import annotations

import json
from glob import escape as glob_escape
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
        "client": None,
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
    # every exclusion rule (X6/X7 included) must be "no" over a sufficient observation:
    # the same conditions report.blocker_partition counts as clear (review F11)
    f["dependency_screen_clear"] = (bool(m.get("observation", {}).get("sufficient"))
                                    and all(rule_state(ex, r) == "no" for r in RULES))
    prev = f["packet_linked"] and f["start_snapshot_complete"] and f["dependency_screen_clear"]
    rec = m.get("funnel") or {}
    for stage in ("privacy_cleared", "payload_materialized", "start_state_reconstructed", "replay_verified"):
        f[stage] = bool(prev and rec.get(stage))
        prev = prev and f[stage]
    return f


# The first three stages are independently observed gates (link, start snapshot, dependency
# screen); each later stage is reached only when every earlier stage holds.
GATES = FUNNEL[:3]
GATED = FUNNEL[3:]


def validate_funnel(f: dict[str, Any]) -> list[str]:
    """Violations of order: a gated stage true while an earlier stage is false.

    Gate flags are independent observations, so e.g. a complete snapshot with no packet
    link is consistent, not a violation."""
    bad = []
    for i, stage in enumerate(GATED, start=len(GATES)):
        if f.get(stage):
            missing = [s for s in FUNNEL[:i] if not f.get(s)]
            if missing:
                bad.append(f"{stage} true while {missing[0]} false")
    return bad


def cumulative_funnel(fl: list[dict[str, Any]]) -> dict[str, int]:
    """Ordered counts: episodes for which a stage and every stage before it hold."""
    return {stage: sum(1 for f in fl if all(f.get(s) for s in FUNNEL[:i + 1])) for i, stage in enumerate(FUNNEL)}


FAILURE_CAUSES = ("store_unusable", "stores_not_configured")


def failure_cause(reason: str) -> str:
    """The recorded cause of a refused or failed start: a known ``cause: detail`` prefix, else write_error."""
    head = reason.split(":", 1)[0].strip()
    return head if head in FAILURE_CAUSES else "write_error"


def provisional_candidate(f: dict[str, Any]) -> bool:
    return all(bool(f.get(s)) for s in FUNNEL[:4])


def load_boundary(cli_version: str | None, search_dirs: list[Path],
                  client: dict[str, Any] | None = None) -> dict[str, Any]:
    """The boundary block for the client an episode ran under.

    The client is identified by executable SHA-256, CLI version (from the transcript) and
    invocation mode (``client_identity``). The record is the first
    ``<dir>/<version>--<mode>.json`` found. It is trusted only when that record names the
    same executable hash and version, carries an owner acceptance, and every relied-upon
    property passed. An unidentified client, a record for another executable, a record
    without an identity (the version-only records that predate identity) or an
    unaccepted candidate is never trusted.
    """
    if not cli_version:
        return {"cli_version": None, "trusted": False, "reason": "cli_unknown"}
    base: dict[str, Any] = {"cli_version": cli_version, "trusted": False,
                            "client": {k: (client or {}).get(k) for k in ("sha256", "mode", "host_version")}}
    legacy = any((d / f"{cli_version}.json").is_file() for d in search_dirs)
    if not legacy and not any(any(d.glob(f"{glob_escape(cli_version)}--*.json")) for d in search_dirs if d.is_dir()):
        return {**base, "reason": "boundary_untested"}
    if not client or client.get("problem") == "not_observed":
        return {**base, "reason": "identity_unknown"}
    if client.get("problem"):
        return {**base, "reason": f"identity_unverified:{client['problem']}"}
    if not client.get("sha256") or not client.get("mode"):
        return {**base, "reason": "identity_unknown"}
    name = f"{cli_version}--{client['mode']}.json"
    for d in search_dirs:
        p = d / name
        if not p.is_file():
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {**base, "reason": "boundary_file_unreadable"}
        ident = rec.get("identity") or {}
        if ident.get("cli_version") != cli_version or ident.get("mode") != client["mode"] or ident.get("problem"):
            return {**base, "reason": "boundary_record_mismatch"}
        if ident.get("sha256") != client["sha256"]:
            return {**base, "reason": "identity_changed"}
        if not (rec.get("accepted") or {}).get("at"):
            return {**base, "reason": "boundary_unaccepted"}
        required = rec.get("relied_upon") or []
        results = rec.get("results") or {}
        if not required:
            return {**base, "reason": "boundary_failed:nothing_relied_upon"}
        failed = [k for k in required if not results.get(k, {}).get("passed")]
        if failed:
            return {**base, "reason": "boundary_failed:" + ",".join(failed)}
        return {**base, "trusted": True, "reason": None, "recorded_at": rec.get("recorded_at"),
                "accepted_at": rec["accepted"]["at"]}
    return {**base, "reason": "identity_untested" if legacy else "mode_untested"}


def refresh(m: dict[str, Any], owner: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute exclusions and funnel in place; returns the manifest."""
    m["exclusions"] = evaluate_rules(m, owner)
    m["funnel"] = compute_funnel(m)
    return m


_SHAPE: dict[str, type] = {"episode_id": str, "started_at": str, "capture": dict, "prompt": dict, "repo": dict,
                           "link": dict, "start_snapshot": dict, "boundary": dict, "observation": dict,
                           "prior_context": dict, "deps": dict, "exclusions": list, "funnel": dict,
                           "edited_files": list}


def shape_problem(m: Any) -> str | None:
    """Why ``m`` cannot be read by refresh, rescreen or the report, else None. A partial
    manifest is reported by its readers, never allowed to abort a run (review F10)."""
    if not isinstance(m, dict):
        return "not an object"
    for key, kind in _SHAPE.items():
        if not isinstance(m.get(key), kind):
            return f"{key} missing or not {kind.__name__}"
    for c in DEP_CATEGORIES:
        if not isinstance(m["deps"].get(c), dict):
            return f"deps.{c} missing or not dict"
    if not all(isinstance(e, dict) and "rule" in e and "state" in e for e in m["exclusions"]):
        return "exclusions entry without rule/state"
    obs = m["observation"]
    if "sufficient" not in obs or not isinstance(obs.get("causes"), list):
        return "observation without sufficient/causes"
    try:  # every remaining read, on a copy
        refresh(json.loads(json.dumps(m)))
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        return f"refresh failed: {type(exc).__name__}: {exc}"
    return None

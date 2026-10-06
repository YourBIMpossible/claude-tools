"""Deterministic re-screen of joined episodes after a join or screen correction.

Every manifest whose transcript turn was joined is re-joined and re-screened from its
preserved transcript (and the subagent transcripts it references) with the current
tool. Only the fields the join and screen own are replaced: prompt origin, attachment
flag and index, prior context, dependencies, edited files, observation and the
mechanical task class; then the rules and funnel are refreshed. Link, boundary,
snapshot, review and retention are left as they are.

A judgement the current tool can no longer support is invalidated, never kept: when the
turn cannot be found again, or the transcript is gone and the episode was judged
sufficient although it delegated work or touched another session, the observation
becomes insufficient and every ``required: no`` that rested on it becomes ``unknown``.

Each changed manifest gets a ``corrections`` entry (tool commit, before and after); the
run writes ``corrections/<stamp>-rescreen.json`` with before/after counts and the list of
changed episodes. Both are committed to the metadata repository. Hashes and counts only.
"""
from __future__ import annotations

import copy
from collections import Counter
from typing import Any

import manifest as mf
from common import iso, now_utc, write_json_atomic
from join import join
from pipeline import (MAINTAIN_LOCK, _try_lock, _unlock, _untracked_paths, episode_lock, find_transcript,
                      resolve_repo_root, tool_identity, transcript_cwds)
from review import load_owner_review
from screen import screen
from store import Stores, commit_meta, manifest_path, scan_manifests, write_manifest

# Fields the join and screen own; nothing else is copied back from the re-screen.
PROMPT_FIELDS = ("uuid", "origin", "index_in_session", "has_attachment")
DELEGATION_CAUSES = ("missing_subagent_transcript", "subagent_beyond_turn")


def summary(m: dict[str, Any]) -> dict[str, Any]:
    """The comparable judgement of one manifest."""
    return {"origin": m["prompt"].get("origin"), "has_attachment": m["prompt"].get("has_attachment"),
            "sufficient": m["observation"].get("sufficient"), "causes": list(m["observation"].get("causes") or []),
            "required": {c: m["deps"][c].get("required") for c in mf.DEP_CATEGORIES},
            "rules": {e["rule"]: e["state"] for e in m["exclusions"]},
            "dependency_screen_clear": bool(m["funnel"].get("dependency_screen_clear")),
            "edited_files": len(m.get("edited_files") or []),
            "task_class": (m.get("task_class") or {}).get("mechanical")}


def _delegated(m: dict[str, Any]) -> bool:
    join_rec = m["capture"].get("join") or {}
    return bool(join_rec.get("subagents")) or bool(m["deps"]["other_session"].get("observed"))


def invalidate(m: dict[str, Any], cause: str) -> None:
    """Withdraw every judgement that rested on a sufficient observation."""
    causes = set(m["observation"].get("causes") or [])
    causes.add(cause)
    m["observation"] = {"sufficient": False, "causes": sorted(causes)}
    for c in mf.DEP_CATEGORIES:
        dep = m["deps"][c]
        dep["observation_sufficient"] = False
        if c == "prior_conversation":
            kinds = {e.get("kind") for e in dep.get("evidence") or [] if isinstance(e, dict)}
            if dep.get("required") == "no" and "first_prompt" not in kinds:
                dep["required"] = "unknown"
                m["prior_context"]["prior_conversation_required"] = "unknown"
        elif dep.get("required") == "no":
            dep["required"] = "unknown"


def rescreen_one(stores: Stores, m: dict[str, Any], env: dict[str, str]) -> str:
    """Re-join and re-screen ``m`` in place; the outcome label."""
    tr = find_transcript(m["session_id"], env)
    if tr is None:
        if m["observation"].get("sufficient") and _delegated(m):
            invalidate(m, "rescreen_transcript_unavailable")
            return "invalidated_transcript_unavailable"
        return "transcript_unavailable"
    probe = copy.deepcopy(m)
    misses: list[str] = []
    obs = join(probe, tr, misses)
    if obs is None:
        invalidate(m, "rescreen_prompt_ambiguous" if "prompt_ambiguous" in misses else "rescreen_prompt_not_found")
        return "invalidated_prompt_not_found"
    cwds = transcript_cwds(tr)
    root = resolve_repo_root(m, cwds)
    if root is None:
        invalidate(m, "rescreen_root_unresolved")
        return "invalidated_root_unresolved"
    screen(probe, obs, root, cwd=cwds[-1] if cwds else str(root),
           untracked_paths=_untracked_paths(stores, m["episode_id"]), complete=True)
    for k in PROMPT_FIELDS:
        m["prompt"][k] = probe["prompt"].get(k)
    for k in ("prior_context", "deps", "edited_files", "observation"):
        m[k] = probe[k]
    m["task_class"]["mechanical"] = probe["task_class"].get("mechanical")
    m["capture"]["join"].update(subagents=obs.get("subagent_count", 0),
                                subagents_unresolved=obs.get("subagent_unresolved", 0))
    return "rescreened"


def _counts(sums: list[dict[str, Any]]) -> dict[str, Any]:
    return {"episodes": len(sums),
            "observation_sufficient": sum(1 for s in sums if s["sufficient"]),
            "dependency_screen_clear": sum(1 for s in sums if s["dependency_screen_clear"]),
            "by_origin": dict(sorted(Counter(str(s["origin"]) for s in sums).items())),
            "has_attachment": sum(1 for s in sums if s["has_attachment"]),
            "required_no": {c: sum(1 for s in sums if s["required"][c] == "no") for c in mf.DEP_CATEGORIES},
            "rules_yes": {r: sum(1 for s in sums if s["rules"].get(r) == "yes") for r in mf.RULES},
            "rules_unknown": {r: sum(1 for s in sums if s["rules"].get(r) == "unknown") for r in mf.RULES},
            "by_cause": dict(sorted(Counter(c for s in sums for c in s["causes"]).items()))}


def run_rescreen(stores: Stores, env: dict[str, str], *, now: str | None = None,
                 commit: bool = True) -> dict[str, Any]:
    """Re-screen every joined episode under the maintenance lock; the corrections record."""
    lock = stores.meta / MAINTAIN_LOCK
    if not _try_lock(lock):
        return {"action": "skipped", "reason": "maintenance running"}
    now = now or iso(now_utc())
    tool_commit, _ = tool_identity()
    try:
        scan = scan_manifests(stores)
        befores, afters, changed, busy = [], [], [], []
        outcomes: Counter[str] = Counter()
        touched = []
        for m in sorted(scan.manifests, key=lambda x: x["started_at"]):
            if (m["capture"].get("join") or {}).get("state") != "joined":
                continue
            ep = m["episode_id"]
            elock = episode_lock(stores, ep)
            if not _try_lock(elock):
                busy.append(ep)
                continue
            try:
                before = summary(m)
                outcome = rescreen_one(stores, m, env)
                mf.refresh(m, (load_owner_review(stores, ep) or {}).get("rules"))
                after = summary(m)
                outcomes[outcome] += 1
                befores.append(before)
                afters.append(after)
                if after != before:
                    m.setdefault("corrections", []).append(
                        {"at": now, "by": "rescreen", "tool_commit": tool_commit, "outcome": outcome,
                         "before": before, "after": after})
                    write_manifest(stores, m)
                    touched.append(manifest_path(stores, ep))
                    changed.append({"episode_id": ep, "outcome": outcome,
                                    "fields": sorted(k for k in after if after[k] != before[k])})
            finally:
                _unlock(elock)
        rec = {"kind": "rescreen", "at": now, "tool_commit": tool_commit, "outcomes": dict(sorted(outcomes.items())),
               "busy_skipped": busy, "before": _counts(befores), "after": _counts(afters),
               "changed": changed, "manifests_unreadable": len(scan.unreadable)}
        path = stores.meta / "corrections" / f"{now.replace(':', '').replace('-', '')}-rescreen.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, rec)
        if commit:
            rec["committed"] = commit_meta(stores, touched + [path],
                                           f"capture rescreen: {len(changed)} manifest(s) corrected")
        rec["record"] = str(path)
        return rec
    finally:
        _unlock(lock)

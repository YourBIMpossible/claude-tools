#!/usr/bin/env python3
"""Hook-side wiring of ``causal.py`` (plan Phase D3/D4): start and end states per episode.

The prompt hook seals a start overlay and records other-worktree fingerprints, cheap
exclusions and tool versions; the stop hook seals an end overlay and attributes every
start->end change to the agent's recorded writes (main transcript and subagents alike).
Sealed states live in the private content store under ``causal/<episode>/``; the manifest
keeps only counts, hashes and reasons (``m["causal"]``), never a plain path.

Everything is fail-open and explicit: any failure becomes a ``causal:*`` reason in
``m["causal"]["errors"]`` and leaves ordinary capture untouched. An episode without a
complete, attributed start/end pair is simply not admissible; nothing here manufactures
eligibility. History is frozen lazily (``capture.py causal-freeze``), not per turn.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import causal
from common import path_sha256, sha256_bytes

CAUSAL_VERSION = 1
START_MIN_REMAINING_S = 0.4  # below this the start overlay is skipped, never squeezed
TOOLS_WAIT_S = 1.0
END_BUDGET_S = 8.0


def causal_dir(content: Path, ep: str) -> Path:
    return content / "causal" / ep


def _reason(stage: str, exc: BaseException) -> str:
    if isinstance(exc, causal.CausalError):
        return f"causal:{stage}:{str(exc).split(':')[0].split(';')[0].strip().replace(' ', '_')[:60]}"
    return f"causal:{stage}:error:{type(exc).__name__}"


def _hashed(fps: dict[str, str]) -> dict[str, str]:
    return {path_sha256(k): v for k, v in sorted(fps.items())}


def _state_summary(dest: Path, rec: dict[str, Any]) -> dict[str, Any]:
    return {"head": rec["head"], "overlay_files": len(rec["files"]), "deleted": len(rec["deleted"]),
            "state_sha256": sha256_bytes((dest / "state.json").read_bytes())}


def start(root: Path, ep: str, content: Path, *, remaining_s: float) -> dict[str, Any]:
    """Run at prompt submit. Never raises."""
    out: dict[str, Any] = {"version": CAUSAL_VERSION, "start": None, "end": None, "errors": []}
    if remaining_s < START_MIN_REMAINING_S:
        out["errors"].append("causal:start:skipped_budget")
        return out
    t0 = time.perf_counter()
    pending: dict[str, Any] = {}
    try:
        pending = causal.tool_versions_async()
        out["unsupported_start"] = causal.unsupported_state(root, lfs_scan=False)
        out["worktrees_start"] = _hashed(causal.worktree_fingerprints(root))
        dest = causal_dir(content, ep) / "start"
        dest.parent.mkdir(parents=True, exist_ok=True)
        timeout = max(0.2, remaining_s - (time.perf_counter() - t0))
        out["start"] = _state_summary(dest, causal.capture_overlay(root, dest, label="start", timeout=timeout))
    except Exception as exc:  # noqa: BLE001 — fail-open: the reason is the record
        out["errors"].append(_reason("start", exc))
    finally:
        if pending:
            try:
                out["tools_start"] = causal.collect_tool_versions(pending, TOOLS_WAIT_S)
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(_reason("tools_start", exc))
    out["start_ms"] = int((time.perf_counter() - t0) * 1000)
    return out


def end(m: dict[str, Any], root: Path, content: Path, agent_writes: list[str]) -> None:
    """Run at Stop, after screening. Fills ``m["causal"]`` in place. Never raises."""
    c = m.get("causal")
    if not isinstance(c, dict):
        m["causal"] = c = {"version": CAUSAL_VERSION, "start": None, "end": None,
                           "errors": ["causal:start:not_recorded"]}
    if c.get("end") is not None or any(e.startswith("causal:end") for e in c["errors"]):
        return  # an end is sealed once; a later finalize (maintenance) never replaces it
    t0 = time.perf_counter()
    pending: dict[str, Any] = {}
    try:
        pending = causal.tool_versions_async()
        c["unsupported_end"] = causal.unsupported_state(root, lfs_scan=False)
        c["worktrees_end"] = _hashed(causal.worktree_fingerprints(root))
        base = causal_dir(content, m["episode_id"])
        dest = base / "end"
        base.mkdir(parents=True, exist_ok=True)
        end_rec = causal.capture_overlay(root, dest, label="end", timeout=END_BUDGET_S)
        c["end"] = _state_summary(dest, end_rec)
        if c.get("start") is None:
            c["errors"].append("causal:attribution:no_start")
        else:
            start_rec = causal.read_state(base / "start")
            top = Path(causal._git(root, "rev-parse", "--show-toplevel").decode().strip())
            s_eff, e_eff = causal.effective_oids(top, start_rec, end_rec)
            att = causal.attribute(s_eff, e_eff, agent_writes, top=str(top),
                                   worktrees_start=c.get("worktrees_start"), worktrees_end=c.get("worktrees_end"))
            drift = causal.dependency_drift(
                {"lockfiles": causal.lockfile_oids(s_eff), "tools": {}},
                {"lockfiles": causal.lockfile_oids(e_eff), "tools": {}}, agent_writes, top=str(top))
            c["attribution"] = {"changed": len(att["changed"]), "unattributed": len(att["unattributed"]),
                                "unattributed_sha256": sorted(path_sha256(p) for p in att["unattributed"]),
                                "other_worktrees_moved": len(att["other_worktrees_moved"]),
                                "head_moved": att["head_moved"], "isolated": att["isolated"]}
            c["dependency_drift"] = [d.split(":", 1)[0] + ":" + path_sha256(d.split(":", 1)[1]) for d in drift]
    except Exception as exc:  # noqa: BLE001 — fail-open: the reason is the record
        c["errors"].append(_reason("end", exc))
    finally:
        if pending:
            try:
                c["tools_end"] = causal.collect_tool_versions(pending, TOOLS_WAIT_S)
            except Exception as exc:  # noqa: BLE001
                c["errors"].append(_reason("tools_end", exc))
    if c.get("tools_start") is not None and c.get("tools_end") is not None:
        c["dependency_drift"] = sorted(set(c.get("dependency_drift", []))
                                       | set(causal.tool_drift(c["tools_start"], c["tools_end"])))
    c["end_ms"] = int((time.perf_counter() - t0) * 1000)

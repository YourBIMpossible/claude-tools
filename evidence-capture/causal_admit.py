"""Commissioning admission for a captured episode: lazy history freeze, outcome-check
registration, and the full eligibility verdict.

- ``freeze``: packs the repository history at the episode's end HEAD (which must contain
  the start HEAD) into ``content/causal/<ep>/history``. Lazy, so it runs only for
  candidate episodes, never in a hook. The repository is named by the caller and must
  hash to the manifest's recorded root.
- ``register``: records a task's outcome checks in the metadata store and commits them
  before any replay output for that episode exists. A registration is never replaced, at
  least one check must inspect a task-specific file (a generic build/test command alone
  is refused), and a rationale is required.
- ``admit``: ``audit.admission_check`` plus ``causal.admit`` on verified states, verified
  history, the full (LFS-scanning) unsupported-state check of the materialized end state,
  attribution, dependency drift and the registered checks evaluated on that end state.
  Anything missing is a reason; nothing missing is ever inferred as satisfied.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import audit
import causal
from causal_hooks import causal_dir
from common import git_env, iso, now_utc, sha256_bytes, write_json_atomic
from pipeline import root_sha256
from store import Stores, commit_meta, log_line, read_manifest

FILE_CHECK_KINDS = frozenset({"file_sha256", "file_absent", "file_contains"})


def registry_path(stores: Stores) -> Path:
    return stores.meta / "causal" / "outcome-checks.json"


def replay_dir(stores: Stores, ep: str) -> Path:
    """Where commissioning replay outputs for ``ep`` are written; registration refuses once it exists."""
    return stores.content / "replays" / ep


def _registry(stores: Stores) -> dict[str, Any]:
    p = registry_path(stores)
    if not p.is_file():
        return {"tasks": {}}
    causal.load_registry(p)  # validates every entry
    return json.loads(p.read_bytes())


def freeze(stores: Stores, ep: str, repo: Path) -> dict[str, Any]:
    m = read_manifest(stores, ep)
    if root_sha256(repo) != m["repo"].get("root_sha256"):
        raise causal.CausalError("repository does not match the episode's recorded root")
    c = m.get("causal") or {}
    if not (c.get("start") and c.get("end")):
        raise causal.CausalError("episode has no sealed start and end state")
    head = c["end"]["head"]
    top = Path(causal._git(repo, "rev-parse", "--show-toplevel").decode().strip())
    anc = subprocess.run(["git", "-C", str(top), "merge-base", "--is-ancestor", c["start"]["head"], head],
                         capture_output=True, env=git_env(), timeout=60)
    if anc.returncode != 0:
        raise causal.CausalError("start head is not an ancestor of end head")
    return causal.freeze_history(top, causal_dir(stores.content, ep) / "history", head=head)


def register(stores: Stores, ep: str, checks: list[dict[str, Any]], rationale: str) -> dict[str, Any]:
    if replay_dir(stores, ep).exists():
        raise causal.CausalError("replay outputs already exist; checks must be registered before replay")
    if not rationale.strip():
        raise causal.CausalError("a rationale tying the checks to the task is required")
    if not any(c.get("kind") in FILE_CHECK_KINDS for c in checks):
        raise causal.CausalError("at least one task-specific file check is required; a generic command alone is refused")
    reg = _registry(stores)
    if ep in reg["tasks"]:
        raise causal.CausalError("checks already registered; a registration is never replaced")
    read_manifest(stores, ep)  # the episode must exist
    entry = {"checks": checks, "rationale": rationale, "registered_at": iso(now_utc()),
             "checks_sha256": sha256_bytes(json.dumps(checks, sort_keys=True).encode())}
    reg["tasks"][ep] = entry
    p = registry_path(stores)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".new")
    write_json_atomic(tmp, reg)
    causal.load_registry(tmp)
    tmp.replace(p)
    if not commit_meta(stores, [p], f"capture causal: outcome checks registered for {ep}"):
        raise causal.CausalError("registration not committed to the metadata store")
    log_line(stores, f"causal register {ep}: {entry['checks_sha256']}")
    return {"episode_id": ep, "registered_at": entry["registered_at"], "checks_sha256": entry["checks_sha256"]}


def admit(stores: Stores, ep: str) -> dict[str, Any]:
    base = audit.admission_check(stores, ep)
    try:
        m = read_manifest(stores, ep)
    except (OSError, ValueError):
        return {**base, "eligible": False, "causal_reasons": ["manifest missing"]}
    c = m.get("causal") or {}
    d = causal_dir(stores.content, ep)
    rec: dict[str, Any] = {"identity_accepted": (m.get("boundary") or {}).get("trusted") is True}
    for side in ("start", "end", "history"):
        sd = d / side
        if side != "history" and not c.get(side):
            rec[f"{side}_problems"] = None
        elif not sd.is_dir():
            rec[f"{side}_problems"] = None
        else:
            rec[f"{side}_problems"] = causal.verify_history(sd) if side == "history" else causal.verify_state(sd)
    rec["unsupported"] = sorted(set(c.get("unsupported_start") or []) | set(c.get("unsupported_end") or []))
    rec["attribution"] = c.get("attribution")
    rec["dependency_drift"] = c.get("dependency_drift") or []
    reg = _registry(stores)["tasks"].get(ep)
    rec["checks"] = reg["checks"] if reg else None
    extra = [f"causal_error:{e}" for e in c.get("errors") or []]
    if c.get("start") is None or c.get("end") is None or rec["history_problems"] is None:
        pass  # causal.admit reports the missing piece
    elif not any(rec[f"{s}_problems"] for s in ("start", "end", "history")):
        with tempfile.TemporaryDirectory(prefix="ec-admit-") as tmp:
            out = Path(tmp) / "end"
            probs = causal.materialize_overlay(d / "end", d / "history", out)
            if probs:
                rec["end_problems"] = [f"materialize: {p}" for p in probs]
            else:
                rec["unsupported"] = sorted(set(rec["unsupported"]) | set(causal.unsupported_state(out)))
                if rec["checks"]:
                    rec["check_results"] = causal.run_checks(rec["checks"], out)
    verdict = causal.admit(rec)
    reasons = verdict["reasons"] + extra
    return {"episode_id": ep, "eligible": base["admitted"] and not reasons, "admission_reasons": base["reasons"],
            "causal_reasons": reasons, "check_results": rec.get("check_results")}

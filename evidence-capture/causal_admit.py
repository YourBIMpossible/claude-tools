"""Commissioning admission for a captured episode: lazy history freeze, correctness-predicate
registration, the full eligibility verdict and the run guard.

- ``freeze``: packs the repository history at the episode's end HEAD (which must contain
  the start HEAD) into ``content/causal/<ep>/history``. Lazy, so it runs only for
  candidate episodes, never in a hook. The repository is named by the caller and must
  hash to the manifest's recorded root.
- ``register``: preregisters a task's correctness predicate (``causal.validate_predicate``:
  task-specific, independently executable, behavioral and/or state-based) with a
  rationale, as one immutable file per episode in the metadata store, committed before any
  replay output exists. A registration is never replaced.
- ``verify_registration``: the registration exists, is committed exactly once and
  unmodified, and its recorded hash matches its checks and rationale.
- ``admit``: ``audit.admission_check`` plus ``causal.admit``: verified states and history,
  byte-exact fidelity of BOTH materialized states against the capture-time manifests, the
  full (LFS-scanning) unsupported check, attribution, other-worktree classification,
  dependency drift, and the predicate run on start (must fail) and end (must pass).
  Anything missing is a reason; nothing missing is ever inferred as satisfied.
- ``authorize_run`` / ``launch``: every commissioning launch goes through ``launch``, which
  refuses unless the registration verifies and the episode is admitted, whatever the
  output location.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import audit
import causal
from causal_hooks import causal_dir
from common import git_env, iso, now_utc, sha256_bytes
from pipeline import root_sha256
from store import Stores, commit_meta, log_line, read_manifest


def checks_path(stores: Stores, ep: str) -> Path:
    return stores.meta / "causal" / "checks" / f"{ep}.json"


def replay_dir(stores: Stores, ep: str) -> Path:
    """Where commissioning replay outputs for ``ep`` are written; registration refuses once it exists."""
    return stores.content / "replays" / ep


def _digest(checks: list[dict[str, Any]], rationale: str) -> str:
    return sha256_bytes(json.dumps({"checks": checks, "rationale": rationale}, sort_keys=True).encode())


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
        raise causal.CausalError("a rationale tying the predicate to the task is required")
    causal.validate_predicate(checks)
    p = checks_path(stores, ep)
    if p.exists():
        raise causal.CausalError("checks already registered; a registration is never replaced")
    read_manifest(stores, ep)  # the episode must exist
    entry = {"episode_id": ep, "checks": checks, "rationale": rationale, "registered_at": iso(now_utc()),
             "sha256": _digest(checks, rationale)}
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "xb") as fh:  # exclusive create: a registration is never replaced
        fh.write(json.dumps(entry, sort_keys=True, indent=1).encode("utf-8"))
    if not commit_meta(stores, [p], f"capture causal: correctness predicate registered for {ep}"):
        p.unlink()
        raise causal.CausalError("registration not committed to the metadata store")
    log_line(stores, f"causal register {ep}: {entry['sha256']}")
    return {"episode_id": ep, "registered_at": entry["registered_at"], "sha256": entry["sha256"]}


def verify_registration(stores: Stores, ep: str) -> tuple[dict[str, Any] | None, list[str] | None]:
    """``(entry, problems)``; ``(None, None)`` when nothing is registered."""
    p = checks_path(stores, ep)
    if not p.exists():
        return None, None
    probs: list[str] = []
    try:
        entry = json.loads(p.read_bytes())
        if entry.get("episode_id") != ep or entry.get("sha256") != _digest(entry["checks"], entry["rationale"]):
            probs.append("registration_hash_mismatch")
        causal.validate_predicate(entry["checks"])
    except (OSError, ValueError, KeyError, TypeError, causal.CausalError) as exc:
        return None, [f"registration_invalid:{type(exc).__name__}"]
    rel = p.resolve().relative_to(stores.meta.resolve()).as_posix()
    g = ["git", "-C", str(stores.meta)]
    if subprocess.run([*g, "ls-files", "--error-unmatch", "--", rel], capture_output=True).returncode != 0:
        probs.append("registration_not_committed")
    elif subprocess.run([*g, "diff", "--quiet", "HEAD", "--", rel], capture_output=True).returncode != 0:
        probs.append("registration_modified")
    elif (log := subprocess.run([*g, "log", "--format=%H", "--", rel], capture_output=True,
                                text=True)).returncode != 0:
        probs.append("registration_history_unreadable")  # git failed: the history was not seen
    elif len(log.stdout.split()) != 1:
        probs.append("registration_rewritten")  # committed more than once
    return entry, probs


def _materialize_and_check(d: Path, side: str, checks: list[dict[str, Any]] | None,
                           rec: dict[str, Any], tmp: Path) -> None:
    out = tmp / side
    probs = causal.materialize_overlay(d / side, d / "history", out)
    if probs:
        rec[f"{side}_problems"] = [f"materialize: {p}" for p in probs]
        return
    rec[f"{side}_fidelity"] = causal.verify_fidelity(d / side, out)
    if side == "end":
        rec["unsupported"] = sorted(set(rec["unsupported"]) | set(causal.unsupported_state(out)))
    if checks:
        rec["check_results" if side == "end" else "check_results_start"] = causal.run_checks(checks, out)


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
        if (side != "history" and not c.get(side)) or not sd.is_dir():
            rec[f"{side}_problems"] = None
        else:
            rec[f"{side}_problems"] = causal.verify_history(sd) if side == "history" else causal.verify_state(sd)
    rec["unsupported"] = sorted(set(c.get("unsupported_start") or []) | set(c.get("unsupported_end") or []))
    rec["attribution"] = c.get("attribution")
    w = c.get("worktrees")
    rec["worktrees"] = None if not isinstance(w, dict) else {
        "contaminating": [None] * int(w["contaminating"]), "unknown": [None] * int(w["unknown"]),
        "shared_state_changed": w["shared_state_changed"]}
    rec["dependency_drift"] = c.get("dependency_drift") or []
    entry, rec["registration_problems"] = verify_registration(stores, ep)
    rec["checks"] = entry["checks"] if entry and not rec["registration_problems"] else None
    extra = [f"causal_error:{e}" for e in c.get("errors") or []]
    if all(rec[f"{s}_problems"] == [] for s in ("start", "end", "history")):
        with tempfile.TemporaryDirectory(prefix="ec-admit-") as tmp:
            for side in ("start", "end"):
                _materialize_and_check(d, side, rec["checks"], rec, Path(tmp))
    verdict = causal.admit(rec)
    reasons = verdict["reasons"] + extra
    return {"episode_id": ep, "eligible": base["admitted"] and not reasons, "admission_reasons": base["reasons"],
            "causal_reasons": reasons, "check_results": rec.get("check_results"),
            "check_results_start": rec.get("check_results_start")}


class RunRefused(RuntimeError):
    """A commissioning launch was refused; nothing was started."""


def authorize_run(stores: Stores, ep: str) -> dict[str, Any]:
    """Raises ``RunRefused`` unless an immutable, verified predicate registration exists
    and the episode is admitted."""
    entry, probs = verify_registration(stores, ep)
    if entry is None or probs is None:
        raise RunRefused(f"{ep}: no verified predicate registration" + (f" ({', '.join(probs)})" if probs else ""))
    if probs:
        raise RunRefused(f"{ep}: registration does not verify ({', '.join(probs)})")
    verdict = admit(stores, ep)
    if not verdict["eligible"]:
        raise RunRefused(f"{ep}: not admitted ({', '.join(verdict['admission_reasons'] + verdict['causal_reasons'])})")
    return {"episode_id": ep, "registration_sha256": entry["sha256"]}


def launch(stores: Stores, ep: str, run: Callable[[dict[str, Any]], Any]) -> Any:
    """The only sanctioned entry to a commissioning run: ``run`` is called (with the
    authorization) only after ``authorize_run`` succeeds."""
    auth = authorize_run(stores, ep)
    log_line(stores, f"causal launch {ep}: authorized {auth['registration_sha256']}")
    return run(auth)

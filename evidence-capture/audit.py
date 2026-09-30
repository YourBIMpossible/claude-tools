#!/usr/bin/env python3
"""False-negative audit of the dependency screen and the first-batch admission gate (plan §5.3).

1. ``build_sheet`` takes the first 20 provisional candidates (by ``started_at``) not in
   an earlier sheet, or all of them if fewer, and writes two committed files:
   ``audits/fn-audit-<k>.labels.json``, the blind sheet the owner fills in (one label per
   category plus prior context: ``needed``, ``not_needed`` or ``cannot_tell``, each with a
   one-line basis; no screen value appears in it), and ``audits/fn-audit-<k>.screen.json``,
   the screen's values at sheet time. The owner labels from the transcript without
   opening the screen file. A new sheet is refused while an earlier one is unscored.
2. The owner sets ``"sealed": true`` and commits the sheet. ``labels_sealed`` accepts it
   only when every label is valid with a basis, the episode list is the sheet's, and the
   file is committed and unmodified in the metadata repository. Nothing reads the labels
   before that.
3. ``score`` counts, per category, FN (screen ``no``, owner ``needed``), TN and
   ``cannot_tell``, and gives exact Clopper–Pearson 95 % intervals. It never claims a
   rate below the interval's upper bound (n = 20, k = 0 gives an upper bound near 17 %).
   Any miss in ``live_remote``, ``outward_action`` or ``other_session`` requires a screen
   amendment before further candidates are admitted (``record_amendment``).
4. ``admission_check`` gates each first-batch episode: a scored, sealed audit covering
   it with no pending amendment, the episode still a provisional candidate with a
   verified payload, and a complete case-by-case owner review
   (``review/<episode_id>.json`` → ``case_by_case``: every category and prior context
   labelled ``not_needed`` with a basis), whatever the audit showed.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any

import manifest as mf
from common import iso, now_utc, read_json, sha256_file, write_json_atomic
from review import load_owner_review, verify_payload
from store import Stores, commit_meta, log_line, read_manifest, scan_manifests

SAMPLE = 20
CATEGORIES = (*mf.DEP_CATEGORIES, "prior_context")
LABELS = ("needed", "not_needed", "cannot_tell")
AMEND_ON_MISS = ("live_remote", "outward_action", "other_session")


class AuditError(RuntimeError):
    """A precondition of the audit does not hold; nothing was written."""


# ---------------------------------------------------------------------- statistics

def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def _bisect(f: Any, lo: float = 0.0, hi: float = 1.0, iters: int = 100) -> float:
    """Root of a function decreasing in p on [lo, hi]."""
    for _ in range(iters):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> list[float] | None:
    """Exact two-sided interval for a binomial proportion; None when ``n`` is 0."""
    if n <= 0:
        return None
    lo = 0.0 if k == 0 else _bisect(lambda p: alpha / 2 - (1 - _binom_cdf(k - 1, n, p)))
    hi = 1.0 if k == n else _bisect(lambda p: _binom_cdf(k, n, p) - alpha / 2)
    return [round(lo, 4), round(hi, 4)]


# ---------------------------------------------------------------------- files

def _audits(stores: Stores) -> Path:
    return stores.meta / "audits"


def labels_path(stores: Stores, k: int) -> Path:
    return _audits(stores) / f"fn-audit-{k}.labels.json"


def screen_path(stores: Stores, k: int) -> Path:
    return _audits(stores) / f"fn-audit-{k}.screen.json"


def result_path(stores: Stores, k: int) -> Path:
    return _audits(stores) / f"fn-audit-{k}.json"


def sheet_numbers(stores: Stores) -> list[int]:
    out = []
    for p in _audits(stores).glob("fn-audit-*.labels.json"):
        stem = p.name[len("fn-audit-"):-len(".labels.json")]
        if stem.isdigit():
            out.append(int(stem))
    return sorted(out)


def _committed_unmodified(stores: Stores, path: Path) -> bool:
    rel = path.relative_to(stores.meta).as_posix()
    tracked = subprocess.run(["git", "-C", str(stores.meta), "ls-files", "--error-unmatch", rel],
                             capture_output=True).returncode == 0
    status = subprocess.run(["git", "-C", str(stores.meta), "status", "--porcelain", "--", rel],
                            capture_output=True, text=True)
    return tracked and status.returncode == 0 and not status.stdout.strip()


def screen_values(m: dict[str, Any]) -> dict[str, str]:
    vals = {c: m["deps"][c]["required"] for c in mf.DEP_CATEGORIES}
    vals["prior_context"] = m["prior_context"]["prior_conversation_required"]
    return vals


# ---------------------------------------------------------------------- sheet

def build_sheet(stores: Stores, *, now: str | None = None, commit: bool = True) -> dict[str, Any]:
    now = now or iso(now_utc())
    done = sheet_numbers(stores)
    open_sheets = [k for k in done if not result_path(stores, k).is_file()]
    if open_sheets:
        raise AuditError(f"sheet {open_sheets[0]} is not scored yet")
    covered: set[str] = set()
    for k in done:
        covered.update(e["episode_id"] for e in read_json(screen_path(stores, k))["episodes"])
    scan = scan_manifests(stores)
    if scan.unreadable:
        # a sample drawn without them is not a sample of the candidates
        raise AuditError(f"{len(scan.unreadable)} manifest(s) unreadable: {', '.join(scan.unreadable[:5])}")
    cands = sorted((m for m in scan.manifests
                    if mf.provisional_candidate(m["funnel"]) and m["episode_id"] not in covered),
                   key=lambda m: m["started_at"])[:SAMPLE]
    if not cands:
        raise AuditError("no provisional candidate to audit")
    k = (done[-1] + 1) if done else 1
    sheet = {"audit": k, "created_at": now, "sealed": False,
             "instructions": "For each episode and category, set label to needed, not_needed or cannot_tell "
                             "and give a one-line basis, judged from the transcript alone. Do not open the "
                             "screen file. Set sealed to true and commit when every label is filled in.",
             "episodes": [{"episode_id": m["episode_id"],
                           "labels": {c: {"label": None, "basis": ""} for c in CATEGORIES}} for m in cands]}
    _audits(stores).mkdir(parents=True, exist_ok=True)
    lp = labels_path(stores, k)
    write_json_atomic(lp, sheet)
    screen = {"audit": k, "created_at": now, "sheet_sha256": sha256_file(lp),
              "episodes": [{"episode_id": m["episode_id"], "screen": screen_values(m)} for m in cands]}
    sp = screen_path(stores, k)
    write_json_atomic(sp, screen)
    if commit:
        commit_meta(stores, [lp, sp], f"capture fn-audit {k}: sheet ({len(cands)} episodes)")
    log_line(stores, f"fn-audit {k}: sheet with {len(cands)} episode(s)")
    return {"audit": k, "episodes": len(cands), "labels": lp, "screen": sp}


def labels_sealed(stores: Stores, k: int) -> list[str]:
    """Problems that keep sheet ``k`` from counting as sealed; empty when sealed."""
    lp, sp = labels_path(stores, k), screen_path(stores, k)
    if not lp.is_file() or not sp.is_file():
        return ["sheet or screen file missing"]
    try:
        sheet, screen = read_json(lp), read_json(sp)
    except (OSError, ValueError):
        return ["sheet unreadable"]
    problems = []
    if sheet.get("sealed") is not True:
        problems.append("not sealed")
    want = [e["episode_id"] for e in screen["episodes"]]
    got = [e.get("episode_id") for e in sheet.get("episodes", [])]
    if got != want:
        problems.append("episode list differs from the sheet")
    for e in sheet.get("episodes", []):
        labels = e.get("labels") or {}
        for c in CATEGORIES:
            lab = labels.get(c) or {}
            if lab.get("label") not in LABELS:
                problems.append(f"{e.get('episode_id')}/{c}: label missing or invalid")
            elif not str(lab.get("basis") or "").strip():
                problems.append(f"{e.get('episode_id')}/{c}: basis missing")
    if not _committed_unmodified(stores, lp):
        problems.append("sheet not committed or modified since commit")
    return problems


# ---------------------------------------------------------------------- score

def score(stores: Stores, k: int, *, now: str | None = None, commit: bool = True) -> dict[str, Any]:
    now = now or iso(now_utc())
    problems = labels_sealed(stores, k)
    if problems:
        raise AuditError("labels not sealed: " + "; ".join(problems[:5]))
    sheet, screen = read_json(labels_path(stores, k)), read_json(screen_path(stores, k))
    labels = {e["episode_id"]: e["labels"] for e in sheet["episodes"]}
    per: dict[str, dict[str, Any]] = {}
    ep_miss: dict[str, list[str]] = {}
    for c in CATEGORIES:
        fn = tn = ct = screened_other = 0
        for e in screen["episodes"]:
            ep, s = e["episode_id"], e["screen"][c]
            lab = labels[ep][c]["label"]
            if s != "no":
                screened_other += 1
                continue
            if lab == "needed":
                fn += 1
                ep_miss.setdefault(ep, []).append(c)
            elif lab == "not_needed":
                tn += 1
            else:
                ct += 1
        decided = fn + tn
        per[c] = {"fn": fn, "tn": tn, "cannot_tell": ct, "screen_not_no": screened_other, "n": decided,
                  "interval_95": clopper_pearson(fn, decided)}
    n = len(screen["episodes"])
    misses = sum(v["fn"] for v in per.values())
    amend = sorted(c for c in AMEND_ON_MISS if per[c]["fn"])
    interval = clopper_pearson(len(ep_miss), n)
    result = {
        "audit": k, "n": n, "audited_at": now, "sheet_sha256": screen["sheet_sha256"],
        "labels_sha256": sha256_file(labels_path(stores, k)), "labels_sealed": True,
        "episodes": [e["episode_id"] for e in screen["episodes"]],
        "per_category": per, "misses_total": misses, "episodes_with_miss": len(ep_miss),
        "interval_95": interval,
        "statement": (f"{len(ep_miss)} of {n} audited episodes had at least one screen miss; the exact 95% "
                      f"interval for the episode miss rate is {interval}. No rate below the upper bound is "
                      "claimed."),
        "amend_screen_required": amend, "screen_amended_after_audit": False, "amendments": [],
    }
    rp = result_path(stores, k)
    write_json_atomic(rp, result)
    if commit:
        commit_meta(stores, [rp], f"capture fn-audit {k}: scored ({misses} miss(es))")
    log_line(stores, f"fn-audit {k}: scored, misses {misses}, amend {amend or 'none'}")
    return result


def record_amendment(stores: Stores, k: int, note: str, tool_commit: str | None, *, now: str | None = None,
                     commit: bool = True) -> dict[str, Any]:
    """Record that the screen was amended in response to audit ``k`` (the code change
    itself is a separate commit of the capture tool, named by ``tool_commit``)."""
    rp = result_path(stores, k)
    if not rp.is_file():
        raise AuditError(f"audit {k} is not scored")
    if not note.strip() or not tool_commit:
        raise AuditError("an amendment needs a note and the amended tool commit")
    res = read_json(rp)
    res["amendments"].append({"at": now or iso(now_utc()), "note": note.strip()[:500], "tool_commit": tool_commit})
    res["screen_amended_after_audit"] = True
    write_json_atomic(rp, res)
    if commit:
        commit_meta(stores, [rp], f"capture fn-audit {k}: screen amendment recorded")
    return res


# ---------------------------------------------------------------------- admission

def _case_by_case_problems(owner: dict[str, Any] | None) -> list[str]:
    cbc = (owner or {}).get("case_by_case")
    if not isinstance(cbc, dict):
        return ["case-by-case review missing"]
    problems = []
    for c in CATEGORIES:
        entry = cbc.get(c) or {}
        if entry.get("label") not in LABELS or not str(entry.get("basis") or "").strip():
            problems.append(f"case-by-case {c}: label or basis missing")
        elif entry["label"] != "not_needed":
            problems.append(f"case-by-case {c}: {entry['label']}")
    return problems


def admission_check(stores: Stores, episode: str) -> dict[str, Any]:
    """Whether ``episode`` may enter the first paid batch; every reason it may not."""
    reasons: list[str] = []
    audit = None
    for k in sheet_numbers(stores):
        rp = result_path(stores, k)
        if rp.is_file():
            res = read_json(rp)
            if episode in res.get("episodes", []):
                audit = res
    if audit is None:
        reasons.append("not covered by a scored FN audit")
    else:
        if audit["amend_screen_required"] and not audit["screen_amended_after_audit"]:
            reasons.append("screen amendment pending for " + ",".join(audit["amend_screen_required"]))
        if audit.get("labels_sha256") != sha256_file(labels_path(stores, audit["audit"])):
            reasons.append("audit labels changed after scoring")
    try:
        m = read_manifest(stores, episode)
    except (OSError, ValueError):
        return {"episode_id": episode, "admitted": False, "reasons": ["manifest missing"]}
    if not mf.provisional_candidate(m["funnel"]):
        reasons.append("not a provisional candidate")
    if not m["funnel"].get("payload_materialized"):
        reasons.append("payload not materialized")
    elif verify_payload(stores, episode):
        reasons.append("payload fails verification")
    reasons += _case_by_case_problems(load_owner_review(stores, episode))
    return {"episode_id": episode, "admitted": not reasons, "reasons": reasons,
            "audit": audit["audit"] if audit else None}

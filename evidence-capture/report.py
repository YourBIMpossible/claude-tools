#!/usr/bin/env python3
"""The §12 checkpoint: counts and hashes over the metadata store, never content.

``build_checkpoint`` reads manifests, owner reviews, FN audits, ``DELETIONS.log``,
``maintenance.json`` and isolation records; it never opens a snapshot or payload file
except to count directories and bytes and to verify checksums. ``write_checkpoint``
writes ``checkpoints/ckpt-<n>.json`` and its markdown rendering and commits both.

Keys beyond the §12 list are additive and documented where they are produced:
``denominator.by_origin.unjoined`` (episodes whose transcript turn was never found, so
their origin is unknown), ``funnel.boundary.untrusted_raw`` (the manifest's own reasons
before mapping onto the four §12 buckets), ``denominator.by_origin.peer``/``meta``
(continuations recorded but never promoted), ``stage_status`` (privacy as passed, failed
or not_reached; reconstruction and replay as passed, failed, not_evaluated or unknown),
``human_yield`` (the experimental population: completed human tasks, with every
independent blocker counted on its own) and ``notes``.
"""
from __future__ import annotations

import copy
import math
from collections import Counter
from pathlib import Path
from typing import Any

import manifest as mf
from common import canonical, iso, now_utc, parse_iso, read_json, sha256_bytes, sha256_file, write_json_atomic
from review import load_owner_review, verify_payload
from store import Stores, check_content_store, commit_meta, meta_clean, read_deletions, scan_manifests

EPISODES_TARGET = 50
ORIGIN_BUCKETS = ("human", "task_notification", "peer", "meta", "hook", "other")
SNAPSHOT_REASONS = ("timeout", "size_cap", "write_error", "head_unresolved", "store_unusable", "stores_not_configured")
BOUNDARY_BUCKETS = ("cli_untested", "bracket_dirty", "lock_present", "hook_killed")


def _origin_bucket(origin: str | None) -> str | None:
    if origin is None:
        return None
    if origin in mf.HUMAN_ORIGINS:
        return "human"
    if origin in ("task_notification", "peer", "meta", "hook"):
        return origin
    return "other"


def privacy_status(m: dict[str, Any]) -> str:
    """``passed``/``failed`` only when the privacy-scope check actually ran; an episode
    held or excluded before it is ``not_reached``, never a privacy failure."""
    pv = (m.get("review") or {}).get("privacy")
    if not isinstance(pv, dict) or pv.get("passed") is None:
        return "not_reached"
    return "passed" if pv["passed"] else "failed"


def later_stage_status(m: dict[str, Any], stage: str) -> str:
    """Reconstruction or replay: ``not_evaluated`` until every earlier stage holds;
    then ``passed`` when recorded true, ``failed`` when a failure is recorded under
    ``m[stage]``, else ``unknown`` (reached, no result)."""
    i = mf.FUNNEL.index(stage)
    f = m["funnel"]
    if not all(f.get(s) for s in mf.FUNNEL[:i]):
        return "not_evaluated"
    if f.get(stage):
        return "passed"
    return "failed" if (m.get(stage) or {}).get("state") == "failed" else "unknown"


def human_yield(ms: list[dict[str, Any]]) -> dict[str, Any]:
    """Candidate yield among completed human tasks. Each blocker is counted
    independently (an episode with three blockers counts in all three), and zero
    candidates is reported as a count, not a judgement about replayability."""
    pop = [m for m in ms if _origin_bucket(m["prompt"].get("origin")) == "human"
           and m["capture"].get("status") == "complete"]
    blockers: Counter[str] = Counter()
    for m in pop:
        f = m["funnel"]
        if not f.get("packet_linked"):
            blockers["link:" + str(m["link"].get("state"))] += 1
        if m["start_snapshot"].get("state") != "complete":
            blockers["snapshot:" + str(m["start_snapshot"].get("reason") or m["start_snapshot"].get("state"))] += 1
        if not m["boundary"].get("trusted"):
            blockers["boundary:" + _boundary_bucket(m["boundary"].get("reason"))] += 1
        for r in ("X1", "X2", "X3", "X4", "X5"):
            st = mf.rule_state(m["exclusions"], r)
            if st != "no":
                blockers[f"{r}:{st}"] += 1
        if not m["observation"].get("sufficient", False):
            blockers["observation:insufficient"] += 1
        if privacy_status(m) != "passed":
            blockers["privacy:" + privacy_status(m)] += 1
    return {"population": "prompt.origin human, capture complete", "completed_human_tasks": len(pop),
            "provisional_candidates": sum(1 for m in pop if mf.provisional_candidate(m["funnel"])),
            "cumulative": mf.cumulative_funnel([m["funnel"] for m in pop]),
            "blocked_only_by": dict(sorted(Counter(
                k for m in pop for k in [_sole_blocker(m)] if k).items())),
            "independent_blockers": dict(sorted(blockers.items())),
            "partition": blocker_partition(pop)}


def blocker_partition(pop: list[dict[str, Any]]) -> dict[str, Any]:
    """Each completed human task in exactly one class, so the classes sum to the
    population. ``boundary_only``: every other pre-privacy gate holds (link, start
    snapshot, sufficient observation, every rule ``no``), so accepting the CLI boundary
    would let it reach privacy. ``boundary_plus_other``: the boundary is untrusted and
    something else also fails, split by whether that is a known scope exclusion (a rule
    ``yes``) or only unresolved evidence. With a trusted boundary, ``scope_excluded``
    (a rule ``yes``) and ``unresolved`` (rule ``unknown``, insufficient observation,
    no link, incomplete snapshot) follow. Privacy stays ``not_reached`` for every
    episode not past the gates, never ``failed``."""
    out: Counter[str] = Counter()
    for m in pop:
        if mf.provisional_candidate(mf.compute_funnel(m)):
            out["candidate"] += 1
            continue
        states = [mf.rule_state(m["exclusions"], r) for r in mf.RULES]
        scope = "yes" in states
        unresolved = ("unknown" in states or not m["observation"].get("sufficient", False)
                      or m["link"].get("state") != "linked"
                      or m["start_snapshot"].get("state") != "complete")
        if not m["boundary"].get("trusted"):
            b = _boundary_bucket(m["boundary"].get("reason"))
            if not scope and not unresolved:
                out[f"boundary_only:{b}"] += 1
            else:
                out[f"boundary_plus_{'scope_excluded' if scope else 'unresolved'}:{b}"] += 1
        elif scope:
            out["scope_excluded"] += 1
        elif unresolved:
            out["unresolved"] += 1
        else:
            out["privacy_" + privacy_status(m)] += 1
    return {"classes": dict(sorted(out.items())), "total": sum(out.values())}


def _sole_blocker(m: dict[str, Any]) -> str | None:
    """The one gate stopping an episode when exactly one of link, snapshot+boundary and
    screen fails; None otherwise."""
    f = m["funnel"]
    failing = [g for g in mf.GATES if not f.get(g)]
    if len(failing) != 1:
        return None
    if failing[0] == "dependency_screen_clear":
        yes = [r for r in ("X1", "X2", "X3", "X4", "X5") if mf.rule_state(m["exclusions"], r) != "no"]
        return "screen:" + "+".join(yes)
    return failing[0]


def _boundary_bucket(reason: str | None) -> str:
    """Maps a manifest boundary reason onto §12's four buckets. An untested, unknown,
    failed, unaccepted or unreadable record, and an unidentified, unverified or changed
    client (the ``identity_*`` and ``mode_untested`` reasons), is ``cli_untested``; a
    failed capture has no tested boundary for its episode either."""
    if reason in ("bracket_dirty", "lock_present", "hook_killed"):
        return reason
    return "cli_untested"


def percentiles(values: list[int]) -> dict[str, int | None]:
    """Nearest-rank p50 and p95 and the maximum."""
    if not values:
        return {"p50": None, "p95": None, "max": None}
    v = sorted(values)

    def rank(p: float) -> int:
        return v[max(0, math.ceil(p * len(v)) - 1)]
    return {"p50": rank(0.50), "p95": rank(0.95), "max": v[-1]}


def _dir_bytes(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _latest_audit(stores: Stores) -> dict[str, Any] | None:
    best = None
    for p in sorted((stores.meta / "audits").glob("fn-audit-*.json")):
        stem = p.name[len("fn-audit-"):-len(".json")]
        if not stem.isdigit():
            continue
        try:
            rec = read_json(p)
        except (OSError, ValueError):
            continue
        if best is None or int(stem) > best[0]:
            best = (int(stem), rec)
    return best[1] if best else None


def _isolation(stores: Stores) -> dict[str, Any]:
    """Records ``isolation.py --out <meta>/isolation/<name>.json`` wrote. A record that cannot
    be read or holds no boolean ``passed`` is counted as unreadable, and as failed."""
    recs, unreadable = [], 0
    for p in sorted((stores.meta / "isolation").glob("*.json")):
        try:
            rec = read_json(p)
        except (OSError, ValueError):
            unreadable += 1
            continue
        if isinstance(rec, dict) and isinstance(rec.get("passed"), bool):
            recs.append((p.name, rec))
        else:
            unreadable += 1
    out: dict[str, Any] = {"deterministic_checks_run": len(recs) + unreadable,
                           "passed": sum(1 for _, r in recs if r["passed"]),
                           "failed": sum(1 for _, r in recs if not r["passed"]) + unreadable,
                           "unreadable": unreadable, "identity": None, "acl_dump_sha256": None}
    if recs:
        last = recs[-1][1]
        out["identity"] = last.get("identity")
        out["acl_dump_sha256"] = sha256_bytes(canonical(last.get("icacls") or {}).encode("utf-8"))
    return out


def build_checkpoint(stores: Stores, n: int, *, tool_commit: str | None = None, now: str | None = None,
                     maintenance: dict[str, Any] | None = None,
                     backstop: dict[str, Any] | None = None) -> dict[str, Any]:
    now = now or iso(now_utc())
    scan = scan_manifests(stores)
    # One classification for every count: each well-formed manifest is read refreshed (rules
    # with its owner review, then the funnel), never as an older tool stored it (review F11). Malformed ones are
    # named and kept out of every count, never fatal (review F10). Stored manifests stay untouched.
    views, malformed = [], []
    for m in scan.manifests:
        problem = mf.shape_problem(m)
        if problem is not None:
            malformed.append(f"{m['episode_id']} ({problem})")
            continue
        views.append(m)
    owners = {m["episode_id"]: load_owner_review(stores, m["episode_id"]) for m in views}
    stored_funnel = {m["episode_id"]: m["funnel"] for m in views}
    ms = sorted((mf.refresh(copy.deepcopy(m), (owners[m["episode_id"]] or {}).get("rules"))
                 for m in views), key=lambda m: m["started_at"])
    maint = maintenance or {}
    notes: list[str] = []
    if backstop is not None and backstop.get("state") != "ok":
        notes.append(f"maintenance backstop {backstop.get('state')}: last success {backstop.get('last_ok')}, "
                     f"{backstop.get('consecutive_failures', 0)} consecutive failure(s)")
    if scan.unreadable:
        notes.append(f"{len(scan.unreadable)} manifest(s) unreadable and outside every count below: "
                     + ", ".join(scan.unreadable[:20]))
    if malformed:
        notes.append(f"{len(malformed)} manifest(s) malformed and outside every count below: "
                     + ", ".join(sorted(malformed)[:20]))

    # window
    ended = [m["ended_at"] for m in ms if m.get("ended_at")]
    first, last = (ms[0]["started_at"] if ms else None), (max(ended) if ended else None)
    a, b = parse_iso(first), parse_iso(last)
    repos = Counter(m["repo"].get("main_root_sha256") or m["repo"].get("root_sha256") or "unknown" for m in ms)

    # denominator
    by_origin = {k: 0 for k in ORIGIN_BUCKETS}
    unjoined = 0
    for m in ms:
        bucket = _origin_bucket(m["prompt"].get("origin"))
        if bucket is None:
            unjoined += 1
        else:
            by_origin[bucket] += 1
    by_origin["unjoined"] = unjoined
    by_status = Counter(m["capture"].get("status") for m in ms)

    # funnel
    fl = [m["funnel"] for m in ms]
    # Manifests written before failure causes were recorded label every refused start write_error;
    # the true cause is the prefix of the recorded error. Stored manifests stay untouched.
    corrected = 0

    def snap_reason(m: dict[str, Any]) -> Any:
        nonlocal corrected
        r = m["start_snapshot"].get("reason")
        if m["capture"].get("status") == "failed" and r == "write_error":
            errs = m["capture"].get("errors") or []
            cause = mf.failure_cause(errs[0]) if errs else r
            if cause != r:
                corrected += 1
                return cause
        return r
    link_states = Counter(m["link"].get("state") for m in ms)
    snap_bad = Counter(snap_reason(m) for m in ms if m["start_snapshot"].get("state") != "complete")
    by_reason = {k: snap_bad.get(k, 0) for k in SNAPSHOT_REASONS}
    other = sum(v for k, v in snap_bad.items() if k not in SNAPSHOT_REASONS)
    if other:
        by_reason["other"] = other
        by_reason["other_detail"] = dict(sorted((str(k), v) for k, v in snap_bad.items() if k not in SNAPSHOT_REASONS))
    x15_unknown = sum(1 for m in ms if not m["funnel"]["dependency_screen_clear"]
                      and not any(mf.rule_state(m["exclusions"], r) == "yes" for r in ("X1", "X2", "X3", "X4", "X5")))
    privacy_by_check: Counter[str] = Counter()
    for m in ms:
        pv = (m.get("review") or {}).get("privacy") or {}
        for k, v in (pv.get("by_check") or {}).items():
            privacy_by_check[k] += int(v)
    untrusted = [m for m in ms if not m["boundary"].get("trusted")]
    raw = Counter(str(m["boundary"].get("reason")) for m in untrusted)
    buckets = Counter(_boundary_bucket(m["boundary"].get("reason")) for m in untrusted)

    def tf(stage: str) -> dict[str, int]:
        t = sum(1 for f in fl if f.get(stage))
        return {"true": t, "false": len(fl) - t}

    funnel = {
        "packet_linked": {**tf("packet_linked"), "by_link_state": dict(sorted(link_states.items()))},
        "start_snapshot_complete": {**tf("start_snapshot_complete"), "by_reason": by_reason},
        "dependency_screen_clear": {**tf("dependency_screen_clear"), "unknown_held": x15_unknown},
        "privacy_cleared": {**tf("privacy_cleared"), "by_check": dict(sorted(privacy_by_check.items()))},
        "payload_materialized": tf("payload_materialized"),
        "start_state_reconstructed": {**tf("start_state_reconstructed"), "by_failure": {}},
        "replay_verified": tf("replay_verified"),
        "boundary": {"trusted": len(ms) - len(untrusted),
                     "untrusted_by_reason": {k: buckets.get(k, 0) for k in BOUNDARY_BUCKETS},
                     "untrusted_raw": dict(sorted(raw.items()))},
        "provisional_candidates": sum(1 for f in fl if mf.provisional_candidate(f)),
        "cumulative": mf.cumulative_funnel(fl),
        "cause_corrections": {"write_error_to_recorded_cause": corrected,
                              "source": "capture.errors prefix; stored manifests unchanged"},
        "stage_status": {"privacy": dict(sorted(Counter(privacy_status(m) for m in ms).items())),
                         "start_state_reconstructed": dict(sorted(Counter(
                             later_stage_status(m, "start_state_reconstructed") for m in ms).items())),
                         "replay_verified": dict(sorted(Counter(
                             later_stage_status(m, "replay_verified") for m in ms).items()))},
    }
    violations = [m["episode_id"] for m in ms if mf.validate_funnel(stored_funnel[m["episode_id"]])]
    if violations:
        notes.append(f"funnel order violated in {len(violations)} manifest(s)")

    # exclusions
    per_rule = {r: {"yes": 0, "no": 0, "unknown": 0} for r in mf.RULES}
    first_failing: Counter[str] = Counter()
    cooc: Counter[tuple[str, ...]] = Counter()
    resolved = {"unknown_to_yes": 0, "unknown_to_no": 0}
    for m in ms:
        yes = []
        for e in m["exclusions"]:
            per_rule[e["rule"]][e["state"]] += 1
            if e["state"] == "yes":
                yes.append(e["rule"])
            if e.get("owner_resolved"):
                resolved["unknown_to_yes" if e["state"] == "yes" else "unknown_to_no"] += 1
        first_failing[yes[0] if yes else "none"] += 1
        if len(yes) > 1:
            cooc[tuple(yes)] += 1

    # screen
    suff = [m for m in ms if (m["capture"].get("join") or {}).get("state") == "joined"]
    causes = Counter(c for m in suff if not m["observation"]["sufficient"] for c in m["observation"]["causes"])
    deps: dict[str, dict[str, int]] = {}
    for cat in mf.DEP_CATEGORIES:
        d = {"observed_total": 0, "required_yes": 0, "required_no": 0, "unknown": 0}
        for m in suff:
            dep = m["deps"][cat]
            d["observed_total"] += int(dep.get("observed") or 0)
            req = dep.get("required")
            d["required_yes" if req == "yes" else "required_no" if req == "no" else "unknown"] += 1
        deps[cat] = d
    prior = {"has_prior_conversation": sum(1 for m in suff if m["prior_context"].get("has_prior_conversation")),
             "required": dict(Counter(m["prior_context"].get("prior_conversation_required") for m in suff)),
             "evidence_kinds": dict(Counter(e.get("kind") if isinstance(e, dict) else str(e)
                                            for m in suff for e in m["prior_context"].get("prior_context_evidence", [])))}

    # FN audit
    audit = _latest_audit(stores)
    reviews_needed = [m["episode_id"] for m in ms if mf.provisional_candidate(m["funnel"])]
    cbc = sum(1 for ep in reviews_needed if (owners.get(ep) or {}).get("case_by_case"))
    fn_audit: dict[str, Any] = {"n": 0, "audited_at": None, "sheet_sha256": None, "labels_sealed": False,
                                "per_category": {}, "misses_total": 0, "interval_95": None,
                                "screen_amended_after_audit": False, "case_by_case_reviews_recorded": cbc}
    if audit:
        fn_audit.update({k: audit.get(k) for k in ("n", "audited_at", "sheet_sha256", "labels_sealed",
                                                     "misses_total", "interval_95", "screen_amended_after_audit")})
        fn_audit["per_category"] = {c: {"screen_no_owner_needed": v.get("fn", 0),
                                        "screen_no_owner_not_needed": v.get("tn", 0),
                                        "cannot_tell": v.get("cannot_tell", 0)}
                                    for c, v in (audit.get("per_category") or {}).items()}

    # task mix
    def task_class(m: dict[str, Any]) -> str:
        tc = m.get("task_class") or {}
        v = tc.get("owner_override") if tc.get("owner_override") is not None else tc.get("mechanical")
        return str(v) if v is not None else "unclassified"
    task_mix = {"by_task_class": dict(Counter(task_class(m) for m in ms)),
                "owner_overrides": sum(1 for m in ms if (m.get("task_class") or {}).get("owner_override") is not None),
                "provisional_candidates_by_task_class": dict(Counter(task_class(m) for m in ms
                                                                     if mf.provisional_candidate(m["funnel"]))),
                "candidates_with_tier1_check": None}
    notes.append("task_mix.candidates_with_tier1_check is null: tier-1 checks are attached at pre-registration, "
                 "not by capture")

    # capture failures
    fails = [m for m in ms if m["capture"].get("status") == "failed"]
    fail_causes = Counter()
    for m in fails:
        errs = m["capture"].get("errors") or []
        fail_causes[snap_reason(m) if m["start_snapshot"].get("state") == "failed"
                    else (errs[0] if errs else "unknown")] += 1
    start_ms = [m["capture"]["start_ms"] for m in ms if isinstance(m["capture"].get("start_ms"), int)]
    end_ms = [m["capture"]["end_ms"] for m in ms if isinstance(m["capture"].get("end_ms"), int)]

    # content store
    snaps = [d for d in stores.snapshots.iterdir() if d.is_dir()] if stores.snapshots.is_dir() else []
    pays = [d for d in stores.payloads.iterdir() if d.is_dir()] if stores.payloads.is_dir() else []
    dels = read_deletions(stores)
    expired = [d for d in dels if d.get("reason") == "expired"]
    store_problems = check_content_store(stores)
    sync = [p for p in store_problems if "sync" in p]
    content_store = {"snapshots_live": len(snaps), "payloads_live": len(pays),
                     "bytes_total": sum(_dir_bytes(d) for d in snaps + pays),
                     "expired_deleted": len(expired), "deletions_verified": sum(1 for d in dels if d.get("verified")),
                     "extended_by_seal": sum(1 for m in ms if (m.get("retention") or {}).get("extended_until")),
                     "tmp_orphans_cleaned": int(maint.get("tmp_orphans_cleaned", 0)),
                     "sync_check": "ok" if not sync else "; ".join(sync)}

    # integrity
    bad = {d.name: verify_payload(stores, d.name) for d in pays}
    integrity = {"sha256sums_verified": sum(1 for v in bad.values() if not v),
                 "mismatches": sum(1 for v in bad.values() if v), "metadata_repo_clean": meta_clean(stores)}

    return {
        "checkpoint": {"n": n, "episodes_target": EPISODES_TARGET, "produced_at": now, "tool_commit": tool_commit,
                       "metadata_commit": _meta_head(stores), "content_store_markers_ok": not store_problems},
        "window": {"first_started_at": first, "last_ended_at": last,
                   "days": round((b - a).total_seconds() / 86400, 2) if a and b else None,
                   "repos": [{"main_root_sha256": k, "episodes": v} for k, v in sorted(repos.items())]},
        "denominator": {"episodes_total": len(ms), "manifests_unreadable": len(scan.unreadable),
                        "manifests_malformed": len(malformed), "by_origin": by_origin,
                        "by_capture_status": {k: by_status.get(k, 0) for k in ("complete", "partial", "failed")}},
        "funnel": funnel,
        "human_yield": human_yield(ms),
        "exclusions": {"per_rule": per_rule, "first_failing_rule": dict(sorted(first_failing.items())),
                       "cooccurrence": [{"rules": list(k), "count": v} for k, v in sorted(cooc.items())],
                       "owner_resolutions": resolved},
        "screen": {"observation_sufficient": {"true": sum(1 for m in suff if m["observation"]["sufficient"]),
                                              "false": sum(1 for m in suff if not m["observation"]["sufficient"]),
                                              "by_cause": dict(sorted(causes.items()))},
                   "deps_per_category": deps, "prior_context": prior},
        "fn_audit": fn_audit,
        "task_mix": task_mix,
        "capture_failures": {"count": len(fails), "by_cause": dict(sorted((str(k), v) for k, v in fail_causes.items())),
                             "hook_latency_ms": {"start": percentiles(start_ms), "end": percentiles(end_ms)},
                             "failure_streak_flags": maint.get("failure_streaks", [])},
        "content_store": content_store,
        "integrity": integrity,
        "isolation": _isolation(stores),
        "twin": {"paid_replays_from_captured_episodes": 0, "tokens": 0},
        "model_probes": {"authorized": False, "runs": 0, "tokens": 0, "ledger_ids": []},
        "decision_inputs": {"continue_proportionate": None, "rules_to_amend": [], "notes": ""},
        "notes": notes,
    }


def _meta_head(stores: Stores) -> str | None:
    from common import git
    try:
        return git(stores.meta, "rev-parse", "HEAD").strip() or None
    except Exception:  # noqa: BLE001 — an empty metadata repository has no HEAD yet
        return None


def _isolation_line(iso_: dict[str, Any]) -> str:
    if not iso_["deterministic_checks_run"]:
        return "Isolation: no check recorded (not verified)."
    return (f"Isolation: {iso_['passed']} passed / {iso_['failed']} failed "
            f"({iso_['unreadable']} unreadable record(s) counted as failed).")


def render_markdown(ck: dict[str, Any]) -> str:
    c, d, f = ck["checkpoint"], ck["denominator"], ck["funnel"]
    lines = [f"# Capture checkpoint {c['n']}", "",
             f"Produced {c['produced_at']}; tool `{c['tool_commit']}`; metadata `{c['metadata_commit']}`. "
             "Counts and hashes only.", "",
             f"Episodes: {d['episodes_total']} of {c['episodes_target']} "
             f"(complete {d['by_capture_status']['complete']}, partial {d['by_capture_status']['partial']}, "
             f"failed {d['by_capture_status']['failed']}).", "",
             "| Funnel stage | true | false |", "|---|---:|---:|"]
    for stage in mf.FUNNEL:
        lines.append(f"| {stage} | {f[stage]['true']} | {f[stage]['false']} |")
    hy = ck["human_yield"]
    lines += ["", f"Cumulative: {f['cumulative']}.", f"Stage status: {f['stage_status']}.", "",
              f"Provisional candidates: {f['provisional_candidates']}. Boundary trusted: {f['boundary']['trusted']}; "
              f"untrusted: {f['boundary']['untrusted_by_reason']}.", "",
              f"Human yield: {hy['provisional_candidates']} candidate(s) of {hy['completed_human_tasks']} completed "
              f"human tasks; cumulative {hy['cumulative']}.", "",
              f"Independent blockers (human): {hy['independent_blockers']}.", "",
              f"Blocked by a single gate (human): {hy['blocked_only_by']}.", "",
              f"Partition (human, sums to population): {hy['partition']['classes']}.", "",
              "| Rule | yes | no | unknown |", "|---|---:|---:|---:|"]
    for r, v in ck["exclusions"]["per_rule"].items():
        lines.append(f"| {r} | {v['yes']} | {v['no']} | {v['unknown']} |")
    lat = ck["capture_failures"]["hook_latency_ms"]
    fa = ck["fn_audit"]
    lines += ["", f"Capture failures: {ck['capture_failures']['count']} {ck['capture_failures']['by_cause']}. "
              f"Start latency ms {lat['start']}; end latency ms {lat['end']}.", "",
              f"FN audit: n={fa['n']}, misses {fa['misses_total']}, 95% interval {fa['interval_95']}, "
              f"sealed {fa['labels_sealed']}; case-by-case reviews recorded {fa['case_by_case_reviews_recorded']}.", "",
              f"Content store: {ck['content_store']}.", f"Integrity: {ck['integrity']}.",
              _isolation_line(ck["isolation"]), "",
              "Twin: 0 paid replays, 0 tokens. Model probes: not authorized, 0 runs.", ""]
    for note in ck.get("notes", []):
        lines.append(f"- {note}")
    return "\n".join(lines).rstrip() + "\n"


def next_checkpoint_n(stores: Stores) -> int:
    ns = [int(p.stem[5:]) for p in (stores.meta / "checkpoints").glob("ckpt-*.json") if p.stem[5:].isdigit()]
    return max(ns, default=0) + 1


def write_checkpoint(stores: Stores, *, tool_commit: str | None = None, n: int | None = None,
                     now: str | None = None, commit: bool = True,
                     backstop: dict[str, Any] | None = None) -> tuple[Path, dict[str, Any]]:
    n = n or next_checkpoint_n(stores)
    maint: dict[str, Any] = {}
    mp = stores.meta / "maintenance.json"
    if mp.is_file():
        try:
            maint = read_json(mp)
        except (OSError, ValueError):
            maint = {}
    ck = build_checkpoint(stores, n, tool_commit=tool_commit, now=now, maintenance=maint, backstop=backstop)
    out = stores.meta / "checkpoints" / f"ckpt-{n}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, ck)
    md = out.with_suffix(".md")
    md.write_text(render_markdown(ck), encoding="utf-8", newline="\n")
    if commit:
        commit_meta(stores, [out, md], f"capture checkpoint {n}")
    ck["_sha256"] = sha256_file(out)
    return out, ck

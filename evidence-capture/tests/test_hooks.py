#!/usr/bin/env python3
"""Step-4 tests (plan §10, §11): the hooks, maintenance, the checkpoint and the FN audit.

Synthetic only: every repository, store, packet and transcript is created in a
temporary directory. The hook scripts run as subprocesses with ``HOME``,
``USERPROFILE`` and ``TEMP`` pointed into the test directory, so neither a real user
config nor the real temp-dir fallback log is touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import shutil
import tempfile
import traceback
import uuid
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
import audit  # noqa: E402
import dryrun  # noqa: E402
import isolation  # noqa: E402
import manifest as mf  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
from common import iso, now_utc, parse_iso, read_json, write_json_atomic  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []
REPORT: dict[str, object] = {}
PROMPT = dryrun.PROMPT
BUDGET_ERRORS = frozenset({"start_over_budget", "end_over_budget"})


def require_yaml(suite: str) -> None:
    """The hook suites' fixtures carry ``.evidence-compiler/config.yaml``; without PyYAML
    ``review.load_capture_config`` fails closed (every episode excluded on
    ``deny_glob:config``, ``capture: false`` not honoured). A missing dependency is an
    environment error, reported before any fixture runs, never a test verdict."""
    try:
        import yaml  # noqa: F401
    except ImportError as exc:
        raise SystemExit(f"{suite}: PyYAML is required (pip install pyyaml); without it every episode is "
                         "excluded on deny_glob:config and capture: false is not honoured") from exc


class Skip(Exception):
    """A test that cannot reach its verdict on this run; the runner counts it apart from pass/fail."""


def load_inconclusive(m: dict) -> str | None:
    """Why ``m`` cannot carry a capture verdict on this run, else None: the hooks ran over
    their budgets (the product then records ``partial``, so X7 is unknown and the review
    is held) or the start snapshot timed out. Both are the budget working as specified
    under machine load, not the behaviour the test was asserting."""
    cap = m["capture"]
    errs = set(cap.get("errors") or [])
    if cap["status"] == "partial" and errs and errs <= BUDGET_ERRORS:
        return (f"over budget: start_ms={cap['start_ms']} end_ms={cap['end_ms']} "
                f"(budget {pipeline.START_BUDGET_MS} ms / {pipeline.END_BUDGET_S:g} s)")
    snap = m.get("start_snapshot") or {}
    # Only a timeout with no other error: an unrelated failure riding along must still fail.
    if (snap.get("state") == "failed" and snap.get("reason") == "timeout"
            and errs <= BUDGET_ERRORS | {"start_hook_killed"}):
        return f"start snapshot timed out: {snap.get('detail')}"
    return None


def manifests(stores: store.Stores) -> list[dict]:
    """Every manifest; a fixture store never holds an unreadable one."""
    scan = store.scan_manifests(stores)
    assert scan.unreadable == [], scan.unreadable
    return scan.manifests


class Fixture:
    """A fixture repository, stores, transcript directory and an isolated hook env."""

    def __init__(self, root: Path, name: str = "fx") -> None:
        self.root = root / name
        self.root.mkdir()
        self.repo = dryrun.build_fixture_repo(self.root)
        self.stores = dryrun.build_stores(self.root)
        self.tdir = self.root / "transcripts"
        home = self.root / "home"
        tmp = self.root / "tmp"
        home.mkdir()
        tmp.mkdir()
        base = {k: v for k, v in os.environ.items()}
        base.update(HOME=str(home), USERPROFILE=str(home), TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp))
        # the isolated home has no git identity; the metadata commits use the environment's
        base.update({k: v for k, v in dryrun.FIXTURE_ENV.items() if not k.endswith("_DATE")})
        self.boundary = dryrun.synthetic_boundary(self.root / "boundary")
        self.env = dryrun.hook_env(self.stores, self.tdir, base, boundary_dir=self.boundary)
        self.tmp = tmp

    def start(self, sid: str, prompt: str = PROMPT, packet: bool = True, env: dict | None = None,
              cwd: Path | None = None) -> tuple[str | None, str]:
        ts = iso(now_utc())
        pid = dryrun.write_packet(self.repo, sid, prompt, ts) if packet else None
        rc, out, _ = dryrun.run_hook("capture_start.py", {"session_id": sid, "prompt": prompt,
                                                          "cwd": str(cwd or self.repo)}, env or self.env)
        assert rc == 0 and out == b"", (rc, out)
        return pid, ts

    def transcript(self, sid: str, prompt: str, pid: str | None, ts: str, append: bool = False,
                   edit: bool = True) -> Path:
        return dryrun.write_transcript(self.tdir, sid, dryrun.transcript_entries(sid, self.repo, prompt, pid, ts,
                                                                                 edit=edit), append=append)

    def end(self, sid: str, transcript: Path | None = None, env: dict | None = None) -> None:
        payload = {"session_id": sid, "cwd": str(self.repo)}
        if transcript is not None:
            payload["transcript_path"] = str(transcript)
        rc, out, _ = dryrun.run_hook("capture_end.py", payload, env or self.env)
        assert rc == 0 and out == b"", (rc, out)

    def episode(self, sid: str) -> dict:
        found = [m for m in manifests(self.stores) if m["session_id"] == sid]
        assert len(found) == 1, f"{len(found)} manifests for session"
        return found[0]


def _git_status(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1"], capture_output=True, text=True,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout


# --------------------------------------------------------------------------- tests

def test_end_to_end_materialized(root: Path) -> None:
    fx = Fixture(root)
    index_before = (fx.repo / ".git" / "index").read_bytes()
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    m = fx.episode(sid)
    assert m["capture"]["phase"] == "started" and m["start_snapshot"]["state"] == "complete", m["start_snapshot"]
    assert m["link"]["state"] == "none", "start never reads a packet"
    tp = fx.transcript(sid, PROMPT, pid, ts)
    fx.end(sid, tp)
    m = fx.episode(sid)
    assert m["capture"]["status"] == "complete", m["capture"]
    assert m["link"]["state"] == "linked" and m["link"]["packet_id"] == pid, m["link"]
    assert m["boundary"]["trusted"] and m["boundary"]["cli_version"] == dryrun.CLI_VERSION, m["boundary"]
    assert m["review"]["state"] == "materialized", m["review"]
    assert all(m["funnel"][s] for s in mf.FUNNEL[:5]), m["funnel"]
    assert (fx.stores.payloads / m["episode_id"]).is_dir()
    assert not (fx.stores.meta / "open" / sid).exists(), "open pointer cleared"
    assert (fx.repo / ".git" / "index").read_bytes() == index_before, "capture changed the index"
    assert _git_status(fx.repo) == "", "capture changed the checkout"
    assert store.meta_clean(fx.stores), "metadata repository left uncommitted changes"
    text = json.dumps(m)
    assert PROMPT not in text and str(fx.repo) not in text, "manifest holds content or a path"
    REPORT["e2e_start_ms"] = m["capture"]["start_ms"]
    REPORT["e2e_end_ms"] = m["capture"]["end_ms"]


def test_fail_open(root: Path) -> None:
    fx = Fixture(root)
    for script in ("capture_start.py", "capture_end.py"):
        for payload in (b"not json", b"[1, 2]", b"", b"\xff\xfe\x00", json.dumps({"session_id": "../x"}).encode()):
            rc, out, _ = dryrun.run_hook(script, payload, fx.env)
            assert rc == 0 and out == b"", (script, payload, rc, out)
    # no stores configured anywhere: nothing written, still silent
    bare = {k: v for k, v in fx.env.items() if not k.startswith("EC_CAPTURE_")}
    bare["EC_CAPTURE_MAINTAIN"] = "0"
    sid = str(uuid.uuid4())
    rc, out, _ = dryrun.run_hook("capture_start.py", {"session_id": sid, "prompt": PROMPT, "cwd": str(fx.repo)}, bare)
    assert rc == 0 and out == b""
    assert (fx.tmp / pipeline.PENDING_NAME).is_file(), "unconfigured stores recorded as pending"
    assert not list(fx.stores.manifests.glob("cap_*.json"))
    # stores that are unusable (content store missing its markers)
    for marker in store.CONTENT_MARKERS:
        (fx.stores.content / marker).unlink()
    sid2 = str(uuid.uuid4())
    fx.start(sid2, packet=False)
    assert not list(fx.stores.manifests.glob("cap_*.json")), "no manifest on an unusable store"
    pend = (fx.tmp / pipeline.PENDING_NAME).read_text(encoding="utf-8").splitlines()
    assert len(pend) == 2 and all(PROMPT not in line for line in pend), pend
    # a crash inside the pipeline is caught by the wrapper
    broken = {**fx.env, "EC_CAPTURE_META": str(fx.repo / "README.md")}
    rc, out, _ = dryrun.run_hook("capture_start.py", {"session_id": sid, "prompt": PROMPT, "cwd": str(fx.repo)}, broken)
    assert rc == 0 and out == b""


def test_unwritable_pending_is_lost_not_silent(root: Path) -> None:
    fx = Fixture(root, "lost")
    bare = {k: v for k, v in fx.env.items() if not k.startswith("EC_CAPTURE_")}
    bare["EC_CAPTURE_MAINTAIN"] = "0"
    (fx.tmp / pipeline.PENDING_NAME).mkdir()  # the pending file cannot be opened for append
    sid = str(uuid.uuid4())
    rc, out, _ = dryrun.run_hook("capture_start.py", {"session_id": sid, "prompt": PROMPT, "cwd": str(fx.repo)}, bare)
    assert rc == 0 and out == b"", "the start hook stays fail-open and silent"
    log = (fx.tmp / "ec-capture-failures.log").read_text(encoding="utf-8")
    assert "LOST, pending record not written" in log, log
    assert PROMPT not in log, "the lost-episode line holds no prompt content"
    assert not list(fx.stores.manifests.glob("cap_*.json"))


def test_capture_disabled_in_twin(root: Path) -> None:
    fx = Fixture(root)
    for var in ("EVIDENCE_CAPTURE", "EVIDENCE_HOOK"):
        sid = str(uuid.uuid4())
        fx.start(sid, env={**fx.env, var: "0"})
        assert not [m for m in manifests(fx.stores) if m["session_id"] == sid], var
    # a repository without .evidence-compiler/ is never captured
    plain = fx.root / "plain"
    plain.mkdir()
    subprocess.run(["git", "init", "-q", str(plain)], check=True, capture_output=True)
    sid = str(uuid.uuid4())
    fx.start(sid, packet=False, cwd=plain)
    assert not [m for m in manifests(fx.stores) if m["session_id"] == sid]
    # capture: false in the repository's config
    (fx.repo / ".evidence-compiler" / "config.yaml").write_text("capture: false\n", encoding="utf-8")
    sid = str(uuid.uuid4())
    fx.start(sid, packet=False)
    assert not [m for m in manifests(fx.stores) if m["session_id"] == sid]
    # skipped prompts leave metadata-only diagnostics; the kill switch leaves none
    skips = [json.loads(x) for x in (fx.stores.meta / pipeline.START_SKIPS).read_text(encoding="utf-8").splitlines()]
    assert [r["reason"] for r in skips] == ["not_enabled", "disabled_by_repo"], skips
    assert skips[1]["session_id"] == sid and all(set(r) == {"at", "reason", "session_id", "cwd_sha256"} for r in skips)
    assert PROMPT not in (fx.stores.meta / pipeline.START_SKIPS).read_text(encoding="utf-8")
    # the Twin's child environment disables capture
    sys.path.insert(0, str(PKG.parent / "evidence-twin"))
    import twin  # noqa: E402
    env, _ = twin.child_env()
    assert env.get("EVIDENCE_CAPTURE") == "0" and env.get("EVIDENCE_HOOK") == "0"
    assert pipeline.disabled(env)


def test_capture_failures_in_denominator(root: Path) -> None:
    fx = Fixture(root)
    # one complete episode
    sid_ok = str(uuid.uuid4())
    pid, ts = fx.start(sid_ok)
    fx.end(sid_ok, fx.transcript(sid_ok, PROMPT, pid, ts))
    # three starts the store could not take (pending), recorded by the next maintenance
    markers = {n: (fx.stores.content / n).read_bytes() for n in store.CONTENT_MARKERS}
    for n in markers:
        (fx.stores.content / n).unlink()
    for _ in range(3):
        fx.start(str(uuid.uuid4()), packet=False)
    for n, data in markers.items():
        (fx.stores.content / n).write_bytes(data)
    # a start killed after its first manifest write
    sid_k = str(uuid.uuid4())
    fx.start(sid_k, packet=False)
    mk = fx.episode(sid_k)
    mk["capture"]["phase"] = "begun"
    mk["started_at"] = iso(now_utc() - timedelta(seconds=pipeline.KILLED_AFTER_S + 30))
    store.write_manifest(fx.stores, mk)
    # a start whose transcript never appears: partial after the re-join window
    sid_p = str(uuid.uuid4())
    fx.start(sid_p, packet=False)
    mp = fx.episode(sid_p)
    mp["started_at"] = iso(now_utc() - timedelta(hours=pipeline.REJOIN_WINDOW_H + 1))
    store.write_manifest(fx.stores, mp)

    res = pipeline.run_maintain(fx.stores, fx.env)
    assert res["action"] == "maintained" and "error" not in res, res
    assert res["pending_drained"] == 3 and res["killed"] == 1 and res["rejoin_expired"] == 1, res
    ms = manifests(fx.stores)
    by_status = {s: sum(1 for m in ms if m["capture"]["status"] == s) for s in ("complete", "partial", "failed")}
    assert by_status == {"complete": 1, "partial": 1, "failed": 4}, by_status
    killed = fx.episode(sid_k)
    assert killed["boundary"]["reason"] == "hook_killed" and mf.rule_state(killed["exclusions"], "X6") == "yes"
    failed_reviews = [(m.get("review") or {}).get("state") for m in ms if m["capture"]["status"] == "failed"]
    assert failed_reviews == ["excluded"] * 4, failed_reviews
    state = read_json(fx.stores.meta / pipeline.MAINTENANCE)
    assert state["failure_streaks"] and state["failure_streaks"][0]["consecutive_failures"] >= 3, state
    assert not (fx.tmp / pipeline.PENDING_NAME).exists(), "pending file drained"

    path, ck = report.write_checkpoint(fx.stores, tool_commit="0" * 40)
    d = ck["denominator"]
    assert d["episodes_total"] == 6 and d["by_capture_status"] == by_status, d
    assert ck["capture_failures"]["count"] == 4 and ck["capture_failures"]["failure_streak_flags"], ck["capture_failures"]
    assert ck["exclusions"]["per_rule"]["X7"]["yes"] == 4, ck["exclusions"]["per_rule"]["X7"]
    assert ck["funnel"]["boundary"]["untrusted_by_reason"]["hook_killed"] == 1
    assert path.with_suffix(".md").is_file() and store.meta_clean(fx.stores)


def test_rejoin_by_maintenance(root: Path) -> None:
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    fx.transcript(sid, PROMPT, pid, ts)  # the Stop hook never ran for this turn
    second = "Now add a docstring to alpha."
    ts2 = iso(now_utc())
    fx.start(sid, prompt=second, packet=False)
    fx.transcript(sid, second, None, ts2, append=True, edit=False)
    res = pipeline.run_maintain(fx.stores, fx.env)
    assert res["rejoined"] >= 1, res
    first = [m for m in manifests(fx.stores) if m["session_id"] == sid and m["started_at"] < ts2]
    assert len(first) == 1
    m = first[0]
    assert m["capture"]["join"]["by"] == "maintain" and m["ended_by"] == "prompt", m["capture"]["join"]
    assert m["link"]["state"] == "linked" and m["review"]["state"] == "materialized", (m["link"], m["review"])
    # the later episode stays open for its own Stop hook
    later = [x for x in manifests(fx.stores) if x["session_id"] == sid and x["started_at"] >= ts2][0]
    assert later["capture"]["phase"] == "started"


def test_end_waits_for_episode_lock(root: Path) -> None:
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    tp = fx.transcript(sid, PROMPT, pid, ts)
    ep = fx.episode(sid)["episode_id"]
    assert pipeline.lock_episode(fx.stores, ep)
    old = pipeline.END_LOCK_WAIT_S
    pipeline.END_LOCK_WAIT_S = 0.2
    try:
        res = pipeline.run_end({"session_id": sid, "transcript_path": str(tp)}, fx.env)
    finally:
        pipeline.END_LOCK_WAIT_S = old
        pipeline._unlock(pipeline.episode_lock(fx.stores, ep))
    assert res["action"] == "busy", res
    assert fx.episode(sid)["capture"]["phase"] == "started", "a busy episode is left untouched"
    res = pipeline.run_end({"session_id": sid, "transcript_path": str(tp)}, fx.env)
    assert res["action"] == "ended" and res["review"] == "materialized", res


def test_boundary_overrides(root: Path) -> None:
    m = mf.new_manifest(episode_id="cap_0", session_id="s", started_at=iso(now_utc()), prompt_sha256="0" * 64,
                        prompt_len=1, tool_commit=None, tool_sha256=None, cwd_sha256="0" * 64)
    bdir = dryrun.synthetic_boundary(root / "boundary")
    env = {"EC_CAPTURE_BOUNDARY_DIRS": str(bdir)}
    ident = read_json(next(bdir.glob("*.json")))["identity"]
    m["client"] = {"sha256": ident["sha256"], "mode": dryrun.SYNTHETIC_MODE, "problem": None}
    m["boundary"]["cli_version"] = dryrun.CLI_VERSION
    m["capture"]["status"] = "complete"
    m["start_snapshot"] = {"state": "complete", "reason": None}
    assert pipeline.boundary_for(m, env=env)["trusted"] is True
    m["start_snapshot"] = {"state": "failed", "reason": "concurrent_change", "detail": "lock present: index.lock"}
    assert pipeline.boundary_for(m, env=env)["reason"] == "lock_present"
    m["start_snapshot"]["detail"] = "bracket differs"
    assert pipeline.boundary_for(m, env=env)["reason"] == "bracket_dirty"
    m["capture"]["phase"] = "killed"
    assert pipeline.boundary_for(m, env=env)["reason"] == "hook_killed"
    m["capture"]["phase"] = "started"
    m["start_snapshot"] = {"state": "complete"}
    m["boundary"]["cli_version"] = "0.0.0-untested"
    b = pipeline.boundary_for(m, env=env)
    assert not b["trusted"] and b["reason"] == "boundary_untested"
    assert report._boundary_bucket(b["reason"]) == "cli_untested"
    # the shipped records: the version-only record predates identity and is never trusted
    m["boundary"]["cli_version"] = dryrun.CLI_VERSION
    b = pipeline.boundary_for(m, env={})
    assert not b["trusted"] and b["reason"] == "identity_untested", b
    assert report._boundary_bucket(b["reason"]) == "cli_untested"


def _seal(fx: Fixture, k: int, miss: tuple[str, str] | None = None) -> None:
    lp = audit.labels_path(fx.stores, k)
    sheet = read_json(lp)
    for e in sheet["episodes"]:
        for c in audit.CATEGORIES:
            needed = miss is not None and e["episode_id"] == miss[0] and c == miss[1]
            e["labels"][c] = {"label": "needed" if needed else "not_needed", "basis": "synthetic label"}
    sheet["sealed"] = True
    write_json_atomic(lp, sheet)


def test_fn_audit_sheet_and_gate(root: Path) -> None:
    fx = Fixture(root)
    eps = []
    for _ in range(3):
        sid = str(uuid.uuid4())
        pid, ts = fx.start(sid)
        fx.end(sid, fx.transcript(sid, PROMPT, pid, ts))
        eps.append(fx.episode(sid)["episode_id"])
    sheet = audit.build_sheet(fx.stores)
    k = sheet["audit"]
    labels = read_json(audit.labels_path(fx.stores, k))
    assert [e["episode_id"] for e in labels["episodes"]] == sorted(
        eps, key=lambda ep: store.read_manifest(fx.stores, ep)["started_at"])
    assert "screen" not in json.dumps(labels["episodes"]), "the sheet is blind to the screen"
    assert all(v["label"] is None for e in labels["episodes"] for v in e["labels"].values())
    try:
        audit.build_sheet(fx.stores)
        raise AssertionError("second sheet while the first is unscored")
    except audit.AuditError:
        pass
    try:
        audit.score(fx.stores, k)
        raise AssertionError("scored an unsealed sheet")
    except audit.AuditError:
        pass
    _seal(fx, k, miss=(eps[0], "live_remote"))
    assert "sheet not committed or modified since commit" in audit.labels_sealed(fx.stores, k)
    store.commit_meta(fx.stores, [audit.labels_path(fx.stores, k)], "owner labels")
    assert audit.labels_sealed(fx.stores, k) == []
    res = audit.score(fx.stores, k)
    assert res["misses_total"] == 1 and res["per_category"]["live_remote"]["fn"] == 1, res["per_category"]
    assert res["amend_screen_required"] == ["live_remote"], res
    lo, hi = res["interval_95"]
    assert lo > 0 and 0.5 < hi < 1, res["interval_95"]
    assert "No rate below" in res["statement"]
    # the gate: pending amendment and missing case-by-case review both block
    adm = audit.admission_check(fx.stores, eps[1])
    assert not adm["admitted"] and any("amendment pending" in r for r in adm["reasons"]), adm
    assert any("case-by-case review missing" in r for r in adm["reasons"]), adm
    audit.record_amendment(fx.stores, k, "live_remote screen widened", "f" * 40)
    owner = {"episode_id": eps[1], "reviewed_at": iso(now_utc()), "rules": {},
             "case_by_case": {c: {"label": "not_needed", "basis": "synthetic"} for c in audit.CATEGORIES}}
    write_json_atomic(fx.stores.meta / "review" / f"{eps[1]}.json", owner)
    adm = audit.admission_check(fx.stores, eps[1])
    assert adm["admitted"], adm
    # one "cannot_tell" in the case-by-case review blocks admission
    owner["case_by_case"]["network"] = {"label": "cannot_tell", "basis": "unclear"}
    write_json_atomic(fx.stores.meta / "review" / f"{eps[1]}.json", owner)
    assert not audit.admission_check(fx.stores, eps[1])["admitted"]
    # labels edited after scoring are detected
    _seal(fx, k)
    assert any("changed after scoring" in r for r in audit.admission_check(fx.stores, eps[2])["reasons"])
    # exact interval: n = 20, k = 0 gives an upper bound near 17 %
    assert audit.clopper_pearson(0, 20) == [0.0, 0.1684], audit.clopper_pearson(0, 20)
    assert audit.clopper_pearson(20, 20)[1] == 1.0
    ck = report.build_checkpoint(fx.stores, 1)
    assert ck["fn_audit"]["n"] == 3 and ck["fn_audit"]["per_category"]["live_remote"]["screen_no_owner_needed"] == 1


def test_checkpoint_schema(root: Path) -> None:
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    fx.end(sid, fx.transcript(sid, PROMPT, pid, ts))
    ck = report.build_checkpoint(fx.stores, 1, tool_commit="0" * 40)
    want = ["checkpoint", "window", "denominator", "funnel", "exclusions", "screen", "fn_audit", "task_mix",
            "capture_failures", "content_store", "integrity", "isolation", "twin", "model_probes", "decision_inputs"]
    assert all(k in ck for k in want), [k for k in want if k not in ck]
    assert set(ck["funnel"]["boundary"]["untrusted_by_reason"]) == set(report.BOUNDARY_BUCKETS)
    assert set(ck["funnel"]["start_snapshot_complete"]["by_reason"]) >= set(report.SNAPSHOT_REASONS)
    assert ck["twin"] == {"paid_replays_from_captured_episodes": 0, "tokens": 0}
    assert ck["model_probes"]["authorized"] is False and ck["model_probes"]["runs"] == 0
    assert ck["integrity"]["sha256sums_verified"] == 1 and ck["integrity"]["mismatches"] == 0
    assert ck["content_store"]["payloads_live"] == 1 and ck["checkpoint"]["content_store_markers_ok"]
    blob = json.dumps(ck)
    assert PROMPT not in blob and str(fx.repo) not in blob, "checkpoint holds content or a path"
    for k in ("start", "end"):
        assert ck["capture_failures"]["hook_latency_ms"][k]["p50"] is not None


def test_checkpoint_isolation_records(root: Path) -> None:
    """Slop audit L10: the checkpoint counts the records the isolation CLI writes into
    ``<meta>/isolation/``; an unreadable or verdict-less record is counted as failed, and no
    record at all renders as not verified, never as 0 failed."""
    fx = Fixture(root)
    _, ck = report.write_checkpoint(fx.stores, n=1, tool_commit="0" * 40, commit=False)
    assert ck["isolation"]["deterministic_checks_run"] == 0
    md = (fx.stores.meta / "checkpoints" / "ckpt-1.md").read_text(encoding="utf-8")
    assert "Isolation: no check recorded (not verified)." in md, md
    target = root / "target.txt"
    target.write_text("synthetic\n", encoding="utf-8")
    spec = root / "targets.json"
    spec.write_text(json.dumps([{"name": "t", "kind": "read", "path": str(target)}]), encoding="utf-8")
    rec_dir = fx.stores.meta / "isolation"
    rc = isolation.main(["--targets", str(spec), "--watch", str(fx.stores.content), "--repo", str(fx.stores.meta),
                         "--out", str(rec_dir / "batch-1.json")])
    assert rc == 1, "the target is readable, so the recorded check fails"
    (rec_dir / "corrupt.json").write_text("{not json", encoding="utf-8")
    (rec_dir / "no-verdict.json").write_text(json.dumps({"passed": "yes"}), encoding="utf-8")
    _, ck = report.write_checkpoint(fx.stores, n=2, tool_commit="0" * 40, commit=False)
    iso_ = ck["isolation"]
    assert (iso_["deterministic_checks_run"], iso_["passed"], iso_["failed"], iso_["unreadable"]) == (3, 0, 3, 2), iso_
    assert iso_["identity"] is not None and iso_["acl_dump_sha256"]
    md = (fx.stores.meta / "checkpoints" / "ckpt-2.md").read_text(encoding="utf-8")
    assert "Isolation: 0 passed / 3 failed (2 unreadable record(s) counted as failed)." in md, md


def test_store_check_stamp(root: Path) -> None:
    fx = Fixture(root)
    stamp = fx.stores.meta / pipeline.STORE_CHECK
    assert not stamp.exists()
    assert pipeline.store_problems(fx.stores) == {"content": [], "meta": []}
    assert stamp.is_file(), "first check writes the stamp"
    rec = read_json(stamp)
    rec["content"] = ["content store has cloud attributes: P"]
    write_json_atomic(stamp, rec)
    assert pipeline.store_problems(fx.stores)["content"] == [], "a stale failure is re-checked, not trusted"
    assert read_json(stamp)["content"] == [], "the re-check rewrites the stamp"
    exclude = fx.stores.meta / ".git" / "info" / "exclude"
    kept = exclude.read_text(encoding="utf-8")
    exclude.write_text("", encoding="utf-8")
    pipeline.full_store_check(fx.stores)  # maintenance records the real problem
    assert pipeline.store_problems(fx.stores)["meta"] == ["metadata .git/info/exclude lacks the content exclusions"]
    assert pipeline.store_problems(fx.stores)["meta"], "a persistent failure still blocks the hot path"
    exclude.write_text(kept, encoding="utf-8")
    assert pipeline.store_problems(fx.stores) == {"content": [], "meta": []}, "repair heals without maintenance"
    gi = (fx.stores.meta / ".gitignore").read_text(encoding="utf-8")
    for local in ("store-check.json", "maintenance.json", "maintain.lock", "open/", "locks/", "capture.log"):
        assert local in gi, local


def test_dry_mode_refuses_unsafe_dirs(root: Path) -> None:
    for bad in (root / "scratchpad" / "dry",):
        try:
            dryrun.check_out_dir(bad)
            raise AssertionError("accepted a scratchpad path")
        except dryrun.DryError:
            pass
    inside = root / "repo"
    inside.mkdir()
    subprocess.run(["git", "init", "-q", str(inside)], check=True, capture_output=True)
    try:
        dryrun.check_out_dir(inside / "dry")
        raise AssertionError("accepted a path inside a repository")
    except dryrun.DryError:
        pass
    busy = root / "busy"
    busy.mkdir()
    (busy / "file.txt").write_text("x", encoding="utf-8")
    try:
        dryrun.check_out_dir(busy)
        raise AssertionError("accepted a non-empty foreign directory")
    except dryrun.DryError:
        pass
    ok = dryrun.check_out_dir(root / "dry")
    assert (ok / dryrun.MARKER).is_file()
    assert dryrun.check_out_dir(root / "dry") == ok, "a marked directory is reusable"


def test_tool_identity_without_git(root: Path) -> None:
    """F8: the commit is read from files with no git binary on PATH, and a tool copy outside
    any repository yields ``commit=None`` with the same code hash, not an error."""
    head = subprocess.run(["git", "-C", str(PKG), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    saved_path, saved_pkg = os.environ.get("PATH", ""), pipeline.PKG
    copy = root / "outside-any-repo" / PKG.name  # same dir name: the hash keys on it
    shutil.copytree(PKG, copy, ignore=shutil.ignore_patterns("tests", "__pycache__", ".git"))
    try:
        os.environ["PATH"] = str(root / "empty-bin")  # no git reachable
        commit, sha = pipeline.tool_identity()
        assert len(sha) == 64 and commit == head, (commit, head)
        pipeline.PKG = copy
        bare_commit, bare_sha = pipeline.tool_identity()
        assert bare_commit is None, bare_commit
        assert bare_sha == sha, "the code hash must not depend on the checkout"
    finally:
        os.environ["PATH"] = saved_path
        pipeline.PKG = saved_pkg


def test_maintain_store_problems_are_findings(root: Path) -> None:
    """2026-10-05: maintenance saw a store problem, logged it, and the backstop still said ok."""
    problems, _ = pipeline.maintain_findings({"action": "maintained", "store_problems": ["metadata x"]})
    assert problems == ["store_problems: metadata x"]
    assert pipeline.maintain_findings({"action": "maintained", "store_problems": []}) == ([], [])


def test_open_pointer_retired_after_stop_window(root: Path) -> None:
    """2026-10-05: seven open/ pointers from Sep 29-30 outlived their maintenance-joined episodes."""
    fx = Fixture(root)
    sid, sid2 = str(uuid.uuid4()), str(uuid.uuid4())
    pid, ts = fx.start(sid)
    fx.transcript(sid, PROMPT, pid, ts)
    second = "Now add a docstring to alpha."
    ts2 = iso(now_utc())
    fx.start(sid, prompt=second, packet=False)
    fx.transcript(sid, second, None, ts2, append=True, edit=False)
    pipeline.run_maintain(fx.stores, fx.env)
    first = [m for m in manifests(fx.stores) if m["session_id"] == sid and m["started_at"] < ts2][0]
    later = [m for m in manifests(fx.stores) if m["session_id"] == sid and m["started_at"] >= ts2][0]
    open_dir = fx.stores.meta / "open"
    (open_dir / sid).write_text(first["episode_id"] + "\n", encoding="utf-8")  # Stop never came
    (open_dir / sid2).write_text(later["episode_id"] + "\n", encoding="utf-8")  # never joined
    soon = pipeline.retire_open_pointers(fx.stores, iso(now_utc() + timedelta(hours=1)))
    assert soon["retired"] == [] and (open_dir / sid).is_file(), soon  # a late Stop may still replace the join
    late = iso(now_utc() + timedelta(hours=pipeline.OPEN_POINTER_STALE_H + 1))
    res = pipeline.retire_open_pointers(fx.stores, late)
    assert res["retired"] == [first["episode_id"]] and not (open_dir / sid).exists(), res
    assert res["open_unjoined"] == [later["episode_id"]] and (open_dir / sid2).is_file(), res
    assert pipeline.read_manifest(fx.stores, first["episode_id"]) == first  # the manifest is untouched


def test_rescreen_corrects_and_records(root: Path) -> None:
    import rescreen
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    tp = fx.transcript(sid, PROMPT, pid, ts)
    fx.end(sid, tp)
    good = fx.episode(sid)
    # idempotent on a manifest the current tool produced
    rec = rescreen.run_rescreen(fx.stores, fx.env)
    assert rec["changed"] == [] and rec["outcomes"] == {"rescreened": 1}, rec
    assert fx.episode(sid)["observation"] == good["observation"]
    # a stale judgement (as an older tool wrote it) is recomputed, with provenance
    stale = fx.episode(sid)
    stale["prompt"]["origin"] = "queued_command:prompt"
    stale["observation"] = {"sufficient": False, "causes": ["stale_cause"]}
    store.write_manifest(fx.stores, stale)
    rec = rescreen.run_rescreen(fx.stores, fx.env)
    m = fx.episode(sid)
    assert m["prompt"]["origin"] == good["prompt"]["origin"] and m["observation"] == good["observation"], m["observation"]
    corr = m["corrections"][-1]
    assert corr["by"] == "rescreen" and corr["before"]["origin"] == "queued_command:prompt"
    assert corr["before"]["causes"] == ["stale_cause"] and corr["after"]["sufficient"] == good["observation"]["sufficient"]
    assert rec["before"]["by_cause"] == {"stale_cause": 1} and rec["changed"][0]["episode_id"] == m["episode_id"]
    assert Path(rec["record"]).is_file() and rec["committed"] and store.meta_clean(fx.stores)
    # transcript gone: a sufficient judgement over delegated work is withdrawn, not kept
    m["observation"] = {"sufficient": True, "causes": []}
    m["capture"]["join"]["subagents"] = 1
    for c in mf.DEP_CATEGORIES:
        m["deps"][c]["required"] = "no"
    store.write_manifest(fx.stores, m)
    tp.unlink()
    rec = rescreen.run_rescreen(fx.stores, fx.env)
    m = fx.episode(sid)
    assert rec["outcomes"] == {"invalidated_transcript_unavailable": 1}, rec["outcomes"]
    assert m["observation"]["sufficient"] is False and "rescreen_transcript_unavailable" in m["observation"]["causes"]
    assert all(m["deps"][c]["required"] == "unknown" for c in mf.DEP_CATEGORIES if c != "prior_conversation")
    assert not m["funnel"]["dependency_screen_clear"]
    assert PROMPT not in Path(rec["record"]).read_text(encoding="utf-8")


def test_rescreen_bounded_child_timeout(root: Path) -> None:
    import rescreen
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    tp = fx.transcript(sid, PROMPT, pid, ts)
    fx.end(sid, tp)
    stuck = (sys.executable, "-c", "import time; time.sleep(30)")
    t0 = time.monotonic()
    rec = rescreen.run_rescreen(fx.stores, fx.env, episode_timeout_s=1.0, child_argv=stuck)
    assert time.monotonic() - t0 < 20, "a stuck child must not hold the run"
    assert rec["outcomes"] == {"invalidated_timeout": 1}, rec["outcomes"]
    m = fx.episode(sid)
    assert m["observation"]["sufficient"] is False and "rescreen_timeout" in m["observation"]["causes"]
    assert not (fx.stores.meta / pipeline.MAINTAIN_LOCK).exists()
    # budget spent: remaining episodes are deferred untouched, and the lock is released
    rec = rescreen.run_rescreen(fx.stores, fx.env, run_budget_s=0.0)
    assert rec["deferred"] == [m["episode_id"]] and rec["outcomes"] == {}, rec
    assert not (fx.stores.meta / pipeline.MAINTAIN_LOCK).exists()


def test_rescreen_removed_worktree_uses_captured_root(root: Path) -> None:
    import rescreen
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    tp = fx.transcript(sid, PROMPT, pid, ts)
    fx.end(sid, tp)
    good = fx.episode(sid)
    snap = good["start_snapshot"]
    shutil.rmtree(fx.repo / ".git", onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
    rec = rescreen.run_rescreen(fx.stores, fx.env)
    m = fx.episode(sid)
    assert rec["outcomes"] == {"rescreened_root_removed": 1}, (rec["outcomes"], (fx.repo / ".git").exists(), m["capture"]["join"])
    assert m["capture"]["join"]["root_source"] == "capture_hash"
    assert m["start_snapshot"] == snap and m["observation"] == good["observation"]


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_rescreen_child_argv_is_fixed(root: Path) -> None:
    """Coverage (trust boundary): transcript and prompt text cannot reach the replay argv.
    The child's argv is exactly CHILD_ARGV; hostile captured text travels only on stdin."""
    import rescreen
    hostile = "--child; & calc.exe $(whoami) `id` -c import os"
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid, prompt=hostile)
    fx.end(sid, fx.transcript(sid, hostile, pid, ts))
    seen: list[tuple[list[str], bytes]] = []
    real = subprocess.Popen

    def spy(argv, *a, **kw):  # type: ignore[no-untyped-def]
        stdin = kw.get("stdin")
        if "--child" in argv:  # the rescreen child; git calls the screen makes are not under test
            pos = stdin.tell()
            seen.append((list(argv), stdin.read()))
            stdin.seek(pos)
        return real(argv, *a, **kw)
    rescreen.subprocess.Popen = spy
    try:
        rec = rescreen.run_rescreen(fx.stores, fx.env)
    finally:
        rescreen.subprocess.Popen = real
    assert rec["outcomes"] == {"rescreened": 1}, rec
    assert seen and all(argv == list(rescreen.CHILD_ARGV) for argv, _ in seen), seen
    assert all(hostile not in " ".join(argv) for argv, _ in seen)
    ep = fx.episode(sid)["episode_id"].encode()
    assert all(ep in data for _, data in seen), "the manifest travels on stdin"


def test_rescreen_child_kills_descendants_holding_its_output(root: Path) -> None:
    """F4: the rescreen child starts a grandchild that inherits its stdout/stderr and
    outlives it. ``run_child`` returns within the bound and the grandchild is gone."""
    import rescreen
    fx = Fixture(root)
    pidfile = root / "grandchild.pid"
    grand = "import time; time.sleep(120)"
    # the child: start a grandchild inheriting stdout/stderr, record its pid, then hang
    child = ("import subprocess, sys, time; "
             f"p = subprocess.Popen([sys.executable, '-c', {grand!r}]); "
             f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(120)")
    m = {"episode_id": "cap_f4_fixture_0000"}
    t0 = time.monotonic()
    outcome, done = rescreen.run_child(fx.stores, m, fx.env, 2.0, (sys.executable, "-c", child))
    took = time.monotonic() - t0
    assert outcome == "timeout" and done is None, outcome
    assert took < 2.0 + rescreen.KILL_WAIT_S + 5, took
    gpid = int(pidfile.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while _pid_alive(gpid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _pid_alive(gpid), "the grandchild outlived the timeout"

    # a child that answers and exits while its grandchild still holds the output: no wait on it
    answer = json.dumps({"outcome": "rescreened", "manifest": m})
    pidfile2 = root / "grandchild2.pid"
    quick = ("import subprocess, sys; "
             f"p = subprocess.Popen([sys.executable, '-c', {grand!r}]); "
             f"open({str(pidfile2)!r}, 'w').write(str(p.pid)); "
             f"sys.stdout.write({answer!r})")
    t0 = time.monotonic()
    outcome, done = rescreen.run_child(fx.stores, m, fx.env, 30.0, (sys.executable, "-c", quick))
    assert outcome == "rescreened" and done == m, (outcome, done)
    assert time.monotonic() - t0 < 15, "waited on a grandchild holding the child's output"
    gpid = int(pidfile2.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while _pid_alive(gpid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _pid_alive(gpid), "a grandchild outlived the child's normal exit"


def test_rescreen_kills_descendants_whose_parent_already_exited(root: Path) -> None:
    """F4: the child starts a grandchild, which starts a great-grandchild and exits; the
    child then hangs. On timeout the great-grandchild (no live parent link back to the
    child) is gone too."""
    import rescreen
    fx = Fixture(root)
    pidfile = root / "greatgrandchild.pid"
    great = "import time; time.sleep(120)"
    grand = ("import subprocess, sys; "
             f"p = subprocess.Popen([sys.executable, '-c', {great!r}]); "
             f"open({str(pidfile)!r}, 'w').write(str(p.pid))")
    child = ("import subprocess, sys, time; "
             f"subprocess.run([sys.executable, '-c', {grand!r}]); time.sleep(120)")
    m = {"episode_id": "cap_f4_fixture_0001"}
    t0 = time.monotonic()
    outcome, done = rescreen.run_child(fx.stores, m, fx.env, 3.0, (sys.executable, "-c", child))
    assert outcome == "timeout" and done is None, outcome
    assert time.monotonic() - t0 < 3.0 + rescreen.KILL_WAIT_S + 5
    ggpid = int(pidfile.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while _pid_alive(ggpid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _pid_alive(ggpid), "a reparented descendant outlived the timeout"

def test_rescreen_partial_manifest_is_reported_not_fatal(root: Path) -> None:
    """F10: a manifest missing fields a rescreen reads is listed, and the rest still run."""
    import rescreen
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    fx.end(sid, fx.transcript(sid, PROMPT, pid, ts))
    good = fx.episode(sid)
    partial = {"episode_id": "cap_partial", "schema": good.get("schema")}
    (fx.stores.manifests / "cap_partial.json").write_text(json.dumps(partial), encoding="utf-8")
    rec = rescreen.run_rescreen(fx.stores, fx.env)
    assert rec["manifests_malformed"] == ["cap_partial"], rec
    assert rec["outcomes"] == {"rescreened": 1}, rec


def test_rescreen_records_never_share_a_name(root: Path) -> None:
    """F12: two records stamped in the same second get distinct files."""
    import rescreen
    d = root / "corrections"
    a = rescreen._new_record_path(d, "20261006T000000Z-rescreen")
    b = rescreen._new_record_path(d, "20261006T000000Z-rescreen")
    assert a != b and a.is_file() and b.is_file() and b.name.endswith("-1.json"), (a, b)



TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]



def main() -> int:
    require_yaml("test_hooks")
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-hooks-test-") as tmp:
        for fn in TESTS:
            if only and fn.__name__ not in only:
                continue
            case = Path(tmp) / fn.__name__
            case.mkdir()
            try:
                fn(case)
                PASSED.append(fn.__name__)
            except Skip as exc:
                SKIPPED.append(fn.__name__)
                print(f"SKIP {fn.__name__}: {exc}")
            except Exception:  # noqa: BLE001 — plain-check runner
                FAILED.append(fn.__name__)
                print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    for name in PASSED:
        print(f"ok   {name}")
    if REPORT:
        print("report", json.dumps(REPORT, sort_keys=True))
    print(f"{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())


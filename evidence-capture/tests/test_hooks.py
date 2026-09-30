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
import manifest as mf  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
from common import iso, now_utc, parse_iso, read_json, write_json_atomic  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
REPORT: dict[str, object] = {}
PROMPT = dryrun.PROMPT


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


def test_store_check_stamp(root: Path) -> None:
    fx = Fixture(root)
    stamp = fx.stores.meta / pipeline.STORE_CHECK
    assert not stamp.exists()
    assert pipeline.store_problems(fx.stores) == {"content": [], "meta": []}
    assert stamp.is_file(), "first check writes the stamp"
    rec = read_json(stamp)
    rec["content"] = ["content store has cloud attributes: P"]
    write_json_atomic(stamp, rec)
    assert pipeline.store_problems(fx.stores)["content"], "a recorded full-check failure blocks the hot path"
    pipeline.full_store_check(fx.stores)
    assert pipeline.store_problems(fx.stores)["content"] == []
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
    commit, sha = pipeline.tool_identity()
    assert len(sha) == 64
    head = subprocess.run(["git", "-C", str(PKG), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert commit == head, (commit, head)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
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
            except Exception:  # noqa: BLE001 — plain-check runner
                FAILED.append(fn.__name__)
                print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    for name in PASSED:
        print(f"ok   {name}")
    if REPORT:
        print("report", json.dumps(REPORT, sort_keys=True))
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

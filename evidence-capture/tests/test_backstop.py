#!/usr/bin/env python3
"""Scheduled backstop tests: expiry without prompts or hooks, and failure reporting.

Synthetic only. Stores, repositories and transcripts live in a temporary directory; the
subprocess run has ``HOME``, ``USERPROFILE`` and ``TEMP`` pointed into the case
directory, so the default status file lands in the isolated home, never the real one.
Nothing here registers a scheduled task.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
import audit  # noqa: E402
import backstop  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
from common import iso, now_utc, read_json, write_json_atomic  # noqa: E402
from review import overdue  # noqa: E402
from test_hooks import PROMPT, Fixture, Skip, load_inconclusive, manifests, require_yaml  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []
REPORT: dict[str, object] = {}


def _hold_lock(root: Path, lock: Path) -> subprocess.Popen:
    """A separate process that holds ``lock`` until killed (a live holder)."""
    ready = root / f"{lock.name}.ready"
    proc = subprocess.Popen([sys.executable, str(HERE / "lockproc.py"), "hold", str(lock), str(ready)])
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert proc.poll() is None and time.monotonic() < deadline, "lock holder did not start"
        time.sleep(0.02)
    return proc


def _materialized(fx: Fixture) -> dict:
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    fx.end(sid, fx.transcript(sid, PROMPT, pid, ts))
    m = fx.episode(sid)
    if (why := load_inconclusive(m)) is not None:  # N20: the one lane failure had this shape
        raise Skip(f"inconclusive under load, {why}")
    assert m["review"]["state"] == "materialized", m["review"]
    return m


def _age_payload(fx: Fixture, ep: str, days: int) -> None:
    p = fx.stores.payloads / ep / "retention.json"
    rec = read_json(p)
    rec["expires_at"] = iso(now_utc() - timedelta(days=days))
    write_json_atomic(p, rec)


def _orphan(fx: Fixture, name: str, age_days: int) -> Path:
    d = fx.stores.snapshots / name
    d.mkdir()
    (d / "x.txt").write_text("x\n", encoding="utf-8")
    store.write_sums(d)
    old = time.time() - age_days * 86400
    os.utime(d, (old, old))
    return d


def _run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(PKG / "capture.py"), "backstop", *args], env=env,
                          capture_output=True, text=True, timeout=300)


# --------------------------------------------------------------------------- tests

def test_backstop_expires_without_prompts(root: Path) -> None:
    """A scheduler-style run (plain process, no hook, no prompt) deletes expired content."""
    fx = Fixture(root)
    m = _materialized(fx)
    ep = m["episode_id"]
    fresh = _materialized(fx)
    _age_payload(fx, ep, days=1)
    orphan = _orphan(fx, "cap_orphan000000000", age_days=100)
    young = _orphan(fx, "cap_young0000000000", age_days=10)
    env = {**fx.env, "EC_CAPTURE_MAINTAIN": "0"}
    t0 = time.monotonic()
    res = _run_cli(env)
    REPORT["cli_backstop_ms"] = round((time.monotonic() - t0) * 1000)
    rec = json.loads(res.stdout)
    assert res.returncode == backstop.EXIT_OK and rec["status"] == "ok", (res.returncode, rec, res.stderr)
    assert rec["counts"]["expired_deleted"] == 2, rec["counts"]
    assert not (fx.stores.payloads / ep).exists() and not orphan.exists(), "expired content deleted"
    assert young.is_dir() and (fx.stores.payloads / fresh["episode_id"]).is_dir(), "unexpired content kept"
    assert overdue(fx.stores) == [], "nothing past expiry remains"
    deleted = {r["episode_id"] for r in store.read_deletions(fx.stores)}
    assert {ep, orphan.name} <= deleted and all(r["verified"] for r in store.read_deletions(fx.stores))
    assert read_json(store.manifest_path(fx.stores, ep))["retention"]["deleted_kind"] == "payloads"
    assert store.meta_clean(fx.stores), "the deletions are committed"
    status = Path(env["USERPROFILE"]) / ".claude" / backstop.STATUS_NAME
    doc = read_json(status)
    assert Path(rec["status_file"]) == status and doc["last_status"] == "ok" and doc["consecutive_failures"] == 0, doc
    assert "backstop ok" in (fx.stores.meta / "capture.log").read_text(encoding="utf-8")
    REPORT["expired_deleted"] = rec["counts"]["expired_deleted"]


def test_backstop_reports_failures(root: Path) -> None:
    fx = Fixture(root)
    status = root / "status" / "backstop.json"
    orphan = _orphan(fx, "cap_orphan000000001", age_days=100)

    # 1. no stores configured
    bare = {k: v for k, v in fx.env.items() if k not in ("EC_CAPTURE_META", "EC_CAPTURE_CONTENT")}
    rec = backstop.run_backstop(bare, status=status, config=root / "no-user-config.json")
    assert rec["exit"] == backstop.EXIT_FAIL and rec["problems"] == ["stores_unconfigured"], rec
    assert read_json(status)["consecutive_failures"] == 1

    # 2. content store unusable: expiry cannot run, and the overdue orphan is reported
    marker = fx.stores.content / ".git-blocked"
    marker.unlink()
    rec = backstop.run_backstop(fx.env, status=status, lock_wait_s=0)
    assert rec["exit"] == backstop.EXIT_FAIL, rec
    assert any(p.startswith("content_store_unusable") for p in rec["problems"]), rec["problems"]
    assert "overdue_remaining: 1" in rec["problems"] and orphan.is_dir(), rec["problems"]
    assert read_json(status)["consecutive_failures"] == 2
    assert "backstop fail" in (fx.stores.meta / "capture.log").read_text(encoding="utf-8")
    marker.write_text("", encoding="utf-8")

    # 3. a maintenance lock that is never released
    holder = _hold_lock(root, fx.stores.meta / "maintain.lock")
    try:
        rec = backstop.run_backstop(fx.env, status=status, lock_wait_s=0)
    finally:
        holder.kill()
        holder.wait()
    assert "maintenance_lock_held" in rec["problems"] and orphan.is_dir(), rec

    # 4. the status file cannot be written: still a failure, still on stderr and in the log
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "status.json").mkdir()
    rec = backstop.run_backstop(fx.env, status=blocked / "status.json")
    assert rec["exit"] == backstop.EXIT_FAIL and any(p.startswith("status_unwritable") for p in rec["problems"]), rec

    # 5. recovery: expiry runs, the failure streak resets
    rec = backstop.run_backstop(fx.env, status=status)
    assert rec["exit"] == backstop.EXIT_OK and not orphan.exists(), rec
    doc = read_json(status)
    assert doc["consecutive_failures"] == 0 and doc["last_ok"] == rec["at"], doc
    assert [h["status"] for h in doc["history"]][:4] == ["ok", "fail", "fail", "fail"], doc["history"]

    # 6. health: ok now, stale when the scheduler stops running it, never_run without a file
    assert backstop.health(status)["state"] == "ok"
    later = iso(now_utc() + timedelta(hours=backstop.STALE_AFTER_H + 1))
    assert backstop.health(status, now=later)["state"] == "stale"
    assert backstop.health(root / "none.json")["state"] == "never_run"

    # 7. refusals after the last pass are never hidden behind its "ok"
    after = [iso(now_utc() + timedelta(minutes=i + 1)) for i in range(pipeline.FAILURE_STREAK)]
    before = [iso(now_utc() - timedelta(days=2))]
    assert backstop.health(status, failure_times=before)["state"] == "ok"
    one = backstop.health(status, failure_times=after[:1], now=after[-1])
    assert one["state"] == "warn" and one["capture_failures_since_last_run"] == 1, one
    many = backstop.health(status, failure_times=before + after, now=after[-1])
    assert many["state"] == "fail" and many["capture_failures_since_last_run"] == pipeline.FAILURE_STREAK, many


def test_backstop_waits_for_maintenance_child(root: Path) -> None:
    """A hook-launched maintenance run holding the lock delays the backstop, not fails it."""
    fx = Fixture(root)
    orphan = _orphan(fx, "cap_orphan000000002", age_days=100)
    holder = _hold_lock(root, fx.stores.meta / "maintain.lock")
    threading.Timer(3.0, holder.kill).start()
    t0 = time.monotonic()
    rec = backstop.run_backstop(fx.env, status=root / "status.json", lock_wait_s=30)
    waited = time.monotonic() - t0
    holder.wait()
    assert rec["exit"] == backstop.EXIT_OK and not orphan.exists(), rec
    assert waited >= 2.5, waited


def test_maintenance_due_probes_the_lock_not_the_file(root: Path) -> None:
    """N23: a ``maintain.lock`` left by a dead run does not suppress maintenance; a live
    holder does."""
    fx = Fixture(root)
    lock = fx.stores.meta / "maintain.lock"
    lock.write_text("stale holder\n", encoding="utf-8")
    assert pipeline.maintenance_due(fx.stores), "a leftover lock file suppressed maintenance"
    holder = _hold_lock(root, lock)
    try:
        assert not pipeline.maintenance_due(fx.stores), "launched over a live maintenance run"
    finally:
        holder.kill()
        holder.wait()
    assert pipeline.maintenance_due(fx.stores), "the lock stayed suppressed after its holder died"
    (fx.stores.meta / pipeline.MAINTENANCE).write_text("{}", encoding="utf-8")
    assert not pipeline.maintenance_due(fx.stores), "a fresh pass is not due again"


def test_backstop_warns_on_undated_content(root: Path) -> None:
    fx = Fixture(root)
    d = fx.stores.payloads / "cap_undated00000000"
    d.mkdir()
    (d / "retention.json").write_text("{}", encoding="utf-8")
    rec = backstop.run_backstop(fx.env, status=root / "status.json")
    assert rec["exit"] == backstop.EXIT_WARN and rec["warnings"] == ["undated_content: 1"], rec
    assert d.is_dir(), "undated content is kept and flagged, never guessed"


def test_checkpoint_notes_backstop(root: Path) -> None:
    fx = Fixture(root)
    _materialized(fx)
    stale = {"state": "stale", "last_ok": "2026-01-01T00:00:00Z", "consecutive_failures": 3}
    _, ck = report.write_checkpoint(fx.stores, backstop=stale)
    assert any(n.startswith("maintenance backstop stale") for n in ck["notes"]), ck["notes"]
    _, ck = report.write_checkpoint(fx.stores, backstop={"state": "ok"})
    assert not any("backstop" in n for n in ck["notes"]), ck["notes"]


def test_task_xml_is_reviewable_not_registered(root: Path) -> None:
    out = root / "task.xml"
    env = {**os.environ, "HOME": str(root), "USERPROFILE": str(root)}
    res = _run_cli(env, "--task-xml", str(out))
    assert res.returncode == 0, res.stderr
    tree = ET.fromstring(out.read_text(encoding="utf-16").split("?>", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert tree.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns) == "InteractiveToken"
    assert tree.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert tree.findtext("t:Settings/t:StartWhenAvailable", namespaces=ns) == "true"
    assert tree.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns) == "IgnoreNew"
    assert tree.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns).endswith("capture.py\" backstop")
    assert Path(tree.findtext("t:Actions/t:Exec/t:Command", namespaces=ns)).is_file()
    src = (PKG / "backstop.py").read_text(encoding="utf-8") + (PKG / "capture.py").read_text(encoding="utf-8")
    assert "schtasks" not in src and "Register-ScheduledTask" not in src, "nothing registers a task"


def _started(fx: Fixture) -> dict:
    sid = str(uuid.uuid4())
    fx.start(sid, packet=False)
    return fx.episode(sid)


def _run_maintain_cli(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(PKG / "capture.py"), "maintain"], env=env,
                          capture_output=True, text=True, timeout=300)


def test_episode_errors_fail_the_pass_and_expiry_still_runs(root: Path) -> None:
    """M4, L11: an episode maintenance cannot handle is counted, fails the backstop and the
    maintain CLI, and does not stop the rest of the pass (expiry included)."""
    fx = Fixture(root)
    status = root / "status.json"
    orphan = _orphan(fx, "cap_orphan000000003", age_days=100)
    bad_key = _started(fx)
    bad_key["capture"]["phase"] = "ended"
    bad_key["ended_at"] = iso(now_utc())
    del bad_key["boundary"]  # KeyError inside the episode
    store.write_manifest(fx.stores, bad_key)
    bad_type = _started(fx)
    bad_type["capture"]["phase"] = "ended"
    bad_type["ended_at"] = iso(now_utc())
    bad_type["link"] = None  # AttributeError: outside the old per-episode tuple
    store.write_manifest(fx.stores, bad_type)

    rec = backstop.run_backstop(fx.env, status=status, lock_wait_s=0)
    assert rec["exit"] == backstop.EXIT_FAIL and rec["status"] == "fail", rec
    assert rec["counts"]["episode_errors"] == 2 and "error" not in rec["counts"], rec["counts"]
    assert set(rec["counts"]["episode_error_ids"]) == {bad_key["episode_id"], bad_type["episode_id"]}
    assert any(p.startswith("episode_errors: 2") for p in rec["problems"]), rec["problems"]
    assert rec["counts"]["expired_deleted"] >= 1 and not orphan.exists(), "expiry ran despite the bad episodes"

    res = _run_maintain_cli(fx.env)
    out = json.loads(res.stdout)
    assert res.returncode == backstop.EXIT_FAIL and out["status"] == "fail", (res.returncode, out, res.stderr)
    assert out["episode_errors"] == 2, out


def test_busy_episode_is_reported_not_silent(root: Path) -> None:
    """M4: an episode skipped because another process holds its lock makes the pass a warning."""
    fx = Fixture(root)
    m = _started(fx)
    holder = _hold_lock(root, pipeline.episode_lock(fx.stores, m["episode_id"]))
    try:
        rec = backstop.run_backstop(fx.env, status=root / "status.json", lock_wait_s=0)
    finally:
        holder.kill()
        holder.wait()
    assert rec["exit"] == backstop.EXIT_WARN and rec["counts"]["busy_skipped"] == 1, rec
    assert "episodes_busy: 1" in rec["warnings"] and not rec["problems"], rec
    rec = backstop.run_backstop(fx.env, status=root / "status.json", lock_wait_s=0)
    assert rec["exit"] == backstop.EXIT_OK and rec["counts"]["busy_skipped"] == 0, rec


def test_unreadable_manifest_is_counted_everywhere(root: Path) -> None:
    """M5: a truncated manifest fails the backstop, shows in the checkpoint denominator and
    blocks an FN-audit sample instead of vanishing from all three."""
    fx = Fixture(root)
    _materialized(fx)
    (fx.stores.manifests / "cap_truncated0000000.json").write_text('{"episode_id": "cap_trunc', encoding="utf-8")
    scan = store.scan_manifests(fx.stores)
    assert scan.unreadable == ["cap_truncated0000000.json"] and len(scan.manifests) == 1, scan
    rec = backstop.run_backstop(fx.env, status=root / "status.json", lock_wait_s=0)
    assert rec["exit"] == backstop.EXIT_FAIL and "manifests_unreadable: 1" in rec["problems"], rec
    ck = report.build_checkpoint(fx.stores, 1, tool_commit="0" * 40)
    d = ck["denominator"]
    assert d["episodes_total"] == 1 and d["manifests_unreadable"] == 1, d
    assert any("manifest(s) unreadable" in n for n in ck["notes"]), ck["notes"]
    try:
        audit.build_sheet(fx.stores, commit=False)
    except audit.AuditError as exc:
        assert "unreadable" in str(exc), exc
    else:
        raise AssertionError("an FN-audit sample was drawn with a manifest unreadable")


def _pending_starts(fx: Fixture, n: int) -> Path:
    """``n`` starts the content store could not take: each becomes a pending record."""
    markers = {k: (fx.stores.content / k).read_bytes() for k in store.CONTENT_MARKERS}
    for k in markers:
        (fx.stores.content / k).unlink()
    for _ in range(n):
        fx.start(str(uuid.uuid4()), packet=False)
    for k, data in markers.items():
        (fx.stores.content / k).write_bytes(data)
    pending = fx.tmp / pipeline.PENDING_NAME
    assert len(pending.read_text(encoding="utf-8").splitlines()) == n
    return pending


def test_pending_record_kept_until_its_manifest_is_written(root: Path) -> None:
    """M6: a pending record whose manifest write fails, or does not read back, stays pending
    and fails the pass; the next good pass records it."""
    fx = Fixture(root)
    status = root / "status.json"
    pending = _pending_starts(fx, 2)
    real = pipeline.write_manifest
    try:
        def unwritable(stores: store.Stores, m: dict) -> Path:
            raise OSError("synthetic: manifest store refused the write")
        pipeline.write_manifest = unwritable  # type: ignore[assignment]
        rec = backstop.run_backstop(fx.env, status=status, lock_wait_s=0)
        assert rec["exit"] == backstop.EXIT_FAIL, rec
        assert rec["counts"]["pending_kept"] == 2 and rec["counts"]["pending_drained"] == 0, rec["counts"]
        assert any(p.startswith("pending_errors: 2") for p in rec["problems"]), rec["problems"]
        assert len(pending.read_text(encoding="utf-8").splitlines()) == 2, "records put back"

        def lost(stores: store.Stores, m: dict) -> Path:  # reports success, writes nothing
            return store.manifest_path(stores, m["episode_id"])
        pipeline.write_manifest = lost  # type: ignore[assignment]
        rec = backstop.run_backstop(fx.env, status=status, lock_wait_s=0)
        assert rec["counts"]["pending_kept"] == 2 and rec["exit"] == backstop.EXIT_FAIL, rec["counts"]
    finally:
        pipeline.write_manifest = real  # type: ignore[assignment]
    assert not list(fx.tmp.glob(f"{pipeline.PENDING_NAME}.*.draining")), "no claimed file left behind"
    rec = backstop.run_backstop(fx.env, status=status, lock_wait_s=0)
    assert rec["counts"]["pending_drained"] == 2 and rec["counts"]["pending_errors"] == 0, rec["counts"]
    assert not pending.exists()
    assert sum(1 for m in manifests(fx.stores) if m["capture"]["status"] == "failed") == 2


def test_orphaned_draining_file_is_recovered(root: Path) -> None:
    """M6: a drain that died after claiming the pending file leaves a ``.draining`` file;
    the next pass records it whatever its age (drains run only under maintain.lock, so no
    live drain can own one)."""
    fx = Fixture(root)
    pending = _pending_starts(fx, 2)
    first, second = pending.read_text(encoding="utf-8").splitlines()
    pending.unlink()
    stale = fx.tmp / f"{pipeline.PENDING_NAME}.4242.1.draining"
    stale.write_text(first + "\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    fresh = fx.tmp / f"{pipeline.PENDING_NAME}.4343.2.draining"
    fresh.write_text(second + "\n", encoding="utf-8")
    res = pipeline.run_maintain(fx.stores, fx.env)
    assert res["pending_drained"] == 2 and res["pending_errors"] == 0, res
    assert not stale.exists() and not fresh.exists(), "every orphaned claim recovered"
    eps = {m["episode_id"] for m in manifests(fx.stores)}
    assert {json.loads(first)["episode_id"], json.loads(second)["episode_id"]} <= eps


def test_live_claim_is_not_taken_by_another_store(root: Path) -> None:
    """The pending file is shared by every store on the machine while maintain.lock is per
    store, so a ``.draining`` file a live drain (another store's maintenance) still holds is
    left to it; once its drainer is gone the next pass recovers it."""
    fx = Fixture(root)
    pending = _pending_starts(fx, 1)
    line = pending.read_text(encoding="utf-8").splitlines()[0]
    pending.unlink()
    live = fx.tmp / f"{pipeline.PENDING_NAME}.4444.3.draining"
    live.write_text(line + "\n", encoding="utf-8")
    holder = _hold_lock(root, pipeline._claim_lock(live))
    try:
        res = pipeline.run_maintain(fx.stores, fx.env)
        assert res["pending_drained"] == 0 and live.exists(), ("a live claim was taken", res)
    finally:
        holder.kill()
        holder.wait()
    res = pipeline.run_maintain(fx.stores, fx.env)
    assert res["pending_drained"] == 1 and not live.exists(), res
    assert not list(fx.tmp.glob(f"{pipeline.PENDING_NAME}.*.lock")), "claim locks left behind"


def test_rejoin_review_error_is_counted(root: Path) -> None:
    """L7: a review error on the re-join path reaches ``review_errors`` (and the backstop)."""
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    fx.transcript(sid, PROMPT, pid, ts)  # the Stop hook never ran for this turn
    second = "Now add a docstring to alpha."
    ts2 = iso(now_utc())
    fx.start(sid, prompt=second, packet=False)
    fx.transcript(sid, second, None, ts2, append=True, edit=False)
    real = pipeline.review_episode
    calls: list[str] = []

    def erroring(stores: store.Stores, m: dict, *args: object, **kw: object) -> dict:
        calls.append(m["episode_id"])
        return {**real(stores, m, *args, **kw), "error": "synthetic review error"}  # type: ignore[arg-type]
    try:
        pipeline.review_episode = erroring  # type: ignore[assignment]
        rec = backstop.run_backstop(fx.env, status=root / "status.json", lock_wait_s=0)
    finally:
        pipeline.review_episode = real  # type: ignore[assignment]
    c = rec["counts"]
    assert c["rejoined"] == 1 and c["reviewed"] == 1 and len(calls) == 1, (c, calls)
    assert c["review_errors"] == 1 and "review_errors: 1" in rec["warnings"], rec


def test_failed_metadata_commit_fails_the_pass(root: Path) -> None:
    """L8: a maintenance commit the metadata repository refuses (a pre-commit hook here)
    is a failure, not a later ``metadata_repo_clean=false``."""
    fx = Fixture(root)
    m = _started(fx)
    m["started_at"] = iso(now_utc() - timedelta(hours=pipeline.REJOIN_WINDOW_H + 1))
    store.write_manifest(fx.stores, m)
    hook = fx.stores.meta / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\necho synthetic refusal >&2\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    rec = backstop.run_backstop(fx.env, status=root / "status.json", lock_wait_s=0)
    assert rec["counts"]["rejoin_expired"] == 1 and rec["counts"]["meta_commit_failed"] is True, rec["counts"]
    assert "metadata_commit_failed" in rec["problems"] and rec["exit"] == backstop.EXIT_FAIL, rec
    hook.unlink()
    rec = backstop.run_backstop(fx.env, status=root / "status.json", lock_wait_s=0)
    assert rec["counts"]["meta_commit_failed"] is False and store.meta_clean(fx.stores), rec


def test_maintenance_relinks_through_reconcile(root: Path) -> None:
    """L12: maintenance re-resolves links through ``linkage.reconcile``, so a stored packet
    that no longer matches is corrected even when the link state itself is unchanged."""
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    _, ts = fx.start(sid, packet=False)
    fx.end(sid, fx.transcript(sid, PROMPT, None, ts))
    m = fx.episode(sid)
    assert m["link"]["state"] == "none" and m["review"]["state"] != "materialized", (m["link"], m["review"])
    m["link"]["packet_id"] = "ep_stale_packet"
    store.write_manifest(fx.stores, m)
    res = pipeline.run_maintain(fx.stores, fx.env)
    got = fx.episode(sid)
    assert res["relinked"] == 1, res
    assert got["link"]["state"] == "none" and got["link"]["packet_id"] is None, got["link"]
    log = (fx.stores.meta / "capture.log").read_text(encoding="utf-8")
    assert f"link {m['episode_id']} -> none" in log
    assert pipeline.run_maintain(fx.stores, fx.env)["relinked"] == 0, "an unchanged link is not rewritten"


TESTS = [test_backstop_expires_without_prompts, test_backstop_reports_failures,
         test_backstop_waits_for_maintenance_child, test_maintenance_due_probes_the_lock_not_the_file,
         test_backstop_warns_on_undated_content,
         test_checkpoint_notes_backstop, test_task_xml_is_reviewable_not_registered,
         test_episode_errors_fail_the_pass_and_expiry_still_runs, test_busy_episode_is_reported_not_silent,
         test_unreadable_manifest_is_counted_everywhere, test_pending_record_kept_until_its_manifest_is_written,
         test_orphaned_draining_file_is_recovered, test_live_claim_is_not_taken_by_another_store,
         test_rejoin_review_error_is_counted,
         test_failed_metadata_commit_fails_the_pass, test_maintenance_relinks_through_reconcile]


def main() -> int:
    require_yaml("test_backstop")
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-backstop-test-") as tmp:
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

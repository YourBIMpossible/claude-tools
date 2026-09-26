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
import backstop  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
from common import iso, now_utc, read_json, write_json_atomic  # noqa: E402
from review import overdue  # noqa: E402
from test_hooks import PROMPT, Fixture  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
REPORT: dict[str, object] = {}


def _materialized(fx: Fixture) -> dict:
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    fx.end(sid, fx.transcript(sid, PROMPT, pid, ts))
    m = fx.episode(sid)
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
    lock = fx.stores.meta / "maintain.lock"
    lock.write_text("1 held\n", encoding="utf-8")
    rec = backstop.run_backstop(fx.env, status=status, lock_wait_s=0)
    assert "maintenance_lock_held" in rec["problems"] and orphan.is_dir(), rec
    lock.unlink()

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


def test_backstop_waits_for_maintenance_child(root: Path) -> None:
    """A hook-launched maintenance run holding the lock delays the backstop, not fails it."""
    fx = Fixture(root)
    orphan = _orphan(fx, "cap_orphan000000002", age_days=100)
    lock = fx.stores.meta / "maintain.lock"
    lock.write_text("1 child\n", encoding="utf-8")
    threading.Timer(3.0, lock.unlink).start()
    t0 = time.monotonic()
    rec = backstop.run_backstop(fx.env, status=root / "status.json", lock_wait_s=30)
    waited = time.monotonic() - t0
    assert rec["exit"] == backstop.EXIT_OK and not orphan.exists(), rec
    assert waited >= 2.5, waited


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


TESTS = [test_backstop_expires_without_prompts, test_backstop_reports_failures,
         test_backstop_waits_for_maintenance_child, test_backstop_warns_on_undated_content,
         test_checkpoint_notes_backstop, test_task_xml_is_reviewable_not_registered]


def main() -> int:
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

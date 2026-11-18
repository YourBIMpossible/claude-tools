#!/usr/bin/env python3
"""Regression checks for the SA11 / SA16 follow-up batch in evidence-capture.

Each check pins one finding from audits/2026-11-11__slop-audit.md (SA11-CT-*) or
audits/2026-11-16__slop-audit.md (SA16-CT-*): dead code stays gone, and each formerly
silent failure path now reports itself. Synthetic only: stores and files live in a
temporary directory.

Run: python evidence-capture/tests/test_sa11_followups.py [test_name ...]
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
import backstop  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


@contextlib.contextmanager
def patched(obj: Any, name: str, value: Any) -> Iterator[None]:
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def plain_stores(root: Path) -> store.Stores:
    st = store.Stores(root / "meta", root / "content")
    store.init_stores(st)
    return st


def test_dead_pipeline_helpers_are_gone(root: Path) -> None:
    """SA11-CT-21 / SA16-CT-27 and SA11-CT-22 / SA16-CT-28: rejoin_deadline had no caller
    and captured_root_gone only test callers (screen_root's capture_hash source replaced
    it); neither may come back as dead code."""
    assert not hasattr(pipeline, "rejoin_deadline")
    assert not hasattr(pipeline, "captured_root_gone")


def test_failure_records_report_unreadable_manifests(root: Path) -> None:
    """SA11-CT-52: a manifest scan_manifests cannot read hides whether that episode's
    capture failed; capture_failure_records reports it as a problem, never as zero."""
    st = plain_stores(root)
    (st.manifests / "cap_bad.json").write_text("{not json", encoding="utf-8")
    tmp = root / "tmp"
    tmp.mkdir()
    times, problems = pipeline.capture_failure_records(st, {"TEMP": str(tmp)})
    assert times == [] and problems == ["manifest unreadable cap_bad.json"], (times, problems)


def test_spawn_maintenance_failure_is_logged(root: Path) -> None:
    """SA11-CT-54: a maintenance launch that fails is logged by spawn_maintenance itself
    (its caller ignores the result), not only noticed later by the backstop's age check."""
    st = plain_stores(root)
    tmp = root / "tmp"
    tmp.mkdir()
    env = {store.ENV_META: str(st.meta), store.ENV_CONTENT: str(st.content), "TEMP": str(tmp)}

    def refuse(*a: Any, **k: Any) -> None:
        raise OSError(8, "Exec format error")

    with patched(subprocess, "Popen", refuse):
        assert pipeline.spawn_maintenance(env) is False
    log = (st.meta / "capture.log").read_text(encoding="utf-8")
    assert "maintenance not launched: OSError" in log, log


def test_maintain_warns_on_unreadable_seals(root: Path) -> None:
    """SA11-CT-19 follow-up: review.expire reports seal files it could not read; the
    maintenance pass carries the count and maintain_findings warns on it."""
    st = plain_stores(root)
    subprocess.run(["git", "-C", str(st.meta), "init", "-q"], check=True, capture_output=True)
    tmp = root / "tmp"
    tmp.mkdir()

    def expire(stores: store.Stores, *, now: str, commit: bool) -> dict[str, int]:
        return {"expired_deleted": 0, "errors": 0, "undated": 0, "seals_unreadable": 2}

    with patched(pipeline, "expire", expire):
        counts = pipeline.run_maintain(st, {"TEMP": str(tmp)})
    assert counts.get("expire_seals_unreadable") == 2, counts
    _, warnings = pipeline.maintain_findings(counts)
    assert "seals_unreadable: 2" in warnings, warnings
    assert pipeline.maintain_findings({"action": "maintained", "expire_seals_unreadable": 0}) == ([], [])


def test_checkpoint_names_unreadable_maintenance_and_audits(root: Path) -> None:
    """SA11-CT-55: an unreadable maintenance.json and an unreadable FN audit are named in
    the checkpoint's notes; neither silently becomes an empty record or an older audit."""
    st = plain_stores(root)
    (st.meta / "maintenance.json").write_text("{not json", encoding="utf-8")
    audits = st.meta / "audits"
    audits.mkdir(parents=True, exist_ok=True)
    (audits / "fn-audit-1.json").write_text(json.dumps({"n": 3}), encoding="utf-8")
    (audits / "fn-audit-2.json").write_text("{not json", encoding="utf-8")
    _, ck = report.write_checkpoint(st, n=1, now="2026-11-17T00:00:00.000000Z", commit=False)
    notes = ck["notes"]
    assert any(n.startswith("maintenance.json unreadable (JSONDecodeError)") for n in notes), notes
    assert any("fn-audit-2.json" in n and "fn-audit-1.json" in n for n in notes), notes
    assert ck["fn_audit"]["n"] == 3, ck["fn_audit"]
    (st.meta / "maintenance.json").write_text(json.dumps([1]), encoding="utf-8")
    _, ck = report.write_checkpoint(st, n=2, now="2026-11-17T00:00:00.000000Z", commit=False)
    assert any(n.startswith("maintenance.json unreadable (not an object)") for n in ck["notes"]), ck["notes"]


def test_unreadable_backstop_status_is_a_warning(root: Path) -> None:
    """SA11-CT-56: an unreadable previous status file no longer silently resets the
    history and the failure streak: the pass warns, and the unreadable file is kept."""
    status = root / "backstop-status.json"
    for body in ("{not json", json.dumps([1])):
        status.write_text(body, encoding="utf-8")
        rec = {"at": "2026-11-17T00:00:00.000000Z", "status": "ok", "problems": [], "warnings": [],
               "exit": backstop.EXIT_OK}
        backstop._record(rec, status, None)
        assert rec["status"] == "warn" and rec["exit"] == backstop.EXIT_WARN, rec
        assert any(w.startswith("status_history_unreadable") for w in rec["warnings"]), rec
        kept = status.with_name(status.name + ".unreadable")
        assert kept.read_text(encoding="utf-8") == body
        doc = json.loads(status.read_text(encoding="utf-8"))
        assert doc["history"][0]["status"] == "warn", doc
    rec = {"at": "2026-11-17T01:00:00.000000Z", "status": "fail", "problems": ["x"], "warnings": [],
           "exit": backstop.EXIT_FAIL}
    backstop._record(rec, status, None)  # a readable file again: the streak counts on
    assert rec["warnings"] == [] and json.loads(status.read_text(encoding="utf-8"))["consecutive_failures"] == 1


TESTS = [test_checkpoint_names_unreadable_maintenance_and_audits, test_unreadable_backstop_status_is_a_warning,
         test_dead_pipeline_helpers_are_gone, test_failure_records_report_unreadable_manifests,
         test_spawn_maintenance_failure_is_logged, test_maintain_warns_on_unreadable_seals]


def main() -> int:
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-sa11-test-") as tmp:
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
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

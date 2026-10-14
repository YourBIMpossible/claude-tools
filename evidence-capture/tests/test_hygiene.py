#!/usr/bin/env python3
"""Regression checks for the 2026-10-12 low-severity audit batch (SA-5..SA-15).

Each check pins one silent-failure path that now reports itself. Synthetic only:
stores, repositories and pending files live in a temporary directory.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterator

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
import backstop  # noqa: E402
import capture  # noqa: E402
import causal  # noqa: E402
import causal_admit  # noqa: E402
import causal_hooks  # noqa: E402
import manifest as mf  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
import rescreen  # noqa: E402
import review  # noqa: E402
import store  # noqa: E402
import test_capture as tc  # noqa: E402
import test_causal as tca  # noqa: E402

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


def test_sa5_start_steps_share_one_budget(root: Path) -> None:
    """SA-5: the worktree step spends the start budget; a slow one skips the overlay and
    bounds the tool-version wait instead of running past the hook's limit."""
    seen: dict[str, float] = {}

    def slow_records(repo: Path, timeout: float = 30.0) -> dict[str, Any]:
        seen["worktrees"] = timeout
        time.sleep(0.5)
        return {}

    def overlay(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AssertionError("the overlay ran with no budget left")

    def collect(pending: Any, timeout: float) -> dict[str, str]:
        seen["tools"] = timeout
        return {}

    with patched(causal, "tool_versions_async", lambda: {"git": "stub"}), \
            patched(causal, "unsupported_state", lambda r, lfs_scan=True: []), \
            patched(causal, "worktree_records", slow_records), \
            patched(causal, "shared_git_state", lambda r: "s"), \
            patched(causal, "capture_overlay", overlay), \
            patched(causal, "collect_tool_versions", collect):
        out = causal_hooks.start(root, "cap_sa5", root / "content", remaining_s=0.8)
    assert seen["worktrees"] <= 0.8, seen
    assert "causal:start:skipped_budget" in out["errors"] and out["start"] is None, out
    assert seen["tools"] <= 0.3, seen  # min(TOOLS_WAIT_S, what is left), never the full second


def test_sa6_stuck_open_pointers_are_counted(root: Path) -> None:
    """SA-6: a pointer naming no manifest, or one that cannot be read, is counted and warned."""
    st = plain_stores(root)
    d = st.meta / "open"
    d.mkdir(parents=True, exist_ok=True)
    (d / "sid-dangling").write_text("cap_missing_0000\n", encoding="utf-8")
    res = pipeline.retire_open_pointers(st, "2026-10-12T00:00:00.000000Z")
    assert res["dangling"] == 1 and res["unreadable"] == 0 and res["retire_failed"] == 0, res
    _, warnings = pipeline.maintain_findings({"open_pointers_dangling": 1, "open_pointers_unreadable": 2,
                                              "open_pointers_retire_failed": 0})
    assert "open_pointers_stuck: dangling 1, unreadable 2" in warnings, warnings
    assert pipeline.maintain_findings({"open_pointers_dangling": 0}) == ([], [])


def test_sa7_unreadable_failure_records_cap_health(root: Path) -> None:
    """SA-7: corrupt pending lines, an unreadable pending file and records claimed by a
    drain in flight are reported, never counted as zero failures."""
    st = plain_stores(root)
    tmp = root / "tmp"
    tmp.mkdir()
    env = {"TEMP": str(tmp)}
    pending = tmp / pipeline.PENDING_NAME
    good = {"episode_id": "cap_a", "started_at": "2026-10-12T01:00:00.000000Z"}
    pending.write_text(json.dumps(good) + "\n{not json\n" + json.dumps({"episode_id": "cap_b"}) + "\n",
                       encoding="utf-8")
    claimed = tmp / f"{pipeline.PENDING_NAME}.1234.draining"
    claimed.write_text(json.dumps({"episode_id": "cap_c", "started_at": "2026-10-12T02:00:00.000000Z"})
                       + "\n" + json.dumps(good) + "\n", encoding="utf-8")
    times, problems = pipeline.capture_failure_records(st, env)
    assert times == [good["started_at"], "2026-10-12T02:00:00.000000Z"], times  # cap_a counted once
    assert problems == [f"pending record unreadable in {pipeline.PENDING_NAME}"] * 2, problems

    pending.unlink()
    claimed.unlink()
    pending.mkdir()  # exists but cannot be read as a file
    times, problems = pipeline.capture_failure_records(st, env)
    assert times == [] and len(problems) == 1 and problems[0].startswith("pending file unreadable"), problems
    assert pipeline.capture_failure_records(st, {"TEMP": str(root / "none")}) == ([], [])

    status = root / "status.json"
    status.write_text(json.dumps({"last_status": "ok", "last_ok": "2026-10-12T03:00:00.000000Z",
                                  "last_run": "2026-10-12T03:00:00.000000Z", "consecutive_failures": 0}),
                      encoding="utf-8")
    now = "2026-10-12T04:00:00.000000Z"
    assert backstop.health(status, now=now)["state"] == "ok"
    capped = backstop.health(status, now=now, failure_problems=problems)
    assert capped["state"] == "warn" and capped["capture_failure_records_unreadable"] == problems, capped


def test_sa8_rescreen_child_failure_and_commit_failure_surface(root: Path) -> None:
    """SA-8: a failing child names its exit and stderr; an uncommitted rescreen exits 2."""
    st = plain_stores(root)
    m = {"episode_id": "cap_sa8_0000"}
    causes: list[str] = []
    child = "import sys; sys.stderr.write('boom from child'); sys.exit(3)"
    outcome, done = rescreen.run_child(st, m, {}, 30.0, (sys.executable, "-c", child), causes=causes)
    assert outcome == "child_failed" and done is None, outcome
    assert causes == ["exit 3: boom from child"], causes
    causes.clear()
    outcome, _ = rescreen.run_child(st, m, {}, 30.0, (sys.executable, "-c", "print('not json')"), causes=causes)
    assert outcome == "child_failed" and causes == ["unreadable reply: JSONDecodeError"], causes

    for committed, rc in ((False, 2), (True, 0), (None, 0)):
        res = {"action": "rescreened", "changed": [], "committed": committed}
        err = io.StringIO()
        with patched(capture, "_stores", lambda env: st), patched(rescreen, "run_rescreen", lambda s, e: res), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            got = capture.main(["rescreen"])
        assert got == rc, (committed, got)
        assert ("metadata commit failed" in err.getvalue()) == (rc == 2), err.getvalue()


def test_sa10_expire_keeps_every_deletion(root: Path) -> None:
    """SA-10: one expiry pass deleting snapshots and causal states records both."""
    st = plain_stores(root)
    ep = "cap_sa10_0000"
    store.write_manifest(st, {"episode_id": ep, "retention": {}})
    verified = {"snapshots": True, "causal": False}
    stamps = {"snapshots": "2026-10-12T00:00:01.000000Z", "causal": "2026-10-12T00:00:02.000000Z"}

    def due(stores: store.Stores, now_dt: Any) -> Iterator[tuple]:
        for kind in ("snapshots", "causal"):
            yield kind, ep, store.manifest_path(stores, ep), store.read_manifest(stores, ep), now_dt, None

    def deleted(stores: store.Stores, kind: str, e: str, why: str) -> dict[str, Any]:
        return {"deleted_at": stamps[kind], "verified": verified[kind]}

    with patched(review, "_content_due", due), patched(review, "verified_delete", deleted):
        counts = review.expire(st, now="2026-10-12T00:00:00.000000Z", commit=False)
    assert counts["expired_deleted"] == 2, counts
    ret = store.read_manifest(st, ep)["retention"]
    assert ret["deletions"] == {k: {"deleted_at": stamps[k], "verified": verified[k]} for k in stamps}, ret
    assert ret["deleted_kind"] == "causal" and ret["deletion_verified"] is False, ret


def test_sa11_both_deletion_errors_are_kept(root: Path) -> None:
    """SA-11: a causal deletion error no longer overwrites the snapshot deletion error."""
    st = tc.make_stores(root)
    repo = tc.make_repo(root, "repo11")
    m = tc.base_manifest("fix alpha")
    ep = m["episode_id"]
    store.write_manifest(st, m)
    (st.snapshots / ep).mkdir(parents=True)
    (st.content / "causal" / ep).mkdir(parents=True)

    def refuse(stores: store.Stores, kind: str, e: str, why: str) -> dict[str, Any]:
        raise store.StoreError(f"{kind} not verified")

    inputs = review.ReviewInputs(repo, "fix alpha", "", {"cli_version": "9.9.9", "plugins": []})
    with patched(review, "decide", lambda s, mm, i: ("excluded", "X1", None)), \
            patched(review, "verified_delete", refuse):
        out = review.review_episode(st, m, inputs, commit=False)
    assert out["error"] == "snapshots not verified; causal not verified", out


def test_sa12_unreadable_git_config_raises(root: Path) -> None:
    """SA-12: a config git cannot parse is an error, not "no conversion settings"."""
    repo = tca.make_repo(root)
    assert causal._conversion(repo)["core.autocrlf"] == "false"
    with open(repo / ".git" / "config", "ab") as f:
        f.write(b"\n[core\nbroken = = =\n")
    tca.raises(lambda: causal._conversion(repo), "git_config_unreadable")


def test_sa13_unread_small_file_marks_lfs_scan_incomplete(root: Path) -> None:
    """SA-13: a small file the LFS scan cannot read leaves the scan incomplete, not clean."""
    repo = tca.make_repo(root, {"ptr.bin": b"short\n", "b.txt": b"x\n"})
    assert causal.unsupported_state(repo) == []
    real = Path.read_bytes

    def guarded(self: Path) -> bytes:
        if self.name == "ptr.bin":
            raise PermissionError("locked")
        return real(self)

    with patched(Path, "read_bytes", guarded):
        assert causal.unsupported_state(repo) == ["lfs_scan_incomplete"]


def test_sa14_failed_history_read_is_not_a_rewrite(root: Path) -> None:
    """SA-14: git log failing reports the history unreadable, not rewritten or clean."""
    st = plain_stores(root)
    ep = "cap_sa14_0000"
    p = causal_admit.checks_path(st, ep)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"episode_id": ep, "sha256": "d", "checks": [], "rationale": ""}), encoding="utf-8")
    real = subprocess.run

    def fake(argv: list[str], *a: Any, **k: Any) -> subprocess.CompletedProcess:
        if argv[:3] == ["git", "-C", str(st.meta)]:
            rc = 128 if "log" in argv else 0
            return subprocess.CompletedProcess(argv, rc, "" if k.get("text") else b"", "")
        return real(argv, *a, **k)

    with patched(causal_admit, "_digest", lambda c, r: "d"), \
            patched(causal, "validate_predicate", lambda c: None), patched(subprocess, "run", fake):
        entry, probs = causal_admit.verify_registration(st, ep)
    assert entry is not None and probs == ["registration_history_unreadable"], probs


def test_sa15_x6_x7_are_unresolved_not_scope(root: Path) -> None:
    """SA-15: X6/X7 restate an incomplete snapshot or capture; they never count as scope."""
    m = tc.base_manifest("fix alpha")
    m["funnel"] = {k: False for k in mf.FUNNEL}
    m["link"]["state"] = "linked"
    m["start_snapshot"]["state"] = "complete"
    m["observation"]["sufficient"] = True
    m["boundary"] = {"trusted": True, "reason": None}
    for rule, state in (("X6", "yes"), ("X7", "yes"), ("X6", "unknown")):
        x = copy.deepcopy(m)
        x["exclusions"] = [{"rule": r, "state": state if r == rule else "no"} for r in mf.RULES]
        assert report.blocker_partition([x])["classes"] == {"unresolved": 1}, (rule, state)
    x = copy.deepcopy(m)
    x["exclusions"] = [{"rule": r, "state": "yes" if r == "X3" else "no"} for r in mf.RULES]
    assert report.blocker_partition([x])["classes"] == {"scope_excluded": 1}


TESTS: list[Callable[[Path], None]] = [
    test_sa5_start_steps_share_one_budget, test_sa6_stuck_open_pointers_are_counted,
    test_sa7_unreadable_failure_records_cap_health, test_sa8_rescreen_child_failure_and_commit_failure_surface,
    test_sa10_expire_keeps_every_deletion, test_sa11_both_deletion_errors_are_kept,
    test_sa12_unreadable_git_config_raises, test_sa13_unread_small_file_marks_lfs_scan_incomplete,
    test_sa14_failed_history_read_is_not_a_rewrite, test_sa15_x6_x7_are_unresolved_not_scope]


def main() -> int:
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-hygiene-") as tmp:
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

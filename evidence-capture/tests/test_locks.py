#!/usr/bin/env python3
"""Multiprocess tests for the capture locks and the locked JSONL appends (review F2, F3,
F9, F13). Every contender is a separate OS process (tests/lockproc.py), coordinated by a
"go" file so they race for real.

Run: python evidence-capture/tests/test_locks.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
import pipeline  # noqa: E402

WORKER = [sys.executable, str(HERE / "lockproc.py")]
PASSED: list[str] = []
FAILED: list[str] = []


def _spawn(*args: object) -> subprocess.Popen:
    return subprocess.Popen(WORKER + [str(a) for a in args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _wait_for(path: Path, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"never appeared: {path}")
        time.sleep(0.01)


def _finish(procs: list[subprocess.Popen], timeout_s: float = 120.0) -> None:
    for p in procs:
        rc = p.wait(timeout=timeout_s)
        err = p.stderr.read().decode(errors="replace") if p.stderr else ""
        assert rc == 0, (rc, err)


def test_live_holder_is_never_stolen(root: Path) -> None:
    """F2/F3: a live holder keeps its lock however old the lock file is; there is no
    staleness rule to steal it by."""
    lock, ready = root / "locks" / "ep.lock", root / "ready"
    holder = _spawn("hold", lock, ready)
    try:
        _wait_for(ready)
        old = time.time() - 10 * 24 * 3600
        os.utime(lock, (old, old))  # a lock file "older" than any former stale limit
        assert not pipeline._try_lock(lock), "a live holder's lock was taken"
        assert not pipeline._wait_lock(lock, 0.5), "a live holder's lock was taken while waiting"
        assert holder.poll() is None
    finally:
        holder.kill()
        holder.wait()


def test_dead_holder_lock_is_free_at_once(root: Path) -> None:
    """F2/F3: the OS drops a killed holder's lock; the next taker needs no age rule."""
    lock, ready = root / "locks" / "ep.lock", root / "ready"
    holder = _spawn("hold", lock, ready)
    _wait_for(ready)
    holder.kill()
    holder.wait()
    assert lock.exists(), "a killed holder leaves its file behind (the case under test)"
    assert pipeline._wait_lock(lock, 5.0), "a dead holder's lock was not reacquirable"
    pipeline._unlock(lock)


def test_exactly_one_contender_wins(root: Path) -> None:
    """F2: N processes released together; exactly one holds the lock each round,
    including rounds where a stale-looking lock file already exists."""
    n = 8
    for rnd in range(6):
        d = root / f"r{rnd}"
        d.mkdir()
        lock, go, release = d / "x.lock", d / "go", d / "release"
        if rnd % 2:
            lock.write_text("1 left by a dead holder\n", encoding="utf-8")
            old = time.time() - 10 * 24 * 3600
            os.utime(lock, (old, old))
        outs = [d / f"out{i}" for i in range(n)]
        procs = [_spawn("once", lock, go, o, release) for o in outs]
        time.sleep(0.5)
        go.write_text("", encoding="utf-8")
        for o in outs:
            _wait_for(o)
        wins = sum(o.read_text(encoding="utf-8") == "1" for o in outs)
        release.write_text("", encoding="utf-8")
        _finish(procs)
        assert wins == 1, f"round {rnd}: {wins} holders"


def test_mutual_exclusion_under_churn(root: Path) -> None:
    """F2: acquire/release churn across processes (the release-unlink race) never lets two
    holders in: every read-increment-write survives."""
    procs_n, per = 6, 40
    lock, counter, go = root / "c.lock", root / "counter", root / "go"
    counter.write_text("0", encoding="utf-8")
    procs = [_spawn("count", lock, counter, per, go) for _ in range(procs_n)]
    time.sleep(0.5)
    go.write_text("", encoding="utf-8")
    _finish(procs)
    assert int(counter.read_text(encoding="utf-8")) == procs_n * per, counter.read_text(encoding="utf-8")


def test_appends_during_drain_lose_and_tear_nothing(root: Path) -> None:
    """F13: concurrent writers append while a drainer repeatedly claims the pending file;
    every record arrives exactly once and whole."""
    writers, per = 5, 120
    src, go = root / pipeline.PENDING_NAME, root / "go"
    procs = [_spawn("append", src, per, f"w{i}", go) for i in range(writers)]
    time.sleep(0.5)
    go.write_text("", encoding="utf-8")
    lines: list[str] = []

    def claim() -> None:
        if src.exists():
            work = pipeline.claim_pending(src)
            lines.extend(work.read_text(encoding="utf-8").splitlines())
            work.unlink()
            pipeline.release_claim(work)

    while any(p.poll() is None for p in procs):
        claim()
        time.sleep(0.01)
    _finish(procs)
    claim()
    recs = [json.loads(line) for line in lines]  # a torn line fails here
    keys = [(r["tag"], r["i"]) for r in recs]
    assert len(keys) == len(set(keys)) == writers * per, (len(keys), len(set(keys)))


def test_same_process_is_not_reentrant(root: Path) -> None:
    lock = root / "r.lock"
    assert pipeline._try_lock(lock)
    try:
        assert not pipeline._try_lock(lock), "a second take in the holder succeeded"
    finally:
        pipeline._unlock(lock)
    assert pipeline._try_lock(lock)
    pipeline._unlock(lock)


def test_release_race_never_gives_two_holders(root: Path) -> None:
    """A contender that opened the lock file before a release, and locks it while the
    release unlinks, must not hold the lock alongside a newcomer that creates a new file."""
    lock = root / "race.lock"
    assert pipeline._try_lock(lock)
    fd_b = os.open(lock, os.O_RDWR | getattr(os, "O_BINARY", 0))  # B opened the old file
    b_holds: list[bool] = []
    real_unlink = Path.unlink

    def unlink_with_contender(self: Path, *args: object, **kwargs: object) -> None:
        if self == lock and not b_holds:  # B locks and re-checks inside the release's unlink
            b_holds.append(pipeline._os_lock(fd_b) and pipeline._same_file(os.fstat(fd_b), os.stat(lock)))
        real_unlink(self, *args, **kwargs)

    Path.unlink = unlink_with_contender  # type: ignore[method-assign]
    try:
        pipeline._unlock(lock)
    finally:
        Path.unlink = real_unlink  # type: ignore[method-assign]
    c_holds = pipeline._try_lock(lock)  # C arrives after the release
    try:
        assert b_holds, "the release never unlinked"
        assert not (b_holds[0] and c_holds), "B and C both hold the lock"
    finally:
        if c_holds:
            pipeline._unlock(lock)
        os.close(fd_b)


TESTS = [test_live_holder_is_never_stolen, test_dead_holder_lock_is_free_at_once, test_exactly_one_contender_wins,
         test_mutual_exclusion_under_churn, test_appends_during_drain_lose_and_tear_nothing,
         test_same_process_is_not_reentrant, test_release_race_never_gives_two_holders]


def main() -> int:
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-locks-test-") as tmp:
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

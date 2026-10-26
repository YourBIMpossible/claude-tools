#!/usr/bin/env python3
"""Worker process for test_locks.py: each mode exercises pipeline's locks from a real,
separate process, so the OS-level behaviour (not a thread simulation) is what is tested.

    hold    LOCK READY            take LOCK, create READY, sleep until killed
    once    LOCK GO OUT RELEASE   wait for GO, try LOCK once, write 1/0 to OUT, hold until RELEASE
    count   LOCK COUNTER N GO     N times: wait for LOCK, read-increment-write COUNTER, release
    append  FILE N TAG GO         N times: append_line one JSON record to FILE
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
import pipeline  # noqa: E402


def _wait_for(path: Path, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"never appeared: {path}")
        time.sleep(0.005)


def main(argv: list[str]) -> int:
    mode = argv[0]
    if mode == "hold":
        lock, ready = Path(argv[1]), Path(argv[2])
        if not pipeline._try_lock(lock):
            return 3
        ready.write_text("held", encoding="utf-8")
        while True:
            time.sleep(1)
    if mode == "once":
        lock, go, out, release = (Path(a) for a in argv[1:5])
        _wait_for(go)
        got = pipeline._try_lock(lock)
        out.write_text("1" if got else "0", encoding="utf-8")
        _wait_for(release)
        if got:
            pipeline._unlock(lock)
        return 0
    if mode == "count":
        lock, counter, n, go = Path(argv[1]), Path(argv[2]), int(argv[3]), Path(argv[4])
        _wait_for(go)
        for _ in range(n):
            if not pipeline._wait_lock(lock, 60.0):
                return 4
            try:
                value = int(counter.read_text(encoding="utf-8") or "0")
                time.sleep(0.001)  # widen the window a broken lock would lose an update in
                counter.write_text(str(value + 1), encoding="utf-8")
            finally:
                pipeline._unlock(lock)
        return 0
    if mode == "append":
        path, n, tag, go = Path(argv[1]), int(argv[2]), argv[3], Path(argv[4])
        _wait_for(go)
        for i in range(n):
            # padded so a torn or interleaved write would break the JSON
            pipeline.append_line(path, json.dumps({"tag": tag, "i": i, "pad": "x" * 512}) + "\n")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

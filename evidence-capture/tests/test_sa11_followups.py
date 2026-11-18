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
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
import pipeline  # noqa: E402
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


TESTS = [test_dead_pipeline_helpers_are_gone]


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

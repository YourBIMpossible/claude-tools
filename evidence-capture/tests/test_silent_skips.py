#!/usr/bin/env python3
"""Failures that used to vanish without a record (slop audits 2026-11-11 / 2026-11-16):
SA11-CT-16 hook import failures, SA11-CT-18 unreadable packets, SA16-CT-11 process-tree
kill failures, SA16-CT-13 an unevaluable cloud-attribute guard.

Run: python evidence-capture/tests/test_silent_skips.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))

PASSED: list[str] = []
FAILED: list[str] = []


def _run_hook_with_broken_pipeline(root: Path, script: str, tag: str) -> None:
    """The hook copied next to a ``pipeline`` that cannot be imported and no ``store``:
    exit 0, nothing printed, and one line naming the failure in the temp fallback log."""
    hook = root / "hook"
    tmp = root / "tmp"
    hook.mkdir()
    tmp.mkdir()
    shutil.copy2(PKG / script, hook / script)
    (hook / "pipeline.py").write_text('raise ImportError("broken on purpose")\n', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp))
    proc = subprocess.run([sys.executable, str(hook / script)], input=b"{}", capture_output=True,
                          cwd=str(root), env=env, timeout=60)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout == b"", proc.stdout
    log = tmp / "ec-capture-failures.log"
    assert log.is_file(), "the import failure left no log line"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    assert f"{tag}: import failed: ImportError" in lines[0] and "broken on purpose" in lines[0], lines


def test_start_hook_import_failure_is_logged(root: Path) -> None:
    """SA11-CT-16: capture_start fails open on an import failure and logs one line."""
    _run_hook_with_broken_pipeline(root, "capture_start.py", "start")


def test_end_hook_import_failure_is_logged(root: Path) -> None:
    """SA11-CT-16: capture_end fails open on an import failure and logs one line."""
    _run_hook_with_broken_pipeline(root, "capture_end.py", "end")


TESTS = [
    test_start_hook_import_failure_is_logged,
    test_end_hook_import_failure_is_logged,
]


def main() -> int:
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-silent-test-") as tmp:
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

#!/usr/bin/env python3
"""Failures that used to vanish without a record (slop audits 2026-11-11 / 2026-11-16):
SA11-CT-16 hook import failures, SA11-CT-18 unreadable packets, SA16-CT-11 process-tree
kill failures, SA16-CT-13 an unevaluable cloud-attribute guard.

Run: python evidence-capture/tests/test_silent_skips.py
"""
from __future__ import annotations

import json
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


SESSION = "11111111-2222-3333-4444-555555555555"
HEAD = "a" * 40


def _packet_dir_with_one_unreadable(root: Path) -> tuple[Path, Path]:
    """Two good packets (one this manifest's, one another prompt's) and one half-written."""
    import common

    pdir = root / "repo" / ".evidence-compiler" / "packets"
    pdir.mkdir(parents=True)
    for pid, prompt in (("ep_0001", "p"), ("ep_0002", "q")):
        (pdir / f"20260926_{pid}.json").write_text(json.dumps({
            "packet_id": pid, "created_at": "2026-09-26T10:00:01Z",
            "identity": {"session_id": SESSION, "head": HEAD, "head_state": None},
            "correlation": {"prompt_hash": common.sha256_text(prompt)}}), encoding="utf-8")
    bad = pdir / "20260926_ep_0003.json"
    bad.write_text('{"packet_id": "ep_0003", "created_at": "2026-09-2', encoding="utf-8")  # half-written
    return root / "repo", bad


def _manifest(prompt: str) -> dict:
    import common
    import manifest as mf

    sha = common.sha256_text(prompt)
    started = "2026-09-26T10:00:00.000000Z"
    m = mf.new_manifest(episode_id=common.episode_id(SESSION, sha, started), session_id=SESSION,
                        started_at=started, prompt_sha256=sha, prompt_len=len(prompt),
                        tool_commit=None, tool_sha256=None, cwd_sha256="0" * 64)
    m["ended_at"] = "2026-09-26T10:01:00Z"
    m["repo"]["head"] = HEAD
    return m


def _stores(root: Path):
    import store

    st = store.Stores(meta=root / "meta", content=root / "content")
    (st.meta / "manifests").mkdir(parents=True)
    return st


def test_unreadable_packet_is_counted_by_load_and_link(root: Path) -> None:
    """SA11-CT-18: load_packets keeps the two good packets and names the unreadable one;
    resolve_link records the count, and a link it could not make says why it may be wrong."""
    import linkage

    repo, bad = _packet_dir_with_one_unreadable(root)
    st = _stores(root)
    pk = linkage.load_packets(linkage.packet_dirs(repo, st))
    assert sorted(p["packet_id"] for p in pk) == ["ep_0001", "ep_0002"], pk
    assert list(pk.unreadable) == [str(bad)], pk.unreadable
    link = linkage.resolve_link(_manifest("p"), pk)
    assert (link["state"], link["packet_id"]) == ("linked", "ep_0001"), link
    assert link["packets_unreadable"] == 1, link
    none = linkage.resolve_link(_manifest("no such prompt"), pk)
    assert none["state"] == "none" and none["packets_unreadable"] == 1, none
    assert "1 packet file(s) unreadable" in none["reason"], none["reason"]
    clean = linkage.resolve_link(_manifest("p"), [p for p in pk])
    assert clean["packets_unreadable"] == 0, clean


def test_reconcile_counts_and_logs_unreadable_packets(root: Path) -> None:
    """SA11-CT-18: reconcile counts the unreadable packet files it saw and logs each one."""
    import linkage

    repo, bad = _packet_dir_with_one_unreadable(root)
    st = _stores(root)
    m = _manifest("p")
    counts = linkage.reconcile(st, [m], {m["episode_id"]: repo})
    assert counts["linked"] == 1, counts
    assert counts["packets_unreadable"] == 1, counts
    log = (st.meta / "capture.log").read_text(encoding="utf-8")
    assert f"packet unreadable: {bad.name}" in log, log


TESTS = [
    test_start_hook_import_failure_is_logged,
    test_end_hook_import_failure_is_logged,
    test_unreadable_packet_is_counted_by_load_and_link,
    test_reconcile_counts_and_logs_unreadable_packets,
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

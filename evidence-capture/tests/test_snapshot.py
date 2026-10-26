#!/usr/bin/env python3
"""Snapshot tests for checkouts git reports as directories or merge sides.

No framework, plain checks, synthetic fixtures only: temporary git repositories and
stores under one temporary directory, removed afterwards. No model call, no network, no
real repository, no real store.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
import common  # noqa: E402
import snapshot  # noqa: E402
import store  # noqa: E402
import test_capture as tc  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def git(repo: Path, *args: str) -> str:
    return tc.git(repo, *args)


def make_stores(root: Path, name: str = "st") -> store.Stores:
    st = store.Stores(root / f"{name}-meta", root / f"{name}-content")
    store.init_stores(st)
    store.init_meta_repo(st)
    return st


def status(repo: Path) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *snapshot.STATUS_ARGS], check=True, capture_output=True,
                          env=tc.GIT_ENV).stdout


def captured_bytes(d: Path) -> set[bytes]:
    return {p.read_bytes() for sub in ("untracked", "worktree", "blobs") if (d / sub).is_dir()
            for p in (d / sub).iterdir()}


def load(d: Path, name: str) -> list[dict]:
    return json.loads((d / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- tests

def test_nested_repo_completes_without_its_bytes(root: Path) -> None:
    """``--untracked-files=all`` reports a nested repository as ``? dir/``: a directory,
    not a file. The snapshot must complete and never read inside it."""
    repo, st = tc.make_repo(root), make_stores(root)
    inner = repo / "inner"
    inner.mkdir()
    git(inner, "init", "-q")
    (inner / "secret.txt").write_bytes(b"inside the nested repo\n")
    (repo / "notes.txt").write_bytes(b"plain untracked\n")
    assert b"? inner/\0" in status(repo), status(repo)
    _, snap = snapshot.take_snapshot(repo, "cap_nested", st, budget_s=60)
    assert snap["state"] == "complete", snap
    d = st.snapshots / "cap_nested"
    assert snap["untracked_count"] == 1 and [e["path"] for e in load(d, "untracked.json")] == ["notes.txt"]
    assert load(d, "nested.json") == [{"path": "inner/", "path_sha256": common.path_sha256("inner/"),
                                       "kind": "nested_repo"}]
    assert b"inside the nested repo\n" not in captured_bytes(d), "nested repository bytes are never read"
    assert not store.verify_sums(d) and snap["bracket"]["stable"] is True, snap["bracket"]
    assert not list(st.tmp.iterdir())


def test_dirty_submodule_completes_without_its_bytes(root: Path) -> None:
    """A submodule with untracked content is a ``1 .M S..U 160000 ...`` record whose path
    is a directory; it is listed in ``nested.json`` and not read."""
    sub = tc.make_repo(root, "sub")
    repo, st = tc.make_repo(root), make_stores(root)
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub.as_posix(), "sm")
    git(repo, "commit", "-q", "-m", "add submodule")
    (repo / "sm" / "dirty.txt").write_bytes(b"inside the submodule\n")
    (repo / "README.md").write_bytes(b"# edited\n")
    rec = [r for r in status(repo).split(b"\0") if r.endswith(b" sm")]
    assert rec and rec[0].startswith(b"1 .M S") and b" 160000 " in rec[0], rec
    _, snap = snapshot.take_snapshot(repo, "cap_sub", st, budget_s=60)
    assert snap["state"] == "complete", snap
    d = st.snapshots / "cap_sub"
    worktree = load(d, "worktree.json")
    assert [e["path"] for e in worktree] == ["README.md"] and not worktree[0].get("deleted"), worktree
    assert load(d, "nested.json") == [{"path": "sm", "path_sha256": common.path_sha256("sm"), "kind": "submodule"}]
    assert b"inside the submodule\n" not in captured_bytes(d), "submodule bytes are never read"
    assert b"# edited\n" in captured_bytes(d)
    assert not store.verify_sums(d) and snap["bracket"]["stable"] is True, snap["bracket"]


def _conflict(root: Path, name: str, *, delete_on: str) -> Path:
    """A merge where README.md is edited on one side and deleted on the other; the
    edited version is what git leaves in the worktree."""
    repo = tc.make_repo(root, name)
    git(repo, "checkout", "-q", "-b", "side")
    if delete_on == "side":
        git(repo, "rm", "-q", "README.md")
        git(repo, "commit", "-q", "-m", "side deletes")
    else:
        (repo / "README.md").write_bytes(b"# side edits\n")
        git(repo, "commit", "-q", "-am", "side edits")
    git(repo, "checkout", "-q", "main")
    if delete_on == "side":
        (repo / "README.md").write_bytes(b"# main edits\n")
        git(repo, "commit", "-q", "-am", "main edits")
    else:
        git(repo, "rm", "-q", "README.md")
        git(repo, "commit", "-q", "-m", "main deletes")
    merge = subprocess.run(["git", "-C", str(repo), "merge", "side"], capture_output=True, env=tc.GIT_ENV)
    assert merge.returncode != 0, "the merge must conflict"
    return repo


def test_unmerged_deleted_by_them_keeps_worktree_bytes(root: Path) -> None:
    """``u UD`` (deleted by them) and ``u DU`` (deleted by us) both leave the surviving
    side's file on disk; the snapshot carries those bytes instead of a deletion."""
    st = make_stores(root)
    for name, delete_on, expect in (("ud", "side", b"# main edits\n"), ("du", "main", b"# side edits\n")):
        repo = _conflict(root, name, delete_on=delete_on)
        xy = b"u UD " if delete_on == "side" else b"u DU "
        assert any(r.startswith(xy) and r.endswith(b" README.md") for r in status(repo).split(b"\0")), status(repo)
        assert (repo / "README.md").read_bytes() == expect
        _, snap = snapshot.take_snapshot(repo, f"cap_{name}", st, budget_s=60)
        assert snap["state"] == "complete", snap
        d = st.snapshots / f"cap_{name}"
        entries = load(d, "worktree.json")
        assert len(entries) == 1 and entries[0]["path"] == "README.md", entries
        assert not entries[0].get("deleted"), f"{name}: a file still on disk was recorded as deleted"
        assert (d / "worktree" / entries[0]["sha256"]).read_bytes() == expect
        assert not store.verify_sums(d) and snap["bracket"]["stable"] is True, snap["bracket"]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    only = sys.argv[1:]
    os.environ.update({k: v for k, v in tc.GIT_ENV.items() if k.startswith("GIT_")})
    with tempfile.TemporaryDirectory(prefix="ec-snapshot-test-") as tmp:
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

#!/usr/bin/env python3
"""Step-2 tests: start-only clone, proofs P1-P10, mutations, reconstruction, transport.

Plain checks, synthetic fixtures only (``fixture_repo.py``): temporary repositories and
temporary stores, no model, no network, no real repository. Everything lives under one
temporary directory removed afterwards.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
import clone_builder as cb  # noqa: E402
import isolation  # noqa: E402
import snapshot  # noqa: E402
import store  # noqa: E402
from fixture_repo import build_source, git  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
REPORT: dict[str, object] = {}
EPISODE = "cap_0123456789abcdef"

# Each sabotage and the exact proofs it must fail (plan §10 test_clone_proof_mutations).
EXPECTED_MUTATIONS = {
    "keep_tag": {"P2"},                        # a ref survives; no content beyond the start
    "keep_reflog": {"P3"},                     # reflog entries survive; they name only the start
    "add_alternates": {"P4", "P5", "P6"},      # the source store becomes readable through the clone
    "leave_remote": {"P2", "P7"},              # remote config and its remote-tracking refs survive
    "add_hook": {"P8"},
    "hardlink_pack": {"P4", "P5", "P6", "P10"},  # the source pack shared by hardlink
    "copy_post_start_blob": {"P5", "P6"},      # loose post-start blobs written into the clone
    "full_fetch": {"P1", "P2", "P5", "P6", "P10"},  # every ref fetched; tags survive remote removal
    "fetch_stash": {"P5", "P6", "P10"},        # stash objects in the pack; the ref goes with the remote
    "fetch_orphan": {"P5", "P6", "P10"},       # a reflog-only commit in the pack
}


def make_stores(root: Path) -> store.Stores:
    st = store.Stores(root / "meta", root / "content")
    store.init_stores(st)
    git(st.meta, "init", "-q")
    return st


def snap_at_start(root: Path, dirty: Callable[[Path], None] | None) -> Callable[[Path, str], None]:
    """An ``at_start`` callback: dirty the checkout, then take the start snapshot."""
    def at_start(repo: Path, _start: str) -> None:
        if dirty:
            dirty(repo)
        st = make_stores(root)
        _, snap = snapshot.take_snapshot(repo, EPISODE, st, budget_s=60)
        assert snap["state"] == "complete", snap
        at_start.snapshot = st.snapshots / EPISODE  # type: ignore[attr-defined]
    return at_start


def failing(record: dict) -> set[str]:
    return set(record["failed"])


def source_state(repo: Path) -> tuple[str, str, str]:
    return (git(repo, "status", "--porcelain=v2", "--untracked-files=all"), git(repo, "for-each-ref"),
            git(repo, "reflog", "show", "--all"))


def dirty_mixed(repo: Path) -> None:
    (repo / "README.md").write_bytes(b"# fixture\nstaged line\n")
    git(repo, "add", "README.md")
    (repo / "notes.txt").write_bytes(b"one\ntwo changed\nthree\n")
    (repo / "scratch" / "deep").mkdir(parents=True)
    (repo / "scratch" / "deep" / "u.txt").write_bytes(b"untracked\n")
    (repo / "build.log").write_bytes(b"ignored\n")


# ------------------------------------------------------------------------ tests

def test_clone_proofs_P1_P10(root: Path) -> None:
    at = snap_at_start(root, dirty_mixed)
    src, start, sentinels = build_source(root, at)
    assert len(git(src, "rev-list", f"{start}..main").split()) >= 79
    before = source_state(src)
    t0 = time.perf_counter()
    rec = cb.clone_start_only(src, start, root / "replay" / "repo", at.snapshot, sentinels)
    REPORT["clone_proofs_seconds"] = round(time.perf_counter() - t0, 1)
    assert rec["error"] is None, rec["error"]
    assert rec["failed"] == [], json.dumps({k: rec["proofs"][k] for k in rec["failed"]}, indent=1)
    assert rec["start_state_reconstructed"] is True
    assert rec["proofs"]["P5"]["checked"] > 79 and not rec["proofs"]["P5"]["vacuous"]
    assert rec["proofs"]["P6"]["sentinels"] == 6
    assert source_state(src) == before, "the source must only be read"
    assert not (root / "replay" / "repo.objects.tmp").exists()
    for c in git(src, "rev-list", f"{start}..main").split()[:5]:
        assert not cb._run(root / "replay" / "repo", "cat-file", "-e", c, check=False).returncode == 0
    REPORT["P7_machine_level_recorded"] = len(rec["proofs"]["P7"]["machine_level_recorded"])


def test_clone_proof_mutations(root: Path) -> None:
    src, start, sentinels = build_source(root)
    observed: dict[str, list[str]] = {}
    for i, sab in enumerate(sorted(EXPECTED_MUTATIONS)):
        dest = root / f"m{i}" / "repo"
        cb.build_clone(src, start, dest, sabotage=[sab])
        proofs = cb.prove_clone(src, start, dest, sentinels)
        observed[sab] = sorted(k for k, v in proofs.items() if not v["passed"])
    REPORT["mutations"] = observed
    wrong = {k: (observed[k], sorted(v)) for k, v in EXPECTED_MUTATIONS.items() if set(observed[k]) != v}
    assert not wrong, f"observed vs expected: {wrong}"
    caught = set().union(*EXPECTED_MUTATIONS.values())
    assert caught == {"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P10"}, "every proof must be shown to fail"
    # the clean build on the same fixture passes everything
    clean = cb.prove_clone(src, start, (lambda d: (cb.build_clone(src, start, d), d)[1])(root / "clean" / "repo"),
                           sentinels)
    assert all(v["passed"] for v in clean.values()), clean


def _case(root: Path, name: str, dirty: Callable[[Path], None], *, symlink: bool = False) -> dict:
    case_root = root / name
    at = snap_at_start(case_root, dirty)
    src, start, sentinels = build_source(case_root, at, post_commits=3, symlink=symlink)
    return cb.clone_start_only(src, start, case_root / "replay" / "repo", at.snapshot, sentinels)


def _staged_only(repo: Path) -> None:
    (repo / "pre.txt").write_bytes(b"pre-start content\nstaged edit\n")
    (repo / "added.txt").write_bytes(b"new staged file\n")
    git(repo, "add", "pre.txt", "added.txt")


def _unstaged_only(repo: Path) -> None:
    (repo / "pre.txt").write_bytes(b"pre-start content\nunstaged edit\n")
    (repo / "notes.txt").unlink()


def _partial_hunks(repo: Path) -> None:
    p = repo / "src" / "alpha.py"
    p.write_bytes(b"def alpha(x):\n    return x + 100\n\n\ndef beta(y):\n    return y * 2\n")
    git(repo, "add", "src/alpha.py")
    p.write_bytes(b"def alpha(x):\n    return x + 100\n\n\ndef beta(y):\n    return y * 200\n")


def _staged_rename(repo: Path) -> None:
    git(repo, "mv", "notes.txt", "renamed-notes.txt")


def _conflict(repo: Path) -> None:
    """Three-stage unmerged entry built directly in the index; no operation in progress."""
    base = git(repo, "rev-parse", "HEAD:notes.txt").strip()
    ours = git(repo, "hash-object", "-w", "--stdin", input_=b"one\nours\nthree\n").strip()
    theirs = git(repo, "hash-object", "-w", "--stdin", input_=b"one\ntheirs\nthree\n").strip()
    info = (f"0 {'0' * 40}\tnotes.txt\n100644 {base} 1\tnotes.txt\n"
            f"100644 {ours} 2\tnotes.txt\n100644 {theirs} 3\tnotes.txt\n")
    git(repo, "update-index", "--index-info", input_=info.encode())
    (repo / "notes.txt").write_bytes(b"one\n<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\nthree\n")


def _ignored_present(repo: Path) -> None:
    (repo / "build.log").write_bytes(b"ignored bytes are never captured\n")
    (repo / "src" / "trace.log").write_bytes(b"also ignored\n")


def _nested_untracked(repo: Path) -> None:
    (repo / "a" / "b" / "c").mkdir(parents=True)
    (repo / "a" / "b" / "c" / "deep.txt").write_bytes(b"deep\n")
    (repo / "a" / "top.txt").write_bytes(b"top\n")


def _symlink_modified(repo: Path) -> None:
    (repo / "README.md").write_bytes(b"# fixture\nedited next to a symlink entry\n")


def _operation_in_progress(repo: Path) -> None:
    (repo / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"), encoding="utf-8")
    (repo / "pre.txt").write_bytes(b"pre-start content\nmid-merge edit\n")


def _symlinks_true(repo: Path) -> None:
    git(repo, "config", "core.symlinks", "true")


def test_index_reconstruction_cases(root: Path) -> None:
    cases = {
        "staged_only": (_staged_only, False), "unstaged_only": (_unstaged_only, False),
        "partial_hunks": (_partial_hunks, False), "staged_rename": (_staged_rename, False),
        "conflict_three_stages": (_conflict, False), "ignored_present": (_ignored_present, False),
        "nested_untracked": (_nested_untracked, False), "symlink_core_false": (_symlink_modified, True),
    }
    results = {}
    for name, (dirty, symlink) in cases.items():
        rec = _case(root, name, dirty, symlink=symlink)
        results[name] = {"failed": rec["failed"], "index": (rec["reconstruction"] or {}).get("index_install"),
                         "p9": {k: v for k, v in rec["proofs"].get("P9", {}).items() if k != "compared"}}
    REPORT["reconstruction_cases"] = results
    bad = {k: v for k, v in results.items() if v["failed"]}
    assert not bad, json.dumps(bad, indent=1)

    # conflict: three stages really present, and reproduced
    rec = _case(root, "conflict_check", _conflict)
    stages = [ln for ln in git(root / "conflict_check" / "replay" / "repo", "ls-files", "-u").splitlines()]
    assert len(stages) == 3 and rec["start_state_reconstructed"], stages

    # an operation in progress cannot be reproduced from the start's ancestry: fail closed
    rec = _case(root, "operation", _operation_in_progress)
    assert rec["proofs"]["P9"]["passed"] is False and rec["proofs"]["P9"]["reason"] == "operation_in_progress"
    assert rec["start_state_reconstructed"] is False

    # core.symlinks=true: reproduced where the machine can create symlinks, fails closed where it cannot
    def symlinks_true(repo: Path) -> None:
        _symlinks_true(repo)

    case_root = root / "symlink_true"
    at = snap_at_start(case_root, symlinks_true)

    def at_start(repo: Path, start: str) -> None:
        at(repo, start)
        git(repo, "config", "core.symlinks", "false")  # the fixture's own post-start checkout
    src, start, sentinels = build_source(case_root, at_start, post_commits=3, symlink=True)
    rec = cb.clone_start_only(src, start, case_root / "replay" / "repo", at.snapshot, sentinels)
    can = _can_symlink(root)
    REPORT["symlink_privilege"] = can
    REPORT["symlink_true_outcome"] = {"failed": rec["failed"], "p9": rec["proofs"]["P9"]}
    if can:
        assert rec["start_state_reconstructed"], rec["proofs"]["P9"]
    else:
        assert rec["proofs"]["P9"]["passed"] is False
        assert rec["proofs"]["P9"].get("reason") == "symlink_unsupported", rec["proofs"]["P9"]
        assert rec["start_state_reconstructed"] is False


def test_p9_detects_divergence(root: Path) -> None:
    """P9 is not a rubber stamp: a changed clone or a tampered snapshot fails it."""
    rec = _case(root, "base", dirty_mixed)
    dest, snap = root / "base" / "replay" / "repo", root / "base" / "content" / "snapshots" / EPISODE
    assert rec["start_state_reconstructed"], rec["failed"]
    assert cb.p9(dest, snap)["passed"]
    checks = {}
    (dest / "notes.txt").write_bytes(b"one\ntwo changed again\nthree\n")       # unstaged edit diverges
    checks["worktree_edit"] = cb.p9(dest, snap)
    (dest / "notes.txt").write_bytes(b"one\ntwo changed\nthree\n")
    assert cb.p9(dest, snap)["passed"], "restoring the bytes must restore equality"
    git(dest, "reset", "-q", "README.md")                                         # staged change dropped
    checks["index_unstaged"] = cb.p9(dest, snap)
    git(dest, "add", "README.md")
    (dest / "scratch" / "deep" / "u.txt").write_bytes(b"other\n")               # untracked bytes differ
    checks["untracked_bytes"] = cb.p9(dest, snap)
    (dest / "scratch" / "deep" / "u.txt").write_bytes(b"untracked\n")
    (dest / "extra.txt").write_bytes(b"x\n")                                     # extra untracked file
    checks["extra_untracked"] = cb.p9(dest, snap)
    (dest / "extra.txt").unlink()
    assert cb.p9(dest, snap)["passed"]
    REPORT["p9_mutations"] = {k: v["passed"] for k, v in checks.items()}
    assert not any(v["passed"] for v in checks.values()), checks

    # a tampered snapshot never reconstructs
    bad = root / "tampered"
    shutil.copytree(snap, bad)
    wt = bad / "worktree.patch"
    wt.write_bytes(wt.read_bytes() + b"\n")
    rec = cb.clone_start_only(root / "base" / "source", rec["start_sha"], root / "t" / "repo", bad)
    assert rec["proofs"]["P9"].get("reason") == "snapshot_unverified", rec["proofs"]["P9"]
    assert rec["start_state_reconstructed"] is False


def _current_sid() -> str:
    sid = isolation.identity()["sid"]
    assert sid and sid.startswith("S-1-"), sid
    return sid


# Data rights only. A full-control deny (F) also denies READ_CONTROL and WRITE_DAC, and
# Python's 0o700 temporary directories carry an OWNER RIGHTS ACE that cancels the owner's
# implicit right to rewrite the DACL: the deny could then never be removed without an
# administrator. Keeping RC/WDAC/WO allowed leaves the test able to undo itself.
DENY_RIGHTS = "(OI)(CI)(RD,WD,AD,REA,WEA,X,DC,RA,WA,D)"


@contextlib.contextmanager
def _deny(paths: list[Path], sid: str):
    """Deny the current identity data access on ``paths`` (inherited), removed on exit."""
    applied: list[Path] = []
    try:
        for p in paths:
            subprocess.run(["icacls", str(p), "/deny", f"*{sid}:{DENY_RIGHTS}"], check=True, capture_output=True,
                           timeout=60)
            applied.append(p)
        yield
    finally:
        stuck = [str(p) for p in reversed(applied)
                 if subprocess.run(["icacls", str(p), "/remove:d", f"*{sid}"], capture_output=True,
                                   timeout=60).returncode != 0]
        if stuck:
            raise RuntimeError(f"deny ACE could not be removed from {stuck}")


def test_fs_isolation_deterministic(root: Path) -> None:
    """Plan §8: a plain process under the replay identity cannot reach the protected paths."""
    if os.name != "nt":
        raise AssertionError("the pinned environment is Windows; this test uses icacls")
    sid = _current_sid()
    src = root / "repos" / "source"
    src.mkdir(parents=True)
    git(src, "init", "-q", "-b", "main")
    (src / "src").mkdir()
    (src / "src" / "a.py").write_bytes(b"x = 1\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "one")
    git(src, "worktree", "add", "-q", str(root / "repos" / "sibling"), "-b", "lane")
    st = make_stores(root / "stores")
    (st.meta / "episodes").mkdir(exist_ok=True)
    (st.meta / "episodes" / "e.json").write_bytes(b"{}\n")
    git(st.meta, "add", "-A")
    git(st.meta, "commit", "-q", "-m", "meta")
    (st.content / "SENTINEL").write_bytes(b"content-store sentinel\n")
    tmp = st.content / "tmp"
    tmp.mkdir(exist_ok=True)
    targets = isolation.standard_targets(
        source_file=src / "src" / "a.py", source_git_objects=src / ".git" / "objects", meta_repo=st.meta,
        content_sentinel=st.content / "SENTINEL", content_tmp=tmp, sibling_worktree=root / "repos" / "sibling")
    watch = [src, root / "repos" / "sibling", st.meta, st.content]
    repos = [src, root / "repos" / "sibling", st.meta]

    # with access intact the check must fail: every attempt succeeds
    open_rec = isolation.check_isolation(targets, watch=watch, repos=repos)
    assert open_rec["passed"] is False
    assert {a["outcome"] for a in open_rec["attempts"]} == {"succeeded"}, open_rec["attempts"]
    assert not list(tmp.iterdir()), "the write probe must remove what it created"

    # a missing target proves nothing and fails
    ghost = [dict(targets[0], path=str(src / "nope.py"))]
    assert isolation.check_isolation(ghost, watch=watch, repos=repos)["failures"] == ["source_file:missing"]

    # deny the identity on the protected roots: every attempt is an access error, state unchanged
    roots = [src, root / "repos" / "sibling", st.meta, st.content]
    rec = isolation.check_isolation(targets, watch=watch, repos=repos, acl_window=_deny(roots, sid))
    REPORT["fs_isolation"] = {"identity_sid_matches": rec["identity"]["sid"] == sid,
                              "attempts": {a["name"]: a["outcome"] for a in rec["attempts"]},
                              "probe_rc": rec["probe_rc"], "state_changed": rec["state_changed"]}
    assert rec["passed"], json.dumps({k: rec[k] for k in ("failures", "attempts", "state_changed")}, indent=1)
    assert rec["identity"]["sid"] == sid
    assert all("DENY" in d.upper() for d in rec["icacls"].values()), rec["icacls"]
    # the deny ACE is gone afterwards
    assert (src / "src" / "a.py").read_bytes() == b"x = 1\n"
    assert os.listdir(st.meta)


def _can_symlink(root: Path) -> bool:
    target = root / "symlink-probe-target"
    target.write_bytes(b"x")
    try:
        os.symlink(target, root / "symlink-probe")
        return True
    except OSError:
        return False


def test_git_transport_pinned_env(root: Path) -> None:
    at = snap_at_start(root, dirty_mixed)
    src, start, sentinels = build_source(root, at, post_commits=3)
    dest = root / "replay" / "repo"
    rec = cb.clone_start_only(src, start, dest, at.snapshot, sentinels)
    assert rec["start_state_reconstructed"], rec["failed"]
    version = rec["git_version"]
    assert version.startswith("git version "), version
    if os.name == "nt":
        assert ".windows." in version, version
    packs = list((dest / ".git" / "objects" / "pack").glob("*.pack"))
    assert packs, "--no-local must produce a real pack"
    links = [p for p in (dest / ".git" / "objects").rglob("*") if p.is_file() and os.stat(p).st_nlink != 1]
    assert not links, links
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    counts = dict(ln.split(": ", 1) for ln in git(dest, "count-objects", "-v").splitlines())
    assert "alternate" not in counts and int(counts["in-pack"]) > 0, counts
    recorded = json.loads((at.snapshot / "git-config.json").read_text(encoding="utf-8"))
    assert rec["core_config"] == {k: recorded[k] for k in ("core.autocrlf", "core.symlinks")}
    assert rec["core_config_effective"] == rec["core_config"], (rec["core_config_effective"], rec["core_config"])
    assert recorded["git_version"] == version
    pack_ino = {os.stat(p).st_ino for p in packs}
    src_ino = {os.stat(p).st_ino for p in (src / ".git" / "objects" / "pack").glob("*.pack")}
    assert not (pack_ino & src_ino), "clone pack shares an inode with the source"
    REPORT["transport"] = {"git_version": version, "core_config": rec["core_config"], "packs": len(packs)}


# ------------------------------------------------------------------------ runner

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-clone-test-") as tmp:
        for fn in TESTS:
            if only and fn.__name__ not in only:
                continue
            case = Path(tmp) / fn.__name__
            case.mkdir()
            t0 = time.perf_counter()
            try:
                fn(case)
                PASSED.append(f"{fn.__name__} ({time.perf_counter() - t0:.1f}s)")
            except Exception:  # noqa: BLE001 — plain-check runner
                FAILED.append(fn.__name__)
                print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    for name in PASSED:
        print(f"ok   {name}")
    if REPORT:
        print("report", json.dumps(REPORT, sort_keys=True, default=str))
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

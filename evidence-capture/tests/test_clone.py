#!/usr/bin/env python3
"""Step-2 tests: start-only clone, proofs P1-P10, mutations, reconstruction, transport.

Plain checks, synthetic fixtures only (``fixture_repo.py``): temporary repositories and
temporary stores, no model, no network, no real repository. Everything lives under one
temporary directory removed afterwards.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import contextlib
import io
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
SKIPPED: list[str] = []


class SkipTest(Exception):
    """A test whose precondition is a platform the mechanism under test does not exist on.
    Reported by name with its reason; never counted as a pass."""
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


def test_reconstruct_entries_stay_in_the_clone(root: Path) -> None:
    """F5: snapshot entry paths are data. Traversal, absolute/drive/UNC/extended forms,
    ``.git``, a non-hex blob name and a write through an earlier symlink are refused before
    any byte lands, whatever SHA256SUMS says."""
    dest, src, outside = root / "clone", root / "src", root / "outside"
    for d in (dest, src, outside):
        d.mkdir()
    blob = b"payload\n"
    sha = cb.sha256_bytes(blob)
    (src / sha).write_bytes(blob)
    bad_paths = ["../escape.txt", "a/../../escape.txt", "/abs.txt", "C:/abs.txt", "C:rel.txt",
                 "//server/share/x.txt", "\\\\?\\C:\\x.txt", "a\\..\\..\\x.txt", ".git/config",
                 ".GIT/hooks/pre-commit", ".git./hooks/post-checkout", ".git /config",
                 "a//b.txt", "./a.txt", "", None]
    if os.name == "nt":  # Win32 would write these somewhere other than their name
        bad_paths += ["dir./x.txt", "a.txt:stream", "sub /x.txt"]
    for rel in bad_paths:
        try:
            cb._write_entry(dest, {"path": rel, "sha256": sha, "path_sha256": "0" * 64}, src, False)
        except cb.ReconstructionError as exc:
            assert exc.reason == "unsafe_entry_path", (rel, exc.reason)
        else:
            raise AssertionError(f"accepted {rel!r}")
    try:
        cb._write_entry(dest, {"path": "ok.txt", "sha256": "../" + sha, "path_sha256": "0" * 64}, src, False)
        raise AssertionError("a traversing blob name was read")
    except cb.ReconstructionError as exc:
        assert exc.reason == "unsafe_entry_path"
    cb._write_entry(dest, {"path": "sub/ok.txt", "sha256": sha, "path_sha256": "0" * 64}, src, False)
    assert (dest / "sub" / "ok.txt").read_bytes() == blob
    kind = "symlink"
    try:
        os.symlink(outside, dest / "link", target_is_directory=True)
    except OSError:
        kind = None
        if os.name == "nt":  # no symlink privilege: a junction is the same escape
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(dest / "link"), str(outside)],
                                  capture_output=True)
            kind = "junction" if made.returncode == 0 else None
    if kind is None:
        REPORT["symlink_escape"] = "unverified: this machine can create neither a symlink nor a junction"
    else:
        try:
            cb._write_entry(dest, {"path": "link/escape.txt", "sha256": sha, "path_sha256": "0" * 64}, src, False)
            raise AssertionError("wrote through an earlier symlink")
        except cb.ReconstructionError as exc:
            assert exc.reason == "unsafe_entry_path"
        REPORT["symlink_escape"] = f"refused ({kind})"
    assert not any(outside.iterdir()) and not (root / "escape.txt").exists()


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
        raise SkipTest("isolation is enforced with icacls deny ACEs (isolation.icacls); there is no "
                       "POSIX enforcement to test, so this check exists only on Windows")
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
    targets = [  # the six attempts plan §8 names, in its order
        {"name": "source_file", "kind": "read", "path": str(src / "src" / "a.py")},
        {"name": "metadata_repo", "kind": "list", "path": str(st.meta)},
        {"name": "content_sentinel", "kind": "read", "path": str(st.content / "SENTINEL")},
        {"name": "content_tmp", "kind": "write", "path": str(tmp)},
        {"name": "source_git_objects", "kind": "open_dir", "path": str(src / ".git" / "objects")},
        {"name": "sibling_worktree", "kind": "resolve", "path": str(root / "repos" / "sibling")},
    ]
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


def test_isolation_cli_unobserved_state_fails(root: Path) -> None:
    """Slop audit M7: an empty watch set is an error, and a store or repository the check
    cannot read (missing path, git failure) fails it instead of comparing equal to itself."""
    src = root / "source"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.py").write_bytes(b"x = 1\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "one")
    plain = root / "not-a-repo"
    plain.mkdir()
    spec = root / "targets.json"
    spec.write_text(json.dumps([{"name": "source_file", "kind": "read", "path": str(src / "a.py")}]),
                    encoding="utf-8")
    out = root / "record.json"
    base = ["--targets", str(spec), "--out", str(out)]

    for extra in ([], ["--watch", str(src)], ["--repo", str(src)]):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                isolation.main(base + extra)
                raise AssertionError(f"an empty watch set was accepted: {extra}")
            except SystemExit as exc:
                assert exc.code == 2, exc.code
        assert "nothing to check" in err.getvalue(), err.getvalue()

    def run(*extra: str) -> tuple[int, dict]:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = isolation.main(base + list(extra))
        return rc, json.loads(out.read_text(encoding="utf-8"))

    # readable watch and repository: observed on both sides (the target read still fails the check)
    rc, rec = run("--watch", str(src), "--repo", str(src))
    assert rc == 1 and rec["state_unreadable"] == [] and rec["failures"] == ["source_file:succeeded"], rec["failures"]
    # a mistyped store path is unobserved, not unchanged
    rc, rec = run("--watch", str(root / "nope"), "--repo", str(src))
    assert rc == 1 and rec["passed"] is False
    assert [f.split(":", 2)[:2] for f in rec["state_unreadable"]] == [["before", "walk"], ["after", "walk"]], rec
    assert set(rec["state_unreadable"]) <= set(rec["failures"])
    # git status failing on a repository is unobserved, not unchanged
    rc, rec = run("--watch", str(src), "--repo", str(plain))
    assert rc == 1 and rec["passed"] is False
    assert [f.split(":", 2)[:2] for f in rec["state_unreadable"]] == [["before", "status"], ["after", "status"]], rec
    assert "rc=128" in rec["state_unreadable"][0], rec["state_unreadable"]


# (git subcommand words, the proof whose command it is): each proof command, made to fail
INJECTED_GIT_FAILURES = {
    ("stash", "list"): "P2", ("symbolic-ref", "-q"): "P2", ("reflog", "show"): "P3",
    ("config", "--local", "--list"): "P4", ("rev-parse", "-q", "--verify"): "P5",
    ("grep", "-c"): "P6", ("log", "--all", "--reflog", "--format=%H"): "P6",
    ("config", "--list", "--show-origin"): "P7",
}


@contextlib.contextmanager
def _git_fails(words: tuple[str, ...]):
    """Every git call whose arguments after ``-C <dir>`` start with ``words`` exits 128 with no output."""
    real = subprocess.run

    def fake(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(cmd, list) and cmd[:1] == ["git"]:
            rest = cmd[3:] if cmd[1:2] == ["-C"] else cmd[1:]
            if tuple(rest[:len(words)]) == words:
                return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: injected failure")
        return real(cmd, *args, **kwargs)

    subprocess.run = fake  # type: ignore[assignment]
    try:
        yield
    finally:
        subprocess.run = real  # type: ignore[assignment]


def test_clone_proof_git_failure_fails(root: Path) -> None:
    """Slop audit L9: a proof whose git command fails (empty output) must not read as clean."""
    at = snap_at_start(root, dirty_mixed)
    src, start, sentinels = build_source(root, at, post_commits=3)
    observed = {}
    for i, (words, proof) in enumerate(sorted(INJECTED_GIT_FAILURES.items())):
        with _git_fails(words):
            rec = cb.clone_start_only(src, start, root / f"f{i}" / "repo", at.snapshot, sentinels)
        observed[" ".join(words)] = (rec["start_state_reconstructed"], rec["error"] or "")
        assert rec["start_state_reconstructed"] is False, (words, proof)
        assert "CloneError" in (rec["error"] or "") and "rc=128" in rec["error"], (words, rec["error"])
        assert proof in rec["failed"], (words, rec["failed"])
    clean = cb.clone_start_only(src, start, root / "clean" / "repo", at.snapshot, sentinels)
    assert clean["start_state_reconstructed"] is True, (clean["failed"], clean["error"])


def _can_symlink(root: Path) -> bool:
    target = root / "symlink-probe-target"
    target.write_bytes(b"x")
    try:
        os.symlink(target, root / "symlink-probe")
        return True
    except OSError:
        return False


def test_core_config_values_are_data(root: Path) -> None:
    """N1: ``git-config.json`` is snapshot data. A value that is not a config value (not a
    string, or not a bare boolean/``input`` word) never becomes a ``git config`` argument:
    ``build_clone`` refuses it and ``clone_start_only`` records the refusal instead of
    raising out of the run."""
    src, start, _ = build_source(root, None, post_commits=1)
    bad_values: list[object] = [5, ["true"], {"v": "true"}, "--unset-all", "-f", "true\n[core]\n\thooksPath = x",
                                "", "true false", "x" * 17]
    for i, value in enumerate(bad_values):
        dest = root / f"c{i}" / "repo"
        try:
            cb.build_clone(src, start, dest, core_config={"core.autocrlf": value})  # type: ignore[dict-item]
        except cb.CloneError as exc:
            assert "core.autocrlf" in str(exc), (value, exc)
        else:
            raise AssertionError(f"build_clone accepted {value!r}")
        assert not dest.exists(), f"a refused value left a clone for {value!r}"
    try:
        cb.build_clone(src, start, root / "k" / "repo", core_config={"remote.origin.url": "x"})
    except cb.CloneError:
        pass
    else:
        raise AssertionError("build_clone accepted a key outside core.autocrlf/core.symlinks")
    # every accepted value is in the clone, including a dash-free one git would also take
    rec = cb.build_clone(src, start, root / "ok" / "repo", core_config={"core.autocrlf": "input", "core.symlinks": None})
    assert any(c["cmd"][-2:] == ["core.autocrlf", "input"] for c in rec), rec
    for i, value in enumerate([["true"], "--unset-all"]):
        snap = root / f"s{i}"
        snap.mkdir()
        (snap / "git-config.json").write_text(json.dumps({"core.autocrlf": value, "core.symlinks": "false"}),
                                              encoding="utf-8")
        rec = cb.clone_start_only(src, start, root / f"r{i}" / "repo", snap, [])
        assert rec["start_state_reconstructed"] is False
        assert rec["error"] and rec["error"].startswith("CloneError") and "core.autocrlf" in rec["error"], rec["error"]
        assert not (root / f"r{i}" / "repo").exists(), "the refusal came before any git ran"
    bad = root / "s_shape"
    bad.mkdir()
    (bad / "git-config.json").write_text("[]", encoding="utf-8")
    rec = cb.clone_start_only(src, start, root / "r_shape" / "repo", bad, [])
    assert rec["error"] and rec["error"].startswith("CloneError"), rec["error"]


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
            except SkipTest as exc:
                SKIPPED.append(fn.__name__)
                print(f"SKIP {fn.__name__}: {exc}")
            except Exception:  # noqa: BLE001 — plain-check runner
                FAILED.append(fn.__name__)
                print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    for name in PASSED:
        print(f"ok   {name}")
    if REPORT:
        print("report", json.dumps(REPORT, sort_keys=True, default=str))
    print(f"{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

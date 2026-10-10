#!/usr/bin/env python3
"""Starting-state snapshot with a concurrent-change bracket (plan §2a, §3).

The snapshot records the checkout exactly as it is when the prompt is submitted:

* ``status.v2z``          ``git status --porcelain=v2 -z --untracked-files=all --ignored --no-renames``
* ``index.patch``         ``git diff-index --cached HEAD -p --binary --full-index --no-renames``
* ``worktree.patch``      ``git diff-files -p --binary --full-index --no-renames`` (unstaged vs index)
* ``index.bin``           a byte copy of ``.git/index`` taken inside the bracket
* ``ls-files-stage.z``    ``git ls-files --stage -z`` (unmerged entries appear as stages 1-3)
* ``ls-tree-head.z``      ``git ls-tree -r -z --full-tree HEAD``
* ``blobs/<oid>``         bytes of every index blob (any stage) not in HEAD's tree: staged content
                          and conflict stages exist only in the source object store
* ``worktree/<sha>``      bytes of tracked paths whose worktree differs from the index;
                          ``worktree.json`` lists them (and worktree deletions)
* ``state/*``             ``MERGE_HEAD``, ``REBASE_HEAD``, ``CHERRY_PICK_HEAD`` ... when present
* ``untracked/<sha>``     every untracked, unignored file as bytes; ``untracked.json`` holds
                          path, mode, size, sha256 and symlink targets
* ``nested.json``         paths of nested repositories (untracked ``dir/`` entries) and submodules
                          whose worktree differs; their contents are never read, so the
                          snapshot completes and the clone's P9 status comparison reports them
* ``ignored.json``        path hashes of ignored files (never their bytes)
* ``git-config.json``     ``core.symlinks``, ``core.autocrlf``, ``core.filemode``, git version
* ``SHA256SUMS``          over everything above; ``INCOMPLETE`` is removed only after every
                          file verified back

Bracket: before and after the bytes are read, ``status``, ``HEAD``, the index file's
sha256 and mtime, and the presence of any ``*.lock`` in the git dir are compared. A
second pass recomputes every captured artifact and compares bytes. Any difference means
``failed`` with reason ``concurrent_change``.

Only commands that never write the index are used, under ``GIT_OPTIONAL_LOCKS=0``. The
porcelain ``git diff`` (worktree against index) rewrites ``.git/index`` when the stat
cache is stale even with optional locks off; the plumbing ``diff-files`` does not. That
is measured by ``test_snapshot_stability_and_index_untouched`` and must stay true.

Each git spawn costs 50-300 ms on Windows, so independent read-only calls within one
phase run concurrently; the phases themselves stay ordered (pre bracket, artifacts, post
bracket, artifacts again).

A failed snapshot is final (plan rule S2): nothing here, and nothing elsewhere, rebuilds
it from later state.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from common import git_env, norm_path, path_sha256, sha256_bytes, sha256_file
from store import Stores, new_tmp_dir, rmtree, verify_sums, write_sums

SIZE_CAP_BYTES = 50 * 1024 * 1024
START_BUDGET_S = 2.0
STATE_FILES = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "MERGE_MSG")
FAIL_REASONS = ("timeout", "size_cap", "write_error", "head_unresolved", "concurrent_change", "not_a_repo")
STATUS_ARGS = ("status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored", "--no-renames")
INDEX_PATCH_ARGS = ("diff-index", "--cached", "HEAD", "-p", "--binary", "--full-index", "--no-renames")
WORKTREE_PATCH_ARGS = ("diff-files", "-p", "--binary", "--full-index", "--no-renames")
LS_STAGE_ARGS = ("ls-files", "--stage", "-z")
LS_TREE_ARGS = ("ls-tree", "-r", "-z", "--full-tree", "HEAD")
IDENTITY_ARGS = ("rev-parse", "--absolute-git-dir", "--git-common-dir", "--show-toplevel", "--abbrev-ref", "HEAD")
HEAD_ARGS = ("rev-parse", "--verify", "-q", "HEAD")
CONFIG_ARGS = ("config", "--get-regexp", r"^core\.(symlinks|autocrlf|filemode)$")
CONFIG_KEYS = ("core.symlinks", "core.autocrlf", "core.filemode")
_GIT_VERSION: str | None = None


class SnapshotFailed(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class Deadline:
    def __init__(self, budget_s: float) -> None:
        self.t0 = time.monotonic()
        self.budget = budget_s

    def check(self, stage: str) -> None:
        if time.monotonic() - self.t0 > self.budget:
            raise SnapshotFailed("timeout", stage)

    def remaining(self) -> float:
        return max(0.05, self.budget - (time.monotonic() - self.t0))

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)


def run_git_parallel(repo: Path, calls: dict[str, tuple[str, ...]], timeout: float,
                     allow_fail: tuple[str, ...] = ()) -> dict[str, bytes]:
    """Run independent read-only git calls concurrently; stdout bytes by key.

    A non-zero exit raises ``RuntimeError`` unless the key is in ``allow_fail`` (its value
    is then ``b""``). Past ``timeout`` every call is killed and ``TimeoutExpired`` raised.
    """
    env = git_env()
    procs = {k: subprocess.Popen(["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env) for k, args in calls.items()}
    deadline = time.monotonic() + timeout
    out: dict[str, bytes] = {}
    try:
        for k, proc in procs.items():
            stdout, stderr = proc.communicate(timeout=max(0.01, deadline - time.monotonic()))
            if proc.returncode != 0 and k not in allow_fail:
                raise RuntimeError(f"git {calls[k][0]} failed: {stderr.decode('utf-8', 'replace')[:200]}")
            out[k] = stdout if proc.returncode == 0 else b""
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    return out


def git_version() -> str:
    """``git --version``, once per process."""
    global _GIT_VERSION
    if _GIT_VERSION is None:
        _GIT_VERSION = subprocess.run(["git", "--version"], capture_output=True, text=True,
                                      timeout=10).stdout.strip()
    return _GIT_VERSION


def repo_identity(raw: bytes) -> dict[str, Any]:
    """git dir, common dir, toplevel and branch from one ``rev-parse`` output."""
    out = raw.decode("utf-8", "replace").splitlines()
    if len(out) < 3:
        raise RuntimeError("not a git work tree")
    gitdir, top = Path(out[0]), Path(out[2])
    common = Path(out[1])
    if not common.is_absolute():
        common = (top / common).resolve()
    ref = out[3] if len(out) > 3 else "HEAD"
    return {"gitdir": gitdir, "common": common, "top": top,
            "branch": None if ref == "HEAD" else ref, "detached": ref == "HEAD"}


def lock_files(gitdir: Path, common: Path) -> list[str]:
    locks: set[str] = set()
    for d in {gitdir, common}:
        for p in d.glob("*.lock"):
            locks.add(norm_path(p))
    return sorted(locks)


def read_index(gitdir: Path) -> tuple[bytes, int | None]:
    index = gitdir / "index"
    if not index.is_file():
        return b"", None
    mtime = index.stat().st_mtime_ns
    return index.read_bytes(), mtime


def parse_status(status: bytes) -> tuple[list[str], list[str], bool, list[str]]:
    """(untracked paths, ignored paths, dirty, nested repositories) from porcelain v2 -z
    output without renames.

    ``--untracked-files=all`` never descends into a nested repository: it is reported as
    one ``? dir/`` record. Such a path is a directory, so it is listed separately and its
    bytes are never read.
    """
    untracked: list[str] = []
    ignored: list[str] = []
    nested: list[str] = []
    dirty = False
    for rec in status.split(b"\0"):
        if not rec:
            continue
        kind = rec[:1]
        if kind == b"?":
            path = rec[2:].decode("utf-8", "surrogateescape")
            (nested if path.endswith("/") else untracked).append(path)
            dirty = True
        elif kind == b"!":
            ignored.append(rec[2:].decode("utf-8", "surrogateescape"))
        else:
            dirty = True
    return untracked, ignored, dirty, nested


def make_bracket(gitdir: Path, common: Path, status: bytes, head_raw: bytes,
                 index: tuple[bytes, int | None]) -> dict[str, Any]:
    """Everything that must not move while the snapshot is read."""
    index_bytes, mtime = index
    return {
        "status_sha256": sha256_bytes(status), "head": head_raw.decode("utf-8", "replace").strip() or None,
        "index_sha256": sha256_bytes(index_bytes), "index_mtime_ns": mtime,
        "index_size": len(index_bytes), "locks": lock_files(gitdir, common),
        "_status": status, "_index": index_bytes,
    }


def public_bracket(b: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in b.items() if not k.startswith("_")}


def bracket_diff(pre: dict[str, Any], post: dict[str, Any]) -> list[str]:
    diffs = [k for k in ("status_sha256", "head", "index_sha256", "index_mtime_ns") if pre[k] != post[k]]
    if pre["locks"] or post["locks"]:
        diffs.append("locks")
    return diffs


def _config(raw: bytes) -> dict[str, str | None]:
    found: dict[str, str | None] = {k: None for k in CONFIG_KEYS}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition(" ")
        if key in found:
            found[key] = value
    return found


def _read_artifacts(repo: Path, dl: Deadline) -> dict[str, bytes]:
    """The git-derived artifacts, in one place so the second pass is identical."""
    art = run_git_parallel(repo, {"index.patch": INDEX_PATCH_ARGS, "worktree.patch": WORKTREE_PATCH_ARGS,
                                  "ls-files-stage.z": LS_STAGE_ARGS, "ls-tree-head.z": LS_TREE_ARGS},
                           dl.remaining())
    dl.check("artifacts")
    return art


def index_blobs_outside_head(stage_listing: bytes, tree_listing: bytes) -> list[str]:
    """Object names of index entries (all stages) whose blob is not in HEAD's tree.

    Staged content and conflict stages live only in the source's object store; the clone
    is built from the start commit's ancestry, so these blobs travel in the snapshot.
    """
    head_blobs = set()
    for rec in tree_listing.split(b"\0"):
        if rec:
            meta = rec.split(b"\t", 1)[0].split(b" ")
            if len(meta) == 3 and meta[1] == b"blob":
                head_blobs.add(meta[2].decode("ascii"))
    need: set[str] = set()
    for rec in stage_listing.split(b"\0"):
        if rec:
            mode, sha, _stage = rec.split(b"\t", 1)[0].split(b" ")
            if mode != b"160000" and sha.decode("ascii") not in head_blobs:
                need.add(sha.decode("ascii"))
    return sorted(need)


def cat_blobs(repo: Path, shas: list[str], timeout: float) -> dict[str, bytes]:
    """Blob bytes by object name, in one ``cat-file --batch`` call."""
    if not shas:
        return {}
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"], input=("\n".join(shas) + "\n").encode(),
                          capture_output=True, timeout=timeout, env=git_env())
    if proc.returncode != 0:
        raise RuntimeError("git cat-file --batch failed")
    out, pos, blobs = proc.stdout, 0, {}
    for sha in shas:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].split(b" ")
        if len(header) != 3 or header[1] != b"blob" or header[0].decode("ascii") != sha:
            raise RuntimeError(f"cat-file: unexpected header for {sha}")
        size = int(header[2])
        blobs[sha] = out[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return blobs


def worktree_changed(status: bytes, top: Path) -> tuple[list[tuple[str, bool]], list[str]]:
    """Tracked paths whose worktree differs from the index: ``(path, deleted)``, plus the
    submodule paths among them.

    From porcelain v2 ``1`` records with a worktree status other than ``.`` and every
    unmerged ``u`` record (the conflicted file as it sits in the worktree). An unmerged
    record's ``XY`` names the merge sides, not the worktree: ``UD`` (deleted by them)
    leaves our version on disk, so deletion is read from the worktree itself. A submodule
    (``sub`` field ``S...``) is a directory whose bytes are never read; its path is
    returned separately.
    """
    out: list[tuple[str, bool]] = []
    submodules: list[str] = []
    for rec in status.split(b"\0"):
        if rec.startswith(b"1 "):
            parts = rec.split(b" ", 8)
            y = parts[1][1:2]
            if y == b".":
                continue
            path = parts[8].decode("utf-8", "surrogateescape")
            if parts[2].startswith(b"S"):
                submodules.append(path)
            else:
                out.append((path, y == b"D"))
        elif rec.startswith(b"u "):
            parts = rec.split(b" ", 10)
            path = parts[10].decode("utf-8", "surrogateescape")
            if parts[2].startswith(b"S"):
                submodules.append(path)
            else:
                out.append((path, not os.path.lexists(top / path)))
    return out, submodules


def _untracked_entry(top: Path, rel: str) -> tuple[dict[str, Any], bytes | None]:
    p = top / rel
    st = os.lstat(p)
    entry: dict[str, Any] = {"path": rel, "path_sha256": path_sha256(rel), "mode": st.st_mode & 0o7777}
    if os.path.islink(p):
        target = os.readlink(p)
        entry.update(symlink=target.replace("\\", "/"), bytes=len(target),
                     sha256=sha256_bytes(target.encode("utf-8")))
        return entry, None
    data = p.read_bytes()
    entry.update(bytes=len(data), sha256=sha256_bytes(data), symlink=None)
    return entry, data


def take_snapshot(repo: Path, episode: str, stores: Stores, *, budget_s: float = START_BUDGET_S,
                  size_cap: int = SIZE_CAP_BYTES) -> tuple[dict[str, Any], dict[str, Any]]:
    """Capture the start state of ``repo`` into ``snapshots/<episode>/``.

    Returns ``(repo_fields, start_snapshot)`` for the manifest. Never raises for a
    capture problem: ``start_snapshot.state`` is ``failed`` with a reason instead.
    """
    dl = Deadline(budget_s)
    repo_fields: dict[str, Any] = {"root_sha256": sha256_bytes(norm_path(repo).encode()), "main_root_sha256": None,
                                   "worktree_id": None, "head": None, "branch": None, "detached": None}
    snap: dict[str, Any] = {"state": "failed", "reason": None, "detail": "", "dirty": None,
                            "status_sha256": None, "diff_sha256": None, "index_patch_sha256": None,
                            "worktree_patch_sha256": None, "index_sha256": None, "untracked": [],
                            "untracked_count": 0, "ignored_count": 0, "sums_sha256": None,
                            "bracket": None, "elapsed_ms": None}
    tmp: Path | None = None
    try:
        # Phase 1: identity + pre bracket, concurrently. A write racing these calls leaves
        # status or index differing from the phase-3 bracket and fails the snapshot.
        try:
            first = run_git_parallel(repo, {"identity": IDENTITY_ARGS, "status": STATUS_ARGS, "head": HEAD_ARGS,
                                            "config": CONFIG_ARGS}, dl.remaining(),
                                     allow_fail=("identity", "head", "config"))
            ident = repo_identity(first["identity"])
        except (RuntimeError, OSError) as exc:
            raise SnapshotFailed("not_a_repo", str(exc)[:200]) from exc
        gitdir, common, top = ident["gitdir"], ident["common"], ident["top"]
        main_root = common.parent if common.name == ".git" else common
        repo_fields["main_root_sha256"] = sha256_bytes(norm_path(main_root).encode())
        if gitdir.parent.name == "worktrees" and gitdir.parent.parent == common:
            repo_fields["worktree_id"] = gitdir.name
        repo_fields.update(branch=ident["branch"], detached=ident["detached"])

        pre = make_bracket(gitdir, common, first["status"], first["head"], read_index(gitdir))
        snap["bracket"] = {"pre": public_bracket(pre), "post": None, "stable": False, "diff": []}
        if pre["locks"]:
            raise SnapshotFailed("concurrent_change", "lock present: " + ",".join(Path(p).name for p in pre["locks"]))
        if not pre["head"]:
            raise SnapshotFailed("head_unresolved", "git rev-parse HEAD failed")
        repo_fields["head"] = pre["head"]
        untracked_paths, ignored_paths, dirty, nested_repos = parse_status(pre["_status"])
        snap.update(status_sha256=pre["status_sha256"], index_sha256=pre["index_sha256"], dirty=dirty)
        dl.check("bracket")

        # Phase 2: artifacts and untracked bytes into a private tmp dir.
        tmp = new_tmp_dir(stores, episode)
        (tmp / "INCOMPLETE").write_bytes(b"")
        (tmp / "status.v2z").write_bytes(pre["_status"])
        (tmp / "index.bin").write_bytes(pre["_index"])
        cfg: dict[str, Any] = {**_config(first["config"]), "git_version": git_version()}
        (tmp / "git-config.json").write_text(json.dumps(cfg, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        art = _read_artifacts(repo, dl)
        total = sum(len(v) for v in art.values())
        for name, data in art.items():
            (tmp / name).write_bytes(data)
        for name in STATE_FILES:
            src = gitdir / name
            if src.is_file():
                (tmp / "state").mkdir(exist_ok=True)
                shutil.copyfile(src, tmp / "state" / name)

        blobs = cat_blobs(repo, index_blobs_outside_head(art["ls-files-stage.z"], art["ls-tree-head.z"]),
                          dl.remaining())
        for sha, data in blobs.items():
            total += len(data)
            (tmp / "blobs").mkdir(exist_ok=True)
            (tmp / "blobs" / sha).write_bytes(data)
        dl.check("blobs")

        def keep_files(paths: list[str], subdir: str) -> list[dict[str, Any]]:
            nonlocal total
            meta: list[dict[str, Any]] = []
            for rel in paths:
                entry, data = _untracked_entry(top, rel)
                total += entry["bytes"]
                if total > size_cap:
                    raise SnapshotFailed("size_cap", f"{total} bytes > {size_cap}")
                if data is not None:
                    (tmp / subdir).mkdir(exist_ok=True)
                    (tmp / subdir / entry["sha256"]).write_bytes(data)
                meta.append(entry)
                dl.check(subdir)
            return meta

        untracked_meta = keep_files(untracked_paths, "untracked")
        changed, submodules = worktree_changed(pre["_status"], top)
        worktree_meta = keep_files([p for p, deleted in changed if not deleted], "worktree")
        worktree_meta += [{"path": p, "path_sha256": path_sha256(p), "deleted": True} for p, deleted in changed if deleted]
        nested_meta = [{"path": p, "path_sha256": path_sha256(p), "kind": "nested_repo"} for p in nested_repos]
        nested_meta += [{"path": p, "path_sha256": path_sha256(p), "kind": "submodule"} for p in submodules]
        (tmp / "untracked.json").write_text(json.dumps(untracked_meta, sort_keys=True, indent=1) + "\n",
                                            encoding="utf-8")
        (tmp / "worktree.json").write_text(json.dumps(worktree_meta, sort_keys=True, indent=1) + "\n",
                                           encoding="utf-8")
        (tmp / "nested.json").write_text(json.dumps(nested_meta, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        ignored = sorted(path_sha256(p) for p in ignored_paths)
        (tmp / "ignored.json").write_text(json.dumps(ignored) + "\n", encoding="utf-8")

        # Phase 3: post bracket, then every artifact again. Anything that moved fails.
        again = run_git_parallel(repo, {"status": STATUS_ARGS, "head": HEAD_ARGS}, dl.remaining(),
                                 allow_fail=("head",))
        post = make_bracket(gitdir, common, again["status"], again["head"], read_index(gitdir))
        snap["bracket"]["post"] = public_bracket(post)
        diffs = bracket_diff(pre, post)
        art2 = _read_artifacts(repo, dl)
        diffs += [name for name, data in art.items() if art2[name] != data]
        for entry in untracked_meta + [e for e in worktree_meta if not e.get("deleted")]:
            try:
                redo, _ = _untracked_entry(top, entry["path"])
            except OSError:
                diffs.append("untracked:" + entry["path_sha256"])
                continue
            if redo["sha256"] != entry["sha256"] or redo["mode"] != entry["mode"]:
                diffs.append("untracked:" + entry["path_sha256"])
        post_untracked, _, _, post_nested = parse_status(post["_status"])
        if set(post_untracked) != {e["path"] for e in untracked_meta}:
            diffs.append("untracked_set")
        if set(post_nested) != set(nested_repos):
            diffs.append("nested_set")
        snap["bracket"]["diff"] = diffs
        if diffs:
            raise SnapshotFailed("concurrent_change", ",".join(diffs))
        snap["bracket"]["stable"] = True
        dl.check("second_pass")

        # Phase 4: verify and publish atomically.
        write_sums(tmp)
        bad = [b for b in verify_sums(tmp) if b != "INCOMPLETE"]
        if bad:
            raise SnapshotFailed("write_error", "verify: " + ",".join(bad[:5]))
        (tmp / "INCOMPLETE").unlink()
        dest = stores.snapshots / episode
        if dest.exists():
            raise SnapshotFailed("write_error", "snapshot exists; never overwritten")
        os.replace(tmp, dest)
        tmp = None

        snap.update(state="complete", reason=None,
                    index_patch_sha256=sha256_bytes(art["index.patch"]),
                    worktree_patch_sha256=sha256_bytes(art["worktree.patch"]),
                    diff_sha256=sha256_bytes(art["index.patch"] + art["worktree.patch"]),
                    untracked=[{"path_sha256": e["path_sha256"], "sha256": e["sha256"], "bytes": e["bytes"]}
                               for e in untracked_meta],
                    untracked_count=len(untracked_meta), ignored_count=len(ignored),
                    sums_sha256=sha256_file(dest / "SHA256SUMS"))
    except SnapshotFailed as exc:
        snap.update(state="failed", reason=exc.reason, detail=exc.detail[:300])
    except subprocess.TimeoutExpired:
        snap.update(state="failed", reason="timeout", detail="git call exceeded the start budget")
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        snap.update(state="failed", reason="write_error", detail=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        if tmp is not None:
            rmtree(tmp)
        snap["elapsed_ms"] = dl.elapsed_ms()
    return repo_fields, snap

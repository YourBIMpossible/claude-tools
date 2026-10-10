#!/usr/bin/env python3
"""Start-only replay clone: build, prove P1-P10, reconstruct the captured start (plan §7, §7a).

Build (plan §7): a bare intermediate fetches exactly ``<start_sha>`` over ``file://`` with
no tags and no FETCH_HEAD; a ``--no-local --no-hardlinks`` clone of that intermediate
forces a real pack transfer; the remote, the temporary branch and every reflog are then
removed, and the intermediate is deleted. The source repository is only read.

Proofs, all recorded with command and outcome in the returned record:

  P1  reachable set in the clone is a subset of the start's ancestry; commit sets equal
  P2  no refs, tags or stash; HEAD detached at the start
  P3  no reflog entries
  P4  no alternates, no worktrees, no shared-repository or gc settings, no hardlinked
      object files
  P5  every object the source holds beyond the start's ancestry is missing in the clone
      (``cat-file --batch-check`` for all; ``rev-parse --verify`` per commit)
  P6  no sentinel string in any object of the clone (every object's bytes, ``git grep``
      and ``log -S`` over all commits) nor in its worktree
  P7  no ``remote.*``, ``credential.*``, ``url.*``, ``include*``, ``core.hooksPath`` or
      ``fetch.*`` in the clone's own configuration (global and system entries are
      recorded, not failed: they belong to the machine, not to the source repository)
  P8  hooks directory holds only ``*.sample``
  P9  after reconstruction, status (without ignored entries), stage listing, unmerged
      listing, both patches and every untracked / worktree file hash equal the capture
  P10 every packed object in the clone is in P1's set

P1-P8 and P10 run before reconstruction (P5/P6 before any captured blob is written into
the clone); the worktree part of P6 runs again after it. Any failure means
``start_state_reconstructed`` is false. These proofs say nothing about what the agent can
reach through other filesystem paths; that is ``isolation.py`` (plan §8).

Reconstruction order: configure ``core.autocrlf`` / ``core.symlinks`` from the capture
before checkout; remove the tracked files of the start checkout; write captured index
blobs with ``hash-object -w`` and check their names; install ``index.bin`` (fall back to
``update-index --index-info`` from the stage listing if this git cannot read it); drop
index extensions that describe the source machine; ``checkout-index -a -f -u``; write the
captured worktree bytes and deletions; write untracked files; refresh; compare (P9).

A start with an operation in progress (``MERGE_HEAD``, ``REBASE_HEAD``,
``CHERRY_PICK_HEAD``, ``REVERT_HEAD``, ``BISECT_LOG``) is not reconstructed: the other
side of that operation is outside the start's ancestry, so the start cannot be reproduced
without the objects P5 forbids. A symlink the machine cannot create fails closed.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from common import git_env, sha256_bytes, sha256_file
from snapshot import INDEX_PATCH_ARGS, LS_STAGE_ARGS, STATUS_ARGS, WORKTREE_PATCH_ARGS
from store import rmtree, verify_sums

HEX = re.compile(rb"^[0-9a-f]{40}([0-9a-f]{24})?$")
OPERATION_FILES = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")
P7_FORBIDDEN = re.compile(r"^(remote\.|credential\.|url\.|include|core\.hookspath=|fetch\.)", re.I)
PROOFS = ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10")
CORE_CONFIG_KEYS = ("core.autocrlf", "core.symlinks")
# The only shapes these keys take (booleans and ``input``); git stops option parsing at
# the key, so a ``-`` value is stored literally, but the value is snapshot data and
# never reaches git unless it looks like one.
CONFIG_VALUE = re.compile(r"[A-Za-z0-9]{1,16}")
SABOTAGES = ("keep_tag", "keep_reflog", "add_alternates", "leave_remote", "add_hook", "hardlink_pack",
             "copy_post_start_blob", "full_fetch", "fetch_stash", "fetch_orphan")
GIT_TIMEOUT_S = 300


class CloneError(RuntimeError):
    """The clone could not be built; nothing about it may be used."""


def _env() -> dict[str, str]:
    env = git_env()
    env.pop("GIT_OBJECT_DIRECTORY", None)
    env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    return env


def _run(cwd: Path | None, *args: str, input_: bytes | None = None, check: bool = True,
         ok: tuple[int, ...] = (0,), log: list[dict[str, Any]] | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run git. With ``check`` any exit code outside ``ok`` raises ``CloneError``; ``ok`` names
    the codes a command uses for an answer (``symbolic-ref -q``: 1 = detached), never for a
    failure. ``check=False`` is for callers that branch on the code themselves."""
    cmd = ["git", *(("-C", str(cwd)) if cwd else ()), *args]
    proc = subprocess.run(cmd, input=input_, capture_output=True, timeout=GIT_TIMEOUT_S, env=_env())
    if log is not None:
        log.append({"cmd": ["git", *args], "rc": proc.returncode,
                    "stderr": proc.stderr.decode("utf-8", "replace").strip()[-300:]})
    if check and proc.returncode not in ok:
        raise CloneError(f"git {' '.join(args[:3])} failed rc={proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return proc


def _out(cwd: Path, *args: str, input_: bytes | None = None) -> bytes:
    return _run(cwd, *args, input_=input_).stdout


def _lines(data: bytes) -> list[str]:
    return [x for x in data.decode("utf-8", "replace").splitlines() if x]


def git_version() -> str:
    return _out(Path.cwd(), "--version").decode().strip()


# ---------------------------------------------------------------------- build

def build_clone(source: Path, start_sha: str, dest: Path, *, core_config: dict[str, str | None] | None = None,
                sabotage: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Build the start-only clone at ``dest`` (plan §7). Returns the command log.

    ``sabotage`` exists for the mutation tests only: each name breaks one step the way a
    faulty builder would, so the proof meant to catch it can be shown to fail.
    """
    bad = set(sabotage) - set(SABOTAGES)
    if bad:
        raise ValueError(f"unknown sabotage {sorted(bad)}")
    sab = set(sabotage)
    if dest.exists():
        raise CloneError(f"destination exists: {dest}")
    if not HEX.match(start_sha.encode()):
        raise CloneError("start_sha must be a full object name")
    for key, value in (core_config or {}).items():
        if key not in CORE_CONFIG_KEYS or not (value is None or isinstance(value, str) and CONFIG_VALUE.fullmatch(value)):
            raise CloneError(f"core_config {key!r} is not a config value: {value!r}"[:300])
    log: list[dict[str, Any]] = []
    dest.parent.mkdir(parents=True, exist_ok=True)
    objects = dest.parent / (dest.name + ".objects.tmp")
    rmtree(objects)
    url = "file://" + source.resolve().as_posix()
    try:
        _run(None, "init", "-q", "--bare", str(objects), log=log)
        _run(objects, "config", "core.logAllRefUpdates", "false", log=log)
        refspecs = [f"{start_sha}:refs/heads/start"]
        if "full_fetch" in sab:
            refspecs += ["+refs/heads/*:refs/heads/src/*", "+refs/tags/*:refs/tags/*"]
        if "fetch_stash" in sab:
            refspecs.append("+refs/stash:refs/heads/stash")
        fetch = ["fetch", "--no-write-fetch-head", "--no-auto-gc", "-q"]
        if "full_fetch" not in sab:
            fetch.append("--no-tags")
        _run(objects, *fetch, url, *refspecs, log=log)
        if "fetch_orphan" in sab:
            for sha in _orphan_commits(source):
                _run(objects, "fetch", "--no-write-fetch-head", "--no-tags", "-q", url, f"{sha}:refs/heads/orphan-{sha[:8]}", log=log)
        _run(None, "clone", "-q", "--no-hardlinks", "--no-local", "--no-checkout", "--branch", "start",
             objects.as_posix(), str(dest), log=log)
        _run(dest, "config", "core.logAllRefUpdates", "false", log=log)
        for key, value in (core_config or {}).items():
            if value is None:
                _run(dest, "config", "--local", "--unset-all", key, ok=(0, 5), log=log)  # 5: key was not set
            else:
                _run(dest, "config", "--local", key, value, log=log)
        if "leave_remote" not in sab:
            _run(dest, "remote", "remove", "origin", log=log)
        _run(dest, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", start_sha, log=log)
        _run(dest, "update-ref", "-d", "refs/heads/start", log=log)
        if "keep_reflog" not in sab:
            _run(dest, "reflog", "expire", "--expire=all", "--all", log=log)
    finally:
        if not rmtree(objects):
            log.append({"cmd": ["rmtree", objects.name], "rc": 1, "stderr": "intermediate not removed"})
    if objects.exists():
        raise CloneError("intermediate object store could not be removed")
    _apply_post_sabotage(source, start_sha, dest, sab, log)
    return log


def _orphan_commits(source: Path) -> list[str]:
    reachable = set(_lines(_out(source, "rev-list", "--all")))
    reflog = set(_lines(_out(source, "rev-list", "--all", "--reflog")))
    return sorted(reflog - reachable)


def _apply_post_sabotage(source: Path, start_sha: str, dest: Path, sab: set[str], log: list[dict[str, Any]]) -> None:
    gitdir = dest / ".git"
    if "keep_tag" in sab:
        _run(dest, "tag", "leftover", start_sha, log=log)
    if "add_alternates" in sab:
        (gitdir / "objects" / "info").mkdir(parents=True, exist_ok=True)
        src_objects = (source / ".git" / "objects").resolve().as_posix()
        (gitdir / "objects" / "info" / "alternates").write_bytes(src_objects.encode("utf-8") + b"\n")
    if "add_hook" in sab:
        (gitdir / "hooks").mkdir(exist_ok=True)
        (gitdir / "hooks" / "post-checkout").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    if "hardlink_pack" in sab:
        src_pack = source / ".git" / "objects" / "pack"
        for f in sorted(src_pack.glob("pack-*")):
            os.link(f, gitdir / "objects" / "pack" / ("linked-" + f.name))
    if "copy_post_start_blob" in sab:
        post = sorted(post_start_objects(source, start_sha))
        kinds = _out(source, "cat-file", "--batch-check", input_=("\n".join(post) + "\n").encode())
        blobs = [ln.split()[0] for ln in _lines(kinds) if ln.split()[1] == "blob"]
        data = _cat(source, blobs)
        for sha, content in data.items():
            got = _out(dest, "hash-object", "-w", "--no-filters", "--stdin", input_=content).decode().strip()
            if got != sha:
                raise CloneError("sabotage copy produced a different object name")


# ---------------------------------------------------------------------- object sets

def ancestry_objects(repo: Path, rev: str) -> set[str]:
    return {ln.split(" ", 1)[0] for ln in _lines(_out(repo, "rev-list", "--objects", rev))}


def post_start_objects(source: Path, start_sha: str) -> set[str]:
    """Every object the source can reach (refs, stash, reflogs) that the start cannot."""
    everything = {ln.split(" ", 1)[0] for ln in _lines(_out(source, "rev-list", "--objects", "--all", "--reflog"))}
    return everything - ancestry_objects(source, start_sha)


def _cat(repo: Path, shas: list[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    out = _out(repo, "cat-file", "--batch", input_=("\n".join(shas) + "\n").encode())
    pos, found = 0, {}
    for sha in shas:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].split(b" ")
        if header[-1] == b"missing":
            pos = nl + 1
            continue
        size = int(header[2])
        found[sha] = out[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return found


def _all_object_bytes(repo: Path) -> bytes:
    """Every object the clone's store holds (packs, loose, alternates), decompressed."""
    return _out(repo, "cat-file", "--batch-all-objects", "--batch", "--unordered")


# ---------------------------------------------------------------------- proofs

def _proof(ok: bool, **detail: Any) -> dict[str, Any]:
    return {"passed": bool(ok), **detail}


def p1(dest: Path, start_objects: set[str], start_commits: set[str]) -> dict[str, Any]:
    reach = {ln.split(" ", 1)[0] for ln in _lines(_out(dest, "rev-list", "--all", "--reflog", "--objects", "HEAD"))}
    commits = set(_lines(_out(dest, "rev-list", "--all", "--reflog", "HEAD")))
    extra = sorted(reach - start_objects)
    return _proof(not extra and commits == start_commits, reachable=len(reach), extra=extra[:10],
                  extra_count=len(extra), commits=len(commits), commit_sets_equal=commits == start_commits)


def p2(dest: Path, start_sha: str) -> dict[str, Any]:
    refs = _lines(_out(dest, "for-each-ref"))
    tags = _lines(_out(dest, "tag", "-l"))
    stash = _lines(_out(dest, "stash", "list"))
    symbolic = _run(dest, "symbolic-ref", "-q", "HEAD", ok=(0, 1)).returncode == 0
    head = _out(dest, "rev-parse", "HEAD").decode().strip()
    packed = (dest / ".git" / "packed-refs")
    packed_refs = [ln for ln in packed.read_text(encoding="utf-8").splitlines()
                   if ln and not ln.startswith("#")] if packed.exists() else []
    return _proof(not refs and not tags and not stash and not symbolic and head == start_sha and not packed_refs,
                  refs=refs[:10], tags=tags[:10], stash=stash[:5], head_symbolic=symbolic,
                  head_is_start=head == start_sha, packed_refs=packed_refs[:10])


def p3(dest: Path) -> dict[str, Any]:
    shown = _lines(_out(dest, "reflog", "show", "--all"))
    logs = dest / ".git" / "logs"
    nonempty = [str(p.relative_to(logs)).replace("\\", "/") for p in logs.rglob("*")
                if p.is_file() and p.stat().st_size > 0] if logs.exists() else []
    return _proof(not shown and not nonempty, reflog_entries=len(shown), nonempty_logs=nonempty[:10])


def p4(dest: Path) -> dict[str, Any]:
    gitdir = dest / ".git"
    alternates = (gitdir / "objects" / "info" / "alternates").exists() or \
        (gitdir / "objects" / "info" / "http-alternates").exists()
    worktrees = (gitdir / "worktrees").exists()
    local = _lines(_out(dest, "config", "--local", "--list"))
    shared = [ln for ln in local if ln.lower().startswith(("core.sharedrepository", "gc."))]
    count = _lines(_out(dest, "count-objects", "-v"))
    alt_line = [ln for ln in count if ln.startswith("alternate:")]
    linked = [str(p.relative_to(gitdir)).replace("\\", "/") for p in (gitdir / "objects").rglob("*")
              if p.is_file() and os.stat(p).st_nlink > 1]
    env_alt = [k for k in ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_OBJECT_DIRECTORY") if os.environ.get(k)]
    return _proof(not (alternates or worktrees or shared or alt_line or linked or env_alt),
                  alternates_file=alternates, worktrees_dir=worktrees, config=shared, count_objects_alternate=alt_line,
                  hardlinked=linked[:10], env=env_alt)


def p5(dest: Path, post: set[str], post_commits: set[str]) -> dict[str, Any]:
    names = sorted(post)
    present: list[str] = []
    if names:
        out = _out(dest, "cat-file", "--batch-check", input_=("\n".join(names) + "\n").encode())
        present = [ln.split(" ", 1)[0] for ln in _lines(out) if not ln.endswith(" missing")]

    def resolves(sha: str) -> bool:
        return _run(dest, "rev-parse", "-q", "--verify", f"{sha}^{{object}}", ok=(0, 1)).returncode == 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        resolved = [sha for sha, ok in zip(sorted(post_commits), pool.map(resolves, sorted(post_commits))) if ok]
    return _proof(not present and not resolved, vacuous=not names, checked=len(names), commits_checked=len(post_commits),
                  present=present[:10], present_count=len(present), rev_parse_resolved=resolved[:10],
                  note="vacuous when the source holds nothing beyond the start; the fixtures never are")


def _worktree_hits(dest: Path, sentinels: list[bytes]) -> list[str]:
    hits = []
    for p in dest.rglob("*"):
        if ".git" in p.relative_to(dest).parts or not p.is_file() or p.is_symlink():
            continue
        data = p.read_bytes()
        if any(s in data for s in sentinels):
            hits.append(str(p.relative_to(dest)).replace("\\", "/"))
    return hits


def p6(dest: Path, sentinels: list[str]) -> dict[str, Any]:
    raw = [s.encode("utf-8") for s in sentinels]
    blob_bytes = _all_object_bytes(dest)
    in_objects = [s for s, b in zip(sentinels, raw) if b in blob_bytes]
    commits = _lines(_out(dest, "rev-list", "--all", "--reflog", "HEAD"))
    grep_hits, log_hits = [], []
    for s in sentinels:
        g = _run(dest, "grep", "-c", "-F", s, *commits, ok=(0, 1)) if commits else None
        if g is not None and g.returncode == 0 and g.stdout.strip():
            grep_hits.append(s)
        if _out(dest, "log", "--all", "--reflog", "--format=%H", f"-S{s}").strip():
            log_hits.append(s)
    wt = _worktree_hits(dest, raw)
    return _proof(bool(sentinels) and not (in_objects or grep_hits or log_hits or wt), sentinels=len(sentinels),
                  in_object_bytes=len(in_objects), git_grep=len(grep_hits), log_S=len(log_hits), worktree=wt[:10],
                  note="no sentinels (a real task) records as not applicable" if not sentinels else "")


def p7(dest: Path) -> dict[str, Any]:
    local = _lines(_out(dest, "config", "--local", "--list"))
    bad = [ln for ln in local if P7_FORBIDDEN.match(ln)]
    everything = _lines(_out(dest, "config", "--list", "--show-origin"))
    machine = [ln for ln in everything if P7_FORBIDDEN.match(ln.split("\t", 1)[-1]) and "file:.git/config" not in ln]
    return _proof(not bad, local_forbidden=bad, machine_level_recorded=machine)


def p8(dest: Path) -> dict[str, Any]:
    hooks = dest / ".git" / "hooks"
    found = sorted(p.name for p in hooks.iterdir() if not p.name.endswith(".sample")) if hooks.exists() else []
    return _proof(not found, non_sample=found)


def p10(dest: Path, start_objects: set[str]) -> dict[str, Any]:
    pack_dir = dest / ".git" / "objects" / "pack"
    listed: set[str] = set()
    packs = sorted(pack_dir.glob("*.idx")) if pack_dir.exists() else []
    for idx in packs:
        for ln in _out(dest, "verify-pack", "-v", str(idx)).splitlines():
            first = ln.split(b" ", 1)[0]
            if HEX.match(first):
                listed.add(first.decode())
    extra = sorted(listed - start_objects)
    return _proof(bool(packs) and not extra, packs=len(packs), objects=len(listed), extra=extra[:10],
                  extra_count=len(extra), note="no pack at all fails: the transfer must be a real pack")


def prove_clone(source: Path, start_sha: str, dest: Path, sentinels: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
    """P1-P8 and P10 on a freshly built clone (before any captured blob is written)."""
    sentinels = list(sentinels)
    start_objects = ancestry_objects(source, start_sha)
    start_commits = set(_lines(_out(source, "rev-list", start_sha)))
    post = post_start_objects(source, start_sha)
    kinds = _out(source, "cat-file", "--batch-check", input_=("\n".join(sorted(post)) + "\n").encode()) if post else b""
    post_commits = {ln.split()[0] for ln in _lines(kinds) if ln.split()[1] == "commit"}
    proofs = {
        "P1": p1(dest, start_objects, start_commits),
        "P2": p2(dest, start_sha),
        "P3": p3(dest),
        "P4": p4(dest),
        "P5": p5(dest, post, post_commits),
        "P6": p6(dest, sentinels) if sentinels else {"passed": True, "not_applicable": "no sentinels given"},
        "P7": p7(dest),
        "P8": p8(dest),
        "P10": p10(dest, start_objects),
    }
    return proofs


# ---------------------------------------------------------------------- reconstruction

class ReconstructionError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def snapshot_core_config(snapshot: Path) -> dict[str, str | None]:
    """The captured ``core.*`` values ``build_clone`` restores. Snapshot data: a value that
    is not a config value (``CONFIG_VALUE``) or ``None`` is a ``CloneError``, so a crafted
    ``git-config.json`` is a recorded failure, never an argument to git."""
    cfg = json.loads((snapshot / "git-config.json").read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise CloneError("git-config.json is not an object")
    out: dict[str, str | None] = {}
    for key in CORE_CONFIG_KEYS:
        value = cfg.get(key)
        if not (value is None or isinstance(value, str) and CONFIG_VALUE.fullmatch(value)):
            raise CloneError(f"git-config.json {key} is not a config value: {value!r}"[:300])
        out[key] = value
    return out


def _stage_entries(listing: bytes) -> list[tuple[str, str, str, str]]:
    out = []
    for rec in listing.split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            mode, sha, stage = meta.decode().split(" ")
            out.append((mode, sha, stage, path.decode("utf-8", "surrogateescape")))
    return out


def _tree_paths(listing: bytes) -> list[str]:
    return [rec.split(b"\t", 1)[1].decode("utf-8", "surrogateescape") for rec in listing.split(b"\0") if rec]


def _remove_path(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        try:
            p.unlink()
        except PermissionError:
            os.chmod(p, stat.S_IWRITE)
            p.unlink()


def _prune_empty_dirs(root: Path) -> None:
    for d in sorted((x for x in root.rglob("*") if x.is_dir() and ".git" not in x.relative_to(root).parts),
                    key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass


_HEX64 = re.compile(r"[0-9a-f]{64}")


def _contained(dest: Path, rel: Any) -> Path:
    """``dest / rel`` when ``rel`` is a plain relative path that stays inside ``dest``:
    no absolute, drive, UNC or ``\\?\\`` form, no ``.``/``..``/empty segment, nothing
    under ``.git`` (also as ``.git.``), no segment Windows would rewrite, and no existing ancestor that is a symlink or junction (a write must
    never pass through an earlier link). Anything else is ``unsafe_entry_path``.

    The ancestor check is check-then-use: a process that swaps an ancestor for a link
    between it and the write already has write access to ``dest`` on this machine and
    could as well rewrite the file afterwards; neither Windows nor POSIX offers a
    portable no-follow directory walk from Python, so the race is accepted.

    Snapshot data is not trusted for this: SHA256SUMS only shows the snapshot is
    internally consistent, not who wrote it."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        raise ReconstructionError("unsafe_entry_path", "not a plain relative path")
    parts = rel.split("/")
    if (rel.startswith("/") or re.match(r"[A-Za-z]:", rel)
            or any(s in ("", ".", "..") for s in parts)
            or parts[0].rstrip(". ").lower() == ".git"):
        raise ReconstructionError("unsafe_entry_path", "absolute, dotted or .git path")
    # Win32 drops a segment's trailing dots and spaces (".git." is ".git") and reads
    # "name:stream" as an alternate data stream; neither can be a faithful file there.
    if os.name == "nt" and any(s != s.rstrip(". ") or ":" in s for s in parts):
        raise ReconstructionError("unsafe_entry_path", "segment Windows would rewrite")
    cur = dest
    for s in parts[:-1]:
        cur = cur / s
        if cur.is_symlink() or getattr(os.path, "isjunction", lambda _p: False)(cur):
            raise ReconstructionError("unsafe_entry_path", "an ancestor is a link")
    target = dest / Path(*parts)
    try:
        target.parent.resolve().relative_to(dest.resolve())
    except ValueError:
        raise ReconstructionError("unsafe_entry_path", "resolves outside the clone") from None
    return target


def _write_entry(dest: Path, entry: dict[str, Any], source_dir: Path, symlinks: bool) -> None:
    target = _contained(dest, entry.get("path"))
    if entry.get("symlink") is None and not (isinstance(entry.get("sha256"), str)
                                             and _HEX64.fullmatch(entry["sha256"])):
        raise ReconstructionError("unsafe_entry_path", "blob name is not a sha256")
    if target.is_symlink() or target.exists():
        _remove_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if entry.get("symlink") is not None:
        try:
            os.symlink(entry["symlink"], target)
        except OSError as exc:
            raise ReconstructionError("symlink_unsupported", f"{entry['path_sha256'][:12]}: {exc}") from exc
        return
    data = (source_dir / entry["sha256"]).read_bytes()
    if sha256_bytes(data) != entry["sha256"]:
        raise ReconstructionError("snapshot_corrupt", entry["path_sha256"][:12])
    target.write_bytes(data)
    if os.name != "nt" and entry.get("mode"):
        os.chmod(target, entry["mode"])


def reconstruct(dest: Path, snapshot: Path) -> dict[str, Any]:
    """Rebuild the captured index and worktree in the clone at ``dest``.

    The SHA256SUMS check below is an integrity check against truncation or corruption,
    not authentication: whoever can write the snapshot can rewrite its sums. Every entry
    path is therefore confined to ``dest`` (``_contained``) on its own."""
    if (snapshot / "INCOMPLETE").exists() or verify_sums(snapshot):
        raise ReconstructionError("snapshot_unverified", "SHA256SUMS mismatch or INCOMPLETE present")
    ops = sorted(p.name for p in (snapshot / "state").glob("*") if p.name in OPERATION_FILES) \
        if (snapshot / "state").is_dir() else []
    if ops:
        raise ReconstructionError("operation_in_progress", ",".join(ops))
    info: dict[str, Any] = {"index_install": None, "blobs": 0, "worktree": 0, "untracked": 0}
    cfg = snapshot_core_config(snapshot)
    symlinks = (cfg.get("core.symlinks") or "").lower() == "true"

    # remove the start checkout's tracked files; the captured index decides what exists
    for rel in _tree_paths((snapshot / "ls-tree-head.z").read_bytes()):
        _remove_path(_contained(dest, rel))
    _prune_empty_dirs(dest)

    blobs_dir = snapshot / "blobs"
    for f in sorted(blobs_dir.iterdir()) if blobs_dir.is_dir() else []:
        got = _out(dest, "hash-object", "-w", "--no-filters", "--stdin", input_=f.read_bytes()).decode().strip()
        if got != f.name:
            raise ReconstructionError("blob_name_mismatch", f.name[:12])
        info["blobs"] += 1

    index = dest / ".git" / "index"
    index.write_bytes((snapshot / "index.bin").read_bytes())
    readable = _run(dest, "ls-files", "--stage", "-z", check=False)
    if readable.returncode == 0:
        info["index_install"] = "index.bin"
        _run(dest, "update-index", "--no-untracked-cache", "--no-fsmonitor")
    else:
        index.unlink()
        _run(dest, "read-tree", "--empty")
        lines = "".join(f"{m} {s} {st}\t{p}\0" for m, s, st, p in
                        _stage_entries((snapshot / "ls-files-stage.z").read_bytes()))
        _out(dest, "update-index", "-z", "--index-info", input_=lines.encode("utf-8", "surrogateescape"))
        info["index_install"] = "index-info"

    co = _run(dest, "checkout-index", "-a", "-f", "-u", check=False)
    if co.returncode != 0:
        err = co.stderr.decode("utf-8", "replace")
        reason = "symlink_unsupported" if "symlink" in err else "checkout_index_failed"
        raise ReconstructionError(reason, err.strip()[:300])

    for entry in json.loads((snapshot / "worktree.json").read_text(encoding="utf-8")):
        if entry.get("deleted"):
            _remove_path(_contained(dest, entry.get("path")))
        else:
            _write_entry(dest, entry, snapshot / "worktree", symlinks)
        info["worktree"] += 1
    for entry in json.loads((snapshot / "untracked.json").read_text(encoding="utf-8")):
        _write_entry(dest, entry, snapshot / "untracked", symlinks)
        info["untracked"] += 1
    _prune_empty_dirs(dest)
    # rc 1 answers "entries still need a merge or update" (a captured conflict); P9 compares
    # the resulting status, so only a failure beyond that answer raises.
    _run(dest, "update-index", "-q", "--refresh", ok=(0, 1))
    return info


def _drop_ignored(status: bytes) -> bytes:
    return b"\0".join(r for r in status.split(b"\0") if r and not r.startswith(b"! ")) + b"\0"


def p9(dest: Path, snapshot: Path) -> dict[str, Any]:
    """Compare the reconstructed clone with the capture (plan §7 P9)."""
    got = {
        "status": _drop_ignored(_out(dest, *STATUS_ARGS)),
        "ls-files-stage.z": _out(dest, *LS_STAGE_ARGS),
        "ls-files-u": _out(dest, "ls-files", "-u", "-z"),
        "index.patch": _out(dest, *INDEX_PATCH_ARGS),
        "worktree.patch": _out(dest, *WORKTREE_PATCH_ARGS),
    }
    stage = (snapshot / "ls-files-stage.z").read_bytes()
    want = {
        "status": _drop_ignored((snapshot / "status.v2z").read_bytes()),
        "ls-files-stage.z": stage,
        "ls-files-u": b"".join(r + b"\0" for r in stage.split(b"\0") if r and not r.split(b"\t", 1)[0].endswith(b" 0")),
        "index.patch": (snapshot / "index.patch").read_bytes(),
        "worktree.patch": (snapshot / "worktree.patch").read_bytes(),
    }
    mismatch = [k for k in want if sha256_bytes(got[k]) != sha256_bytes(want[k])]
    files: list[str] = []
    for name in ("untracked.json", "worktree.json"):
        for entry in json.loads((snapshot / name).read_text(encoding="utf-8")):
            p = dest / entry["path"]
            if entry.get("deleted"):
                if p.exists() or p.is_symlink():
                    files.append(entry["path_sha256"][:12])
            elif entry.get("symlink") is not None:
                if not p.is_symlink() or os.readlink(p).replace("\\", "/") != entry["symlink"]:
                    files.append(entry["path_sha256"][:12])
            elif not p.is_file() or sha256_file(p) != entry["sha256"]:
                files.append(entry["path_sha256"][:12])
    return _proof(not mismatch and not files, mismatch=mismatch, files=files[:10],
                  compared=sorted(want) + ["untracked+worktree file hashes"])


# ---------------------------------------------------------------------- one call

def clone_start_only(source: Path, start_sha: str, dest: Path, snapshot: Path | None,
                     sentinels: Iterable[str] = (), sabotage: Iterable[str] = ()) -> dict[str, Any]:
    """Build, prove and reconstruct. The record says whether the start was reproduced.

    ``snapshot`` is the verified snapshot (or payload) directory for the episode. Without
    one there is no start state to reproduce and the record is never
    ``start_state_reconstructed``.
    """
    sentinels = list(sentinels)
    record: dict[str, Any] = {"start_sha": start_sha, "git_version": git_version(), "commands": [],
                              "proofs": {}, "reconstruction": None, "start_state_reconstructed": False,
                              "core_config": None, "error": None}
    try:
        cfg = snapshot_core_config(snapshot) if snapshot else None
        record["core_config"] = cfg
        record["commands"] = build_clone(source, start_sha, dest, core_config=cfg, sabotage=sabotage)
        record["proofs"] = prove_clone(source, start_sha, dest, sentinels)
        if cfg is not None:
            effective = {k: (_run(dest, "config", "--get", k, ok=(0, 1)).stdout.decode().strip() or None)
                         for k in cfg}
            record["core_config_effective"] = effective
        if snapshot is None:
            record["proofs"]["P9"] = _proof(False, reason="no_snapshot")
        else:
            try:
                record["reconstruction"] = reconstruct(dest, snapshot)
                record["proofs"]["P9"] = p9(dest, snapshot)
            except ReconstructionError as exc:
                record["proofs"]["P9"] = _proof(False, reason=exc.reason, detail=exc.detail)
            if sentinels:
                hits = _worktree_hits(dest, [s.encode("utf-8") for s in sentinels])
                record["proofs"]["P6"]["worktree_after_reconstruction"] = hits[:10]
                if hits:
                    record["proofs"]["P6"]["passed"] = False
            if cfg is not None and record.get("core_config_effective") != cfg:
                record["proofs"]["P9"] = {**record["proofs"]["P9"], "passed": False,
                                          "core_config_mismatch": True}
    except (CloneError, OSError, subprocess.SubprocessError) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"[:400]
    record["failed"] = sorted(k for k in PROOFS if not record["proofs"].get(k, {}).get("passed"))
    record["start_state_reconstructed"] = not record["error"] and not record["failed"]
    return record

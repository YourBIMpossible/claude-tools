#!/usr/bin/env python3
"""The capture hooks' pipeline (plan §9, §11 step 4): start, end and maintenance.

``run_start`` is the ``UserPromptSubmit`` side: it decides whether this prompt is
captured, writes the manifest, takes the start snapshot and returns. It never reads a
packet, never prints and never raises (the hook wrapper also catches). ``run_end`` is the
``Stop`` side: it joins the episode to its transcript, links it to its packet, screens
it, sets its boundary and reviews it. ``run_maintain`` is everything that may wait: the
tmp sweep, pending failures, hook-killed starts, re-joins for up to 24 h, link
reconciliation, review, expiry and the failure-streak flag. The start hook launches it
detached, so a prompt never waits for housekeeping.

Concurrency. Every manifest write after the start happens under that episode's lock
(``locks/<episode_id>.lock``, an OS advisory lock the OS drops when its holder dies); maintenance additionally holds
``maintain.lock`` so only one maintenance run exists. The end hook and maintenance
therefore never interleave writes to one manifest. Maintenance only finalises an episode
whose turn is over (a later episode of the same session exists, or its transcript has
been idle for ``IDLE_S``); an episode it joined is re-finalised by a late ``Stop`` until
it is materialised.

Local state that never enters the metadata repository: ``capture.log``,
``store-check.json`` (the last full store check), ``maintenance.json`` (counters and
flags), ``open/<session_id>`` (the session's open episode) and ``locks/``. A failure the
stores cannot take is appended to ``ec-capture-pending.jsonl`` in the temp directory
(hashes and identifiers only) and becomes a ``failed`` manifest at the next maintenance
run that can write.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import client_identity as ci
import manifest as mf
from common import iso, norm_path, now_utc, parse_iso, read_json, sha256_bytes, sha256_text, write_json_atomic
from common import episode_id as make_episode_id
from join import join, parse_transcript, transcript_for
from linkage import load_packets, packet_dirs, reconcile, resolve_link
from review import ReviewInputs, decide, expire, load_capture_config, load_owner_review, review_episode
from screen import screen
from snapshot import take_snapshot
from store import (CONTENT_DIRS, CONTENT_MARKERS, ENV_DISABLE, Stores, _under_sync_root, check_content_store,
                   check_meta_store, clean_tmp_orphans, commit_meta, log_line, manifest_path, read_manifest,
                   resolve_stores, scan_manifests, write_manifest)

PKG = Path(__file__).resolve().parent
BOUNDARY_DIRS = [PKG / "boundary"]
ENV_BOUNDARY_DIRS = "EC_CAPTURE_BOUNDARY_DIRS"  # replaces BOUNDARY_DIRS (synthetic tests, dry runs)
START_BUDGET_MS = 2000
END_BUDGET_S = 20.0
END_LOCK_WAIT_S = 5.0
REJOIN_WINDOW_H = 24
IDLE_S = 1800
KILLED_AFTER_S = 120
STORE_CHECK_TTL_S = 24 * 3600
MAINTAIN_MIN_INTERVAL_S = 60
FAILURE_STREAK = 3
STREAK_SNAPSHOT_REASONS = ("timeout", "write_error", "store_unusable", "stores_not_configured")
ENV_TRANSCRIPTS = "EC_CAPTURE_TRANSCRIPTS"
ENV_MAINTAIN = "EC_CAPTURE_MAINTAIN"  # "0": the start hook does not launch maintenance
PENDING_NAME = "ec-capture-pending.jsonl"
STORE_CHECK = "store-check.json"
MAINTENANCE = "maintenance.json"
MAINTAIN_LOCK = "maintain.lock"
SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


# ---------------------------------------------------------------------- small helpers

def disabled(env: dict[str, str]) -> bool:
    """Capture is off when ``EVIDENCE_CAPTURE=0`` or ``EVIDENCE_HOOK=0`` (the Twin sets both)."""
    return env.get(ENV_DISABLE) == "0" or env.get("EVIDENCE_HOOK") == "0"


def find_repo_root(cwd: str | Path | None) -> Path | None:
    """The checkout containing ``cwd`` when it carries ``.evidence-compiler/``; else None.

    The first ancestor with a ``.git`` entry (directory or worktree file) is the root; no
    git process is spawned."""
    if not cwd:
        return None
    try:
        p = Path(cwd).resolve()
    except (OSError, RuntimeError):
        return None
    for d in (p, *p.parents):
        if (d / ".git").exists():
            return d if (d / ".evidence-compiler").is_dir() else None
    return None


def root_sha256(root: Path) -> str:
    return sha256_bytes(norm_path(root).encode())


def _git_dir_of(start: Path) -> Path | None:
    for d in (start, *start.parents):
        g = d / ".git"
        if g.is_dir():
            return g
        if g.is_file():
            try:
                text = g.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                target = Path(text.split(":", 1)[1].strip())
                return target if target.is_absolute() else (d / target).resolve()
            return None
    return None


def _read_ref(gitdir: Path, ref: str) -> str | None:
    dirs = [gitdir]
    common = gitdir / "commondir"
    if common.is_file():
        try:
            c = Path(common.read_text(encoding="utf-8").strip())
            dirs.append(c if c.is_absolute() else (gitdir / c).resolve())
        except OSError:
            pass
    for d in dirs:
        f = d / ref
        if f.is_file():
            try:
                return f.read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
    for d in dirs:
        packed = d / "packed-refs"
        if packed.is_file():
            try:
                for line in packed.read_text(encoding="utf-8").splitlines():
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
            except OSError:
                return None
    return None


def tool_identity() -> tuple[str | None, str]:
    """``(tool_commit, tool_sha256)``: the capture tool's commit, read from files without
    spawning git, and a hash over its code and boundary records (what actually ran)."""
    h = hashlib.sha256()
    for p in sorted([*PKG.glob("*.py"), *(PKG / "boundary").glob("*.json")]):
        try:
            h.update(f"{p.parent.name}/{p.name}\0".encode() + hashlib.sha256(p.read_bytes()).hexdigest().encode() + b"\n")
        except OSError:
            h.update(f"{p.name}\0unreadable\n".encode())
    commit = None
    gitdir = _git_dir_of(PKG)
    if gitdir is not None:
        try:
            head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            head = ""
        commit = _read_ref(gitdir, head[5:].strip()) if head.startswith("ref:") else (head or None)
    return commit, h.hexdigest()


def _age_s(ts: str | None, now: str) -> float | None:
    a, b = parse_iso(ts), parse_iso(now)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def _pending_file(env: dict[str, str]) -> Path:
    return Path(env.get("TEMP") or env.get("TMPDIR") or ".") / PENDING_NAME


# ---------------------------------------------------------------------- locks
#
# A lock is an OS advisory lock on an open handle to the lock file (``msvcrt.locking``
# on Windows, ``fcntl.flock`` elsewhere), not the file's existence. The OS drops it when
# the holder's process dies, so there is no staleness rule and no steal: a live holder
# keeps its lock however long it runs, and a dead holder's lock is free at once. The file
# itself only carries "pid time" for diagnostics. A contender re-checks after locking that
# the path still names the file it locked, else it retries. On POSIX release unlinks while
# still locked, then unlocks: unlinking after the unlock would let a contender lock the old
# file in between while a third process creates and locks a new one. On Windows the holder
# cannot unlink its open file, so release unlocks and closes first; the unlink then fails
# while any contender has the file open, so the path keeps naming the file it locks.

_HELD: dict[str, int] = {}


def _os_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _try_lock(path: Path) -> bool:
    """Take ``path``'s lock without waiting. False while any holder (this process
    included) has it, or when the lock file cannot be opened."""
    key = str(path)
    if key in _HELD:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
        except OSError:
            return False
        if not _os_lock(fd):
            os.close(fd)
            return False
        try:
            same = _same_file(os.fstat(fd), os.stat(path))
        except OSError:
            same = False
        if not same:  # released and unlinked between our open and our lock
            _os_unlock(fd)
            os.close(fd)
            continue
        try:
            # diagnostics after byte 0 so the locked byte (Windows) is never written
            os.lseek(fd, 1, os.SEEK_SET)
            os.write(fd, f"{os.getpid()} {iso(now_utc())}\n".encode())
        except OSError:
            pass
        _HELD[key] = fd
        return True
    return False


def _unlock(path: Path) -> None:
    fd = _HELD.pop(str(path), None)
    if fd is None:
        return
    if os.name != "nt":
        try:
            path.unlink()
        except OSError:
            pass
    _os_unlock(fd)
    os.close(fd)
    if os.name == "nt":
        try:
            path.unlink()  # best effort; a contender holding it open keeps it
        except OSError:
            pass


def _wait_lock(path: Path, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while True:
        if _try_lock(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def episode_lock(stores: Stores, ep: str) -> Path:
    return stores.meta / "locks" / f"{ep}.lock"


def lock_episode(stores: Stores, ep: str, wait_s: float = 0.0) -> bool:
    return _wait_lock(episode_lock(stores, ep), wait_s)


APPEND_LOCK_WAIT_S = 2.0


def _append_lock(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def append_line(path: Path, line: str) -> None:
    """Append one JSONL line under ``path``'s append lock, as a single write, so
    concurrent writers never interleave and a drain's claim never splits a line.
    Raises OSError (TimeoutError when the lock stays busy)."""
    lock = _append_lock(path)
    if not _wait_lock(lock, APPEND_LOCK_WAIT_S):
        raise TimeoutError(f"append lock busy: {lock.name}")
    try:
        with path.open("ab") as fh:
            fh.write(line.encode("utf-8"))
    finally:
        _unlock(lock)


# ---------------------------------------------------------------------- store readiness

def _stores_key(stores: Stores) -> str:
    return sha256_bytes(f"{norm_path(stores.meta)}|{norm_path(stores.content)}".encode())


def _quick_problems(stores: Stores) -> list[str]:
    problems: list[str] = []
    if not stores.meta.is_dir():
        return ["metadata store missing"]
    if not (stores.meta / ".git").exists():
        problems.append("metadata store is not a git repository")
    if not stores.content.is_dir():
        return problems + ["content store missing"]
    problems += [f"marker missing: {n}" for n in CONTENT_MARKERS if not (stores.content / n).is_file()]
    problems += [f"missing directory: {d}" for d in CONTENT_DIRS if not (stores.content / d).is_dir()]
    sync = _under_sync_root(stores.content)
    if sync:
        problems.append(f"content store under a sync root: {sync}")
    return problems


def full_store_check(stores: Stores) -> dict[str, Any]:
    """Every check (spawns git and attrib) and a fresh ``store-check.json``."""
    content = check_content_store(stores)
    meta = check_meta_store(stores) if stores.meta.is_dir() else ["metadata store missing"]
    rec = {"checked_at": iso(now_utc()), "stores": _stores_key(stores), "content": content, "meta": meta}
    try:
        write_json_atomic(stores.meta / STORE_CHECK, rec)
    except OSError:
        pass
    return rec


def store_problems(stores: Stores) -> dict[str, list[str]]:
    """``{"content": [...], "meta": [...]}`` for the hot path: the cheap checks plus the
    last full check. A missing stamp, or one for other stores, runs the full check here;
    an old clean one is only refreshed by maintenance. A stamp that records problems is
    re-checked here, so one transient failure cannot refuse every start until the next
    maintenance pass; a problem that persists still refuses."""
    quick = _quick_problems(stores)
    stamp = None
    p = stores.meta / STORE_CHECK
    if p.is_file():
        try:
            stamp = read_json(p)
        except (OSError, ValueError):
            stamp = None
    if (not isinstance(stamp, dict) or stamp.get("stores") != _stores_key(stores)
            or stamp.get("content") or stamp.get("meta")):
        if quick and ("metadata store missing" in quick or "content store missing" in quick):
            stamp = {"content": [], "meta": []}
        else:
            stamp = full_store_check(stores)
    content = sorted(set([q for q in quick if "metadata" not in q] + list(stamp.get("content") or [])))
    meta = sorted(set([q for q in quick if "metadata" in q] + list(stamp.get("meta") or [])))
    return {"content": content, "meta": meta}


# ---------------------------------------------------------------------- pending failures

def record_pending(env: dict[str, str], rec: dict[str, Any]) -> str | None:
    """Append one failure the stores could not take (identifiers and hashes only).

    Returns None once the record is on disk, else the write error: the caller must count
    the episode as lost, because maintenance can never turn it into a ``failed`` manifest.
    """
    try:
        append_line(_pending_file(env), json.dumps(rec, sort_keys=True) + "\n")
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _pend_or_lose(stores: Stores | None, env: dict[str, str], rec: dict[str, Any], reason: str) -> dict[str, Any]:
    """Record a start the stores could not take; an unwritable pending file is a lost episode."""
    error = record_pending(env, {**rec, "reason": reason[:300]})
    if error is None:
        return {"action": "pending", "episode_id": rec["episode_id"], "reason": reason}
    log_line(stores if stores is not None and stores.meta.is_dir() else None,
             f"start {rec['episode_id']}: LOST, pending record not written ({error}); cause: {reason[:300]}")
    return {"action": "lost", "episode_id": rec["episode_id"], "reason": reason, "error": error}


def failed_manifest(rec: dict[str, Any], reason: str) -> dict[str, Any]:
    commit, tsha = tool_identity()
    m = mf.new_manifest(episode_id=rec["episode_id"], session_id=rec["session_id"], started_at=rec["started_at"],
                        prompt_sha256=rec["prompt_sha256"], prompt_len=int(rec.get("prompt_len") or 0),
                        tool_commit=commit, tool_sha256=tsha, cwd_sha256=rec.get("cwd_sha256") or "")
    m["repo"]["root_sha256"] = rec.get("root_sha256")
    m["start_snapshot"] = {"state": "failed", "reason": mf.failure_cause(reason), "detail": reason[:300]}
    m["capture"].update(status="failed", phase="failed", errors=[reason[:300]])
    m["boundary"] = {"cli_version": None, "trusted": False, "reason": "capture_failed"}
    return mf.refresh(m)


def _claim_lock(work: Path) -> Path:
    """The lock a drainer holds on its ``.draining`` file for as long as it works on it."""
    return work.with_name(f"{work.name}.lock")


def claim_pending(src: Path) -> Path:
    """Move the pending file aside for draining, under its append lock, so an append is
    either wholly in the claimed file or wholly in the next pending file. The returned
    ``.draining`` file is held by its claim lock (taken before it exists, so no other
    drainer sees it free); release it with ``release_claim``. Raises OSError."""
    work = src.with_name(f"{src.name}.{os.getpid()}.{time.time_ns()}.draining")
    if not _try_lock(_claim_lock(work)):
        raise TimeoutError(f"claim lock busy: {_claim_lock(work).name}")
    lock = _append_lock(src)
    try:
        if not _wait_lock(lock, APPEND_LOCK_WAIT_S):
            raise TimeoutError(f"append lock busy: {lock.name}")
        try:
            os.replace(src, work)
        finally:
            _unlock(lock)
    except BaseException:
        _unlock(_claim_lock(work))
        raise
    return work


def release_claim(work: Path) -> None:
    _unlock(_claim_lock(work))


def drain_pending(stores: Stores, env: dict[str, str]) -> dict[str, Any]:
    """Turn every pending failure into a ``failed`` manifest.

    A record leaves the pending file only once its manifest is on disk and reads back; a
    record that cannot be turned into a manifest goes back into the pending file for the
    next pass. A ``.draining`` file left by a drain that died between claiming the pending
    file and finishing is taken up by the next drain whatever its age. The pending file is
    shared by every store on the machine while ``maintain.lock`` is per store, so a
    ``.draining`` file is taken only when its claim lock is free: a live drain elsewhere
    keeps its own. Returns
    ``drained`` (episode ids), ``kept`` (records put back) and ``errors`` (one line each)."""
    src = _pending_file(env)
    out: dict[str, Any] = {"drained": [], "kept": 0, "errors": []}
    claimed: list[Path] = []
    # a .draining file whose claim lock is free belongs to a drain that died: claimed at
    # once, with no age rule; one still held is a live drain's (another store's)
    for orphan in sorted(src.parent.glob(f"{src.name}.*.draining")):
        if _try_lock(_claim_lock(orphan)):
            if orphan.exists():
                claimed.append(orphan)
            else:  # finished by its drainer between the glob and the lock
                release_claim(orphan)
    # a claim lock without its .draining file was left by a drainer that died after
    # removing the file: free, so taking it removes it
    for stray in src.parent.glob(f"{src.name}.*.draining.lock"):
        work = stray.with_name(stray.name[:-len(".lock")])
        if not work.exists() and _try_lock(stray):
            _unlock(stray)
    try:
        _drain_claimed(stores, src, claimed, out)
    finally:
        for work in claimed:
            release_claim(work)
    for err in out["errors"]:
        log_line(stores, err)
    if out["drained"]:
        log_line(stores, f"pending capture failures recorded: {len(out['drained'])}")
    return out


def _drain_claimed(stores: Stores, src: Path, claimed: list[Path], out: dict[str, Any]) -> None:
    """Record every line of the claimed files (claim locks held by the caller) and claim
    the current pending file too."""
    if src.is_file():
        try:
            claimed.append(claim_pending(src))
        except OSError as exc:
            out["errors"].append(f"pending file not claimed: {type(exc).__name__}: {exc}"[:300])
    retry: list[str] = []
    readable: list[Path] = []
    for work in claimed:
        try:
            lines = work.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError) as exc:
            # left in place: an unread .draining file is retried by the next drain
            out["errors"].append(f"pending file unreadable {work.name}: {type(exc).__name__}")
            continue
        readable.append(work)
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                ep = rec["episode_id"]
                if not manifest_path(stores, ep).exists():
                    write_manifest(stores, failed_manifest(rec, rec.get("reason") or "store_unwritable"))
                    if read_manifest(stores, ep).get("episode_id") != ep:
                        raise ValueError("manifest did not read back")
                out["drained"].append(ep)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                retry.append(line)
                out["errors"].append(f"pending record not recorded: {type(exc).__name__}: {exc}"[:300])
    if retry:
        try:
            append_line(src, "".join(f"{line}\n" for line in retry))
            out["kept"] = len(retry)
        except OSError as exc:
            # the claimed files stay; the next drain retries them, so nothing is lost
            out["errors"].append(f"pending records not put back: {type(exc).__name__}")
            readable = []
    for work in readable:
        try:
            work.unlink()
        except OSError as exc:
            out["errors"].append(f"pending file not removed {work.name}: {type(exc).__name__}")


# ---------------------------------------------------------------------- boundary

def boundary_dirs(env: dict[str, str] | None) -> list[Path]:
    raw = (env if env is not None else os.environ).get(ENV_BOUNDARY_DIRS)
    return [Path(p) for p in raw.split(os.pathsep) if p] if raw else BOUNDARY_DIRS


def client_for(m: dict[str, Any], stores: Stores | None, env: dict[str, str] | None) -> dict[str, Any]:
    """The episode's client identity with its executable hash resolved when possible.

    The metadata store's cache supplies the hash, or the executable is hashed when the
    calling process (end hook, or maintenance launched by a start hook) runs under a
    client whose executable has the episode's path, size and modification time. The
    scheduled backstop has no client, so only the cache serves it. A resolved hash is
    kept in the manifest; an unresolved one is retried at the next pass."""
    start = m.get("client")
    if not start:
        return {"sha256": None, "problem": "not_observed"}
    if start.get("sha256") or start.get("problem"):
        return start
    cache = stores.meta / ci.CACHE_NAME if stores is not None and stores.meta.is_dir() else None
    got = ci.resolve(start, env, cache)
    if got.get("sha256"):
        m["client"] = {**start, "sha256": got["sha256"]}
    return got


def boundary_for(m: dict[str, Any], stores: Stores | None = None,
                 env: dict[str, str] | None = None) -> dict[str, Any]:
    """The episode's boundary: the tested record for its client (executable hash, CLI
    version, invocation mode), overridden when this episode's own start shows the
    boundary did not hold."""
    cap, snap = m["capture"], m["start_snapshot"]
    rec = mf.load_boundary(m["boundary"].get("cli_version"), boundary_dirs(env), client_for(m, stores, env))
    override = None
    if cap.get("phase") == "killed":
        override = "hook_killed"
    elif snap.get("reason") == "concurrent_change":
        override = "lock_present" if str(snap.get("detail", "")).startswith("lock present") else "bracket_dirty"
    elif cap.get("status") == "failed":
        override = "capture_failed"
    if override:
        rec = {**rec, "trusted": False, "reason": override, "cli_reason": rec.get("reason")}
    return rec


# ---------------------------------------------------------------------- transcripts

def transcript_dirs(env: dict[str, str]) -> list[Path]:
    raw = env.get(ENV_TRANSCRIPTS)
    if raw:
        return [Path(p) for p in raw.split(os.pathsep) if p]
    return [Path.home() / ".claude" / "projects"]


def find_transcript(session_id: str, env: dict[str, str], hint: str | None = None) -> Path | None:
    """The session's transcript: the hook's path if it names this session, else a
    ``<session_id>.jsonl`` directly in, or one level below, a transcript directory."""
    if not SESSION_RE.match(session_id or ""):
        return None
    if hint and Path(hint).name == f"{session_id}.jsonl":
        found = transcript_for(hint, None, session_id)
        if found:
            return found
    for d in transcript_dirs(env):
        direct = d / f"{session_id}.jsonl"
        if direct.is_file():
            return direct
        if d.is_dir():
            for p in sorted(d.glob(f"*/{session_id}.jsonl")):
                return p
    return None


def transcript_cwds(path: Path) -> list[str]:
    """Distinct ``cwd`` values recorded in the transcript, in order of appearance."""
    out: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except (ValueError, AttributeError):
                    continue
                if isinstance(cwd, str) and cwd not in out:
                    out.append(cwd)
    except OSError:
        return out
    return out


def resolve_repo_root(m: dict[str, Any], cwds: list[str]) -> Path | None:
    """The checkout the start hook captured: the first ancestor of a transcript cwd whose
    path hash matches and that still has a ``.git`` entry. The ``.evidence-compiler/``
    marker was checked at capture; a later checkout of that worktree need not carry it."""
    want = m["repo"].get("root_sha256")
    for d in _root_candidates(want, cwds):
        if (d / ".git").exists():
            return d
    return None


def screen_root(m: dict[str, Any], cwds: list[str]) -> tuple[Path | None, str | None]:
    """The root path the screen classifies against, and where it came from.

    The screen needs the captured root only as a path (inside versus outside), so a
    worktree removed after capture still yields it: the path whose hash the start hook
    recorded, recovered from the transcript cwds. ``checkout`` when the checkout still
    exists, ``capture_hash`` when only the recorded hash identifies it. Today's main
    checkout is never substituted."""
    live = resolve_repo_root(m, cwds)
    if live is not None:
        return live, "checkout"
    cands = _root_candidates(m["repo"].get("root_sha256"), cwds)
    return (cands[0], "capture_hash") if cands else (None, None)


def captured_root_gone(m: dict[str, Any], cwds: list[str]) -> bool:
    """True when the captured root is identified among the cwd ancestors but no longer
    exists as a checkout (a removed worktree)."""
    return any(not (d / ".git").exists() for d in _root_candidates(m["repo"].get("root_sha256"), cwds))


def _root_candidates(want: str | None, cwds: list[str]) -> list[Path]:
    out: list[Path] = []
    if not want:
        return out
    for cwd in cwds:
        if not cwd:
            continue
        start = Path(cwd)
        for d in (start, *start.parents):
            if d not in out and root_sha256(d) == want:
                out.append(d)
    return out


def session_roots(cwds: list[str]) -> list[Path]:
    """Every checkout the session's transcript ran in; EC stores packets under these."""
    out: list[Path] = []
    for cwd in cwds:
        root = find_repo_root(cwd)
        if root is not None and root not in out:
            out.append(root)
    return out


def _untracked_paths(stores: Stores, ep: str) -> list[str]:
    p = stores.snapshots / ep / "untracked.json"
    if not p.is_file():
        return []
    try:
        return [e["path"] for e in read_json(p) if isinstance(e, dict) and isinstance(e.get("path"), str)]
    except (OSError, ValueError):
        return []


def _pick_brief(obs: dict[str, Any], m: dict[str, Any]) -> dict[str, Any] | None:
    """The brief injected for the linked packet; the only brief when unlinked."""
    pid = m["link"].get("packet_id")
    if m["link"].get("state") == "linked":
        return next((b for b in obs["briefs"] if b["packet_id"] == pid), None)
    return obs["briefs"][0] if len(obs["briefs"]) == 1 else None


# ---------------------------------------------------------------------- finalize

def finalize(stores: Stores, m: dict[str, Any], transcript: Path, cwds: list[str], *, now: str, by: str,
             t0: float | None = None, env: dict[str, str] | None = None) -> ReviewInputs | None:
    """Join, link, bound, screen and refresh ``m`` in place; the review inputs, or None.

    The episode's end is the ``Stop`` time for the end hook, else the transcript's last
    event inside the turn. Content read here (prompt and brief text) is returned for the
    payload, never written to the manifest."""
    cap = m["capture"]
    errors = [e for e in cap.get("errors", []) if not e.startswith(("join:", "end_", "root:"))]
    m["ended_by"] = None  # recomputed by every join
    misses: list[str] = []
    obs = join(m, transcript, misses)
    if obs is None:
        reason = "join:prompt_ambiguous" if "prompt_ambiguous" in misses else "join:prompt_not_in_transcript"
        cap.update(errors=errors + [reason], status="partial")
        cap["join"] = {"state": "prompt_not_found", "by": by, "at": now}
        mf.refresh(m, (load_owner_review(stores, m["episode_id"]) or {}).get("rules"))
        return None
    turn_end = parse_iso(obs.get("turn_end_ts"))
    if by == "end" and m.get("ended_by") == "stop":
        m["ended_at"] = now
    else:
        m["ended_at"] = iso(turn_end) if turn_end else now
    root = resolve_repo_root(m, cwds)
    m["link"] = resolve_link(m, load_packets(packet_dirs(root, stores, session_roots(cwds))), now)
    brief = _pick_brief(obs, m)
    m["link"].update(brief_sha256=brief["sha256"] if brief else None, brief_len=brief["len"] if brief else None,
                     brief_empty=(brief["len"] == 0) if brief else None)
    m["boundary"] = boundary_for(m, stores, env)
    status = "complete"
    sroot, source = screen_root(m, cwds)
    if sroot is None:
        errors.append("root:unresolved")
        status = "partial"
    else:
        screen(m, obs, sroot, cwd=cwds[-1] if cwds else str(sroot),
               untracked_paths=_untracked_paths(stores, m["episode_id"]), complete=True)
    if cap.get("status") == "failed":
        status = "failed"
    elif any(e.startswith("start_") for e in errors):
        status = "partial"
    if t0 is not None:
        cap["end_ms"] = int((time.perf_counter() - t0) * 1000)
        if cap["end_ms"] > END_BUDGET_S * 1000:
            errors.append("end_over_budget")
            if status == "complete":
                status = "partial"
    cap.update(status=status, errors=errors, phase="ended")
    cap["join"] = {"state": "joined", "by": by, "at": now, "tool_calls": len(obs["tool_calls"]),
                   "subagents": obs.get("subagent_count", 0),
                   "subagents_unresolved": obs.get("subagent_unresolved", 0), "root_source": source}
    mf.refresh(m, (load_owner_review(stores, m["episode_id"]) or {}).get("rules"))
    if root is None:
        return None
    return ReviewInputs(repo_root=root, prompt_text=obs["turn"]["prompt_text"] or "",
                        brief_text=brief["text"] if brief else "",
                        settings={"cli_version": m["boundary"].get("cli_version")})


def inputs_from_transcript(m: dict[str, Any], env: dict[str, str]) -> ReviewInputs | None:
    """Review inputs for an ended episode, re-read from its transcript (maintenance)."""
    if not m.get("ended_at"):
        return None
    tr = find_transcript(m["session_id"], env)
    if tr is None:
        return None
    probe = copy.deepcopy(m)
    obs = join(probe, tr)
    if obs is None:
        return None
    root = resolve_repo_root(m, transcript_cwds(tr))
    if root is None:
        return None
    brief = _pick_brief(obs, m)
    return ReviewInputs(repo_root=root, prompt_text=obs["turn"]["prompt_text"] or "",
                        brief_text=brief["text"] if brief else "",
                        settings={"cli_version": m["boundary"].get("cli_version")})


def _lazy_inputs(stores: Stores, m: dict[str, Any], env: dict[str, str]) -> ReviewInputs | None:
    """Review inputs only when the decision needs them: re-reading a transcript for an
    episode held or excluded on other grounds would be wasted work at every run."""
    probe = copy.deepcopy(m)
    mf.refresh(probe, (load_owner_review(stores, m["episode_id"]) or {}).get("rules"))
    _state, reason, _ = decide(stores, probe, None)
    return inputs_from_transcript(m, env) if reason == "inputs_unavailable" else None


# ---------------------------------------------------------------------- start

def _open_pointer(stores: Stores, session_id: str) -> Path:
    return stores.meta / "open" / session_id


OPEN_POINTER_STALE_H = 72  # a Stop this long after maintenance joined the episode is not expected


def retire_open_pointers(stores: Stores, now: str) -> dict[str, Any]:
    """Remove ``open/`` pointers that only wait for a Stop which will not come.

    A pointer is kept after a maintenance join so a later Stop can replace that provisional
    join. Once the episode has been maintenance-joined for ``OPEN_POINTER_STALE_H`` the
    session is over; the pointer is local operational state and the manifest keeps every
    fact, so retiring it loses no evidence. Episodes never joined stay open and are counted."""
    out: dict[str, Any] = {"retired": [], "open_unjoined": [], "dangling": 0}
    now_dt = parse_iso(now)
    d = stores.meta / "open"
    if now_dt is None or not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        try:
            ep = p.read_text(encoding="utf-8").strip()
            m = read_manifest(stores, ep) if manifest_path(stores, ep).is_file() else None
        except (OSError, ValueError):
            continue
        if m is None:
            out["dangling"] += 1
            continue
        cap = m["capture"]
        joined = parse_iso((cap.get("join") or {}).get("at") or m.get("ended_at") or "")
        if (cap.get("phase") == "ended" and (cap.get("join") or {}).get("by") == "maintain"
                and joined is not None and now_dt - joined > timedelta(hours=OPEN_POINTER_STALE_H)):
            try:
                if p.read_text(encoding="utf-8").strip() == ep:
                    p.unlink()
                    out["retired"].append(ep)
            except OSError:
                pass
        elif cap.get("phase") == "started":
            started = parse_iso(m.get("started_at") or "")
            if started is not None and now_dt - started > timedelta(hours=OPEN_POINTER_STALE_H):
                out["open_unjoined"].append(ep)
    return out


START_SKIPS = "diagnostics/start-skips.jsonl"


def record_start_skip(env: dict[str, str], payload: dict[str, Any], reason: str, now: str | None) -> None:
    """Metadata-only record of a prompt the start hook deliberately did not capture:
    reason, session id, time and a cwd hash. Never the prompt. Never raises."""
    try:
        stores = resolve_stores(env)
        if stores is None:
            return
        sid = payload.get("session_id")
        cwd = payload.get("cwd")
        rec = {"at": now or iso(now_utc()), "reason": reason,
               "session_id": sid if isinstance(sid, str) and SESSION_RE.match(sid) else None,
               "cwd_sha256": sha256_bytes(norm_path(cwd).encode()) if isinstance(cwd, str) and cwd else None}
        path = stores.meta / START_SKIPS
        path.parent.mkdir(parents=True, exist_ok=True)
        append_line(path, json.dumps(rec, sort_keys=True) + "\n")
    except Exception:  # diagnostics must never affect the prompt
        return


def run_start(payload: dict[str, Any], env: dict[str, str], *, now: str | None = None) -> dict[str, Any]:
    """The ``UserPromptSubmit`` side. Returns what happened (for tests and dry mode)."""
    t0 = time.perf_counter()
    if disabled(env):  # the kill switch (and the Twin) means write nothing at all
        return {"action": "disabled"}
    session_id, prompt, cwd = payload.get("session_id"), payload.get("prompt"), payload.get("cwd")
    if not isinstance(session_id, str) or not SESSION_RE.match(session_id) or not isinstance(prompt, str):
        log_line(resolve_stores(env), "start: hook input lacks session_id or prompt")
        return {"action": "bad_input"}
    root = find_repo_root(cwd)
    if root is None:
        record_start_skip(env, payload, "not_enabled", now)
        return {"action": "not_enabled"}
    cfg = load_capture_config(root)
    if not cfg["enabled"]:
        record_start_skip(env, payload, "disabled_by_repo", now)
        return {"action": "disabled_by_repo"}
    stores = resolve_stores(env)
    started_at = now or iso(now_utc())
    psha = sha256_text(prompt)
    ep = make_episode_id(session_id, psha, started_at)
    rec = {"episode_id": ep, "session_id": session_id, "prompt_sha256": psha, "prompt_len": len(prompt),
           "started_at": started_at, "cwd_sha256": sha256_bytes(norm_path(str(cwd)).encode()),
           "root_sha256": root_sha256(root)}
    if stores is None:
        log_line(None, "start: capture stores not configured")
        return _pend_or_lose(None, env, rec, "stores_not_configured")
    problems = store_problems(stores)
    if problems["content"] or problems["meta"]:
        reason = "store_unusable: " + "; ".join(problems["meta"] + problems["content"])
        log_line(stores if stores.meta.is_dir() else None, f"start {ep}: {reason}")
        return _pend_or_lose(stores, env, rec, reason)

    commit, tsha = tool_identity()
    m = mf.new_manifest(episode_id=ep, session_id=session_id, started_at=started_at, prompt_sha256=psha,
                        prompt_len=len(prompt), tool_commit=commit, tool_sha256=tsha, cwd_sha256=rec["cwd_sha256"])
    m["repo"]["root_sha256"] = rec["root_sha256"]
    try:
        m["client"] = ci.persisted(ci.observe(env))
    except Exception as exc:  # noqa: BLE001 — an unidentified client is untrusted, never a failed start
        m["client"] = {"sha256": None, "problem": f"observe_error:{type(exc).__name__}"}
    m["capture"]["phase"] = "begun"
    write_manifest(stores, m)  # a start killed after this line is found by maintenance
    budget = max(0.2, START_BUDGET_MS / 1000 - (time.perf_counter() - t0))
    repo_fields, snap = take_snapshot(root, ep, stores, budget_s=budget)
    m["repo"].update(repo_fields)
    m["start_snapshot"] = snap
    m["capture"]["start_ms"] = int((time.perf_counter() - t0) * 1000)
    if m["capture"]["start_ms"] > START_BUDGET_MS:
        m["capture"]["errors"].append("start_over_budget")
    if cfg.get("error"):
        m["capture"]["errors"].append(f"config:{cfg['error']}")
    m["capture"]["phase"] = "started"
    mf.refresh(m)
    write_manifest(stores, m)
    try:
        _open_pointer(stores, session_id).parent.mkdir(parents=True, exist_ok=True)
        _open_pointer(stores, session_id).write_text(ep + "\n", encoding="utf-8")
    except OSError as exc:
        log_line(stores, f"start {ep}: open pointer not written: {exc}")
    return {"action": "captured", "episode_id": ep, "snapshot": snap["state"], "reason": snap.get("reason"),
            "start_ms": m["capture"]["start_ms"], "stores": stores}


def maintenance_due(stores: Stores) -> bool:
    """Whether the start hook should launch maintenance: nothing holds ``maintain.lock``
    (a leftover file from a dead run is not a holder) and the last pass is old enough."""
    lock = stores.meta / MAINTAIN_LOCK
    if not _try_lock(lock):
        return False
    _unlock(lock)
    p = stores.meta / MAINTENANCE
    try:
        return time.time() - p.stat().st_mtime >= MAINTAIN_MIN_INTERVAL_S
    except OSError:
        return True


def spawn_maintenance(env: dict[str, str]) -> bool:
    """Launch ``capture.py maintain`` detached with no inherited handles; never waits."""
    if env.get(ENV_MAINTAIN) == "0":
        return False
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    try:
        subprocess.Popen([sys.executable, str(PKG / "capture.py"), "maintain", "--quiet"], env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags, start_new_session=os.name != "nt")
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------- end

def _can_finalize(m: dict[str, Any], by: str) -> bool:
    cap = m["capture"]
    if (m.get("review") or {}).get("state") == "materialized":
        return False
    phase = cap.get("phase")
    if phase == "started":
        return True
    # a maintenance join is provisional until the Stop hook's own join replaces it
    return by == "end" and phase == "ended" and (cap.get("join") or {}).get("by") == "maintain"


def _commit_episode(stores: Stores, ep: str, msg: str) -> None:
    commit_meta(stores, [manifest_path(stores, ep), stores.meta / "DELETIONS.log"], msg)


def run_end(payload: dict[str, Any], env: dict[str, str], *, now: str | None = None) -> dict[str, Any]:
    """The ``Stop`` side: finalise and review the session's open episode."""
    t0 = time.perf_counter()
    if disabled(env):
        return {"action": "disabled"}
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not SESSION_RE.match(session_id):
        return {"action": "bad_input"}
    stores = resolve_stores(env)
    if stores is None or not stores.meta.is_dir():
        return {"action": "no_stores"}
    pointer = _open_pointer(stores, session_id)
    if not pointer.is_file():
        return {"action": "no_open_episode"}
    ep = pointer.read_text(encoding="utf-8").strip()
    if not manifest_path(stores, ep).is_file():
        return {"action": "no_open_episode"}
    if not lock_episode(stores, ep, END_LOCK_WAIT_S):
        log_line(stores, f"end {ep}: episode busy; left to maintenance")
        return {"action": "busy", "episode_id": ep}
    try:
        m = read_manifest(stores, ep)
        if not _can_finalize(m, "end"):
            return {"action": "not_open", "episode_id": ep}
        now = now or iso(now_utc())
        tr = find_transcript(session_id, env, payload.get("transcript_path"))
        if tr is None:
            if "join:transcript_missing" not in m["capture"]["errors"]:
                m["capture"]["errors"].append("join:transcript_missing")
            write_manifest(stores, m)
            _commit_episode(stores, ep, f"capture end {ep}: transcript missing")
            return {"action": "transcript_missing", "episode_id": ep}
        cwds = transcript_cwds(tr)
        if isinstance(payload.get("cwd"), str):
            cwds.append(payload["cwd"])
        inputs = finalize(stores, m, tr, cwds, now=now, by="end", t0=t0, env=env)
        write_manifest(stores, m)
        problems = store_problems(stores)
        result: dict[str, Any] = {"action": "ended", "episode_id": ep, "status": m["capture"]["status"],
                                  "link": m["link"]["state"]}
        if problems["content"]:
            log_line(stores, f"end {ep}: review deferred: {'; '.join(problems['content'])}")
            result["review"] = "deferred"
        else:
            res = review_episode(stores, m, inputs, now=now, commit=False)
            result["review"] = res["state"]
            result["review_reason"] = res["reason"]
        m = read_manifest(stores, ep)
        m["capture"]["end_ms"] = int((time.perf_counter() - t0) * 1000)
        write_manifest(stores, m)
        result["end_ms"] = m["capture"]["end_ms"]
        _commit_episode(stores, ep, f"capture end {ep}: {result.get('review')}")
        try:
            if pointer.read_text(encoding="utf-8").strip() == ep:
                pointer.unlink()
        except OSError:
            pass
        return result
    finally:
        _unlock(episode_lock(stores, ep))


# ---------------------------------------------------------------------- maintenance

def _mark_killed(m: dict[str, Any]) -> None:
    m["start_snapshot"] = {**m["start_snapshot"], "state": "failed", "reason": "timeout",
                           "detail": "start hook did not finish (killed or abandoned)"}
    m["capture"].update(status="failed", phase="killed")
    m["capture"]["errors"].append("start_hook_killed")
    m["boundary"] = boundary_for(m)
    mf.refresh(m)


def _turn_over(m: dict[str, Any], manifests: list[dict[str, Any]], transcript: Path | None, now: str) -> bool:
    later = any(o["session_id"] == m["session_id"] and o["started_at"] > m["started_at"] for o in manifests)
    if later:
        return True
    if transcript is None:
        return False
    try:
        return time.time() - transcript.stat().st_mtime >= IDLE_S
    except OSError:
        return False


def _is_capture_failure(m: dict[str, Any]) -> bool:
    return m["capture"].get("status") == "failed" or (
        m["start_snapshot"].get("state") == "failed" and m["start_snapshot"].get("reason") in STREAK_SNAPSHOT_REASONS)


def capture_failure_times(stores: Stores, env: dict[str, str]) -> list[str]:
    """Start times of every capture failure on record: failed manifests and not-yet-drained pending records."""
    times = [m["started_at"] for m in scan_manifests(stores).manifests if _is_capture_failure(m)]
    try:
        for line in _pending_file(env).read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("started_at"), str):
                times.append(rec["started_at"])
    except OSError:
        pass
    return sorted(times)


def failure_streaks(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repos whose latest ``FAILURE_STREAK`` or more episodes are all capture failures."""
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for m in manifests:
        # the checkout's own hash: a start that failed early never learned main_root_sha256
        key = m["repo"].get("root_sha256") or m["repo"].get("main_root_sha256") or "unknown"
        by_repo.setdefault(key, []).append(m)
    flags = []
    for key, ms in sorted(by_repo.items()):
        ms.sort(key=lambda x: x["started_at"])
        run = 0
        for m in reversed(ms):
            if not _is_capture_failure(m):
                break
            run += 1
        if run >= FAILURE_STREAK:
            flags.append({"repo_sha256": key, "consecutive_failures": run, "last_episode": ms[-1]["episode_id"]})
    return flags


def _read_state(stores: Stores) -> dict[str, Any]:
    p = stores.meta / MAINTENANCE
    try:
        s = read_json(p)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def run_maintain(stores: Stores, env: dict[str, str], *, now: str | None = None) -> dict[str, Any]:
    """Everything that may wait for the next start (see the module docstring)."""
    lock = stores.meta / MAINTAIN_LOCK
    if not stores.meta.is_dir() or not _try_lock(lock):
        return {"action": "skipped"}
    now = now or iso(now_utc())
    counts: dict[str, Any] = {"action": "maintained", "killed": 0, "rejoined": 0, "rejoin_expired": 0,
                              "relinked": 0, "reviewed": 0, "review_errors": 0, "pending_drained": 0,
                              "pending_kept": 0, "pending_errors": 0, "tmp_orphans_cleaned": 0,
                              "episode_errors": 0, "busy_skipped": 0, "manifests_unreadable": 0,
                              "meta_commit_failed": False}
    touched: set[Path] = set()
    failed_eps: list[str] = []
    try:
        check = full_store_check(stores)
        content_ok = not check["content"]
        counts["store_problems"] = check["content"] + check["meta"]
        if check["meta"]:
            log_line(stores, f"maintain: metadata store problems: {'; '.join(check['meta'])}")
        if content_ok:
            counts["tmp_orphans_cleaned"] = len(clean_tmp_orphans(stores))
        drain = drain_pending(stores, env)
        counts.update(pending_drained=len(drain["drained"]), pending_kept=drain["kept"],
                      pending_errors=len(drain["errors"]))
        touched.update(manifest_path(stores, ep) for ep in drain["drained"])

        manifests, unreadable = scan_manifests(stores)
        for name in unreadable:
            log_line(stores, f"maintain: manifest unreadable: {name}")
        for m0 in manifests:
            ep = m0["episode_id"]
            if (m0.get("review") or {}).get("state") == "materialized":
                continue
            if not lock_episode(stores, ep):
                counts["busy_skipped"] += 1  # an end hook (or a stale lock) holds it; the next pass retries
                continue
            try:
                m = read_manifest(stores, ep)
                phase = m["capture"].get("phase")
                age = _age_s(m["started_at"], now) or 0.0
                changed = False
                reviewed = False
                if phase == "begun" and age >= KILLED_AFTER_S:
                    _mark_killed(m)
                    counts["killed"] += 1
                    changed = True
                elif phase == "started":
                    tr = find_transcript(m["session_id"], env)
                    if tr is not None and _turn_over(m, manifests, tr, now):
                        inputs = finalize(stores, m, tr, transcript_cwds(tr), now=now, by="maintain", env=env)
                        counts["rejoined"] += 1
                        changed = True
                        if inputs is not None and content_ok:
                            write_manifest(stores, m)
                            res = review_episode(stores, m, inputs, now=now, commit=False)
                            counts["reviewed"] += 1
                            counts["review_errors"] += int(bool(res["error"]))
                            reviewed = True
                            m = read_manifest(stores, ep)
                    elif age >= REJOIN_WINDOW_H * 3600:
                        m["capture"]["errors"].append("join:rejoin_expired")
                        m["capture"].update(status="partial", phase="ended")
                        m["capture"]["join"] = {"state": "rejoin_expired", "by": "maintain", "at": now}
                        mf.refresh(m, (load_owner_review(stores, ep) or {}).get("rules"))
                        counts["rejoin_expired"] += 1
                        changed = True
                if m["capture"].get("phase") in ("ended", "killed", "failed", None) and not reviewed:
                    # terminal now, including episodes killed or expired in this pass
                    if m["boundary"].get("cli_version") and not m["boundary"].get("trusted"):
                        # an owner-accepted boundary record, or a hash cached since, takes effect here
                        new_b = boundary_for(m, stores, env)
                        if new_b != m["boundary"]:
                            m["boundary"] = new_b
                            mf.refresh(m, (load_owner_review(stores, ep) or {}).get("rules"))
                            changed = True
                    if m["link"].get("state") not in mf.FINAL_LINK_STATES and m.get("ended_at"):
                        tr = find_transcript(m["session_id"], env)
                        tcwds = transcript_cwds(tr) if tr else []
                        root = resolve_repo_root(m, tcwds) if tr else None
                        rc = reconcile(stores, [m], {ep: root} if root else None, now=now,
                                       rules_for=lambda e: (load_owner_review(stores, e) or {}).get("rules"),
                                       session_roots=session_roots(tcwds))
                        if rc["changed"]:
                            counts["relinked"] += 1
                            changed = False  # reconcile wrote the manifest, with every change made so far
                    if content_ok:
                        if changed:
                            write_manifest(stores, m)
                            changed = False
                        res = review_episode(stores, m, _lazy_inputs(stores, m, env), now=now, commit=False)
                        counts["reviewed"] += 1
                        counts["review_errors"] += int(bool(res["error"]))
                        m = read_manifest(stores, ep)
                if changed:
                    write_manifest(stores, m)
                if m != m0:
                    touched.add(manifest_path(stores, ep))
            except Exception as exc:  # noqa: BLE001 — one bad episode must not stop the pass or expiry; counted
                counts["episode_errors"] += 1
                failed_eps.append(ep)
                log_line(stores, f"maintain {ep}: {type(exc).__name__}: {exc}")
            finally:
                _unlock(episode_lock(stores, ep))
        counts["episode_error_ids"] = failed_eps[:20]

        if content_ok:
            exp = expire(stores, now=now, commit=False)
            counts.update(expired_deleted=exp["expired_deleted"], expire_errors=exp["errors"],
                          expire_undated=exp["undated"])
        ptr = retire_open_pointers(stores, now)
        counts.update(open_pointers_retired=len(ptr["retired"]), open_unjoined_stale=ptr["open_unjoined"][:20])
        if ptr["retired"]:
            log_line(stores, f"maintain: retired {len(ptr['retired'])} open pointer(s) past their Stop window: "
                             + ",".join(ptr["retired"][:20]))
        after = scan_manifests(stores)
        counts["manifests_unreadable"] = len(set(unreadable) | set(after.unreadable))
        flags = failure_streaks(after.manifests)
        for f in flags:
            log_line(stores, f"flag: {f['consecutive_failures']} consecutive capture failures in repo "
                             f"{f['repo_sha256'][:12]} (last {f['last_episode']})")
        counts["failure_streaks"] = len(flags)
        state = _read_state(stores)
        state.update(last_run=now, runs=int(state.get("runs", 0)) + 1, failure_streaks=flags,
                     tmp_orphans_cleaned=int(state.get("tmp_orphans_cleaned", 0)) + counts["tmp_orphans_cleaned"],
                     store_check={"content": check["content"], "meta": check["meta"]})
        write_json_atomic(stores.meta / MAINTENANCE, state)
        if not commit_meta(stores, [stores.manifests, stores.meta / "DELETIONS.log"], f"capture maintain {now}"):
            counts["meta_commit_failed"] = True  # commit_meta logged the cause
    except Exception as exc:  # noqa: BLE001 — maintenance is fail-open like the hooks
        log_line(stores, f"maintain: {type(exc).__name__}: {exc}")
        counts["error"] = f"{type(exc).__name__}"
    finally:
        _unlock(lock)
    counts["touched"] = len(touched)
    return counts


def maintain_findings(counts: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The problems (failures) and warnings in one ``run_maintain`` result. The backstop and
    ``capture.py maintain`` both judge a pass by this, so neither reports a pass whose
    episodes, pending records, manifests or metadata commit failed as ok."""
    problems: list[str] = []
    warnings: list[str] = []
    if counts.get("error"):
        problems.append(f"maintenance_error: {counts['error']}")
    if counts.get("store_problems"):
        # the hot path refuses captures while these stand; a pass that saw them is not ok
        problems.append("store_problems: " + "; ".join(counts["store_problems"]))
    if counts.get("episode_errors"):
        problems.append(f"episode_errors: {counts['episode_errors']} "
                        f"({','.join(counts.get('episode_error_ids') or [])})")
    if counts.get("manifests_unreadable"):
        problems.append(f"manifests_unreadable: {counts['manifests_unreadable']}")
    if counts.get("pending_errors"):
        problems.append(f"pending_errors: {counts['pending_errors']} (kept {counts.get('pending_kept', 0)})")
    if counts.get("meta_commit_failed"):
        problems.append("metadata_commit_failed")
    if counts.get("expire_errors"):
        problems.append(f"expiry_errors: {counts['expire_errors']}")
    if counts.get("busy_skipped"):
        warnings.append(f"episodes_busy: {counts['busy_skipped']}")
    if counts.get("expire_undated"):
        warnings.append(f"undated_content: {counts['expire_undated']}")
    if counts.get("review_errors"):
        warnings.append(f"review_errors: {counts['review_errors']}")
    if counts.get("failure_streaks"):
        warnings.append(f"capture_failure_streaks: {counts['failure_streaks']}")
    return problems, warnings


def rejoin_deadline(started_at: str) -> str | None:
    dt = parse_iso(started_at)
    return iso(dt + timedelta(hours=REJOIN_WINDOW_H)) if dt else None

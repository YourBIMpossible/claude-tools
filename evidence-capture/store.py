#!/usr/bin/env python3
"""The two capture stores and their guarantees.

Metadata store (a git repository, no remote): manifests, review records, audit files,
checkpoints, ``capture.log`` and ``DELETIONS.log``. Never a byte of repository content.

Content store (NOT a git repository, never synced): ``snapshots/``, ``payloads/`` and
``tmp/``. Its markers ``.git-blocked`` and ``NOSYNC`` must exist, no ``.git`` may sit
above it, and it may not live under a cloud-sync root. Partial writes go to ``tmp/``
on the same volume and are renamed into place, so an entry is either complete or
absent. Deletion is verified (directory gone, every listed path gone) and logged to
the metadata repo; there is no trash directory.

Store locations come from the environment (``EC_CAPTURE_META``, ``EC_CAPTURE_CONTENT``)
or a user config file; the code holds no machine path.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common import iso, now_utc, read_json, sha256_file, write_json_atomic

ENV_META = "EC_CAPTURE_META"
ENV_CONTENT = "EC_CAPTURE_CONTENT"
ENV_PACKET_DIRS = "EC_CAPTURE_PACKET_DIRS"
ENV_DISABLE = "EVIDENCE_CAPTURE"
USER_CONFIG = Path.home() / ".claude" / "evidence-capture.json"
SYNC_ROOT_NAMES = ("onedrive", "dropbox", "icloud", "google drive", "googledrive", "box sync", "box")
CONTENT_DIRS = ("snapshots", "payloads", "tmp")
META_DIRS = ("manifests", "review", "audits", "checkpoints", "boundary")
CONTENT_MARKERS = (".git-blocked", "NOSYNC")
TMP_MAX_AGE_S = 3600
META_GITIGNORE = "# capture content never enters this repository\n*.patch\n*.bin\nsnapshot*\npayload*\n*.tmp\n"


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class Stores:
    meta: Path
    content: Path
    packet_dirs: tuple[Path, ...] = ()

    @property
    def manifests(self) -> Path:
        return self.meta / "manifests"

    @property
    def snapshots(self) -> Path:
        return self.content / "snapshots"

    @property
    def payloads(self) -> Path:
        return self.content / "payloads"

    @property
    def tmp(self) -> Path:
        return self.content / "tmp"


def resolve_stores(env: dict[str, str] | None = None, config: Path = USER_CONFIG) -> Stores | None:
    """Store paths from the environment, else the user config; None when neither says."""
    env = os.environ if env is None else env
    meta, content, packets = env.get(ENV_META), env.get(ENV_CONTENT), env.get(ENV_PACKET_DIRS)
    if not (meta and content) and config.is_file():
        try:
            cfg = read_json(config)
        except (OSError, ValueError):
            cfg = {}
        meta = meta or cfg.get("meta")
        content = content or cfg.get("content")
        packets = packets or os.pathsep.join(cfg.get("packet_dirs") or [])
    if not (meta and content):
        return None
    dirs = tuple(Path(p) for p in (packets or "").split(os.pathsep) if p)
    return Stores(Path(meta), Path(content), dirs)


def init_stores(stores: Stores) -> None:
    """Create the layout and markers; idempotent. Never touches an existing file."""
    for d in META_DIRS:
        (stores.meta / d).mkdir(parents=True, exist_ok=True)
    for name in ("capture.log", "DELETIONS.log"):
        p = stores.meta / name
        if not p.exists():
            p.write_text("", encoding="utf-8")
    gi = stores.meta / ".gitignore"
    if not gi.exists():
        gi.write_text(META_GITIGNORE, encoding="utf-8", newline="\n")
    for d in CONTENT_DIRS:
        (stores.content / d).mkdir(parents=True, exist_ok=True)
    for name in CONTENT_MARKERS:
        p = stores.content / name
        if not p.exists():
            p.write_text("capture content store: not a git repository, never synced\n", encoding="utf-8")
    sentinel = stores.content / "SENTINEL"
    if not sentinel.exists():
        sentinel.write_text("capture-content-sentinel " + os.urandom(8).hex() + "\n", encoding="utf-8")


def _under_git(path: Path) -> bool:
    proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--git-dir"], capture_output=True)
    return proc.returncode == 0


def _under_sync_root(path: Path) -> str | None:
    for part in path.resolve().parts:
        low = part.lower()
        if any(low == n or low.startswith(n + " ") or low.startswith(n + "-") for n in SYNC_ROOT_NAMES):
            return part
    return None


def _windows_attrib_flags(path: Path) -> str | None:
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["attrib", str(path)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.strip().splitlines()[-1] if out.strip() else ""
    flags = line[: max(0, line.find(str(path)[:1]))] if line else ""
    return flags.replace(" ", "")


def check_content_store(stores: Stores) -> list[str]:
    """Failures that forbid writing content. Empty means the store is usable."""
    c = stores.content
    problems: list[str] = []
    if not c.is_dir():
        return ["content store missing"]
    for name in CONTENT_MARKERS:
        if not (c / name).is_file():
            problems.append(f"marker missing: {name}")
    if _under_git(c):
        problems.append("content store is inside a git repository")
    sync = _under_sync_root(c)
    if sync:
        problems.append(f"content store under a sync root: {sync}")
    flags = _windows_attrib_flags(c)
    if flags and any(f in flags for f in ("P", "U", "O")):
        problems.append(f"content store has cloud attributes: {flags}")
    for d in CONTENT_DIRS:
        if not (c / d).is_dir():
            problems.append(f"missing directory: {d}")
    return problems


def check_meta_store(stores: Stores) -> list[str]:
    m = stores.meta
    problems: list[str] = []
    if not m.is_dir():
        return ["metadata store missing"]
    if not _under_git(m):
        problems.append("metadata store is not a git repository")
    else:
        remotes = subprocess.run(["git", "-C", str(m), "remote"], capture_output=True, text=True).stdout
        if remotes.strip():
            problems.append("metadata store has a remote")
    gi = m / ".gitignore"
    if not gi.is_file() or "*.patch" not in gi.read_text(encoding="utf-8"):
        problems.append("metadata .gitignore lacks the content exclusions")
    return problems


def log_line(stores: Stores | None, message: str) -> None:
    """One line to ``capture.log``; falls back to the temp dir; never raises."""
    line = f"{iso(now_utc())} {message}\n"
    targets = []
    if stores is not None:
        targets.append(stores.meta / "capture.log")
    targets.append(Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or ".") / "ec-capture-failures.log")
    for t in targets:
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
            with t.open("a", encoding="utf-8") as fh:
                fh.write(line)
            return
        except OSError:
            continue


def _force_remove(func: Any, path: str, _exc: Any) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path: Path, tries: int = 5, delay: float = 0.2) -> bool:
    for attempt in range(tries):
        try:
            if path.exists():
                shutil.rmtree(path, onerror=_force_remove)
            return True
        except OSError:
            if attempt == tries - 1:
                return not path.exists()
            time.sleep(delay)
    return not path.exists()


def new_tmp_dir(stores: Stores, episode: str) -> Path:
    d = stores.tmp / f"{episode}.{os.getpid()}"
    if d.exists():
        rmtree(d)
    d.mkdir(parents=True)
    return d


def clean_tmp_orphans(stores: Stores, max_age_s: int = TMP_MAX_AGE_S) -> list[str]:
    """Delete ``tmp/`` entries older than ``max_age_s``; return their names."""
    gone: list[str] = []
    if not stores.tmp.is_dir():
        return gone
    cutoff = time.time() - max_age_s
    for entry in sorted(stores.tmp.iterdir()):
        try:
            if entry.stat().st_mtime < cutoff and rmtree(entry):
                gone.append(entry.name)
        except OSError:
            continue
    if gone:
        log_line(stores, f"tmp orphans removed: {','.join(gone)}")
    return gone


def write_sums(directory: Path, skip: tuple[str, ...] = ("SHA256SUMS", "INCOMPLETE")) -> Path:
    """``SHA256SUMS`` over every file under ``directory`` (posix relative paths, sorted)."""
    lines = []
    for f in sorted(p for p in directory.rglob("*") if p.is_file()):
        rel = f.relative_to(directory).as_posix()
        if rel in skip:
            continue
        lines.append(f"{sha256_file(f)}  {rel}")
    out = directory / "SHA256SUMS"
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")
    return out


def read_sums(directory: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    p = directory / "SHA256SUMS"
    if not p.is_file():
        return sums
    for line in p.read_text(encoding="utf-8").splitlines():
        if "  " in line:
            digest, rel = line.split("  ", 1)
            sums[rel] = digest
    return sums


def verify_sums(directory: Path) -> list[str]:
    """Paths whose bytes differ from ``SHA256SUMS`` or are missing; extra files count too."""
    sums = read_sums(directory)
    bad = []
    for rel, digest in sums.items():
        f = directory / rel
        if not f.is_file() or sha256_file(f) != digest:
            bad.append(rel)
    present = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    for rel in sorted(present - set(sums) - {"SHA256SUMS", "INCOMPLETE"}):
        bad.append(rel)
    if (directory / "INCOMPLETE").exists():
        bad.append("INCOMPLETE")
    return bad


def verified_delete(stores: Stores, kind: str, episode: str, reason: str) -> dict[str, Any]:
    """Delete ``<kind>/<episode>`` from the content store, verify it is gone, log it.

    ``kind`` is ``snapshots`` or ``payloads``. The record is appended to
    ``DELETIONS.log`` in the metadata repo. Raises ``StoreError`` when verification fails.
    """
    target = stores.content / kind / episode
    record: dict[str, Any] = {"episode_id": episode, "kind": kind, "reason": reason,
                              "deleted_at": iso(now_utc()), "sums_sha256": None, "verified": False,
                              "listed_paths": 0}
    if not target.exists():
        record["verified"] = True
        record["note"] = "absent"
        _append_deletion(stores, record)
        return record
    sums_file = target / "SHA256SUMS"
    listed = list(read_sums(target))
    record["listed_paths"] = len(listed)
    if sums_file.is_file():
        record["sums_sha256"] = sha256_file(sums_file)
    rmtree(target, tries=10)
    remaining = [rel for rel in listed if (target / rel).exists()]
    record["verified"] = not target.exists() and not remaining
    _append_deletion(stores, record)
    if not record["verified"]:
        raise StoreError(f"deletion of {kind}/{episode} not verified: {remaining or 'directory remains'}")
    return record


def _append_deletion(stores: Stores, record: dict[str, Any]) -> None:
    with (stores.meta / "DELETIONS.log").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def read_deletions(stores: Stores) -> list[dict[str, Any]]:
    p = stores.meta / "DELETIONS.log"
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def manifest_path(stores: Stores, episode: str) -> Path:
    return stores.manifests / f"{episode}.json"


def write_manifest(stores: Stores, manifest: dict[str, Any]) -> Path:
    p = manifest_path(stores, manifest["episode_id"])
    write_json_atomic(p, manifest)
    return p


def read_manifest(stores: Stores, episode: str) -> dict[str, Any]:
    return read_json(manifest_path(stores, episode))


def iter_manifests(stores: Stores) -> list[dict[str, Any]]:
    out = []
    if not stores.manifests.is_dir():
        return out
    for p in sorted(stores.manifests.glob("cap_*.json")):
        try:
            out.append(read_json(p))
        except (OSError, ValueError):
            continue
    return out

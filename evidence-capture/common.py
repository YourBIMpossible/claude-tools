#!/usr/bin/env python3
"""Shared helpers for the capture tool: hashing, canonical JSON, time, git.

Every git call made by capture runs with ``GIT_OPTIONAL_LOCKS=0`` so that read-only
commands (``status``, ``diff``, ``ls-files``) never refresh and rewrite ``.git/index``
behind the bracket. Dates and identities are never overridden here: capture observes a
checkout, it does not commit to one.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
SHA_HEX = 64


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    # Same rule as the Evidence Compiler prompt hash: UTF-8 with surrogatepass, so the
    # capture's prompt hash equals the packet's without importing the compiler.
    return hashlib.sha256((text or "").encode("utf-8", errors="surrogatepass")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write ``obj`` next to ``path`` and rename into place; never a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def plus_days(value: str, days: int) -> str:
    dt = parse_iso(value)
    if dt is None:
        raise ValueError(f"not a timestamp: {value!r}")
    return iso(dt + timedelta(days=days))


def norm_path(path: str | Path) -> str:
    return str(path).replace("\\", "/").rstrip("/")


def git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    return env


def git_bytes(repo: Path, *args: str, check: bool = True, timeout: float | None = None,
              env: dict[str, str] | None = None) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout,
                          env=env or git_env())
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: "
                           f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def git(repo: Path, *args: str, check: bool = True, timeout: float | None = None,
        env: dict[str, str] | None = None) -> str:
    return git_bytes(repo, *args, check=check, timeout=timeout, env=env).decode("utf-8", "replace")


def git_ok(repo: Path, *args: str, timeout: float | None = None) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=timeout,
                          env=git_env())
    return proc.returncode == 0


def episode_id(session_id: str, prompt_sha256: str, started_at: str) -> str:
    return "cap_" + hashlib.sha256(f"{session_id}|{prompt_sha256}|{started_at}".encode()).hexdigest()[:16]


def path_sha256(rel: str) -> str:
    return sha256_bytes(rel.replace("\\", "/").encode("utf-8"))

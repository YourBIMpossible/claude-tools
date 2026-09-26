"""The identity of the client process a hook runs under: executable hash, version, mode.

A boundary record (``boundary/<cli version>--<mode>.json``) is evidence for exactly the
client that was tested: that executable (SHA-256), that version and that invocation
mode. The CLI updates itself, and one executable runs in several modes (the desktop
client's streaming mode, headless print mode, the terminal UI), each with its own hook
path, so an episode's boundary is trusted only when all three match an accepted record.
Anything else fails closed for replay eligibility; capture itself and the client are
unaffected.

What is read, and what never is:

- the client's process id from ``CLAUDE_PID`` and that process's image path from the
  operating system (on Windows ``QueryFullProcessImageNameW``, elsewhere
  ``/proc/<pid>/exe``);
- ``CLAUDE_CODE_EXECPATH``, only to cross-check that image path;
- the invocation mode from ``CLAUDE_CODE_ENTRYPOINT``, and the host's version from
  ``CLAUDE_CODE_DESKTOP_APP_VERSION`` (recorded, not matched);
- never a command line, argument value, token, socket or any other variable.

The start hook only observes (a process query and a ``stat``). Hashing the executable
happens at the end hook or in maintenance, once per executable, through a cache in the
metadata store keyed by path, size and modification time, with the ``stat`` re-checked
after hashing. Manifests store a hash of the path, never the path.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

from common import read_json, sha256_bytes, write_json_atomic

ENV_PID = "CLAUDE_PID"
ENV_EXECPATH = "CLAUDE_CODE_EXECPATH"
ENV_MODE = "CLAUDE_CODE_ENTRYPOINT"
ENV_HOST_VERSION = "CLAUDE_CODE_DESKTOP_APP_VERSION"
CACHE_NAME = "identity-cache.json"
MODE_UNSET = "unset"
MODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHUNK = 1 << 20


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def path_sha256(p: str) -> str:
    return sha256_bytes(_norm(p).encode("utf-8"))


def pid_image(pid: int) -> str | None:
    """The executable image of a running process, or None."""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return None
            return buf.value or None
        finally:
            k32.CloseHandle(handle)
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def mode_of(env: dict[str, str]) -> str | None:
    """The invocation mode, or None when the variable holds something unexpected."""
    raw = env.get(ENV_MODE)
    if raw is None or raw == "":
        return MODE_UNSET
    return raw if MODE_RE.match(raw) else None


def observe(env: dict[str, str]) -> dict[str, Any]:
    """What the start hook records: cheap, no hashing, no command line.

    ``problem`` is set when the client cannot be identified; such an episode is never
    trusted. ``_path`` is for the calling process only and is never persisted."""
    obs: dict[str, Any] = {"path_sha256": None, "size": None, "mtime_ns": None, "mode": mode_of(env),
                           "host_version": (env.get(ENV_HOST_VERSION) or None), "sha256": None, "problem": None}
    host = obs["host_version"]
    if host is not None and not MODE_RE.match(host):
        obs["host_version"] = None
    if obs["mode"] is None:
        obs["problem"] = "mode_malformed"
        return obs
    raw_pid = env.get(ENV_PID, "")
    if not raw_pid.isdigit():
        obs["problem"] = "pid_missing"
        return obs
    image = pid_image(int(raw_pid))
    if not image:
        obs["problem"] = "pid_image_unavailable"
        return obs
    declared = env.get(ENV_EXECPATH)
    if declared and _norm(declared) != _norm(image):
        obs["problem"] = "execpath_mismatch"
        return obs
    try:
        st = os.stat(image)
    except OSError:
        obs["problem"] = "exe_unreadable"
        return obs
    obs.update(path_sha256=path_sha256(image), size=st.st_size, mtime_ns=st.st_mtime_ns, _path=image)
    return obs


def persisted(obs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obs.items() if not k.startswith("_")}


def hash_file(path: str) -> tuple[str, int, int] | None:
    """SHA-256 of a file whose ``stat`` did not change while it was read, or None."""
    try:
        before = os.stat(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                h.update(block)
        after = os.stat(path)
    except OSError:
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    return h.hexdigest(), after.st_size, after.st_mtime_ns


def _cache_key(obs: dict[str, Any]) -> str:
    return f"{obs['path_sha256']}|{obs['size']}|{obs['mtime_ns']}"


def cache_lookup(cache: Path | None, obs: dict[str, Any]) -> str | None:
    if cache is None or not obs.get("path_sha256"):
        return None
    try:
        doc = read_json(cache) if cache.is_file() else {}
    except (OSError, ValueError):
        return None
    val = (doc.get("entries") or {}).get(_cache_key(obs))
    return val if isinstance(val, str) and re.fullmatch(r"[0-9a-f]{64}", val) else None


def cache_store(cache: Path | None, obs: dict[str, Any], sha: str) -> None:
    if cache is None:
        return
    try:
        doc = read_json(cache) if cache.is_file() else {}
    except (OSError, ValueError):
        doc = {}
    entries = dict(doc.get("entries") or {})
    entries[_cache_key(obs)] = sha
    try:
        write_json_atomic(cache, {"entries": dict(list(entries.items())[-50:])})
    except OSError:
        pass


def resolve(start: dict[str, Any] | None, env: dict[str, str] | None, cache: Path | None) -> dict[str, Any]:
    """The start observation with ``sha256`` filled in, or ``problem`` set.

    The executable is hashed only when the end hook runs under the same client (same
    path, size and modification time as at the start). Otherwise only the cache can
    supply the hash; without it the identity is ``unverifiable``."""
    if not start:
        return {"sha256": None, "problem": "not_observed"}
    obs = persisted(start)
    if obs.get("problem") or obs.get("sha256"):
        return obs
    sha = cache_lookup(cache, obs)
    if sha:
        return {**obs, "sha256": sha}
    now = observe(env) if env is not None else None
    if not now or now.get("problem") or now.get("path_sha256") != obs.get("path_sha256"):
        return {**obs, "problem": "unverifiable"}
    if (now["size"], now["mtime_ns"]) != (obs.get("size"), obs.get("mtime_ns")):
        return {**obs, "problem": "exe_changed"}
    got = hash_file(now["_path"])
    if got is None or (got[1], got[2]) != (obs["size"], obs["mtime_ns"]):
        return {**obs, "problem": "exe_changed"}
    cache_store(cache, obs, got[0])
    return {**obs, "sha256": got[0]}


def tested_identity(cli: Path, version: str, observed: list[dict[str, Any]]) -> dict[str, Any]:
    """A harness's tested identity: the ``--cli`` executable's hash and version, and the
    mode every probe-hook observation agreed on. ``problem`` is set when the probe hooks
    saw no mode, disagreed, or ran under a different executable."""
    got = hash_file(str(cli))
    ident: dict[str, Any] = {"sha256": got[0] if got else None, "cli_version": version, "mode": None,
                             "host_versions": sorted({o.get("host_version") or "" for o in observed} - {""}),
                             "observations": len(observed), "problem": None}
    if got is None:
        ident["problem"] = "cli_unreadable"
        return ident
    modes = {o.get("mode") for o in observed}
    paths = {o.get("path_sha256") for o in observed}
    problems = sorted({str(o["problem"]) for o in observed if o.get("problem")})
    if not observed:
        ident["problem"] = "not_observed"
    elif problems:
        ident["problem"] = "observation:" + ",".join(problems)
    elif len(modes) != 1 or None in modes:
        ident["problem"] = "mode_inconsistent"
    elif paths != {path_sha256(str(cli.resolve()))}:
        ident["problem"] = "hooks_ran_under_other_executable"
    else:
        ident["mode"] = modes.pop()
    return ident


def record_name(cli_version: str, mode: str) -> str:
    return f"{cli_version}--{mode}.json"


def main() -> int:
    """Print this process's client observation (no path, no hash): a probe for hooks."""
    import json
    print(json.dumps(persisted(observe(dict(os.environ))), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

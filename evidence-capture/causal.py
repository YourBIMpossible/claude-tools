#!/usr/bin/env python3
"""Causal machinery for twin replay (plan Phase D3).

The start snapshot (``snapshot.py``) records how a turn began. Attributing an outcome
to the brief also needs how it ended, a store that replays without the source
repository, and proof that nothing else moved. This module adds those pieces:

* ``capture_state``      immutable, verified worktree state (start or end): every
                         tracked and untracked unignored path as raw bytes, read twice
                         (any difference is ``concurrent_change``), content-addressed by
                         SHA-256, sealed with ``SHA256SUMS`` and made read-only
* ``capture_overlay``    the hook-time form: HEAD plus the exact bytes of every path git
                         reports as differing from it, sealed the same way;
                         ``effective_oids`` compares two of them, ``materialize_overlay``
                         rebuilds one from frozen history
* ``freeze_history``     a self-contained pack of every object reachable from HEAD,
                         verified by indexing it into an empty repository (no
                         alternates, no reference to the source)
* ``materialize``        raw-byte restore of a state; ``neutralize_attributes`` and
                         ``RAW_GIT_ARGS`` keep git's eol/filter/textconv machinery off
                         any replay clone
* ``unsupported_state``  submodules, LFS, sparse checkout, shallow history and
                         in-progress operations: the episode is ineligible, with reasons
* ``attribute``          every start->end change must be one the agent wrote; other
                         worktrees of the same repository must not move during the turn
* ``dependency_fingerprint`` / ``dependency_drift``  lockfile hashes and tool versions
* ``load_registry`` / ``run_checks``  per-task outcome checks, run against a
                         materialized end state, never against the live checkout
* ``admit``              the gate combining all of the above

Nothing here writes to the source repository: reads use ``GIT_OPTIONAL_LOCKS=0`` and
plumbing commands only. Synthetic tests: ``tests/test_causal.py``.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from common import git_bytes, git_env, sha256_bytes
from store import verify_sums, write_sums

STATE_VERSION = 1
STATE_FILES = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG",
               "rebase-merge", "rebase-apply")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
LOCKFILES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Pipfile.lock", "uv.lock", "requirements.txt", "pyproject.toml", "Cargo.lock", "go.sum",
    "Gemfile.lock", "composer.lock", "packages.lock.json", "Directory.Packages.props",
    "global.json", ".python-version", ".nvmrc", ".tool-versions",
})
DEFAULT_TOOLS: dict[str, list[str]] = {
    "python": [sys.executable, "--version"],
    "git": ["git", "--version"],
    "node": ["node", "--version"],
    "dotnet": ["dotnet", "--version"],
}
RAW_GIT_ARGS = ("-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.safecrlf=false",
                "-c", f"core.attributesFile={os.devnull}", "-c", "diff.noprefix=false")
NEUTRAL_ATTRIBUTES = b"* -text !eol -filter -ident -diff -merge -working-tree-encoding -export-subst\n"
CHECK_KINDS = ("file_sha256", "file_absent", "file_contains", "command")


class CausalError(RuntimeError):
    """A state cannot be captured or verified; the reason is the message."""


# ------------------------------------------------------------------ state capture


def _git(repo: Path, *args: str, timeout: float = 60.0) -> bytes:
    return git_bytes(repo, *args, timeout=timeout)


def _gitdirs(repo: Path) -> tuple[Path, Path]:
    raw = _git(repo, "rev-parse", "--absolute-git-dir", "--git-common-dir").decode().splitlines()
    gitdir = Path(raw[0])
    common = Path(raw[1]) if os.path.isabs(raw[1]) else (repo / raw[1]).resolve()
    return gitdir, common


def list_paths(repo: Path) -> list[str]:
    """Tracked plus untracked-unignored paths, repository-relative, sorted."""
    raw = _git(repo, "ls-files", "-z", "-c", "-o", "--exclude-standard", "--deduplicate")
    return sorted({p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p})


def _read_entry(top: Path, rel: str) -> tuple[dict[str, Any], bytes] | None:
    full = top / rel
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        data = os.readlink(full).encode("utf-8", "surrogateescape")
        mode = "120000"
    elif stat.S_ISREG(st.st_mode):
        data = full.read_bytes()
        mode = "100755" if st.st_mode & stat.S_IXUSR and os.name != "nt" else "100644"
    else:
        return None  # a directory reported by ls-files is a nested repository (gitlink)
    return {"sha256": sha256_bytes(data), "size": len(data), "mode": mode}, data


def _scan(top: Path) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    files: dict[str, dict[str, Any]] = {}
    blobs: dict[str, bytes] = {}
    for rel in list_paths(top):
        got = _read_entry(top, rel)
        if got is None:
            continue
        files[rel] = got[0]
        blobs[got[0]["sha256"]] = got[1]
    return files, blobs


def byte_manifest(top: Path, *, deadline: float | None = None, timeout: float = 60.0) -> dict[str, str] | None:
    """``{path: sha256(raw bytes)}`` over every tracked and untracked-unignored path present
    in the worktree (symlinks hashed as their target text). ``None`` when ``deadline``
    (``time.monotonic``) passes first: fidelity is then unproven, never approximated."""
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    paths = list_paths(top)

    def one(rel: str) -> tuple[str, str] | None:
        if deadline is not None and _time.monotonic() > deadline:
            raise TimeoutError
        got = _read_entry(top, rel)
        return None if got is None else (rel, got[0]["sha256"])

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(one, paths))
    except TimeoutError:
        return None
    return dict(r for r in rows if r is not None)


def manifest_digest(manifest: dict[str, str]) -> str:
    return sha256_bytes(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def verify_fidelity(state_dest: Path, out: Path) -> list[str]:
    """Problems if the materialized ``out`` is not byte-identical, path for path, to the
    worktree at capture time (``state.json`` ``bytes``). No manifest is a problem."""
    rec = read_state(state_dest)
    want = rec.get("bytes")
    if not isinstance(want, dict):
        return [f"fidelity_unproven:{rec.get('bytes_problem') or 'no_byte_manifest'}"]
    if rec.get("bytes_sha256") != manifest_digest(want):
        return ["fidelity_manifest_corrupt"]
    got = byte_manifest(out) or {}
    missing = [p for p in want if p not in got]
    extra = [p for p in got if p not in want]
    differ = [p for p in want if p in got and got[p] != want[p]]
    probs = []
    if missing:
        probs.append(f"fidelity_missing:{len(missing)}")
    if extra:
        probs.append(f"fidelity_extra:{len(extra)}")
    if differ:
        probs.append(f"fidelity_bytes_differ:{len(differ)}")
    return probs


def _make_read_only(directory: Path) -> None:
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_file():
            os.chmod(path, stat.S_IREAD)


def capture_state(repo: Path, dest: Path, *, label: str) -> dict[str, Any]:
    """Write a sealed, read-only state of ``repo`` to ``dest`` (which must not exist)."""
    if dest.exists():
        raise CausalError("state exists; never overwritten")
    top = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip())
    head_before = _git(top, "rev-parse", "HEAD").decode().strip()
    files, blobs = _scan(top)
    again, _ = _scan(top)
    head_after = _git(top, "rev-parse", "HEAD").decode().strip()
    if again != files or head_after != head_before:
        raise CausalError("concurrent_change")
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        raise CausalError("stale temporary state present")
    (tmp / "objects").mkdir(parents=True)
    for digest, data in blobs.items():
        (tmp / "objects" / digest).write_bytes(data)
    record = {"version": STATE_VERSION, "label": label, "head": head_before, "files": files}
    (tmp / "state.json").write_bytes(json.dumps(record, sort_keys=True, indent=1).encode("utf-8"))
    write_sums(tmp)
    problems = verify_state(tmp)
    if problems:
        raise CausalError("verify failed: " + "; ".join(problems[:3]))
    os.replace(tmp, dest)
    _make_read_only(dest)
    return record


def verify_state(dest: Path) -> list[str]:
    """Problems with a stored state; empty means intact."""
    if not (dest / "state.json").is_file():
        return ["state.json missing"]
    problems = list(verify_sums(dest))
    record = read_state(dest)
    for rel, entry in record["files"].items():
        obj = dest / "objects" / entry["sha256"]
        if not obj.is_file():
            problems.append(f"object missing for {rel}")
        elif sha256_bytes(obj.read_bytes()) != entry["sha256"]:
            problems.append(f"object corrupt for {rel}")
    return problems


def read_state(dest: Path) -> dict[str, Any]:
    return json.loads((dest / "state.json").read_bytes())


def materialize(dest: Path, out: Path) -> None:
    """Restore a state's exact bytes into an empty ``out``. Symlinks fail closed."""
    problems = verify_state(dest)
    if problems:
        raise CausalError("state not intact: " + "; ".join(problems[:3]))
    if out.exists() and any(out.iterdir()):
        raise CausalError("materialize target not empty")
    for rel, entry in read_state(dest)["files"].items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (dest / "objects" / entry["sha256"]).read_bytes()
        if entry["mode"] == "120000":
            try:
                os.symlink(data.decode("utf-8", "surrogateescape"), target)
            except OSError as exc:
                raise CausalError(f"symlink not creatable: {rel}") from exc
        else:
            target.write_bytes(data)
            if entry["mode"] == "100755":
                os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR)


def neutralize_attributes(gitdir: Path) -> None:
    """Make a replay clone's git ops byte-exact: ``info/attributes`` outranks .gitattributes."""
    info = gitdir / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_bytes(NEUTRAL_ATTRIBUTES)


# ------------------------------------------------------------------ hook-time overlay states
#
# A full ``capture_state`` reads every file twice: seconds on a large checkout, too slow for
# the prompt hook. The hooks instead seal an *overlay*: HEAD plus the bytes of every path
# git reports as differing from it (tracked diffs, staged or not, and untracked-unignored
# files), plus the tracked paths that are gone. Overlay bytes are raw and exact. Paths
# outside the overlay are git-equivalent to HEAD: replay checks them out of the frozen
# history under the recorded conversion settings (``core.autocrlf``/``core.eol``), so they
# match the original up to git's own line-ending conversion, which git itself reports as
# "unchanged". Every entry also carries its git blob id (``oid``, cleaned as git would) so
# that start and end compare in git's terms.

OVERLAY_VERSION = 1
OVERLAY_MAX_FILES = 2000
CONVERSION_KEYS = ("core.autocrlf", "core.eol", "core.safecrlf")


def _blob_oid(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 — git's object id


def _overlay_candidates(top: Path, timeout: float) -> tuple[list[str], set[str]]:
    """(paths differing from HEAD or untracked, the subset tracked in HEAD)."""
    diff = _git(top, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "HEAD", timeout=timeout)
    untracked = _git(top, "ls-files", "-z", "-o", "--exclude-standard", timeout=timeout)
    tracked = {p.decode("utf-8", "surrogateescape") for p in diff.split(b"\0") if p}
    others = {p.decode("utf-8", "surrogateescape") for p in untracked.split(b"\0") if p}
    return sorted(tracked | others), tracked


def _conversion(top: Path) -> dict[str, str | None]:
    """The effective (last-wins) value of each ``CONVERSION_KEYS`` setting, or None.
    Raises CausalError when git cannot read the configuration (rc 1 is "no match")."""
    proc = subprocess.run(["git", "-C", str(top), "config", "-z", "--get-regexp", r"^core\.(autocrlf|eol|safecrlf)$"],
                          capture_output=True, env=git_env(), timeout=10)
    if proc.returncode not in (0, 1):
        raise CausalError(f"git_config_unreadable: rc {proc.returncode}")
    out: dict[str, str | None] = dict.fromkeys(CONVERSION_KEYS)
    for rec in proc.stdout.decode("utf-8", "replace").split("\0"):
        key, _, value = rec.partition("\n")
        if key.lower() in out:
            out[key.lower()] = value
    return out


def _clean_oids(top: Path, rels: list[str], timeout: float) -> list[str]:
    if not rels:
        return []
    proc = subprocess.run(["git", "-C", str(top), "hash-object", "--stdin-paths"],
                          input="\n".join(rels).encode("utf-8", "surrogateescape") + b"\n",
                          capture_output=True, env=git_env(), timeout=timeout)
    if proc.returncode != 0:
        raise CausalError("hash-object failed")
    oids = proc.stdout.decode().split()
    if len(oids) != len(rels):
        raise CausalError("hash-object count mismatch")
    return oids


def _read_overlay(top: Path, timeout: float, *, oids: bool = True) -> tuple[dict[str, Any], dict[str, bytes]]:
    paths, tracked = _overlay_candidates(top, timeout)
    if len(paths) > OVERLAY_MAX_FILES:
        raise CausalError(f"overlay_too_large:{len(paths)}")
    files: dict[str, dict[str, Any]] = {}
    blobs: dict[str, bytes] = {}
    deleted: list[str] = []
    for rel in paths:
        got = _read_entry(top, rel)
        if got is None:
            if rel not in tracked:
                raise CausalError("concurrent_change")  # an untracked file vanished mid-read
            deleted.append(rel)
            continue
        files[rel] = dict(got[0])
        blobs[got[0]["sha256"]] = got[1]
    if not oids:
        return {"files": files, "deleted": deleted}, blobs
    regular = [r for r in files if files[r]["mode"] != "120000"]
    for rel, oid in zip(regular, _clean_oids(top, regular, timeout), strict=True):
        files[rel]["oid"] = oid
    for rel, entry in files.items():
        if entry["mode"] == "120000":
            entry["oid"] = _blob_oid(blobs[entry["sha256"]])
    return {"files": files, "deleted": deleted}, blobs


def _seal(dest: Path, record: dict[str, Any], blobs: dict[str, bytes]) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        raise CausalError("stale temporary state present")
    (tmp / "objects").mkdir(parents=True)
    for digest, data in blobs.items():
        (tmp / "objects" / digest).write_bytes(data)
    (tmp / "state.json").write_bytes(json.dumps(record, sort_keys=True, indent=1).encode("utf-8"))
    write_sums(tmp)
    problems = verify_state(tmp)
    if problems:
        raise CausalError("verify failed: " + "; ".join(problems[:3]))
    os.replace(tmp, dest)
    _make_read_only(dest)


def capture_overlay(repo: Path, dest: Path, *, label: str, timeout: float = 10.0,
                    bytes_deadline: float | None = None) -> dict[str, Any]:
    """Seal HEAD + overlay to ``dest`` (which must not exist). Read twice; any change is
    ``concurrent_change``. Writes nothing to the repository (``GIT_OPTIONAL_LOCKS=0``).

    Between the two reads it also records ``bytes``, the raw-byte manifest of the whole
    worktree (``byte_manifest``), so a restore can be proven byte-exact rather than merely
    git-equivalent; past ``bytes_deadline`` it records ``bytes: None`` with the reason."""
    if dest.exists():
        raise CausalError("state exists; never overwritten")
    top_raw, head = _git(repo, "rev-parse", "--show-toplevel", "HEAD", timeout=timeout).decode().splitlines()  # top may contain spaces
    top = Path(top_raw)
    first, blobs = _read_overlay(top, timeout)  # with oids (git reads the files once more)
    manifest = byte_manifest(top, deadline=bytes_deadline, timeout=timeout)
    second, _ = _read_overlay(top, timeout, oids=False)
    third = {rel: got[0] for rel in first["files"] if (got := _read_entry(top, rel)) is not None}
    plain = {rel: {k: v for k, v in e.items() if k != "oid"} for rel, e in first["files"].items()}
    if (plain != second["files"] or first["deleted"] != second["deleted"] or third != second["files"]
            or _git(top, "rev-parse", "HEAD", timeout=timeout).decode().strip() != head):
        raise CausalError("concurrent_change")
    record = {"version": OVERLAY_VERSION, "kind": "overlay", "label": label, "head": head,
              "conversion": _conversion(top), **first}
    if manifest is not None:
        for rel, entry in first["files"].items():
            if manifest.get(rel) != entry["sha256"]:
                raise CausalError("concurrent_change")
        record.update(bytes=manifest, bytes_sha256=manifest_digest(manifest), bytes_problem=None)
    else:
        record.update(bytes=None, bytes_sha256=None, bytes_problem="budget")
    _seal(dest, record, blobs)
    return record


def _head_oids(top: Path, head: str, timeout: float) -> dict[str, str]:
    raw = _git(top, "ls-tree", "-r", "-z", "--full-tree", head, timeout=timeout)
    out: dict[str, str] = {}
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        parts = meta.split()
        if parts[1] == b"blob":
            out[path.decode("utf-8", "surrogateescape")] = parts[2].decode()
    return out


def effective_oids(top: Path, start: dict[str, Any], end: dict[str, Any],
                   timeout: float = 30.0) -> tuple[dict[str, Any], dict[str, Any]]:
    """``{"head", "files": {path: oid}}`` for start and end over every path either overlay or
    a HEAD move touches: the shape ``attribute`` and ``changed_paths`` compare."""
    head_s, head_e = _head_oids(top, start["head"], timeout), _head_oids(top, end["head"], timeout)
    paths = set(start["files"]) | set(start["deleted"]) | set(end["files"]) | set(end["deleted"])
    paths |= {p for p in set(head_s) | set(head_e) if head_s.get(p) != head_e.get(p)}

    def at(rec: dict[str, Any], head: dict[str, str]) -> dict[str, Any]:
        files: dict[str, Any] = {}
        for p in paths:
            if p in rec["files"]:
                files[p] = rec["files"][p]["oid"]
            elif p not in rec["deleted"] and p in head:
                files[p] = head[p]
        return {"head": rec["head"], "files": files}

    return at(start, head_s), at(end, head_e)


def lockfile_oids(state: dict[str, Any]) -> dict[str, str]:
    """Lockfile entries of an ``effective_oids`` state (only paths that moved are present)."""
    return {p: o for p, o in state["files"].items() if p.rsplit("/", 1)[-1] in LOCKFILES}


HOOK_TOOLS: dict[str, list[str]] = {"git": ["git", "--version"], "node": ["node", "--version"],
                                    "dotnet": ["dotnet", "--version"]}


def tool_versions_async(tools: dict[str, list[str]] | None = None) -> dict[str, subprocess.Popen[bytes] | str]:
    """Start every version probe at once; ``collect_tool_versions`` reaps them."""
    out: dict[str, subprocess.Popen[bytes] | str] = {"python": sys.version.split()[0]}
    for name, argv in sorted((tools or HOOK_TOOLS).items()):
        try:
            out[name] = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            out[name] = "absent"
    return out


def collect_tool_versions(pending: dict[str, subprocess.Popen[bytes] | str], timeout: float) -> dict[str, str]:
    import time as _time
    deadline = _time.monotonic() + timeout
    out: dict[str, str] = {}
    for name, proc in pending.items():
        if isinstance(proc, str):
            out[name] = proc
            continue
        try:
            so, se = proc.communicate(timeout=max(0.0, deadline - _time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            out[name] = "unrecorded:timeout"
            continue
        text = (so or se).decode("utf-8", "replace").strip()
        out[name] = text.splitlines()[0] if proc.returncode == 0 and text else f"error:{proc.returncode}"
    return out


def tool_drift(start: dict[str, str], end: dict[str, str]) -> list[str]:
    """A changed or unrecorded version is drift: an unknown is never assumed equal."""
    drift = []
    for name in sorted(set(start) | set(end)):
        a, b = start.get(name), end.get(name)
        if a is None or b is None or a.startswith("unrecorded") or b.startswith("unrecorded") or a != b:
            drift.append(f"tool:{name}")
    return drift


def materialize_overlay(state_dest: Path, history_dest: Path, out: Path) -> list[str]:
    """Rebuild a hook-time state from frozen history + overlay into an empty ``out`` (a git
    checkout on branch ``replay``). Returns problems; empty means git sees exactly the
    recorded overlay as differing from HEAD."""
    problems = verify_state(state_dest) + verify_history(history_dest)
    if problems:
        return problems
    rec = read_state(state_dest)
    if out.exists() and any(out.iterdir()):
        return ["materialize target not empty"]
    env = git_env()
    conv = [a for k, v in rec["conversion"].items() if v is not None for a in ("-c", f"{k}={v}")]
    with tempfile.TemporaryDirectory(prefix="ec-mat-") as tmp:
        bare = Path(tmp) / "h.git"
        restore_history(history_dest, bare)
        pin = subprocess.run(["git", "-C", str(bare), "update-ref", "refs/heads/replay", rec["head"]],
                             capture_output=True, env=env)  # the state's HEAD, an ancestor of the frozen head
        if pin.returncode != 0:
            return ["state head not in frozen history"]
        proc = subprocess.run(["git", *conv, "-c", "core.longpaths=true", "clone", "-q", "--no-hardlinks", "-b",
                               "replay", str(bare), str(out)], capture_output=True, env=env)
        if proc.returncode != 0:
            return ["clone failed: " + proc.stderr.decode("utf-8", "replace").strip()[:200]]
    for k, v in rec["conversion"].items():
        if v is not None:
            subprocess.run(["git", "-C", str(out), "config", k, v], check=True, env=env, capture_output=True)
    subprocess.run(["git", "-C", str(out), "remote", "remove", "origin"], env=env, capture_output=True)
    for rel in rec["deleted"]:
        (out / rel).unlink(missing_ok=True)
    for rel, entry in rec["files"].items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (state_dest / "objects" / entry["sha256"]).read_bytes()
        if entry["mode"] == "120000":
            return [f"symlink not reproducible: {rel}"]
        target.write_bytes(data)
    _restore_raw_checkout_bytes(out, rec)
    paths, _ = _overlay_candidates(out, 60.0)
    expected = sorted(set(rec["files"]) | set(rec["deleted"]))
    if paths != expected:
        problems.append(f"overlay mismatch: {len(set(paths) ^ set(expected))} path(s)")
    return problems


def _restore_raw_checkout_bytes(out: Path, rec: dict[str, Any]) -> None:
    """A clean tracked file can hold bytes a fresh checkout would not write: it was checked
    out before the recorded conversion config was set (an LF file under ``autocrlf=true``),
    which git still calls clean. For each base path whose checked-out bytes miss the
    capture-time sha256, write the unfiltered blob instead when THAT matches the sha256.
    Nothing is guessed: a path matching neither stays different and ``verify_fidelity``
    reports it."""
    want = rec.get("bytes")
    if not isinstance(want, dict):
        return
    got = byte_manifest(out) or {}
    stale = [p for p, h in want.items() if p not in rec["files"] and p in got and got[p] != h]
    if not stale:
        return
    oids = _head_oids(out, rec["head"], 60.0)
    stale = [p for p in stale if p in oids]
    proc = subprocess.run(["git", "-C", str(out), "cat-file", "--batch"], capture_output=True, env=git_env(),
                          input="".join(f"{oids[p]}\n" for p in stale).encode(), timeout=120)
    buf, pos = proc.stdout, 0
    for rel in stale:
        nl = buf.index(b"\n", pos)
        head = buf[pos:nl].split()
        if len(head) != 3:  # "<oid> missing": leave it for verify_fidelity to report
            pos = nl + 1
            continue
        size = int(head[2])
        data = buf[nl + 1:nl + 1 + size]
        pos = nl + 2 + size
        if sha256_bytes(data) == want[rel]:
            (out / rel).write_bytes(data)


# ------------------------------------------------------------------ history store


def freeze_history(repo: Path, dest: Path, *, head: str | None = None) -> dict[str, Any]:
    """Pack every object reachable from ``head`` (default HEAD) into ``dest/history.pack``;
    verify standalone."""
    if dest.exists():
        raise CausalError("history exists; never overwritten")
    head = _git(repo, "rev-parse", "--verify", f"{head or 'HEAD'}^{{commit}}").decode().strip()
    proc = subprocess.run(["git", "-C", str(repo), "pack-objects", "--revs", "--stdout", "-q"],
                          input=f"{head}\n".encode(), capture_output=True, env=git_env(), timeout=600)
    if proc.returncode != 0:
        raise CausalError("pack-objects failed: " + proc.stderr.decode("utf-8", "replace").strip())
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.mkdir(parents=True)
    (tmp / "history.pack").write_bytes(proc.stdout)
    (tmp / "history.json").write_bytes(json.dumps({"head": head}, sort_keys=True).encode())
    write_sums(tmp)
    problems = verify_history(tmp)
    if problems:
        raise CausalError("history verify failed: " + "; ".join(problems))
    os.replace(tmp, dest)
    _make_read_only(dest)
    return {"head": head, "pack_sha256": sha256_bytes(proc.stdout)}


def restore_history(dest: Path, bare: Path) -> str:
    """Index the frozen pack into a fresh bare repository; returns HEAD's commit id."""
    head = json.loads((dest / "history.json").read_bytes())["head"]
    env = git_env()
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=env, capture_output=True)
    subprocess.run(["git", "-C", str(bare), "unpack-objects", "-q"], input=(dest / "history.pack").read_bytes(),
                   check=True, env=env, capture_output=True)
    subprocess.run(["git", "-C", str(bare), "update-ref", "refs/heads/replay", head], check=True, env=env,
                   capture_output=True)
    return head


def verify_history(dest: Path) -> list[str]:
    problems = list(verify_sums(dest))
    if problems:
        return problems
    with tempfile.TemporaryDirectory(prefix="ec-hist-") as tmp:
        bare = Path(tmp) / "h.git"
        try:
            head = restore_history(dest, bare)
        except subprocess.CalledProcessError as exc:
            return [f"restore failed: {exc}"]
        if (bare / "objects" / "info" / "alternates").exists():
            problems.append("alternates present")
        fsck = subprocess.run(["git", "-C", str(bare), "fsck", "--connectivity-only", "--no-dangling", head],
                              capture_output=True, env=git_env())
        if fsck.returncode != 0:
            problems.append("history not connected: " + fsck.stderr.decode("utf-8", "replace").strip()[:200])
    return problems


# ------------------------------------------------------------------ exclusions


def unsupported_state(repo: Path, *, lfs_scan: bool = True) -> list[str]:
    """Reasons the checkout cannot be captured faithfully; empty means supported.

    ``lfs_scan=False`` skips the per-file LFS pointer scan (the prompt hook's budget);
    the ``.gitattributes`` check still runs. Admission repeats the full scan."""
    reasons: list[str] = []
    gitdir, common = _gitdirs(repo)
    entries = [e for e in _git(repo, "ls-files", "-s", "-t", "-z").split(b"\0") if e]  # "<tag> <mode> ..."
    if any(e[2:].startswith(b"160000 ") for e in entries) or (repo / ".gitmodules").exists():
        reasons.append("submodule")
    if (common / "shallow").exists():
        reasons.append("shallow_history")
    sparse = subprocess.run(["git", "-C", str(repo), "config", "--bool", "core.sparseCheckout"],
                            capture_output=True, env=git_env())
    if sparse.stdout.strip() == b"true" or any(e.startswith(b"S ") for e in entries):
        reasons.append("sparse_checkout")
    if any((gitdir / name).exists() for name in STATE_FILES):
        reasons.append("operation_in_progress")
    lfs = unread = False
    rels = list_paths(repo) if lfs_scan else [
        name for e in entries
        if (name := e.split(b"\t", 1)[1].decode("utf-8", "surrogateescape")).rsplit("/", 1)[-1] == ".gitattributes"]
    for rel in rels:
        full = repo / rel
        if rel.rsplit("/", 1)[-1] == ".gitattributes" and full.is_file() and b"filter=lfs" in full.read_bytes():
            lfs = True
            break
        try:
            if full.is_file() and not full.is_symlink() and full.stat().st_size < 1024:
                if full.read_bytes().startswith(LFS_POINTER):
                    lfs = True
                    break
        except OSError:
            # an unread small file may be an LFS pointer: the scan is incomplete, not clean
            unread = True
    if lfs:
        reasons.append("lfs")
    elif unread:
        reasons.append("lfs_scan_incomplete")
    return reasons


# ------------------------------------------------------------------ attribution


def changed_paths(start: dict[str, Any], end: dict[str, Any]) -> list[str]:
    a, b = start["files"], end["files"]
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def _norm_rel(path: str, top: str | None) -> str:
    p = path.replace("\\", "/")
    if top:
        t = top.replace("\\", "/").rstrip("/") + "/"
        if os.path.normcase(p).startswith(os.path.normcase(t)):
            p = p[len(t):]
    return p[2:] if p.startswith("./") else p


def _status_paths(status: bytes) -> list[str]:
    """Paths named by ``git status --porcelain=v2 -z`` (renames: both sides)."""
    recs = status.split(b"\0")
    out: set[str] = set()
    i = 0
    while i < len(recs):
        r = recs[i]
        i += 1
        if not r:
            continue
        kind = r[:1]
        if kind == b"1":
            out.add(r.split(b" ", 8)[8].decode("utf-8", "surrogateescape"))
        elif kind == b"2":
            out.add(r.split(b" ", 9)[9].decode("utf-8", "surrogateescape"))
            if i < len(recs):
                out.add(recs[i].decode("utf-8", "surrogateescape"))
                i += 1
        elif kind == b"u":
            out.add(r.split(b" ", 10)[10].decode("utf-8", "surrogateescape"))
        elif kind in (b"?", b"!"):
            out.add(r[2:].decode("utf-8", "surrogateescape"))
    return sorted(out)


def shared_git_state(repo: Path) -> str:
    """sha256 over the repository state every worktree shares and that can change how this
    checkout behaves: the common ``config``, ``info/`` (exclude, attributes, sparse
    settings) and ``hooks/``, plus this worktree's own ``HEAD`` reference text. Other
    worktrees' branch refs and objects are deliberately outside it (they cannot alter
    this checkout's files or configuration)."""
    gitdir, common = _gitdirs(repo)
    parts: list[bytes] = []
    for base in (common / "config", common / "info", common / "hooks", gitdir / "HEAD", gitdir / "config.worktree"):
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file()) if base.is_dir() else []
        for f in files:
            parts.append(str(f.relative_to(common if f.is_relative_to(common) else gitdir)).replace("\\", "/").encode()
                         + b"\0" + f.read_bytes())
    return sha256_bytes(b"\0\0".join(parts))


def worktree_records(repo: Path, timeout: float = 30.0) -> dict[str, dict[str, Any]]:
    """Every OTHER worktree sharing this repository: ``{path: {"fp", "head", "paths"}}``,
    where ``fp`` is sha256(HEAD + status) and ``paths`` the paths its status names
    (statuses run concurrently; one that cannot finish raises ``worktree_status_timeout``).

    A worktree whose status command fails (non-zero exit: broken gitdir pointer, locked
    index, unreadable checkout), or whose status resolved to some other repository (its
    ``.git`` marker gone, git walked up to an enclosing checkout), is recorded as unobservable: ``fp`` and ``paths`` are
    ``None`` and ``status_error`` carries the exit code and stderr. Its state was not
    observed, so it is never equal to anything, not even to itself."""
    me = os.path.normcase(os.path.realpath(_git(repo, "rev-parse", "--show-toplevel").decode().strip()))
    out: dict[str, dict[str, Any]] = {}
    procs: dict[str, tuple[bytes, subprocess.Popen[bytes]]] = {}
    for block in _git(repo, "worktree", "list", "--porcelain", "-z").split(b"\0\0"):
        fields = dict(f.split(b" ", 1) if b" " in f else (f, b"") for f in block.split(b"\0") if f)
        path = fields.get(b"worktree", b"").decode("utf-8", "surrogateescape")
        if not path or b"bare" in fields or os.path.normcase(os.path.realpath(path)) == me:
            continue
        if not os.path.isdir(path):
            out[path] = {"fp": "missing", "head": None, "paths": None}
            continue
        procs[path] = (fields.get(b"HEAD", b""), subprocess.Popen(
            ["git", "-C", path, "status", "--porcelain=v2", "-z", "--untracked-files=all"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env()))
    for path, (head, proc) in procs.items():
        try:
            status, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise CausalError("worktree_status_timeout") from None
        if proc.returncode != 0:
            out[path] = {"fp": None, "head": head.decode() or None, "paths": None,
                         "status_error": {"rc": proc.returncode,
                                          "stderr": err.decode("utf-8", "replace").strip()[:2000]}}
            continue
        mismatch = _toplevel_mismatch(path, timeout)
        if mismatch is not None:
            out[path] = {"fp": None, "head": head.decode() or None, "paths": None,
                         "status_error": {"rc": 0, "stderr": mismatch}}
            continue
        out[path] = {"fp": sha256_bytes(head + b"\0" + status), "head": head.decode() or None,
                     "paths": _status_paths(status)}
    return out


def _toplevel_mismatch(path: str, timeout: float) -> str | None:
    """Prove the status just taken was of ``path`` itself. A worktree whose ``.git`` marker
    is gone makes git walk up to an enclosing repository (the house layout nests lanes
    in an ignored directory inside the repository): status then exits 0 with
    no output whatever changed inside. Returns the reason when the toplevel git resolves
    for ``path`` is not ``path``, or when it cannot be resolved; ``None`` when it matches."""
    try:
        res = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True,
                             env=git_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return "toplevel unresolved: timeout"
    if res.returncode != 0:
        return f"toplevel unresolved: rc {res.returncode}: {res.stderr.decode('utf-8', 'replace').strip()[:500]}"
    got = res.stdout.decode("utf-8", "surrogateescape").strip()
    if os.path.normcase(os.path.realpath(got)) != os.path.normcase(os.path.realpath(path)):
        return f"toplevel mismatch: {got}"
    return None


def unobservable_worktrees(*records: dict[str, dict[str, Any]] | None) -> list[str]:
    """Worktrees any given record set could not observe (``fp`` is ``None``: the status
    command failed). Such a worktree has no state to compare, so it can never be unmoved."""
    return sorted({k for recs in records if recs for k, v in recs.items() if (v or {}).get("fp") is None})


def worktree_fingerprints(repo: Path, timeout: float = 30.0) -> dict[str, str | None]:
    """``None`` for a worktree whose status could not be observed."""
    return {k: v["fp"] for k, v in worktree_records(repo, timeout).items()}


INSTALL_WORDS = ("npm install", "npm ci", "pnpm install", "yarn install", "yarn add", "npm i ",
                 "pip install", "uv sync", "uv pip", "poetry install", "dotnet restore", "nuget restore",
                 "dotnet add", "cargo fetch", "go mod download", "bundle install", "composer install")


def classify_worktrees(start: dict[str, dict[str, Any]] | None, end: dict[str, dict[str, Any]] | None, *,
                       shared_start: str | None, shared_end: str | None,
                       calls: list[dict[str, Any]] | None, subagents_unresolved: int | None,
                       top: str | None = None) -> dict[str, Any]:
    """Split other-worktree movement during a turn into ``contaminating`` (it can have
    reached this task's inputs, dependencies, shared state or attribution), ``unrelated``
    (preserved evidence shows it cannot have) and ``unknown`` (the evidence to decide is
    missing). Unknown is never unrelated. A worktree whose status failed on either side
    (``fp`` None, see ``worktree_records``) is unknown whether or not it appears moved.

    A moved worktree is unrelated only when every one of these holds: both records exist
    with path lists; the shared git state is unchanged; every tool call of the turn
    (subagents included, none unresolved) is preserved and none names that worktree's
    path; and no call ran a package install/restore (shared caches)."""
    out: dict[str, Any] = {"contaminating": [], "unrelated": [], "unknown": [], "shared_state_changed": None}
    if start is None or end is None:
        out["unknown"].append("records_missing")
        return out
    if shared_start is None or shared_end is None:
        out["shared_state_changed"] = None
    else:
        out["shared_state_changed"] = shared_start != shared_end
    unobserved = set(unobservable_worktrees(start, end))
    out["unknown"] += sorted(unobserved)  # status failed on one side: no state to compare, never unmoved
    moved = sorted(k for k in (set(start) | set(end)) - unobserved
                   if (start.get(k) or {}).get("fp") != (end.get(k) or {}).get("fp"))
    texts: list[str] | None = None
    if calls is not None and subagents_unresolved == 0:
        texts = [json.dumps(c.get("input") or {}, ensure_ascii=False).replace("\\\\", "/").replace("\\", "/").lower()
                 for c in calls]
    installs = texts is not None and any(w in t for t in texts for w in INSTALL_WORDS)
    for wt in moved:
        a, b = start.get(wt), end.get(wt)
        if out["shared_state_changed"] is True:
            out["contaminating"].append(wt)
            continue
        if (not a or not b or a.get("paths") is None or b.get("paths") is None
                or out["shared_state_changed"] is None or texts is None):
            out["unknown"].append(wt)
            continue
        needles = {wt.replace("\\", "/").rstrip("/").lower()}
        if top:
            t_norm = top.replace("\\", "/").rstrip("/").lower() + "/"
            if next(iter(needles)).startswith(t_norm):
                needles.add(next(iter(needles))[len(t_norm):])  # a nested worktree named relatively
        if installs or any(n in t for t in texts for n in needles):
            out["contaminating"].append(wt)
        elif any("../" in t or "..\\" in t for t in texts):
            out["unknown"].append(wt)  # a parent-relative reference could name it indirectly
        else:
            out["unrelated"].append(wt)
    return out


def attribute(start: dict[str, Any], end: dict[str, Any], agent_writes: list[str], *,
              top: str | None = None, worktrees_start: dict[str, str | None] | None = None,
              worktrees_end: dict[str, str | None] | None = None) -> dict[str, Any]:
    """Every change must be one the agent wrote; other worktrees must not move. A
    worktree fingerprint of ``None`` (status failed) is never equal: it counts as moved."""
    written = {os.path.normcase(_norm_rel(p, top)) for p in agent_writes}
    changed = changed_paths(start, end)
    unattributed = [p for p in changed if os.path.normcase(p) not in written]
    ws, we = worktrees_start or {}, worktrees_end or {}
    moved = sorted(k for k in set(ws) | set(we)
                   if ws.get(k) is None or we.get(k) is None or ws.get(k) != we.get(k))
    return {"changed": changed, "unattributed": unattributed, "other_worktrees_moved": moved,
            "head_moved": start.get("head") != end.get("head"),
            "isolated": not unattributed and not moved}


# ------------------------------------------------------------------ dependencies


def _tool_version(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return "absent"
    text = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip()
    return text.splitlines()[0] if proc.returncode == 0 and text else f"error:{proc.returncode}"


def dependency_fingerprint(state: dict[str, Any], tools: dict[str, list[str]] | None = None) -> dict[str, Any]:
    locks = {rel: e["sha256"] for rel, e in state["files"].items() if rel.rsplit("/", 1)[-1] in LOCKFILES}
    versions = {name: _tool_version(argv) for name, argv in sorted((tools or DEFAULT_TOOLS).items())}
    return {"lockfiles": dict(sorted(locks.items())), "tools": versions}


def dependency_drift(start: dict[str, Any], end: dict[str, Any], agent_writes: list[str], *,
                     top: str | None = None) -> list[str]:
    """Lockfile changes the agent did not write, and any tool-version change."""
    written = {os.path.normcase(_norm_rel(p, top)) for p in agent_writes}
    drift: list[str] = []
    for rel in sorted(set(start["lockfiles"]) | set(end["lockfiles"])):
        if start["lockfiles"].get(rel) != end["lockfiles"].get(rel) and os.path.normcase(rel) not in written:
            drift.append(f"lockfile:{rel}")
    for name in sorted(set(start["tools"]) | set(end["tools"])):
        if start["tools"].get(name) != end["tools"].get(name):
            drift.append(f"tool:{name}")
    return drift


# ------------------------------------------------------------------ outcome checks


def load_registry(path: Path) -> dict[str, list[dict[str, Any]]]:
    """``{"tasks": {task_id: {"checks": [...]}}}``; malformed entries raise."""
    return validate_tasks(json.loads(path.read_bytes()))


def validate_tasks(raw: Any) -> dict[str, list[dict[str, Any]]]:
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, dict):
        raise CausalError("registry: 'tasks' object required")
    out: dict[str, list[dict[str, Any]]] = {}
    for task, body in tasks.items():
        checks = body.get("checks") if isinstance(body, dict) else None
        if not isinstance(checks, list) or not checks:
            raise CausalError(f"registry: task {task} has no checks")
        for c in checks:
            if not isinstance(c, dict) or c.get("kind") not in CHECK_KINDS:
                raise CausalError(f"registry: task {task} has an unknown check kind")
            if c["kind"] == "command" and not (isinstance(c.get("argv"), list) and c["argv"]):
                raise CausalError(f"registry: task {task} command check needs argv")
            if c["kind"] != "command" and not isinstance(c.get("path"), str):
                raise CausalError(f"registry: task {task} {c['kind']} check needs path")
        out[task] = checks
    return out


GENERIC_TARGETS = frozenset({"", ".", "./", "all", "build", "test", "tests", "check", "lint", "ci", "--", "*"})


def validate_predicate(checks: list[dict[str, Any]]) -> None:
    """A correctness predicate must be task-specific and independently executable.

    State checks (``file_*``) name a path inside the materialized state. Behavioral
    checks (``command``) must carry ``asserts`` (what task behavior they establish) and a
    ``target`` (the task's own test id, script or path) that appears in ``argv``; no
    argument may be an absolute path (it would reach outside the materialized state).
    A predicate whose behavioral checks are all generic (no target) is refused, and so is
    an empty one: a build or full-suite result alone does not identify the task."""
    if not isinstance(checks, list) or not checks:
        raise CausalError("predicate has no checks")
    validate_tasks({"tasks": {"_": {"checks": checks}}})
    specific = 0
    for c in checks:
        if c["kind"] == "command":
            argv = [str(a) for a in c["argv"]]
            if any(os.path.isabs(a) or (len(a) > 1 and a[1] == ":") for a in argv):
                raise CausalError("command check uses an absolute path; it must run inside the materialized state")
            target = str(c.get("target") or "").strip()
            if not str(c.get("asserts") or "").strip():
                raise CausalError("command check needs 'asserts': the task behavior it establishes")
            if target.lower() in GENERIC_TARGETS or not any(target in a for a in argv):
                continue  # generic: allowed alongside, never sufficient
            specific += 1
        else:
            rel = c["path"].replace("\\", "/")
            if os.path.isabs(c["path"]) or rel.startswith("../") or "/../" in rel or (len(rel) > 1 and rel[1] == ":"):
                raise CausalError("state check path must be relative to the repository")
            specific += 1
    if not specific:
        raise CausalError("no task-specific check: a generic build/test result alone is not a correctness predicate")


def _inside(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise CausalError(f"check path escapes the materialized state: {rel}")
    return target


def run_checks(checks: list[dict[str, Any]], materialized: Path) -> list[dict[str, Any]]:
    """Evaluate checks against a materialized end state. ``error`` means not evaluable."""
    results: list[dict[str, Any]] = []
    for i, c in enumerate(checks):
        res: dict[str, Any] = {"index": i, "kind": c["kind"]}
        try:
            if c["kind"] == "command":
                proc = subprocess.run(c["argv"], cwd=materialized, capture_output=True,
                                      timeout=float(c.get("timeout_s", 120)))
                res["passed"] = proc.returncode == int(c.get("expect_exit", 0))
                res["exit"] = proc.returncode
            else:
                target = _inside(materialized, c["path"])
                if c["kind"] == "file_absent":
                    res["passed"] = not target.exists()
                elif not target.is_file():
                    res["passed"] = False
                elif c["kind"] == "file_sha256":
                    res["passed"] = sha256_bytes(target.read_bytes()) == c["sha256"]
                else:
                    needle = bytes.fromhex(c["hex"]) if "hex" in c else str(c["text"]).encode("utf-8")
                    res["passed"] = needle in target.read_bytes()
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, CausalError) as exc:
            res["passed"] = None
            res["error"] = type(exc).__name__
        results.append(res)
    return results


# ------------------------------------------------------------------ admission


def admit(record: dict[str, Any]) -> dict[str, Any]:
    """Commissioning eligibility. Every reason is listed; any reason means ineligible.

    ``record`` keys: ``identity_accepted`` (bool), ``start_problems``/``end_problems``/
    ``history_problems`` (lists), ``unsupported`` (list), ``attribution`` (from
    ``attribute``), ``start_fidelity``/``end_fidelity`` (``verify_fidelity`` lists; None =
    not proven), ``worktrees`` (``classify_worktrees`` lists), ``dependency_drift`` (list),
    ``registration_problems`` (None = nothing registered), ``checks``, and
    ``check_results``/``check_results_start`` (``run_checks`` on the materialized end and
    start). The predicate must pass on the end and must not already pass on the start.
    """
    reasons: list[str] = []
    if record.get("identity_accepted") is not True:
        reasons.append("identity_not_accepted")
    for key in ("start", "end", "history"):
        probs = record.get(f"{key}_problems")
        if probs is None:
            reasons.append(f"{key}_missing")
        elif probs:
            reasons.append(f"{key}_unverified")
    reasons += [f"unsupported:{r}" for r in record.get("unsupported") or []]
    for key in ("start", "end"):
        fid = record.get(f"{key}_fidelity")
        if fid is None:
            reasons.append(f"{key}_fidelity_not_proven")
        else:
            reasons += [f"{key}_{p}" for p in fid]
    att = record.get("attribution")
    if not att:
        reasons.append("attribution_missing")
    elif att["unattributed"]:
        reasons.append("unattributed_change")
    wt = record.get("worktrees")
    if wt is None:
        reasons.append("worktree_classification_missing")
    else:
        if wt.get("shared_state_changed") is True:
            reasons.append("shared_git_state_changed")
        if wt["contaminating"]:
            reasons.append("other_worktree_contaminating")
        if wt["unknown"]:
            reasons.append("other_worktree_unknown")
    reasons += [f"dependency_drift:{d}" for d in record.get("dependency_drift") or []]
    if record.get("registration_problems") is None:
        reasons.append("no_outcome_check")
    elif record["registration_problems"]:
        reasons.append("outcome_check_registration_unverified")
    else:
        checks = record.get("checks") or []
        end_r, start_r = record.get("check_results"), record.get("check_results_start")
        if not end_r or len(end_r) != len(checks) or not start_r or len(start_r) != len(checks):
            reasons.append("outcome_checks_not_run")
        elif any(r.get("passed") is None for r in end_r + start_r):
            reasons.append("outcome_check_not_evaluable")
        else:
            if not all(r["passed"] for r in end_r):
                reasons.append("predicate_rejects_reference_end")
            if all(r["passed"] for r in start_r):
                reasons.append("predicate_not_discriminating")  # already true before the task
    return {"eligible": not reasons, "reasons": reasons}

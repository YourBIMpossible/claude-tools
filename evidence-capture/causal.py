#!/usr/bin/env python3
"""Causal machinery for twin replay (plan Phase D3).

The start snapshot (``snapshot.py``) records how a turn began. Attributing an outcome
to the brief also needs how it ended, a store that replays without the source
repository, and proof that nothing else moved. This module adds those pieces:

* ``capture_state``      immutable, verified worktree state (start or end): every
                         tracked and untracked unignored path as raw bytes, read twice
                         (any difference is ``concurrent_change``), content-addressed by
                         SHA-256, sealed with ``SHA256SUMS`` and made read-only
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


# ------------------------------------------------------------------ history store


def freeze_history(repo: Path, dest: Path) -> dict[str, Any]:
    """Pack every object reachable from HEAD into ``dest/history.pack``; verify standalone."""
    if dest.exists():
        raise CausalError("history exists; never overwritten")
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
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


def unsupported_state(repo: Path) -> list[str]:
    """Reasons the checkout cannot be captured faithfully; empty means supported."""
    reasons: list[str] = []
    gitdir, common = _gitdirs(repo)
    stage = _git(repo, "ls-files", "-s", "-z")
    if any(e.startswith(b"160000 ") for e in stage.split(b"\0")) or (repo / ".gitmodules").exists():
        reasons.append("submodule")
    if (common / "shallow").exists():
        reasons.append("shallow_history")
    sparse = subprocess.run(["git", "-C", str(repo), "config", "--bool", "core.sparseCheckout"],
                            capture_output=True, env=git_env())
    tags = _git(repo, "ls-files", "-t", "-z")
    if sparse.stdout.strip() == b"true" or any(e.startswith(b"S ") for e in tags.split(b"\0")):
        reasons.append("sparse_checkout")
    if any((gitdir / name).exists() for name in STATE_FILES):
        reasons.append("operation_in_progress")
    lfs = False
    for rel in list_paths(repo):
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
            continue
    if lfs:
        reasons.append("lfs")
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


def worktree_fingerprints(repo: Path) -> dict[str, str]:
    """sha256(HEAD + status) of every OTHER worktree sharing this repository."""
    me = os.path.normcase(os.path.realpath(_git(repo, "rev-parse", "--show-toplevel").decode().strip()))
    out: dict[str, str] = {}
    for block in _git(repo, "worktree", "list", "--porcelain", "-z").split(b"\0\0"):
        fields = dict(f.split(b" ", 1) if b" " in f else (f, b"") for f in block.split(b"\0") if f)
        path = fields.get(b"worktree", b"").decode("utf-8", "surrogateescape")
        if not path or b"bare" in fields or os.path.normcase(os.path.realpath(path)) == me:
            continue
        if not os.path.isdir(path):
            out[path] = "missing"
            continue
        status = git_bytes(Path(path), "status", "--porcelain=v2", "-z", "--untracked-files=all", check=False)
        out[path] = sha256_bytes(fields.get(b"HEAD", b"") + b"\0" + status)
    return out


def attribute(start: dict[str, Any], end: dict[str, Any], agent_writes: list[str], *,
              top: str | None = None, worktrees_start: dict[str, str] | None = None,
              worktrees_end: dict[str, str] | None = None) -> dict[str, Any]:
    """Every change must be one the agent wrote; other worktrees must not move."""
    written = {os.path.normcase(_norm_rel(p, top)) for p in agent_writes}
    changed = changed_paths(start, end)
    unattributed = [p for p in changed if os.path.normcase(p) not in written]
    moved = sorted(k for k in set(worktrees_start or {}) | set(worktrees_end or {})
                   if (worktrees_start or {}).get(k) != (worktrees_end or {}).get(k))
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
    raw = json.loads(path.read_bytes())
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
    ``attribute``), ``dependency_drift`` (list), ``checks`` (registered list or None),
    ``check_results`` (from ``run_checks``).
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
    att = record.get("attribution")
    if not att:
        reasons.append("attribution_missing")
    else:
        if att["unattributed"]:
            reasons.append("unattributed_change")
        if att["other_worktrees_moved"]:
            reasons.append("other_worktree_moved")
    reasons += [f"dependency_drift:{d}" for d in record.get("dependency_drift") or []]
    if not record.get("checks"):
        reasons.append("no_outcome_check")
    else:
        results = record.get("check_results")
        if not results or len(results) != len(record["checks"]):
            reasons.append("outcome_checks_not_run")
        elif any(r.get("passed") is None for r in results):
            reasons.append("outcome_check_not_evaluable")
    return {"eligible": not reasons, "reasons": reasons}

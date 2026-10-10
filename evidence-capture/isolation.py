#!/usr/bin/env python3
"""Deterministic filesystem-isolation check for replay (plan §8). No model, no tokens.

A plain child process (``python isolation.py --probe``), started under the identity the
replay agent will run as, attempts each forbidden access:

  read       read one byte of a file (a source file, the content store's ``SENTINEL``)
  list       list a directory (the metadata repository)
  open_dir   list a directory (the source's ``.git/objects``)
  resolve    stat and list a path (a sibling worktree)
  write      create a file in a directory (the content store's ``tmp/``); removed if it
             was created, and the success still fails the check

Every attempt must end in an access error. A success, a missing target (nothing was
proven) or any other error fails the check. Around the probe the parent records, per
watched store, a stat walk (paths, sizes, mtimes) and ``git status`` of every watched
repository; any change fails the check. At least one watched store and one watched
repository are required, and a store or repository that cannot be read (missing path,
unreadable entry, git failure) fails the check: an unobserved state is never "unchanged". The record keeps the identity the child
reported, each target's ``icacls`` dump, exit codes and per-attempt outcomes.

The child runs as the current identity unless ``probe_prefix`` wraps it in something
that switches identity (a scheduled task or service account set up by the owner). This
module never creates accounts, changes system settings or handles credentials; the
synthetic test applies and removes a deny ACE on its own temporary directories only.

``replay_verified`` depends on this check passing for the batch configuration; a clean
clone object database (``clone_builder``) is never cited in its place.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

KINDS = ("read", "list", "open_dir", "resolve", "write")
ACCESS_WINERRORS = {5, 1314}  # ERROR_ACCESS_DENIED, ERROR_PRIVILEGE_NOT_HELD
PROBE_TIMEOUT_S = 60


# ---------------------------------------------------------------------- child

def _attempt(kind: str, path: Path) -> str:
    try:
        if kind == "read":
            with path.open("rb") as fh:
                fh.read(1)
        elif kind in ("list", "open_dir"):
            os.listdir(path)
        elif kind == "resolve":
            os.stat(path)
            if path.is_dir():
                os.listdir(path)
        elif kind == "write":
            probe = path / f"isolation-probe-{os.getpid()}"
            with probe.open("xb") as fh:
                fh.write(b"x")
            # The write already succeeded; a failed cleanup must not turn it into a denial.
            try:
                probe.unlink()
            except OSError as exc:
                return f"succeeded:probe file left behind ({type(exc).__name__})"
        else:
            return f"error:unknown kind {kind}"
        return "succeeded"
    except PermissionError:
        return "denied"
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        if getattr(exc, "winerror", None) in ACCESS_WINERRORS:
            return "denied"
        return f"error:{type(exc).__name__}"


def identity() -> dict[str, str | None]:
    """The identity this process runs as: user name and, on Windows, the SID."""
    name = os.environ.get("USERNAME") or os.environ.get("USER")
    sid = None
    if os.name == "nt":
        try:
            out = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                                 timeout=30).stdout
            row = next(csv.reader(io.StringIO(out)))
            name, sid = row[0], row[1]
        except (OSError, StopIteration, IndexError, subprocess.SubprocessError):
            pass
    return {"name": name, "sid": sid}


def probe_main(spec: str) -> int:
    targets = json.loads(spec)
    out = {"identity": identity(), "attempts": [
        {"name": t["name"], "kind": t["kind"], "outcome": _attempt(t["kind"], Path(t["path"]))} for t in targets]}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


# ---------------------------------------------------------------------- parent

class StateUnreadable(RuntimeError):
    """A watched store or repository could not be observed; its state is unknown."""


def stat_walk(root: Path) -> list[tuple[str, int, int]]:
    """Every path under the directory ``root`` with size and mtime (ns), for before/after
    comparison. Raises ``OSError`` when ``root`` or any entry under it cannot be read."""

    def fail(exc: OSError) -> None:
        raise exc

    if not root.is_dir():
        raise FileNotFoundError(f"watched store is not a directory: {root}")
    rows = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
        dirnames.sort()
        for name in sorted(filenames) + dirnames:
            p = Path(dirpath) / name
            st = os.lstat(p)
            rows.append((str(p.relative_to(root)).replace("\\", "/"), st.st_size, st.st_mtime_ns))
    rows.append((".", 0, os.lstat(root).st_mtime_ns))
    return rows


def git_status(repo: Path) -> str:
    """``git status`` of ``repo``; raises ``StateUnreadable`` when git fails."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    proc = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v2", "--untracked-files=all"],
                          capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode != 0:
        raise StateUnreadable(f"git status rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def icacls(path: Path) -> str:
    """The ACL governing ``path``: its own, or its nearest ancestor's when the recorder
    cannot open ``path`` itself (it sits under a directory denied to this identity)."""
    if os.name != "nt":
        return "<icacls not available on this platform>"
    for p in (path, *path.parents):
        proc = subprocess.run(["icacls", str(p)], capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return proc.stdout.strip()
    return "<icacls failed on the path and every ancestor>"


def state(watch: list[Path], repos: list[Path]) -> tuple[dict[str, Any], list[str]]:
    """The observed state and, for every store or repository that could not be read, an error."""
    out: dict[str, Any] = {"walk": {}, "status": {}}
    errors: list[str] = []
    for p in watch:
        try:
            out["walk"][str(p)] = stat_walk(p)
        except OSError as exc:
            errors.append(f"walk:{p}: {type(exc).__name__}: {exc}"[:300])
    for r in repos:
        try:
            out["status"][str(r)] = git_status(r)
        except (OSError, StateUnreadable, subprocess.SubprocessError) as exc:
            errors.append(f"status:{r}: {type(exc).__name__}: {exc}"[:300])
    return out, errors


def check_isolation(targets: list[dict[str, str]], *, watch: list[Path], repos: list[Path],
                    probe_prefix: list[str] | None = None, acl_window: Any = None) -> dict[str, Any]:
    """Run the probe and judge it. ``targets``: ``[{"name", "kind", "path"}]``.

    ``acl_window`` (tests only) is a context manager entered just around the icacls dumps
    and the probe: after the before-state is taken, and left before the after-state.

    Raises ``ValueError`` when ``targets``, ``watch`` or ``repos`` is empty: nothing would be
    checked, and a check of nothing must not read as a pass.
    """
    empty = [name for name, v in (("targets", targets), ("watch", watch), ("repos", repos)) if not v]
    if empty:
        raise ValueError(f"nothing to check: no {', '.join(empty)}")
    for t in targets:
        if t["kind"] not in KINDS:
            raise ValueError(f"unknown kind {t['kind']!r}")
    record: dict[str, Any] = {"targets": targets, "icacls": {}, "identity": None, "attempts": [],
                              "probe_rc": None, "state_changed": [], "state_unreadable": [], "passed": False,
                              "failures": []}
    before, before_errors = state(watch, repos)
    cmd = [*(probe_prefix or []), sys.executable, str(Path(__file__).resolve()), "--probe", json.dumps(targets)]

    def run_probe() -> subprocess.CompletedProcess[str]:
        record["icacls"] = {t["name"]: icacls(Path(t["path"])) for t in targets}
        return subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)

    if acl_window is not None:
        with acl_window:
            proc = run_probe()
    else:
        proc = run_probe()
    after, after_errors = state(watch, repos)
    record["state_unreadable"] = [f"before:{e}" for e in before_errors] + [f"after:{e}" for e in after_errors]
    record["probe_rc"] = proc.returncode
    try:
        child = json.loads(proc.stdout.strip().splitlines()[-1])
        record["identity"], record["attempts"] = child["identity"], child["attempts"]
    except (ValueError, IndexError, KeyError):
        record["failures"].append("probe produced no result")
    for key in ("walk", "status"):
        for name in [n for n in before[key] if n in after[key]]:  # an unread side is in state_unreadable
            if before[key][name] != after[key][name]:
                record["state_changed"].append(f"{key}:{name}")
    record["failures"] += [f"{a['name']}:{a['outcome']}" for a in record["attempts"] if a["outcome"] != "denied"]
    record["failures"] += record["state_changed"]
    record["failures"] += record["state_unreadable"]
    if proc.returncode != 0:
        record["failures"].append(f"probe rc={proc.returncode}")
    record["passed"] = bool(record["attempts"]) and not record["failures"] and \
        len(record["attempts"]) == len(targets)
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe", help=argparse.SUPPRESS)
    ap.add_argument("--targets", type=Path, help="JSON list of {name, kind, path}")
    ap.add_argument("--watch", type=Path, action="append", default=[],
                    help="store directory whose stat walk must not change (repeat; at least one)")
    ap.add_argument("--repo", type=Path, action="append", default=[],
                    help="repository whose git status must not change (repeat; at least one)")
    ap.add_argument("--probe-prefix", default="", help="command prefix that runs the probe as the replay identity")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    if args.probe is not None:
        return probe_main(args.probe)
    if not args.targets:
        ap.error("--targets is required")
    targets = json.loads(args.targets.read_text(encoding="utf-8"))
    try:
        rec = check_isolation(targets, watch=args.watch, repos=args.repo,
                              probe_prefix=args.probe_prefix.split() if args.probe_prefix else None)
    except ValueError as exc:
        ap.error(str(exc))
    text = json.dumps(rec, indent=1, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"passed": rec["passed"], "failures": rec["failures"]}))
    return 0 if rec["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

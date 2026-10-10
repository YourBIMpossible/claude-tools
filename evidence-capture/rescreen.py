"""Deterministic re-screen of joined episodes after a join or screen correction.

Every manifest whose transcript turn was joined is re-joined and re-screened from its
preserved transcript (and the subagent transcripts it references) with the current
tool. Only the fields the join and screen own are replaced: prompt origin, attachment
flag and index, prior context, dependencies, edited files, observation and the
mechanical task class; then the rules and funnel are refreshed. Link, boundary,
snapshot, review and retention are left as they are.

A judgement the current tool can no longer support is invalidated, never kept: when the
turn cannot be found again, the captured root cannot be identified, the episode's work
does not finish within its time limit, or the transcript is gone and the episode was
judged sufficient although it delegated work or touched another session, the
observation becomes insufficient and every ``required: no`` that rested on it becomes
``unknown``. A worktree removed after capture is not such a case: the screen uses the
root path whose hash the start hook recorded (``root_source: capture_hash``).

Bounded: each episode is re-screened in a child process killed after
``EPISODE_TIMEOUT_S``; the maintenance lock is refreshed after every episode, and once
``RUN_BUDGET_S`` is spent the remaining episodes are left unchanged and listed as
``deferred``. A stuck episode therefore cannot hold maintenance.

Each changed manifest gets a ``corrections`` entry (tool commit, before and after); the
run writes ``corrections/<stamp>-rescreen.json`` with before/after counts and the list of
changed episodes. Both are committed to the metadata repository. Hashes and counts only.
"""
from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import manifest as mf
from common import iso, now_utc, write_json_atomic
from join import join
from pipeline import (MAINTAIN_LOCK, _try_lock, _unlock, _untracked_paths, episode_lock,
                      find_transcript, screen_root, tool_identity, transcript_cwds)
from review import load_owner_review
from screen import screen
from store import Stores, commit_meta, manifest_path, scan_manifests, write_manifest

# Fields the join and screen own; nothing else is copied back from the re-screen.
PROMPT_FIELDS = ("uuid", "origin", "index_in_session", "has_attachment")
DELEGATION_CAUSES = ("missing_subagent_transcript", "subagent_beyond_turn")
EPISODE_TIMEOUT_S = 60.0
RUN_BUDGET_S = 1800.0
CHILD_ARGV = (sys.executable, str(Path(__file__).resolve()), "--child")


def summary(m: dict[str, Any]) -> dict[str, Any]:
    """The comparable judgement of one manifest."""
    return {"origin": m["prompt"].get("origin"), "has_attachment": m["prompt"].get("has_attachment"),
            "sufficient": m["observation"].get("sufficient"), "causes": list(m["observation"].get("causes") or []),
            "required": {c: m["deps"][c].get("required") for c in mf.DEP_CATEGORIES},
            "rules": {e["rule"]: e["state"] for e in m["exclusions"]},
            "dependency_screen_clear": bool(m["funnel"].get("dependency_screen_clear")),
            "edited_files": len(m.get("edited_files") or []),
            "task_class": (m.get("task_class") or {}).get("mechanical")}


def _well_formed(m: dict[str, Any]) -> bool:
    """True when ``m`` has every field a rescreen reads; a partial manifest is reported,
    never allowed to abort the run."""
    if mf.shape_problem(m) is not None:
        return False
    try:
        summary(m)
        return True
    except (KeyError, TypeError, AttributeError):
        return False


def _new_record_path(directory: Path, stem: str) -> Path:
    """A record path no earlier record has: ``<stem>.json``, else ``<stem>-<n>.json``,
    reserved by exclusive create so two runs in one second never share one."""
    directory.mkdir(parents=True, exist_ok=True)
    n = 0
    while True:
        path = directory / (f"{stem}.json" if n == 0 else f"{stem}-{n}.json")
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            n += 1


def _delegated(m: dict[str, Any]) -> bool:
    join_rec = m["capture"].get("join") or {}
    return bool(join_rec.get("subagents")) or bool(m["deps"]["other_session"].get("observed"))


def invalidate(m: dict[str, Any], cause: str) -> None:
    """Withdraw every judgement that rested on a sufficient observation."""
    # A cause from an earlier rescreen is superseded by this one, never accumulated.
    causes = {c for c in m["observation"].get("causes") or [] if not c.startswith("rescreen_")}
    causes.add(cause)
    m["observation"] = {"sufficient": False, "causes": sorted(causes)}
    for c in mf.DEP_CATEGORIES:
        dep = m["deps"][c]
        dep["observation_sufficient"] = False
        if c == "prior_conversation":
            kinds = {e.get("kind") for e in dep.get("evidence") or [] if isinstance(e, dict)}
            if dep.get("required") == "no" and "first_prompt" not in kinds:
                dep["required"] = "unknown"
                m["prior_context"]["prior_conversation_required"] = "unknown"
        elif dep.get("required") == "no":
            dep["required"] = "unknown"


def rescreen_one(stores: Stores, m: dict[str, Any], env: dict[str, str]) -> str:
    """Re-join and re-screen ``m`` in place; the outcome label."""
    tr = find_transcript(m["session_id"], env)
    if tr is None:
        if m["observation"].get("sufficient") and _delegated(m):
            invalidate(m, "rescreen_transcript_unavailable")
            return "invalidated_transcript_unavailable"
        return "transcript_unavailable"
    probe = copy.deepcopy(m)
    misses: list[str] = []
    obs = join(probe, tr, misses)
    if obs is None:
        invalidate(m, "rescreen_prompt_ambiguous" if "prompt_ambiguous" in misses else "rescreen_prompt_not_found")
        return "invalidated_prompt_not_found"
    # The prompt's own classification does not depend on the checkout: keep it even
    # when the screen below cannot run.
    for k in PROMPT_FIELDS:
        m["prompt"][k] = probe["prompt"].get(k)
    cwds = transcript_cwds(tr)
    root, source = screen_root(m, cwds)
    if root is None:
        invalidate(m, "rescreen_root_unresolved")
        return "invalidated_root_unresolved"
    screen(probe, obs, root, cwd=cwds[-1] if cwds else str(root),
           untracked_paths=_untracked_paths(stores, m["episode_id"]), complete=True)
    for k in ("prior_context", "deps", "edited_files", "observation"):
        m[k] = probe[k]
    m["task_class"]["mechanical"] = probe["task_class"].get("mechanical")
    m["capture"]["join"].update(subagents=obs.get("subagent_count", 0),
                                subagents_unresolved=obs.get("subagent_unresolved", 0), root_source=source)
    return "rescreened" if source == "checkout" else "rescreened_root_removed"


def run_child(stores: Stores, m: dict[str, Any], env: dict[str, str], timeout_s: float,
              argv: tuple[str, ...] = CHILD_ARGV) -> tuple[str, dict[str, Any] | None]:
    """Re-screen ``m`` in a child process; (outcome, re-screened manifest or None).

    The child gets the manifest and store paths on stdin and is killed after
    ``timeout_s``; a timeout, crash or unreadable reply is an outcome, never a hang.

    The child's stdin/stdout/stderr are temporary files, not pipes: a descendant it
    started (git, say) that outlives it cannot hold a pipe open and stall the drain. The
    child runs in its own session (POSIX) or job object (Windows), and the whole tree is
    killed when ``run_child`` returns, whether the child timed out or exited: nothing it
    started survives the episode."""
    req = json.dumps({"manifest": m, "meta": str(stores.meta), "content": str(stores.content)})
    with tempfile.TemporaryFile() as fin, tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        fin.write(req.encode("utf-8"))
        fin.seek(0)
        try:
            proc = subprocess.Popen(list(argv), stdin=fin, stdout=fout, stderr=ferr,
                                    env={**env, "PYTHONIOENCODING": "utf-8"}, **_GROUP_KW)
        except OSError:
            return "child_failed", None
        tree = _ProcessTree(proc)
        try:
            try:
                rc = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                return "timeout", None
            if rc != 0:
                return "child_failed", None
            fout.seek(0)
            try:
                rep = json.loads(fout.read().decode("utf-8"))
                return str(rep["outcome"]), rep["manifest"]
            except (ValueError, KeyError, TypeError):
                return "child_failed", None
        finally:
            tree.kill()


_GROUP_KW: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                             else {"start_new_session": True})
KILL_WAIT_S = 10.0


class _ProcessTree:
    """Everything ``proc`` starts, killable as one unit after ``proc`` itself is gone.

    POSIX: the child is a session leader, so its process group holds every descendant
    that did not call ``setsid`` and the group id stays reserved while any member lives.
    Windows: a job object with ``KILL_ON_JOB_CLOSE``; a grandchild whose parent already
    exited is still a member, which ``taskkill /T`` (a walk over live parent links) would
    miss. The child is assigned right after it is created, before its interpreter can
    start anything; a failed assignment falls back to the parent-link walk."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.job: int | None = None
        if os.name == "nt":
            try:
                self.job = _win_job_for(proc)
            except Exception:
                self.job = None  # fall back to the parent-link walk; never leak the child

    def kill(self) -> None:
        try:
            if os.name == "nt":
                if self.job is not None:
                    _win_terminate_job(self.job)
                    self.job = None
                elif self.proc.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=KILL_WAIT_S)
            else:
                os.killpg(self.proc.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            pass


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _win_job_for(proc: subprocess.Popen) -> int | None:
    """A kill-on-close job object holding ``proc``, or None when the OS refused."""
    import ctypes
    from ctypes import wintypes

    class BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                    "OtherOperationCount", "ReadTransferCount",
                                                    "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimit(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = (k32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                      ctypes.sizeof(info))
          and k32.AssignProcessToJobObject(job, int(proc._handle)))  # type: ignore[attr-defined]
    if not ok:
        k32.CloseHandle(job)
        return None
    return int(job)


def _win_terminate_job(job: int) -> None:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.TerminateJobObject(job, 1)
    k32.CloseHandle(job)


def _child_main() -> int:
    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    stores = Stores(meta=Path(req["meta"]), content=Path(req["content"]))
    m = req["manifest"]
    outcome = rescreen_one(stores, m, dict(os.environ))
    sys.stdout.write(json.dumps({"outcome": outcome, "manifest": m}))
    return 0


def _counts(sums: list[dict[str, Any]]) -> dict[str, Any]:
    return {"episodes": len(sums),
            "observation_sufficient": sum(1 for s in sums if s["sufficient"]),
            "dependency_screen_clear": sum(1 for s in sums if s["dependency_screen_clear"]),
            "by_origin": dict(sorted(Counter(str(s["origin"]) for s in sums).items())),
            "has_attachment": sum(1 for s in sums if s["has_attachment"]),
            "required_no": {c: sum(1 for s in sums if s["required"][c] == "no") for c in mf.DEP_CATEGORIES},
            "rules_yes": {r: sum(1 for s in sums if s["rules"].get(r) == "yes") for r in mf.RULES},
            "rules_unknown": {r: sum(1 for s in sums if s["rules"].get(r) == "unknown") for r in mf.RULES},
            "by_cause": dict(sorted(Counter(c for s in sums for c in s["causes"]).items()))}


def run_rescreen(stores: Stores, env: dict[str, str], *, now: str | None = None,
                 commit: bool = True, episode_timeout_s: float = EPISODE_TIMEOUT_S,
                 run_budget_s: float = RUN_BUDGET_S, child_argv: tuple[str, ...] = CHILD_ARGV) -> dict[str, Any]:
    """Re-screen every joined episode under the maintenance lock; the corrections record."""
    lock = stores.meta / MAINTAIN_LOCK
    if not _try_lock(lock):
        return {"action": "skipped", "reason": "maintenance running"}
    now = now or iso(now_utc())
    tool_commit, _ = tool_identity()
    try:
        scan = scan_manifests(stores)
        befores, afters, changed, busy, deferred = [], [], [], [], []
        outcomes: Counter[str] = Counter()
        touched = []
        deadline = time.monotonic() + run_budget_s
        usable, malformed = [], []
        for m in scan.manifests:
            (usable if _well_formed(m) else malformed).append(m)
        for m in sorted(usable, key=lambda x: x["started_at"]):
            if (m["capture"].get("join") or {}).get("state") != "joined":
                continue
            ep = m["episode_id"]
            if time.monotonic() >= deadline:
                deferred.append(ep)
                continue
            elock = episode_lock(stores, ep)
            if not _try_lock(elock):
                busy.append(ep)
                continue
            try:
                before = summary(m)
                join_before = dict(m["capture"].get("join") or {})
                outcome, done = run_child(stores, m, env, episode_timeout_s, child_argv)
                if done is not None:
                    m = done
                else:
                    invalidate(m, f"rescreen_{outcome}")
                    outcome = f"invalidated_{outcome}"
                mf.refresh(m, (load_owner_review(stores, ep) or {}).get("rules"))
                after = summary(m)
                outcomes[outcome] += 1
                befores.append(before)
                afters.append(after)
                if after != before or m["capture"].get("join") != join_before:
                    m.setdefault("corrections", []).append(
                        {"at": now, "by": "rescreen", "tool_commit": tool_commit, "outcome": outcome,
                         "before": before, "after": after})
                    write_manifest(stores, m)
                    touched.append(manifest_path(stores, ep))
                    changed.append({"episode_id": ep, "outcome": outcome,
                                    "fields": sorted(k for k in after if after[k] != before[k])})
            finally:
                _unlock(elock)
        rec = {"kind": "rescreen", "at": now, "tool_commit": tool_commit, "outcomes": dict(sorted(outcomes.items())),
               "busy_skipped": busy, "deferred": deferred,
               "limits": {"episode_timeout_s": episode_timeout_s, "run_budget_s": run_budget_s}, "before": _counts(befores), "after": _counts(afters),
               "changed": changed, "manifests_unreadable": len(scan.unreadable),
               "manifests_malformed": sorted(str(m.get("episode_id")) for m in malformed)}
        path = _new_record_path(stores.meta / "corrections", f"{now.replace(':', '').replace('-', '')}-rescreen")
        write_json_atomic(path, rec)
        if commit:
            rec["committed"] = commit_meta(stores, touched + [path],
                                           f"capture rescreen: {len(changed)} manifest(s) corrected")
        rec["record"] = str(path)
        return rec
    finally:
        _unlock(lock)


if __name__ == "__main__" and sys.argv[1:] == ["--child"]:
    sys.exit(_child_main())

#!/usr/bin/env python3
"""Integration tests: causal start/end states wired into the capture hooks (plan D3/D4).

Synthetic only: repositories, stores, transcripts and subagent transcripts are created
in a temporary directory; the hook scripts run as subprocesses with an isolated home.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
import causal  # noqa: E402
import causal_admit  # noqa: E402
import causal_hooks  # noqa: E402
import dryrun  # noqa: E402
import review  # noqa: E402
from common import iso, now_utc  # noqa: E402
from test_hooks import PROMPT, Fixture  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
AGENT_ID = "a1b2c3d4e5f6a7b8c"

# The hook wall-clock budgets (start 2 s, ends 20 s / 8 s) are product behaviour, asserted
# in-process below (skipped_budget, bytes budget). Under machine load a subprocess hook can
# overrun them, which records partial / skipped_budget errors these attribution tests never
# meant to exercise, so here the hooks run with budgets far above any load.
UNBUDGETED_S = 120
_UNBUDGETED_SHIM = (
    "import runpy, sys; sys.path.insert(0, sys.argv[1]); import causal_hooks, pipeline; "
    "s = float(sys.argv[3]); pipeline.START_BUDGET_MS = s * 1000; pipeline.END_BUDGET_S = s; "
    "causal_hooks.END_BUDGET_S = s; sys.argv = sys.argv[2:3]; "
    "runpy.run_path(sys.argv[0], run_name='__main__')"
)


def run_hook_unbudgeted(script: str, payload: dict | bytes, env: dict[str, str]) -> tuple[int, bytes, float]:
    """``dryrun.run_hook`` with the hook budgets raised to ``UNBUDGETED_S``."""
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, "-c", _UNBUDGETED_SHIM, str(PKG), str(PKG / script), str(UNBUDGETED_S)],
                          input=data, capture_output=True, env=env, timeout=UNBUDGETED_S * 2)
    return proc.returncode, proc.stdout, (time.perf_counter() - t0) * 1000


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout


def status(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
                          capture_output=True, text=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout


def git_state(repo: Path) -> dict[str, bytes]:
    g = repo / ".git"
    return {n: (g / n).read_bytes() for n in ("index", "config", "HEAD") if (g / n).is_file()}


def turn(fx: Fixture, sid: str, *, subagent_write: str | None = None) -> Path:
    """Start, then the agent's edit of ``src/alpha.py`` (and a delegated write), then the transcript."""
    pid, ts = fx.start(sid)
    alpha = fx.repo / "src" / "alpha.py"
    alpha.write_bytes(alpha.read_bytes().replace(b"x * 3 + 1", b"x * 3 + 2"))
    entries = dryrun.transcript_entries(sid, fx.repo, PROMPT, pid, ts)
    if subagent_write:
        (fx.repo / subagent_write).write_text("delegated\n", encoding="utf-8")
        tid = f"toolu_{sid[:8]}_agent"
        base = {"sessionId": sid, "cwd": str(fx.repo), "version": dryrun.CLI_VERSION, "timestamp": ts}
        entries[-1:-1] = [
            {**base, "type": "assistant", "message": {"id": "msg_ag", "role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": "Agent", "input": {"prompt": "write it"}}]}},
            {**base, "type": "user", "toolUseResult": {"status": "completed", "agentId": AGENT_ID},
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": tid, "content": f"done\nagentId: {AGENT_ID}"}]}}]
        sub = fx.tdir / "fixture-project" / sid / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        sb = {"isSidechain": True, "agentId": AGENT_ID, "sessionId": sid, "timestamp": ts, "cwd": str(fx.repo)}
        lines = [{**sb, "type": "user", "message": {"role": "user", "content": "write it"}},
                 {**sb, "type": "assistant", "message": {"id": "m_s0", "role": "assistant", "content": [
                     {"type": "tool_use", "id": "toolu_s0", "name": "Write",
                      "input": {"file_path": str(fx.repo / subagent_write), "content": "delegated\n"}}]}},
                 {**sb, "type": "user", "message": {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "toolu_s0", "content": "ok"}]}}]
        (sub / f"agent-{AGENT_ID}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        (sub / f"agent-{AGENT_ID}.meta.json").write_text(json.dumps({"toolUseId": tid}), encoding="utf-8")
    return dryrun.write_transcript(fx.tdir, sid, entries)


def cdir(fx: Fixture, m: dict) -> Path:
    return causal_hooks.causal_dir(fx.stores.content, m["episode_id"])


# --------------------------------------------------------------------------- tests

def test_start_end_preserved_and_attributed(root: Path) -> None:
    fx = Fixture(root)
    (fx.repo / "scratch.txt").write_text("untracked before\n", encoding="utf-8")  # dirty at start
    sid = str(uuid.uuid4())
    before = git_state(fx.repo)
    tp = turn(fx, sid, subagent_write="NEW.md")
    mid_status = status(fx.repo)
    fx.end(sid, tp)
    m = fx.episode(sid)
    c = m["causal"]
    assert c["errors"] == [], c["errors"]
    assert c["start"]["overlay_files"] == 1 and c["end"]["overlay_files"] == 3, c
    assert c["attribution"] == {"changed": 2, "unattributed": 0, "unattributed_sha256": [],
                                "other_worktrees_moved": 0, "head_moved": False, "isolated": True}, c["attribution"]
    assert c["dependency_drift"] == [], c["dependency_drift"]
    assert m["capture"]["status"] == "complete", m["capture"]
    for side in ("start", "end"):
        d = cdir(fx, m) / side
        assert causal.verify_state(d) == [], side
        assert not os.stat(d / "state.json").st_mode & stat.S_IWRITE, "sealed state is read-only"
    end_rec = causal.read_state(cdir(fx, m) / "end")
    assert (cdir(fx, m) / "end" / "objects" / end_rec["files"]["src/alpha.py"]["sha256"]).read_bytes() == \
        (fx.repo / "src" / "alpha.py").read_bytes(), "end bytes are exact"
    assert git_state(fx.repo) == before, "capture changed index, config or HEAD"
    assert status(fx.repo) == mid_status, "capture changed the checkout"
    text = json.dumps(m)
    assert "alpha.py" not in text and "NEW.md" not in text and str(fx.repo) not in text, "plain path in manifest"


def test_unattributed_change_and_other_worktree(root: Path) -> None:
    fx = Fixture(root)
    wt = fx.root / "wt2"
    git(fx.repo, "worktree", "add", "-q", str(wt), "-b", "other")
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    (fx.repo / "README.md").write_text("someone else\n", encoding="utf-8")  # no tool call wrote this
    (wt / "x.txt").write_text("other lane\n", encoding="utf-8")
    tp = dryrun.write_transcript(fx.tdir, sid, dryrun.transcript_entries(sid, fx.repo, PROMPT, pid, ts, edit=False))
    fx.end(sid, tp)
    a = fx.episode(sid)["causal"]["attribution"]
    assert a["unattributed"] == 1 and a["other_worktrees_moved"] == 1 and not a["isolated"], a
    w = fx.episode(sid)["causal"]["worktrees"]
    assert (w["unrelated"], w["contaminating"], w["unknown"], w["shared_state_changed"]) == (1, 0, 0, False), w


def test_unsupported_reason_explicit(root: Path) -> None:
    fx = Fixture(root)
    (fx.repo / ".git" / "MERGE_HEAD").write_text(git(fx.repo, "rev-parse", "HEAD"), encoding="utf-8")
    sid = str(uuid.uuid4())
    tp = turn(fx, sid)
    fx.end(sid, tp)
    c = fx.episode(sid)["causal"]
    assert c["unsupported_start"] == ["operation_in_progress"] == c["unsupported_end"], c


def test_failures_are_explicit_and_fail_open(root: Path) -> None:
    fx = Fixture(root)
    blocker = fx.stores.content / "causal"
    blocker.write_text("not a directory\n", encoding="utf-8")  # every causal write fails
    sid = str(uuid.uuid4())
    tp = turn(fx, sid)
    fx.end(sid, tp)
    m = fx.episode(sid)
    errs = m["causal"]["errors"]
    assert any(e.startswith("causal:start:error:") for e in errs), errs
    assert any(e.startswith("causal:end:error:") for e in errs), errs
    assert m["causal"]["start"] is None and m["causal"]["end"] is None
    assert m["capture"]["status"] == "complete" and m["review"]["state"] == "materialized", "ordinary capture kept"
    # in-process: budget skip, overflow and a non-repository are named, never raised
    assert causal_hooks.start(fx.repo, "e1", root / "c1", remaining_s=0.1)["errors"] == ["causal:start:skipped_budget"]
    old = causal.OVERLAY_MAX_FILES
    causal.OVERLAY_MAX_FILES = 0
    try:
        out = causal_hooks.start(fx.repo, "e2", root / "c2", remaining_s=5)
    finally:
        causal.OVERLAY_MAX_FILES = old
    assert out["errors"] == ["causal:start:overlay_too_large"], out["errors"]
    nore = root / "plain"
    nore.mkdir()
    assert causal_hooks.start(nore, "e3", root / "c3", remaining_s=5)["errors"][0].startswith("causal:start:")


def test_end_never_recorded_by_maintenance_or_twice(root: Path) -> None:
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    turn(fx, sid)
    m = fx.episode(sid)
    m2 = dict(m)
    causal_hooks.end(m2, fx.repo, fx.stores.content, ["src/alpha.py"])
    first = json.dumps(m2["causal"], sort_keys=True)
    causal_hooks.end(m2, fx.repo, fx.stores.content, ["src/alpha.py"])
    assert json.dumps(m2["causal"], sort_keys=True) == first, "an end is sealed once"
    src = (PKG / "pipeline.py").read_text(encoding="utf-8")
    assert 'if by == "end" and root is not None:' in src, "maintenance finalize must not capture an end"


def test_overlay_materializes_from_frozen_history(root: Path) -> None:
    repo = root / "r x"  # a space in the top-level path (regression: rev-parse output split)
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "core.autocrlf", "true")
    (repo / "a.txt").write_bytes(b"one\r\ntwo\r\n")
    (repo / "b.txt").write_bytes(b"keep\n")
    (repo / "c.txt").write_bytes(b"gone\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    for name in ("a.txt", "b.txt", "c.txt"):  # a checkout written by git, as a real worktree is
        (repo / name).unlink()
    git(repo, "checkout", "-q", "--", ".")
    (repo / "a.txt").write_bytes(b"one\r\nTWO\r\n")
    (repo / "c.txt").unlink()
    (repo / "new.bin").write_bytes(b"\x00\x01raw\r\n")
    st = root / "state"
    rec = causal.capture_overlay(repo, st, label="start")
    assert sorted(rec["files"]) == ["a.txt", "new.bin"] and rec["deleted"] == ["c.txt"], rec
    hist = root / "hist"
    causal.freeze_history(repo, hist)
    out = root / "out"
    assert causal.materialize_overlay(st, hist, out) == []
    for name in ("a.txt", "new.bin", "b.txt"):
        assert (out / name).read_bytes() == (repo / name).read_bytes(), name
    assert not (out / "c.txt").exists()
    s_eff, e_eff = causal.effective_oids(repo, rec, rec)
    assert causal.attribute(s_eff, e_eff, [])["isolated"]
    real = causal._read_entry  # a write landing between the reads is refused, never sealed
    calls = {"n": 0}

    def racing(top: Path, rel: str):  # noqa: ANN202
        calls["n"] += 1
        got = real(top, rel)
        return ({**got[0], "sha256": "0" * 64}, got[1]) if got and calls["n"] > 2 * len(rec["files"]) else got
    causal._read_entry = racing
    try:
        causal.capture_overlay(repo, root / "state2", label="start")
        raise AssertionError("concurrent change not detected")
    except causal.CausalError as exc:
        assert str(exc) == "concurrent_change", exc
    finally:
        causal._read_entry = real


def test_causal_states_follow_snapshot_retention(root: Path) -> None:
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    tp = turn(fx, sid)
    fx.end(sid, tp)
    m = fx.episode(sid)
    assert cdir(fx, m).is_dir()
    later = iso(now_utc() + timedelta(days=review.RETENTION_DAYS + 30))
    kinds = {d["kind"] for d in review.overdue(fx.stores, now=later) if d["episode_id"] == m["episode_id"]}
    assert "causal" in kinds, kinds
    review.expire(fx.stores, now=later)
    assert not cdir(fx, m).exists(), "expired causal state deleted"
    log = (fx.stores.meta / "DELETIONS.log").read_text(encoding="utf-8")
    assert '"kind": "causal"' in log
    rec = [json.loads(x) for x in log.splitlines() if '"kind": "causal"' in x][-1]
    assert rec["verified"] and rec["listed_paths"] > 0, rec


def test_freeze_register_admit(root: Path) -> None:
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    fx.end(sid, turn(fx, sid))
    ep = fx.episode(sid)["episode_id"]
    before = causal_admit.admit(fx.stores, ep)
    assert {"history_missing", "no_outcome_check"} <= set(before["causal_reasons"]), before
    assert not before["eligible"]
    causal_admit.freeze(fx.stores, ep, fx.repo)
    refused = (
        ([{"kind": "command", "argv": ["python", "-m", "pytest"], "asserts": "suite passes"}], "generic"),
        ([{"kind": "command", "argv": ["python", "-m", "pytest", "tests"], "target": "tests",
           "asserts": "suite passes"}], "generic target"),
        ([{"kind": "command", "argv": ["python", "t_alpha.py"], "target": "t_alpha.py"}], "no asserts"),
        ([{"kind": "command", "argv": ["python", str(root / "t.py")], "target": "t.py", "asserts": "x"}], "abs"),
        ([{"kind": "file_absent", "path": "../x"}], "escapes"),
        ([{"kind": "file_absent", "path": "x"}], ""),
        ([], "empty"),
    )
    for bad, why in refused:
        try:
            causal_admit.register(fx.stores, ep, bad, why)
            raise AssertionError(f"registration should be refused: {why}")
        except causal.CausalError:
            pass
    # a behavioral predicate needs no file_* check; a generic command may ride along
    causal.validate_predicate([{"kind": "command", "argv": ["python", "-m", "pytest", "tests/test_alpha.py::k"],
                                "target": "tests/test_alpha.py::k", "asserts": "alpha returns 3x+2"},
                               {"kind": "command", "argv": ["python", "-m", "pytest"], "asserts": "no regressions"}])
    calls: list[str] = []
    try:
        causal_admit.launch(fx.stores, ep, lambda a: calls.append("ran"))
        raise AssertionError("launch without a registration must refuse")
    except causal_admit.RunRefused:
        pass
    assert calls == [], "the model launcher was called before authorization"
    probe = "import sys; sys.exit(0 if b'x * 3 + 2' in open('src/alpha.py','rb').read() else 1)"
    checks = [{"kind": "file_contains", "path": "src/alpha.py", "text": "x * 3 + 2"},
              {"kind": "command", "argv": ["python", "-c", probe], "target": "src/alpha.py",
               "asserts": "alpha's constant is 2"}]
    causal_admit.register(fx.stores, ep, checks, "the task changes alpha's constant")
    try:
        causal_admit.register(fx.stores, ep, checks, "again")
        raise AssertionError("re-registration should be refused")
    except causal.CausalError:
        pass
    assert causal_admit.verify_registration(fx.stores, ep)[1] == []
    res = causal_admit.admit(fx.stores, ep)
    assert [r["passed"] for r in res["check_results"]] == [True, True], res
    assert [r["passed"] for r in res["check_results_start"]] == [False, False], res
    bad = {"history_missing", "no_outcome_check", "end_unverified", "history_unverified", "start_fidelity_not_proven",
           "end_fidelity_not_proven", "predicate_not_discriminating", "predicate_rejects_reference_end",
           "outcome_check_registration_unverified", "worktree_classification_missing"}
    assert not bad & set(res["causal_reasons"]), res
    assert not any("fidelity" in r for r in res["causal_reasons"]), res
    assert "registered" in git(fx.stores.meta, "log", "-1", "--format=%s")
    # tampering after registration: the guard refuses before any launch
    p = causal_admit.checks_path(fx.stores, ep)
    os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    original = p.read_bytes()
    p.write_bytes(original.replace(b"x * 3 + 2", b"x * 3 + 9"))
    assert causal_admit.verify_registration(fx.stores, ep)[1], "tampered registration verified"
    assert "outcome_check_registration_unverified" in causal_admit.admit(fx.stores, ep)["causal_reasons"]
    try:
        causal_admit.launch(fx.stores, ep, lambda a: calls.append("ran"))
        raise AssertionError("tampered registration must refuse")
    except causal_admit.RunRefused:
        pass
    assert calls == []
    p.write_bytes(original)
    # a predicate already true at start does not measure the task
    sid2 = str(uuid.uuid4())
    fx.end(sid2, turn(fx, sid2))
    ep2 = fx.episode(sid2)["episode_id"]
    causal_admit.freeze(fx.stores, ep2, fx.repo)
    causal_admit.register(fx.stores, ep2, [{"kind": "file_contains", "path": "src/alpha.py", "text": "def "}], "weak")
    assert "predicate_not_discriminating" in causal_admit.admit(fx.stores, ep2)["causal_reasons"]
    sid3 = str(uuid.uuid4())
    fx.end(sid3, turn(fx, sid3))
    ep3 = fx.episode(sid3)["episode_id"]
    causal_admit.replay_dir(fx.stores, ep3).mkdir(parents=True)
    try:
        causal_admit.register(fx.stores, ep3, checks, "late")
        raise AssertionError("registration after replay outputs should be refused")
    except causal.CausalError as exc:
        assert "before replay" in str(exc)


def test_fidelity_is_byte_exact_not_git_equivalent(root: Path) -> None:
    repo = root / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "lf.txt").write_bytes(b"checked out before autocrlf was set\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "lf")
    git(repo, "config", "core.autocrlf", "true")  # git still calls lf.txt clean; a checkout would write CRLF
    (repo / "a.txt").write_bytes(b"one\r\n")
    (repo / "b.txt").write_bytes(b"two\r\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    for n in ("a.txt", "b.txt"):
        (repo / n).unlink()
    git(repo, "checkout", "-q", "--", "a.txt", "b.txt")
    assert status(repo) == ""
    hist = root / "hist"
    causal.freeze_history(repo, hist)
    good = causal.capture_overlay(repo, root / "s1", label="start")
    assert good["bytes"] and good["bytes_problem"] is None, good
    assert causal.materialize_overlay(root / "s1", hist, root / "o1") == []
    assert causal.verify_fidelity(root / "s1", root / "o1") == []
    assert (root / "o1" / "lf.txt").read_bytes() == b"checked out before autocrlf was set\n"
    (repo / "b.txt").write_bytes(b"two\n")  # git may call this clean (normalized); the bytes differ
    causal.capture_overlay(repo, root / "s2", label="start")
    (root / "o1" / "b.txt").write_bytes(b"two\r\n")
    assert causal.verify_fidelity(root / "s2", root / "o1") == ["fidelity_bytes_differ:1"]
    (root / "o1" / "extra").write_bytes(b"")
    (root / "o1" / "a.txt").unlink()
    got = causal.verify_fidelity(root / "s1", root / "o1")
    assert "fidelity_missing:1" in got and "fidelity_extra:1" in got, got
    over = causal.capture_overlay(repo, root / "s3", label="start", bytes_deadline=time.monotonic() - 1)
    assert over["bytes"] is None and over["bytes_problem"] == "budget", over
    assert causal.verify_fidelity(root / "s3", root / "o1") == ["fidelity_unproven:budget"]
    assert "start_fidelity_not_proven" in causal.admit({"start_fidelity": None})["reasons"]


def test_other_worktree_classification(root: Path) -> None:
    rec = {"fp": "a", "head": "h", "paths": []}
    moved = {"fp": "b", "head": "h", "paths": ["x.txt"]}
    s, e = {"/r/wt2": rec}, {"/r/wt2": moved}
    read = [{"input": {"file_path": "/r/main/src/a.py"}}]

    def cls(**kw):  # noqa: ANN003, ANN202
        args = {"shared_start": "s", "shared_end": "s", "calls": read, "subagents_unresolved": 0, "top": "/r/main"}
        return causal.classify_worktrees(s, e, **{**args, **kw})
    assert cls()["unrelated"] == ["/r/wt2"]
    assert cls(calls=None)["unknown"] == ["/r/wt2"], "missing tool evidence is unknown, not unrelated"
    assert cls(subagents_unresolved=1)["unknown"] == ["/r/wt2"], "unresolved subagent is unknown"
    assert cls(shared_end="t")["contaminating"] == ["/r/wt2"], "shared git state moved"
    assert cls(shared_end=None)["unknown"] == ["/r/wt2"]
    assert cls(calls=[{"input": {"command": "type \\r\\wt2\\x.txt"}}])["contaminating"] == ["/r/wt2"]
    assert cls(calls=[{"input": {"command": "npm install"}}])["contaminating"] == ["/r/wt2"], "shared caches"
    assert cls(calls=[{"input": {"command": "cat ../x"}}])["unknown"] == ["/r/wt2"]
    assert causal.classify_worktrees(None, e, shared_start="s", shared_end="s", calls=read,
                                     subagents_unresolved=0)["unknown"] == ["records_missing"]
    nested = "/r/main/lanes/l"
    got = causal.classify_worktrees({nested: rec}, {nested: moved}, shared_start="s", shared_end="s",
                                    calls=[{"input": {"pattern": "x", "path": "lanes/l"}}],
                                    subagents_unresolved=0, top="/r/main")
    assert got["contaminating"] == [nested], got
    v = causal.admit({"worktrees": {"contaminating": [], "unknown": ["x"], "shared_state_changed": False}})
    assert "other_worktree_unknown" in v["reasons"] and "other_worktree_moved" not in v["reasons"], v


TESTS = [test_freeze_register_admit, test_start_end_preserved_and_attributed, test_unattributed_change_and_other_worktree,
         test_unsupported_reason_explicit, test_failures_are_explicit_and_fail_open,
         test_end_never_recorded_by_maintenance_or_twice, test_overlay_materializes_from_frozen_history,
         test_causal_states_follow_snapshot_retention, test_fidelity_is_byte_exact_not_git_equivalent,
         test_other_worktree_classification]


def main() -> int:
    only = sys.argv[1:]
    dryrun.run_hook = run_hook_unbudgeted  # Fixture (test_hooks) calls the hooks through it
    with tempfile.TemporaryDirectory(prefix="ec-causal-hooks-") as tmp:
        for fn in TESTS:
            if only and fn.__name__ not in only:
                continue
            case = Path(tmp) / fn.__name__
            case.mkdir()
            try:
                fn(case)
                PASSED.append(fn.__name__)
            except Exception:  # noqa: BLE001 — plain-check runner
                FAILED.append(fn.__name__)
                print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    for name in PASSED:
        print(f"ok   {name}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

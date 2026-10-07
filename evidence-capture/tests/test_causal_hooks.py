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
import traceback
import uuid
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
import causal  # noqa: E402
import causal_hooks  # noqa: E402
import dryrun  # noqa: E402
import review  # noqa: E402
from common import iso, now_utc  # noqa: E402
from test_hooks import PROMPT, Fixture  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
AGENT_ID = "a1b2c3d4e5f6a7b8c"


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
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


TESTS = [test_start_end_preserved_and_attributed, test_unattributed_change_and_other_worktree,
         test_unsupported_reason_explicit, test_failures_are_explicit_and_fail_open,
         test_end_never_recorded_by_maintenance_or_twice, test_overlay_materializes_from_frozen_history,
         test_causal_states_follow_snapshot_retention]


def main() -> int:
    only = sys.argv[1:]
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

#!/usr/bin/env python3
"""Step-1 tests for the capture tool: snapshot, manifest rules, linkage, join, screen.

No framework, plain checks, synthetic fixtures only: temporary git repositories, hand-
built transcripts and packets. No model call, no network, no real repository, no real
store. Everything is created under one temporary directory and removed afterwards.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
import common  # noqa: E402
import join  # noqa: E402
import linkage  # noqa: E402
import manifest as mf  # noqa: E402
import screen  # noqa: E402
import snapshot  # noqa: E402
import store  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
SESSION = "11111111-2222-3333-4444-555555555555"
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=GIT_ENV).stdout


def make_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "alpha.py").write_text("def alpha(x: int) -> int:\n    return x * 3 + 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "start")
    return repo


def make_stores(root: Path, name: str = "st") -> store.Stores:
    st = store.Stores(root / f"{name}-meta", root / f"{name}-content")
    store.init_stores(st)
    git(st.meta, "init", "-q")
    return st


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def base_manifest(prompt: str, started: str = "2026-09-26T10:00:00.000000Z", session: str = SESSION) -> dict:
    sha = common.sha256_text(prompt)
    return mf.new_manifest(episode_id=common.episode_id(session, sha, started), session_id=session,
                           started_at=started, prompt_sha256=sha, prompt_len=len(prompt),
                           tool_commit=None, tool_sha256=None, cwd_sha256="0" * 64)


def packet(pid: str, prompt: str, created: str, head_sha: str | None, session: str = SESSION) -> dict:
    return {"packet_id": pid, "created_at": created, "session_id": session, "head": head_sha,
            "head_state": None if head_sha else "probe_timeout", "prompt_hash": common.sha256_text(prompt), "path": ""}


# --------------------------------------------------------------- synthetic transcripts

def transcript_lines(turns: list[dict], version: str = "9.9.9") -> list[str]:
    """``turns``: [{prompt, origin?, meta?, tools:[{name,input,result,error?}], brief?}]."""
    lines: list[str] = []
    n = 0
    for t in turns:
        n += 1
        ts = f"2026-09-26T10:{n:02d}:00.000Z"
        entry = {"type": "user", "uuid": f"u-{n}", "timestamp": ts, "sessionId": SESSION, "version": version,
                 "message": {"role": "user", "content": t["prompt"]}}
        if t.get("meta"):
            entry["isMeta"] = True
        if t.get("origin"):
            entry["origin"] = {"kind": t["origin"]}
        lines.append(json.dumps(entry))
        if t.get("brief"):
            lines.append(json.dumps({"type": "attachment", "timestamp": ts, "sessionId": SESSION,
                                     "attachment": {"type": "hook_additional_context",
                                                    "content": [f'<context_brief packet_id="{t["brief"]}">x</context_brief>']}}))
        for k, tool in enumerate(t.get("tools", [])):
            tid = f"toolu_{n}_{k}"
            lines.append(json.dumps({"type": "assistant", "timestamp": ts, "sessionId": SESSION, "version": version,
                                     "message": {"id": f"msg_{n}_{k}", "role": "assistant", "content": [
                                         {"type": "tool_use", "id": tid, "name": tool["name"], "input": tool["input"]}]}}))
            lines.append(json.dumps({"type": "user", "timestamp": ts, "sessionId": SESSION,
                                     "message": {"role": "user", "content": [
                                         {"type": "tool_result", "tool_use_id": tid, "is_error": bool(tool.get("error")),
                                          "content": tool.get("result", "ok")}]}}))
        if t.get("tools") or t.get("reply", True):
            lines.append(json.dumps({"type": "assistant", "timestamp": ts, "sessionId": SESSION, "version": version,
                                     "message": {"id": f"msg_{n}_end", "role": "assistant",
                                                 "content": [{"type": "text", "text": "done"}]}}))
    return lines


def write_transcript(root: Path, turns: list[dict], name: str = SESSION) -> Path:
    p = root / f"{name}.jsonl"
    p.write_text("\n".join(transcript_lines(turns)) + "\n", encoding="utf-8")
    return p


def joined(root: Path, repo: Path, turns: list[dict], which: int, complete: bool = True,
           untracked: tuple[str, ...] = ()) -> dict:
    """Manifest for turn ``which`` of ``turns`` after join + screen + refresh."""
    tr = write_transcript(root, turns)
    prompt = turns[which]["prompt"]
    m = base_manifest(prompt, started=f"2026-09-26T10:{which + 1:02d}:00.000000Z")
    m["repo"]["head"] = head(repo)
    obs = join.join(m, tr)
    assert obs is not None, "turn not found"
    m["capture"]["status"] = "complete" if complete else "partial"
    m["start_snapshot"] = {"state": "complete", "reason": None}
    m["boundary"] = {"cli_version": "9.9.9", "trusted": True, "reason": None}
    m["link"]["state"] = "linked"
    screen.screen(m, obs, repo, cwd=str(repo), untracked_paths=untracked, complete=complete)
    return mf.refresh(m)


def rule(m: dict, r: str) -> str:
    return mf.rule_state(m["exclusions"], r)


# --------------------------------------------------------------------------- tests

def test_snapshot_clean_and_dirty(root: Path) -> None:
    repo, st = make_repo(root), make_stores(root)
    fields, snap = snapshot.take_snapshot(repo, "cap_clean", st)
    assert snap["state"] == "complete", snap
    assert snap["dirty"] is False and fields["head"] == head(repo) and fields["branch"] == "main"
    assert snapshot.snapshot_ok(st, "cap_clean")
    assert not (st.snapshots / "cap_clean" / "INCOMPLETE").exists()
    # dirty: staged + unstaged in one file, untracked, ignored
    (repo / "src" / "alpha.py").write_text("def alpha(x: int) -> int:\n    return x * 3 + 2\n", encoding="utf-8")
    git(repo, "add", "src/alpha.py")
    (repo / "src" / "alpha.py").write_text("def alpha(x: int) -> int:\n    return x * 3 + 3\n", encoding="utf-8")
    (repo / "notes.txt").write_bytes(b"untracked\n")
    (repo / "debug.log").write_text("ignored\n", encoding="utf-8")
    fields, snap = snapshot.take_snapshot(repo, "cap_dirty", st)
    assert snap["state"] == "complete", snap
    assert snap["dirty"] and snap["untracked_count"] == 1 and snap["ignored_count"] == 1
    d = st.snapshots / "cap_dirty"
    assert b"x * 3 + 2" in (d / "index.patch").read_bytes()
    assert b"x * 3 + 3" in (d / "worktree.patch").read_bytes()
    assert (d / "untracked" / snap["untracked"][0]["sha256"]).read_bytes() == b"untracked\n"
    assert not any(p.is_file() and p.read_bytes() == b"ignored\n" for p in d.rglob("*")), "ignored bytes must never be captured"
    assert not store.verify_sums(d)
    assert snap["bracket"]["stable"] is True and snap["bracket"]["diff"] == []
    assert not list(st.tmp.iterdir()), "tmp must be empty after a snapshot"


def test_snapshot_stability_and_index_untouched(root: Path) -> None:
    """Capture commands must not rewrite .git/index even when the stat cache is stale."""
    repo, st = make_repo(root), make_stores(root)
    (repo / "notes.txt").write_text("u\n", encoding="utf-8")
    index = repo / ".git" / "index"
    # stale stat cache: rewrite a tracked file with identical content and a new mtime
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    future = time.time() + 5
    os.utime(repo / "README.md", (future, future))
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    _, snap1 = snapshot.take_snapshot(repo, "cap_s1", st)
    after = (index.read_bytes(), index.stat().st_mtime_ns)
    assert snap1["state"] == "complete", snap1
    assert before == after, "capture refreshed the index"
    assert snap1["bracket"]["pre"]["index_sha256"] == snap1["bracket"]["post"]["index_sha256"]
    # a second capture of the same tree yields byte-identical artifacts
    _, snap2 = snapshot.take_snapshot(repo, "cap_s2", st)
    for key in ("status_sha256", "index_patch_sha256", "worktree_patch_sha256", "index_sha256"):
        assert snap1[key] == snap2[key], key
    assert store.read_sums(st.snapshots / "cap_s1") == store.read_sums(st.snapshots / "cap_s2")
    # control: the same status command WITHOUT GIT_OPTIONAL_LOCKS=0 is allowed to refresh the index
    os.utime(repo / "README.md", (future + 5, future + 5))
    env = {k: v for k, v in os.environ.items() if k != "GIT_OPTIONAL_LOCKS"}
    subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v2"], check=True, capture_output=True, env=env)
    control_changed = (index.read_bytes(), index.stat().st_mtime_ns) != after
    REPORT["index_refresh_without_optional_locks_off"] = control_changed


def test_concurrent_change_bracket(root: Path) -> None:
    repo, st = make_repo(root), make_stores(root)
    (repo / "notes.txt").write_text("u\n", encoding="utf-8")
    orig = snapshot._read_artifacts
    calls = {"n": 0}

    def tamper(r: Path, dl: snapshot.Deadline) -> dict[str, bytes]:
        calls["n"] += 1
        if calls["n"] == 1:
            (r / "src" / "alpha.py").write_text("changed mid-capture\n", encoding="utf-8")
        return orig(r, dl)

    snapshot._read_artifacts = tamper
    try:
        _, snap = snapshot.take_snapshot(repo, "cap_cc1", st)
    finally:
        snapshot._read_artifacts = orig
    assert snap["state"] == "failed" and snap["reason"] == "concurrent_change", snap
    assert "status_sha256" in snap["bracket"]["diff"]
    assert not (st.snapshots / "cap_cc1").exists() and not list(st.tmp.iterdir())
    git(repo, "checkout", "-q", "--", "src/alpha.py")

    # untracked file appearing between passes
    def add_untracked(r: Path, dl: snapshot.Deadline) -> dict[str, bytes]:
        calls["n"] += 1
        if calls["n"] == 3:
            (r / "late.txt").write_text("late\n", encoding="utf-8")
        return orig(r, dl)

    snapshot._read_artifacts = add_untracked
    try:
        _, snap = snapshot.take_snapshot(repo, "cap_cc2", st)
    finally:
        snapshot._read_artifacts = orig
    assert snap["state"] == "failed" and snap["reason"] == "concurrent_change", snap
    (repo / "late.txt").unlink()

    # index write between passes (git add) → index sha/mtime differ
    def stage(r: Path, dl: snapshot.Deadline) -> dict[str, bytes]:
        calls["n"] += 1
        if calls["n"] == 5:
            git(r, "add", "notes.txt")
        return orig(r, dl)

    snapshot._read_artifacts = stage
    try:
        _, snap = snapshot.take_snapshot(repo, "cap_cc3", st)
    finally:
        snapshot._read_artifacts = orig
    assert snap["state"] == "failed" and snap["reason"] == "concurrent_change", snap
    assert "index_sha256" in snap["bracket"]["diff"]
    git(repo, "reset", "-q")

    # a lock file at the bracket
    lock = repo / ".git" / "index.lock"
    lock.write_bytes(b"")
    try:
        _, snap = snapshot.take_snapshot(repo, "cap_cc4", st)
    finally:
        lock.unlink()
    assert snap["state"] == "failed" and snap["reason"] == "concurrent_change" and "lock" in snap["detail"], snap
    # each failed snapshot is permanently ineligible
    for ep in ("cap_cc1", "cap_cc2", "cap_cc3", "cap_cc4"):
        m = base_manifest("p")
        m["start_snapshot"] = snap
        mf.refresh(m)
        assert rule(m, "X6") == "yes" and not m["funnel"]["start_snapshot_complete"]


def test_snapshot_failed_is_final(root: Path) -> None:
    repo, st = make_repo(root), make_stores(root)
    (repo / "big.bin").write_bytes(b"\0" * 4096)
    _, cap = snapshot.take_snapshot(repo, "cap_cap", st, size_cap=1024)
    assert cap["state"] == "failed" and cap["reason"] == "size_cap", cap
    _, to = snapshot.take_snapshot(repo, "cap_to", st, budget_s=0.0)
    assert to["state"] == "failed" and to["reason"] == "timeout", to
    (repo / "big.bin").unlink()
    for snap in (cap, to):
        m = base_manifest("p")
        m["start_snapshot"] = snap
        m["capture"]["status"] = "complete"
        mf.refresh(m)
        assert rule(m, "X6") == "yes"
        # an owner review entry cannot repair X6, and X7 is likewise owner-proof
        mf.refresh(m, owner={"X6": {"state": "no", "basis": "trust me"}, "X7": {"state": "no", "basis": "x"}})
        assert rule(m, "X6") == "yes" and not m["funnel"]["start_snapshot_complete"]
    # a later run with a clean tree does not repair: the same episode is never re-snapshotted
    assert not snapshot.snapshot_ok(st, "cap_cap") and not snapshot.snapshot_ok(st, "cap_to")
    _, ok = snapshot.take_snapshot(repo, "cap_ok", st)
    assert ok["state"] == "complete"
    _, again = snapshot.take_snapshot(repo, "cap_ok", st)
    assert again["state"] == "failed" and again["reason"] == "write_error", again  # never overwritten
    assert snapshot.snapshot_ok(st, "cap_ok")


def test_snapshot_atomic(root: Path) -> None:
    repo, st = make_repo(root), make_stores(root)
    # a snapshot dir left with INCOMPLETE (a kill mid-write after the rename would be impossible;
    # a kill before it leaves a tmp dir) is failed either way
    d = st.snapshots / "cap_killed"
    d.mkdir()
    (d / "status.v2z").write_bytes(b"")
    (d / "INCOMPLETE").write_bytes(b"")
    store.write_sums(d)
    assert not snapshot.snapshot_ok(st, "cap_killed")
    orphan = store.new_tmp_dir(st, "cap_orphan")
    (orphan / "INCOMPLETE").write_bytes(b"")
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    removed = store.clean_tmp_orphans(st)
    assert removed and not orphan.exists(), removed
    # a partially written snapshot (missing file) fails verification
    _, ok = snapshot.take_snapshot(repo, "cap_v", st)
    assert ok["state"] == "complete"
    (st.snapshots / "cap_v" / "status.v2z").unlink()
    assert not snapshot.snapshot_ok(st, "cap_v")


def test_capture_without_ec_packet(root: Path) -> None:
    repo, st = make_repo(root), make_stores(root)
    m = base_manifest("fix the parser")
    fields, snap = snapshot.take_snapshot(repo, m["episode_id"], st)
    m["repo"].update(fields)
    m["start_snapshot"] = snap
    m["ended_at"] = "2026-09-26T10:01:00.000000Z"
    m["capture"]["status"] = "complete"
    m["boundary"] = {"cli_version": "9.9.9", "trusted": True, "reason": None}
    assert not (repo / ".evidence-compiler").exists()
    m["link"] = linkage.resolve_link(m, linkage.load_packets(linkage.packet_dirs(repo, st)))
    mf.refresh(m)
    assert m["link"]["state"] == "none" and m["funnel"]["packet_linked"] is False
    assert not mf.provisional_candidate(m["funnel"])
    # reconcilable later: the packet appears → linked, deterministic
    pk = packet("ep_1", "fix the parser", "2026-09-26T10:00:00.500000Z", head(repo))
    m["link"] = linkage.resolve_link(m, [pk])
    assert m["link"]["state"] == "linked" and m["link"]["packet_id"] == "ep_1"
    assert m["link"]["matched_on"] == ["session_id", "prompt_sha256", "created_at", "head"]


def test_link_ambiguous_and_mismatch(root: Path) -> None:
    repo = make_repo(root)
    h = head(repo)
    m = base_manifest("p", started="2026-09-26T10:00:00.000000Z")
    m["ended_at"] = "2026-09-26T10:05:00.000000Z"
    m["repo"]["head"] = h
    two = [packet("ep_a", "p", "2026-09-26T10:00:01Z", h), packet("ep_b", "p", "2026-09-26T10:00:02Z", h)]
    amb = linkage.resolve_link(m, two)
    assert amb["state"] == "ambiguous" and amb["packet_id"] is None, amb
    mis = linkage.resolve_link(m, [packet("ep_c", "p", "2026-09-26T10:00:01Z", "f" * 40)])
    assert mis["state"] == "mismatch", mis
    none_head = linkage.resolve_link(m, [packet("ep_d", "p", "2026-09-26T10:00:01Z", None)])
    assert none_head["state"] == "mismatch", none_head
    # out of bounds, other session, other hash → none (not final)
    for pk in (packet("ep_e", "p", "2026-09-26T11:00:00Z", h), packet("ep_f", "p", "2026-09-26T10:00:01Z", h, session="other"),
               packet("ep_g", "q", "2026-09-26T10:00:01Z", h)):
        assert linkage.resolve_link(m, [pk])["state"] == "none"
    # final states are never recomputed, even when a clean match appears later
    for final in (amb, mis):
        m2 = dict(m, link=final)
        again = linkage.resolve_link(m2, [packet("ep_z", "p", "2026-09-26T10:00:01Z", h)])
        assert again["state"] == final["state"]
        m2["start_snapshot"] = {"state": "complete"}
        m2["boundary"] = {"trusted": True}
        mf.refresh(m2)
        assert not m2["funnel"]["packet_linked"] and not mf.provisional_candidate(m2["funnel"])


def test_link_deterministic(root: Path) -> None:
    repo, st = make_repo(root), make_stores(root)
    h = head(repo)
    pdir = repo / ".evidence-compiler" / "packets"
    pdir.mkdir(parents=True)
    for pid, prompt, created in (("ep_0001", "p", "2026-09-26T10:00:01Z"), ("ep_0002", "q", "2026-09-26T10:00:01Z"),
                                 ("ep_0003", "p", "2026-09-26T09:00:00Z")):
        (pdir / f"{pid}.json").write_text(json.dumps({
            "schema_version": 1, "packet_id": pid, "created_at": created,
            "identity": {"session_id": SESSION, "head": h, "head_state": None},
            "correlation": {"prompt_hash": common.sha256_text(prompt)}}), encoding="utf-8")
    (pdir / "garbage.json").write_text("{not json", encoding="utf-8")
    m = base_manifest("p")
    m["ended_at"] = "2026-09-26T10:01:00Z"
    m["repo"]["head"] = h
    results = set()
    for _ in range(5):
        pk = linkage.load_packets(linkage.packet_dirs(repo, st))
        for order in (pk, list(reversed(pk))):
            link = linkage.resolve_link(m, order)
            results.add((link["state"], link["packet_id"]))
    assert results == {("linked", "ep_0001")}, results
    # reconcile writes and counts
    store.write_manifest(st, m)
    counts = linkage.reconcile(st, [m], {m["episode_id"]: repo})
    assert counts["linked"] == 1 and store.read_manifest(st, m["episode_id"])["link"]["state"] == "linked"
    counts = linkage.reconcile(st, [store.read_manifest(st, m["episode_id"])], {m["episode_id"]: repo})
    assert counts["final_skipped"] == 1


def test_join_turn_and_bounds(root: Path) -> None:
    repo = make_repo(root)
    turns = [
        {"prompt": "first task", "brief": "ep_0123abcd", "tools": [{"name": "Read", "input": {"file_path": str(repo / "README.md")}, "result": "# fixture"}]},
        {"prompt": "<task-notification>done</task-notification>", "meta": True, "reply": False},
        {"prompt": "second task", "tools": [{"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "a", "new_string": "b"}}]},
        {"prompt": "third", "origin": "peer_session"},
    ]
    tr = write_transcript(root, turns)
    m = base_manifest("second task", started="2026-09-26T10:03:00.000000Z")
    obs = join.join(m, tr)
    assert obs is not None
    assert m["prompt_uuid"] == "u-3" and m["prompt"]["origin"] == "user" and m["prompt"]["index_in_session"] == 1
    assert m["ended_by"] == "prompt" and m["boundary"]["cli_version"] == "9.9.9"
    assert [c["name"] for c in obs["tool_calls"]] == ["Edit"]
    assert m["prior_context"]["has_prior_conversation"] is True
    m1 = base_manifest("first task", started="2026-09-26T10:01:00.000000Z")
    obs1 = join.join(m1, tr)
    assert m1["link"]["brief_sha256"] and m1["link"]["brief_len"] > 0 and m1["prompt"]["index_in_session"] == 0
    assert [c["origin"] for c in obs1["continuations"]] == ["task_notification"]
    assert join.join(base_manifest("absent"), tr) is None
    # the peer-session prompt is a boundary but not a human task
    m3 = base_manifest("third", started="2026-09-26T10:04:00.000000Z")
    join.join(m3, tr)
    mf.refresh(m3)
    assert m3["prompt"]["origin"] == "peer_session" and rule(m3, "X1") == "yes"


def test_screen_clean_episode(root: Path) -> None:
    repo = make_repo(root)
    turns = [{"prompt": "fix alpha", "tools": [
        {"name": "Read", "input": {"file_path": str(repo / "src" / "alpha.py")}, "result": "def alpha"},
        {"name": "Bash", "input": {"command": "python -m pytest tests"}, "result": "1 passed"},
        {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}]}]
    m = joined(root, repo, turns, 0)
    assert m["observation"]["sufficient"] is True, m["observation"]
    assert all(m["deps"][c]["required"] == "no" for c in mf.DEP_CATEGORIES), {c: m["deps"][c]["required"] for c in mf.DEP_CATEGORIES}
    assert all(rule(m, r) == "no" for r in mf.RULES), m["exclusions"]
    assert m["funnel"]["dependency_screen_clear"] and mf.provisional_candidate(m["funnel"]) is False  # privacy not yet cleared
    assert m["edited_files"] == [{"path_sha256": common.path_sha256("src/alpha.py")}]
    assert m["task_class"]["mechanical"] == "code_change"


def test_live_remote_and_outward_required(root: Path) -> None:
    repo = make_repo(root)
    turns = [{"prompt": "rebase onto origin/main and push", "tools": [
        {"name": "Bash", "input": {"command": "git fetch origin && git rebase origin/main"}},
        {"name": "Bash", "input": {"command": "git push -u origin HEAD"}},
        {"name": "Edit", "input": {"file_path": str(repo / "README.md"), "old_string": "a", "new_string": "b"}}]}]
    m = joined(root, repo, turns, 0)
    assert m["deps"]["live_remote"]["required"] == "yes" and m["deps"]["outward_action"]["required"] == "yes"
    assert rule(m, "X3") == "yes"
    turns = [{"prompt": "open a PR for this", "tools": [{"name": "Bash", "input": {"command": "gh pr create --fill"}}]}]
    m = joined(root, repo, turns, 0)
    assert m["deps"]["outward_action"]["required"] == "yes" and rule(m, "X3") == "yes"


def test_other_session_required(root: Path) -> None:
    repo = make_repo(root)
    wt = repo / ".claude" / "worktrees" / "lane-a"
    turns = [{"prompt": "fan out to subagents and finish lane-a", "tools": [
        {"name": "Agent", "input": {"prompt": "x"}, "result": "done"},
        {"name": "SendMessage", "input": {"to": "peer", "message": "hi"}},
        {"name": "Edit", "input": {"file_path": str(wt / "src" / "alpha.py"), "old_string": "a", "new_string": "b"}}]}]
    tr = write_transcript(root, turns)
    m = base_manifest("fan out to subagents and finish lane-a", started="2026-09-26T10:01:00.000000Z")
    m["repo"]["head"] = head(repo)
    obs = join.join(m, tr)
    m["capture"]["status"] = "complete"
    screen.screen(m, obs, repo, cwd=str(repo))
    mf.refresh(m)
    assert m["deps"]["other_session"]["required"] == "yes" and rule(m, "X3") == "yes"
    assert "missing_subagent_transcript" in m["observation"]["causes"]


def test_external_repo_doc_required(root: Path) -> None:
    repo = make_repo(root)
    other = make_repo(root, "other")
    doc = "the quick brown fox jumps over the lazy dog, twice\n"
    (other / "DOC.md").write_text(doc, encoding="utf-8")
    turns = [{"prompt": "port the doc", "tools": [
        {"name": "Read", "input": {"file_path": str(other / "DOC.md")}, "result": doc},
        {"name": "Write", "input": {"file_path": str(repo / "DOC.md"), "content": doc}}]}]
    m = joined(root, repo, turns, 0)
    assert m["deps"]["external_repository"]["required"] == "yes", m["deps"]["external_repository"]
    assert any(e["kind"] == "edit_depends_on_read" for e in m["deps"]["external_repository"]["evidence"])
    assert rule(m, "X4") == "yes"


def test_observed_not_required(root: Path) -> None:
    repo = make_repo(root)
    elsewhere = root / "elsewhere"
    elsewhere.mkdir()
    turns = [{"prompt": "fix alpha", "tools": [
        {"name": "Bash", "input": {"command": f"ls {elsewhere}"}, "result": "nothing"},
        {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}]}]
    m = joined(root, repo, turns, 0)
    assert m["deps"]["external_path"]["observed"] == 1
    assert m["deps"]["external_path"]["required"] == "unknown" and rule(m, "X4") == "unknown"


def test_insufficient_observation_stays_unknown(root: Path) -> None:
    repo = make_repo(root)
    edit = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    # shell-path miss: an unknown verb with a path-like argument
    m = joined(root, repo, [{"prompt": "fix", "tools": [{"name": "Bash", "input": {"command": "frobnicate ./src/alpha.py"}}, edit]}], 0)
    assert "shell_path_miss" in m["observation"]["causes"], m["observation"]
    assert m["deps"]["network"]["observed"] == 0 and m["deps"]["network"]["required"] == "unknown"
    # truncated tool result
    m = joined(root, repo, [{"prompt": "fix", "tools": [{"name": "Read", "input": {"file_path": str(repo / "README.md")}, "result": "x\n[output truncated]"}, edit]}], 0)
    assert "truncated_result" in m["observation"]["causes"] and m["deps"]["external_path"]["required"] == "unknown"
    # missing subagent transcript
    m = joined(root, repo, [{"prompt": "fix", "tools": [{"name": "Agent", "input": {"prompt": "look"}}, edit]}], 0)
    assert "missing_subagent_transcript" in m["observation"]["causes"]
    # partial transcript
    m = joined(root, repo, [{"prompt": "fix", "tools": [edit]}], 0, complete=False)
    assert m["observation"]["sufficient"] is False and rule(m, "X7") == "unknown"
    assert all(m["deps"][c]["required"] == "unknown" for c in mf.DEP_CATEGORIES)
    # attachment
    m = joined(root, repo, [{"prompt": "fix this [Image #1]", "tools": [edit]}], 0)
    assert m["prompt"]["has_attachment"] and m["deps"]["attachment"]["required"] == "yes" and rule(m, "X4") == "yes"


def test_untracked_input_and_memory(root: Path) -> None:
    repo = make_repo(root)
    mem = Path.home() / ".claude" / "projects" / "x" / "memory" / "MEMORY.md"
    edit = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    turns = [{"prompt": "fix", "tools": [
        {"name": "Read", "input": {"file_path": str(repo / "notes.txt")}, "result": "n"},
        {"name": "Read", "input": {"file_path": str(mem)}, "result": "m"}, edit]}]
    m = joined(root, repo, turns, 0, untracked=("notes.txt",))
    assert m["deps"]["untracked_input"]["observed"] == 1 and m["deps"]["untracked_input"]["required"] == "unknown"
    assert m["deps"]["memory_or_scratch"]["observed"] == 1 and m["deps"]["memory_or_scratch"]["required"] == "unknown"


def test_later_prompt_not_excluded_by_position(root: Path) -> None:
    repo = make_repo(root)
    edit = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    turns = [{"prompt": f"task {i}", "tools": [{"name": "Read", "input": {"file_path": str(repo / "README.md")}, "result": f"content {i} of a longer line"}]}
             for i in range(5)]
    turns.append({"prompt": "in src/alpha.py change the constant from 1 to 2", "tools": [edit]})
    m = joined(root, repo, turns, 5)
    assert m["prompt"]["index_in_session"] == 5 and m["prior_context"]["has_prior_conversation"] is True
    assert m["prior_context"]["prior_conversation_required"] == "no", m["prior_context"]
    assert rule(m, "X5") == "no"


def test_prior_context_evidence_shapes(root: Path) -> None:
    repo = make_repo(root)
    line = "SHARED_CONSTANT = 12345678  # from earlier output\n"
    edit_same = {"name": "Write", "input": {"file_path": str(repo / "src" / "c.py"), "content": line}}
    edit_other = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    first = {"prompt": "read things", "tools": [{"name": "Read", "input": {"file_path": str(repo / "README.md")}, "result": line}]}
    earlier_edit = {"prompt": "write foo", "tools": [{"name": "Write", "input": {"file_path": str(repo / "docs" / "foo.md"), "content": "x"}}]}
    cases = [
        ([{"prompt": "start", "tools": [edit_other]}], 0, "first_prompt", "no"),
        ([first, {"prompt": "now write the constant", "tools": [edit_same]}], 1, "edit_depends_on_earlier_output", "yes"),
        ([earlier_edit, {"prompt": "update docs/foo.md too", "tools": [edit_other]}], 1, "prompt_names_earlier_artifact", "yes"),
        ([first, {"prompt": "continue with that file", "tools": [edit_other]}], 1, "prompt_reference", "unknown"),
        ([first, {"prompt": "ok", "tools": [edit_other]}], 1, "prompt_reference", "unknown"),
        ([first, {"prompt": "in src/alpha.py rename x", "tools": [edit_other]}], 1, None, "no"),
    ]
    for turns, which, kind, want in cases:
        m = joined(root, repo, turns, which)
        kinds = {e["kind"] for e in m["prior_context"]["prior_context_evidence"]}
        assert (kind in kinds) if kind else not kinds, (kind, kinds)
        assert m["prior_context"]["prior_conversation_required"] == want, (kind, m["prior_context"])
        assert rule(m, "X5") == want


def test_funnel_stages_monotone(root: Path) -> None:
    m = base_manifest("p")
    m["funnel"].update(privacy_cleared=True, payload_materialized=True, replay_verified=True)
    f = mf.compute_funnel(m)
    assert not any(f.values()), f  # nothing earlier holds → nothing later survives
    assert mf.validate_funnel({"packet_linked": False, "start_snapshot_complete": True}) == ["start_snapshot_complete true after packet_linked false"]
    assert mf.validate_funnel({k: True for k in mf.FUNNEL}) == []
    m["link"]["state"] = "linked"
    m["start_snapshot"] = {"state": "complete"}
    m["boundary"] = {"trusted": True}
    m["capture"]["status"] = "complete"
    m["prompt"]["origin"] = "user"
    m["edited_files"] = [{"path_sha256": "x"}]
    for c in mf.DEP_CATEGORIES:
        m["deps"][c]["required"] = "no"
    m["prior_context"]["prior_conversation_required"] = "no"
    m["observation"]["sufficient"] = True
    m["funnel"] = {k: True for k in mf.FUNNEL}
    m["funnel"]["start_state_reconstructed"] = False
    mf.refresh(m)
    assert m["funnel"]["payload_materialized"] and not m["funnel"]["replay_verified"]
    assert mf.validate_funnel(m["funnel"]) == []
    # boundary untrusted kills start_snapshot_complete and everything after
    m["boundary"] = mf.load_boundary("0.0.0", [PKG / "boundary"])
    assert m["boundary"]["trusted"] is False and m["boundary"]["reason"] == "boundary_untested"
    mf.refresh(m)
    assert not m["funnel"]["start_snapshot_complete"] and not m["funnel"]["privacy_cleared"]
    assert mf.load_boundary(None, [])["reason"] == "cli_unknown"


def test_owner_resolution_limits(root: Path) -> None:
    m = base_manifest("p")
    m["capture"]["status"] = "partial"
    mf.refresh(m, owner={"X2": {"state": "no", "basis": "reviewed"}, "X7": {"state": "no", "basis": "no"}})
    assert rule(m, "X2") == "no" and rule(m, "X7") == "unknown"
    m["edited_files"] = []
    m["observation"]["sufficient"] = True
    m["capture"]["status"] = "complete"
    mf.refresh(m, owner={"X2": {"state": "no", "basis": "cannot flip a yes"}})
    assert rule(m, "X2") == "yes"


# ------------------------------------------------------------------------ runner

REPORT: dict[str, object] = {}
TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-capture-test-") as tmp:
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
    if REPORT:
        print("report", json.dumps(REPORT, sort_keys=True))
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

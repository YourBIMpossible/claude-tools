#!/usr/bin/env python3
"""Step-1 tests for the capture tool: snapshot, manifest rules, linkage, join, screen.

No framework, plain checks, synthetic fixtures only: temporary git repositories, hand-
built transcripts and packets. No model call, no network, no real repository, no real
store. Everything is created under one temporary directory and removed afterwards.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import copy
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
import pipeline  # noqa: E402
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
    """``turns``: [{prompt, origin?, meta?, queued?, tools:[{name,input,result,error?,agent?}], brief?}].

    ``queued``: the prompt arrives as a ``queued_command`` attachment carrying these extra
    attachment fields (commandMode, origin, isMeta) — the transcript's real shape. ``agent``
    on a tool: its result reports that subagent id, as an Agent call's result does."""
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
        if isinstance(t.get("queued"), dict):
            entry = {"type": "attachment", "uuid": f"u-{n}", "timestamp": ts, "sessionId": SESSION, "version": version,
                     "attachment": {"type": "queued_command", "prompt": t["prompt"], **t["queued"]}}
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
            result = {"type": "user", "timestamp": ts, "sessionId": SESSION,
                      "message": {"role": "user", "content": [
                          {"type": "tool_result", "tool_use_id": tid, "is_error": bool(tool.get("error")),
                           "content": tool.get("result", "ok")}]}}
            if tool.get("agent"):
                result["toolUseResult"] = {"status": "completed", "agentId": tool["agent"]}
            lines.append(json.dumps(result))
        if t.get("tools") or t.get("reply", True):
            lines.append(json.dumps({"type": "assistant", "timestamp": ts, "sessionId": SESSION, "version": version,
                                     "message": {"id": f"msg_{n}_end", "role": "assistant",
                                                 "content": [{"type": "text", "text": "done"}]}}))
    return lines


def write_transcript(root: Path, turns: list[dict], name: str = SESSION) -> Path:
    p = root / f"{name}.jsonl"
    p.write_text("\n".join(transcript_lines(turns)) + "\n", encoding="utf-8")
    return p


def write_subagent(root: Path, agent: str, tools: list[dict], spawned_by: str | None = None,
                   ts: str = "2026-09-26T10:01:00.000Z", cwd: str | None = None, name: str = SESSION) -> Path:
    """``<session>/subagents/agent-<agent>.jsonl`` (every line ``isSidechain``) plus its
    ``.meta.json`` naming the spawning tool-use id, when ``spawned_by`` is given."""
    d = root / name / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    base = {"isSidechain": True, "agentId": agent, "sessionId": name, "timestamp": ts, "cwd": cwd}
    lines = [json.dumps({**base, "type": "user", "message": {"role": "user", "content": "delegated task"}})]
    for k, tool in enumerate(tools):
        tid = f"toolu_{agent}_{k}"
        lines.append(json.dumps({**base, "type": "assistant", "timestamp": tool.get("ts", ts), "message": {
            "id": f"msg_{agent}_{k}", "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": tool["name"], "input": tool["input"]}]}}))
        result = {**base, "type": "user", "timestamp": tool.get("ts", ts), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": tool.get("result", "ok")}]}}
        if tool.get("agent"):
            result["toolUseResult"] = {"agentId": tool["agent"]}
        lines.append(json.dumps(result))
    path = d / f"agent-{agent}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if spawned_by:
        (d / f"agent-{agent}.meta.json").write_text(json.dumps(
            {"agentType": "general-purpose", "toolUseId": spawned_by, "spawnDepth": 1}), encoding="utf-8")
    return path


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
    assert not store.verify_sums(st.snapshots / "cap_clean")
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
    assert not (st.snapshots / "cap_cap").exists() and not (st.snapshots / "cap_to").exists()
    _, ok = snapshot.take_snapshot(repo, "cap_ok", st)
    assert ok["state"] == "complete"
    _, again = snapshot.take_snapshot(repo, "cap_ok", st)
    assert again["state"] == "failed" and again["reason"] == "write_error", again  # never overwritten
    assert not store.verify_sums(st.snapshots / "cap_ok")


def test_snapshot_atomic(root: Path) -> None:
    repo, st = make_repo(root), make_stores(root)
    # a snapshot dir left with INCOMPLETE (a kill mid-write after the rename would be impossible;
    # a kill before it leaves a tmp dir) is failed either way
    d = st.snapshots / "cap_killed"
    d.mkdir()
    (d / "status.v2z").write_bytes(b"")
    (d / "INCOMPLETE").write_bytes(b"")
    store.write_sums(d)
    assert store.verify_sums(d) == ["INCOMPLETE"]
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
    assert store.verify_sums(st.snapshots / "cap_v") == ["status.v2z"]


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
    assert m["prompt_uuid"] == "u-3" and m["prompt"]["origin"] == "human" and m["prompt"]["index_in_session"] == 1
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
    dot_edit = {"prompt": "write ci", "tools": [{"name": "Write", "input": {"file_path": str(repo / ".github" / "workflows" / "ci.yml"), "content": "x"}}]}
    cases = [
        ([{"prompt": "start", "tools": [edit_other]}], 0, "first_prompt", "no"),
        ([first, {"prompt": "now write the constant", "tools": [edit_same]}], 1, "edit_depends_on_earlier_output", "yes"),
        ([earlier_edit, {"prompt": "update docs/foo.md too", "tools": [edit_other]}], 1, "prompt_names_earlier_artifact", "yes"),
        ([dot_edit, {"prompt": "fix .github/workflows/ci.yml again", "tools": [edit_other]}], 1, "prompt_names_earlier_artifact", "yes"),
        ([dot_edit, {"prompt": "fix ./.github/workflows/ci.yml again", "tools": [edit_other]}], 1, "prompt_names_earlier_artifact", "yes"),
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
    # gate flags are independent observations: a complete snapshot without a link is consistent
    assert mf.validate_funnel({"packet_linked": False, "start_snapshot_complete": True}) == []
    assert mf.validate_funnel({"packet_linked": True, "start_snapshot_complete": False,
                               "dependency_screen_clear": True, "privacy_cleared": True}) == [
        "privacy_cleared true while start_snapshot_complete false"]
    fl = [{"packet_linked": False, "start_snapshot_complete": True},
          {"packet_linked": True, "start_snapshot_complete": True, "dependency_screen_clear": False},
          {"packet_linked": True, "start_snapshot_complete": False}]
    cum = mf.cumulative_funnel(fl)
    assert (cum["packet_linked"], cum["start_snapshot_complete"], cum["dependency_screen_clear"]) == (2, 1, 0), cum
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
def test_failure_cause_from_recorded_error(root: Path) -> None:
    assert mf.failure_cause("store_unusable: metadata store is not a git repository") == "store_unusable"
    assert mf.failure_cause("stores_not_configured") == "stores_not_configured"
    assert mf.failure_cause("OSError: disk full") == "write_error"
    rec = {"episode_id": "cap_f", "session_id": "s", "started_at": "2026-10-05T11:13:55Z",
           "prompt_sha256": "0" * 64, "prompt_len": 1}
    m = pipeline.failed_manifest(rec, "store_unusable: metadata .git/info/exclude lacks the content exclusions")
    assert m["start_snapshot"]["reason"] == "store_unusable" and m["capture"]["status"] == "failed"
    assert not m["funnel"]["start_snapshot_complete"]  # a failed start is never promoted


def test_packet_discovery_repo_store_names_and_session_roots(root: Path) -> None:
    import json as _j
    wt, proj = root / "wt", root / "proj"
    for r in (wt, proj):
        (r / ".evidence-compiler" / "packets").mkdir(parents=True)
    pk = {"packet_id": "ep_00000000000000aa", "created_at": "2026-10-01T00:00:00Z",
          "identity": {"session_id": "s1", "head": "h"}, "correlation": {"prompt_hash": "x"}}
    # EC repo stores prefix the id with a timestamp; the archive does not
    (proj / ".evidence-compiler" / "packets" / "2026-10-01T00-00-00.000000Z_ep_00000000000000aa.json").write_text(
        _j.dumps(pk), encoding="utf-8")
    st = type("S", (), {"packet_dirs": []})()
    assert linkage.load_packets(linkage.packet_dirs(wt, st)) == []
    found = linkage.load_packets(linkage.packet_dirs(wt, st, [proj, wt]))
    assert [p["packet_id"] for p in found] == ["ep_00000000000000aa"], found
    assert len(linkage.packet_dirs(wt, st, [wt])) == 1


def test_find_turn_meta_and_contained_prompts(root: Path) -> None:
    h = common.sha256_text
    inner = '<agent-message from="lane-b">\nplease review the hand-back\n</agent-message>'
    wrapped = "Another Claude session sent a message:\n" + inner + "\n\nThis came from another Claude session."
    ev = lambda i, kind, text, ts: join.Event(i, kind, ts, {"sha256": h(text), "text": text, "uuid": str(i),
                                                          "origin": "meta", "attachment": False})
    tr = join.Transcript(root / "t.jsonl", [ev(0, "candidate", "meta prompt", "2026-10-01T00:00:00Z"),
                                            ev(1, "prompt", wrapped, "2026-10-01T00:05:00Z")], 0, [], "s")
    assert join.find_turn(tr, h("meta prompt"), "2026-10-01T00:00:01Z") == (0, "exact")
    tr.events.insert(0, join.Event(9, "candidate", "2026-10-01T00:00:01Z", {"sha256": h("meta prompt"),
                                                                          "origin": "queue_enqueue"}))
    assert join.find_turn(tr, h("meta prompt"), "2026-10-01T00:00:01Z") == (1, "exact")  # enqueue skipped
    assert join.find_turn(tr, h(inner), "2026-10-01T00:05:00Z", len(inner)) == (2, "contained")
    # strict: no length, a wrong length, or outside the window never matches
    assert join.find_turn(tr, h(inner), "2026-10-01T00:05:00Z") is None
    assert join.find_turn(tr, h(inner), "2026-10-01T00:05:00Z", len(inner) - 1) is None
    assert join.find_turn(tr, h(inner), "2026-10-01T01:00:00Z", len(inner)) is None


# ------------------------------------------------------------- delegated work (D1)

AGENT_ID = "a1b2c3d4e5f60718"


def _delegating_turns(repo: Path, extra: list[dict] | None = None) -> list[dict]:
    return [{"prompt": "fix alpha via a helper", "tools": [
        {"name": "Agent", "input": {"prompt": "do it"}, "result": f"done\nagentId: {AGENT_ID}", "agent": AGENT_ID},
        *(extra or [])]}, {"prompt": "next task"}]


def test_delegated_reads_and_edits_are_observed(root: Path) -> None:
    repo = make_repo(root)
    other = make_repo(root, "other")
    doc = "the quick brown fox jumps over the lazy dog, twice\n"
    (other / "DOC.md").write_text(doc, encoding="utf-8")
    write_subagent(root, AGENT_ID, [
        {"name": "Read", "input": {"file_path": str(other / "DOC.md")}, "result": doc},
        {"name": "Write", "input": {"file_path": str(repo / "DOC.md"), "content": doc}}], spawned_by="toolu_1_0")
    turns = _delegating_turns(repo)
    tr = write_transcript(root, turns)
    m = base_manifest(turns[0]["prompt"], started="2026-09-26T10:01:00.000000Z")
    obs = join.join(m, tr)
    assert obs["subagent_count"] == 1 and obs["subagent_unresolved"] == 0
    assert {c["source"] for c in obs["tool_calls"]} == {"main", f"agent-{AGENT_ID}"}
    m = joined(root, repo, turns, 0)
    assert "missing_subagent_transcript" not in m["observation"]["causes"], m["observation"]
    assert m["edited_files"] == [{"path_sha256": common.path_sha256("DOC.md")}]
    assert m["deps"]["external_repository"]["required"] == "yes" and rule(m, "X4") == "yes"
    assert rule(m, "X2") == "no"  # the delegated edit is a repository edit


def test_delegated_network_external_path_and_other_session(root: Path) -> None:
    repo = make_repo(root)
    outside = root / "outside"
    outside.mkdir()
    write_subagent(root, AGENT_ID, [
        {"name": "WebFetch", "input": {"url": "https://example.invalid/doc"}, "result": "page"},
        {"name": "Read", "input": {"file_path": str(outside / "notes.txt")}, "result": "n"},
        {"name": "Bash", "input": {"command": "git worktree add ../lane-x"}, "result": ""},
        {"name": "SendMessage", "input": {"to": "lane-b", "message": "hi"}, "result": "sent"}],
        spawned_by="toolu_1_0")
    m = joined(root, repo, _delegating_turns(repo), 0)
    obs = {c: m["deps"][c]["observed"] for c in ("network", "external_path", "other_session")}
    assert obs["network"] >= 1 and obs["external_path"] >= 1 and obs["other_session"] >= 3, obs
    assert m["deps"]["network"]["required"] != "no" and m["deps"]["external_path"]["required"] != "no"


def test_delegation_resolves_by_agent_id_and_nested_agents(root: Path) -> None:
    repo = make_repo(root)
    nested = "f0e1d2c3b4a59687"
    # no meta file: resolved through the agentId the Agent result reports
    write_subagent(root, AGENT_ID, [{"name": "Agent", "input": {"prompt": "deeper"}, "result": "ok", "agent": nested}])
    write_subagent(root, nested, [{"name": "WebSearch", "input": {"query": "x"}, "result": "r"}],
                   spawned_by=f"toolu_{AGENT_ID}_0")
    tr = write_transcript(root, _delegating_turns(repo))
    m = base_manifest("fix alpha via a helper", started="2026-09-26T10:01:00.000000Z")
    obs = join.join(m, tr)
    assert obs["subagent_count"] == 2 and obs["subagent_unresolved"] == 0, obs["causes"]
    assert any(c["name"] == "WebSearch" and c["source"] == f"agent-{nested}" for c in obs["tool_calls"])
    m = joined(root, repo, _delegating_turns(repo), 0)
    assert m["deps"]["network"]["observed"] >= 1


def test_missing_subagent_stays_unknown(root: Path) -> None:
    repo = make_repo(root)
    edit = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    # one delegation joined, a second one with no preserved transcript
    write_subagent(root, AGENT_ID, [{"name": "Read", "input": {"file_path": str(repo / "README.md")}}], spawned_by="toolu_1_0")
    turns = [{"prompt": "fix alpha", "tools": [
        {"name": "Agent", "input": {"prompt": "a"}, "agent": AGENT_ID},
        {"name": "Agent", "input": {"prompt": "b"}, "agent": "deadbeefdeadbeef"}, edit]}]
    m = joined(root, repo, turns, 0)
    assert "missing_subagent_transcript" in m["observation"]["causes"] and m["observation"]["sufficient"] is False
    assert all(m["deps"][c]["required"] != "no" for c in mf.DEP_CATEGORIES if c != "prior_conversation")


def test_subagent_of_another_turn_is_not_attached(root: Path) -> None:
    repo = make_repo(root)
    # the agent is spawned in turn 2; turn 1 must not see its WebFetch
    write_subagent(root, AGENT_ID, [{"name": "WebFetch", "input": {"url": "https://example.invalid"}, "result": "p"}],
                   spawned_by="toolu_2_0", ts="2026-09-26T10:02:00.000Z")
    edit = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    turns = [{"prompt": "fix alpha", "tools": [edit]},
             {"prompt": "research", "tools": [{"name": "Agent", "input": {"prompt": "r"}, "agent": AGENT_ID}]}]
    m = joined(root, repo, turns, 0)
    assert m["deps"]["network"]["observed"] == 0 and m["observation"]["sufficient"] is True, m["observation"]
    m2 = joined(root, repo, turns, 1)
    assert m2["deps"]["network"]["observed"] == 1


def test_resumed_subagent_joins_the_resuming_turn(root: Path) -> None:
    repo = make_repo(root)
    write_subagent(root, AGENT_ID, [
        {"name": "Read", "input": {"file_path": str(repo / "README.md")}, "result": "r"},
        {"name": "WebFetch", "input": {"url": "https://example.invalid"}, "result": "p", "ts": "2026-09-26T10:02:30.000Z"}],
        spawned_by="toolu_1_0")
    turns = [{"prompt": "start a helper", "tools": [{"name": "Agent", "input": {"prompt": "x"}, "agent": AGENT_ID}]},
             {"prompt": "continue the helper", "tools": [{"name": "SendMessage", "input": {"to": AGENT_ID, "message": "go"}}]}]
    m1 = joined(root, repo, turns, 0)
    m2 = joined(root, repo, turns, 1)
    assert m1["deps"]["network"]["observed"] == 0 and "subagent_beyond_turn" not in m1["observation"]["causes"]
    assert m2["deps"]["network"]["observed"] == 1
    # never referenced again: activity after the turn ended leaves the turn unverified
    turns[1] = {"prompt": "unrelated"}
    m1 = joined(root, repo, turns, 0)
    assert "subagent_beyond_turn" in m1["observation"]["causes"] and m1["observation"]["sufficient"] is False


def test_main_transcript_ignores_inline_sidechain_entries(root: Path) -> None:
    tr = write_transcript(root, [{"prompt": "hello"}])
    with tr.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "isSidechain": True, "timestamp": "2026-09-26T10:01:05.000Z",
                             "message": {"role": "user", "content": "side prompt"}}) + "\n")
    assert [e.data["text"] for e in join.parse_transcript(tr).events if e.kind == "prompt"] == ["hello"]
    assert [e.data["text"] for e in join.parse_transcript(tr, sidechain=True).events if e.kind == "prompt"] == ["hello", "side prompt"]


# ------------------------------------------------------------------ origins (D3)

def test_origin_shapes_from_real_transcripts(root: Path) -> None:
    repo = make_repo(root)
    turns = [
        {"prompt": "fix alpha", "origin": "human"},
        {"prompt": "also update the readme", "queued": {"commandMode": "prompt", "origin": {"kind": "human"}}},
        {"prompt": "<task-notification>bg done</task-notification>", "queued": {"commandMode": "task-notification"}},
        {"prompt": "<task-notification>x</task-notification>", "queued": {"commandMode": "task-notification",
                                                                        "origin": {"kind": "task-notification", "producer": "session-task"}}},
        {"prompt": '<agent-message from="b">\nhi\n</agent-message>', "queued": {"commandMode": "prompt", "isMeta": True,
                                                                               "origin": {"kind": "peer", "from": "b"}}},
        {"prompt": "<task-notification>y</task-notification>", "origin": "task-notification"},
        {"prompt": "relay", "origin": "peer", "meta": True},
        {"prompt": "legacy queued", "queued": {"commandMode": "prompt"}},
        {"prompt": "second task"},
    ]
    tr = write_transcript(root, turns)
    events = [e for e in join.parse_transcript(tr).events if e.kind in ("prompt", "candidate")]
    assert [e.data["origin"] for e in events] == [
        "human", "human", "task_notification", "task_notification", "peer", "task_notification", "peer",
        "queued_command:prompt", "human"], [e.data["origin"] for e in events]
    m = base_manifest("fix alpha", started="2026-09-26T10:01:00.000000Z")
    obs = join.join(m, tr)
    # only the next human prompt typed between turns ends the episode
    assert obs["turn"]["end"] == next(i for i, e in enumerate(join.parse_transcript(tr).events)
                                      if e.data.get("text") == "second task")
    assert "interleaved_human_prompt" in obs["causes"]
    q = base_manifest("also update the readme", started="2026-09-26T10:02:00.000000Z")
    join.join(q, tr)
    mf.refresh(q)
    assert q["prompt"]["origin"] == "human" and rule(q, "X1") == "no"
    for prompt, minute, origin in (("<task-notification>bg done</task-notification>", 3, "task_notification"),
                                   ("relay", 7, "peer"), ("legacy queued", 8, "queued_command:prompt")):
        n = base_manifest(prompt, started=f"2026-09-26T10:{minute:02d}:00.000000Z")
        join.join(n, tr)
        mf.refresh(n)
        assert n["prompt"]["origin"] == origin and rule(n, "X1") == "yes", (prompt, n["prompt"]["origin"])


# ------------------------------------------------------------ attachments (D2)

def test_inline_paste_is_not_an_attachment(root: Path) -> None:
    repo = make_repo(root)
    edit = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    inline = "apply the pasted text\n<pasted_content>\ndef alpha(x): return x\n</pasted_content>"
    m = joined(root, repo, [{"prompt": inline, "tools": [edit]}], 0)
    assert m["prompt"]["has_attachment"] is False and m["deps"]["attachment"]["required"] == "no", m["deps"]["attachment"]
    for prompt in ("apply this [Pasted text #1 +12 lines]", "see <attached_file path=x.png>", "fix [Image #2]",
                   "apply the pasted text"):
        m = joined(root, repo, [{"prompt": prompt, "tools": [edit]}], 0)
        assert m["deps"]["attachment"]["required"] == "yes" and rule(m, "X4") == "yes", prompt


# -------------------------------------------------------- wrapped prompts (item 7)

def test_envelope_match_is_parsed_unique_and_exact(root: Path) -> None:
    h = common.sha256_text
    element = '<agent-message from="lane-b">\nplease review\n</agent-message>'
    peer = "Another Claude session sent a message:\n" + element + "\n\nThat sender is an agent."
    ev = lambda i, text, ts: join.Event(i, "prompt", ts, {"sha256": h(text), "text": text, "uuid": str(i),
                                                         "origin": "peer", "attachment": False})
    t0 = "2026-10-01T00:05:00Z"
    tr = join.Transcript(root / "t.jsonl", [ev(0, peer, t0)], 0, [], "s")
    assert join.find_turn(tr, h(element), t0, len(element)) == (0, "contained")
    # slash command envelopes: /name and /name args
    cmd = "<command-message>close</command-message>\n<command-name>/close</command-name>"
    cmd_args = cmd + "\n<command-args>status</command-args>"
    tr = join.Transcript(root / "t.jsonl", [ev(0, cmd, t0), ev(1, cmd_args, "2026-10-01T00:09:00Z")], 0, [], "s")
    assert join.find_turn(tr, h("/close"), t0, len("/close")) == (0, "contained")
    assert join.find_turn(tr, h("/close status"), "2026-10-01T00:09:00Z", len("/close status")) == (1, "contained")
    # an unrelated substring in arbitrary text never matches, even with exact hash and length
    skill = "Base directory for this skill\n## /close status (read-only)\nReport lines"
    tr = join.Transcript(root / "t.jsonl", [ev(0, skill, t0)], 0, [], "s")
    assert join.find_turn(tr, h("/close"), t0, len("/close")) is None
    assert join.find_turn(tr, h(element), t0, len(element)) is None
    # the element inside an unrecognized wrapper is not a parsed envelope
    tr = join.Transcript(root / "t.jsonl", [ev(0, "FYI\n" + element + "\nthanks", t0)], 0, [], "s")
    assert join.find_turn(tr, h(element), t0, len(element)) is None
    # two envelopes carrying the same payload inside the window: ambiguous, nothing joined
    tr = join.Transcript(root / "t.jsonl", [ev(0, peer, t0), ev(1, peer, "2026-10-01T00:05:30Z")], 0, [], "s")
    misses: list[str] = []
    assert join.find_turn(tr, h(element), t0, len(element), misses) is None and misses == ["prompt_ambiguous"]
    # a matching envelope outside the window is not ambiguous with one inside it
    tr = join.Transcript(root / "t.jsonl", [ev(0, peer, t0), ev(1, peer, "2026-10-01T00:20:00Z")], 0, [], "s")
    assert join.find_turn(tr, h(element), t0, len(element)) == (0, "contained")


# ------------------------------------------------------------- report statuses

def test_report_stage_status_and_human_yield(root: Path) -> None:
    import report
    repo = make_repo(root)
    edit = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    m = joined(root, repo, [{"prompt": "fix alpha", "origin": "human", "tools": [edit]}], 0)
    m["capture"]["status"] = "complete"
    assert report.privacy_status(m) == "not_reached"
    m["review"] = {"privacy": {"passed": False}}
    assert report.privacy_status(m) == "failed"
    m["review"] = {"privacy": {"passed": True}}
    assert report.privacy_status(m) == "passed"
    assert report.later_stage_status(m, "replay_verified") == "not_evaluated"
    for k in mf.FUNNEL[:5]:
        m["funnel"][k] = True
    assert report.later_stage_status(m, "start_state_reconstructed") == "unknown"
    m["start_state_reconstructed"] = {"state": "failed"}
    assert report.later_stage_status(m, "start_state_reconstructed") == "failed"
    m["funnel"]["start_state_reconstructed"] = True
    assert report.later_stage_status(m, "start_state_reconstructed") == "passed"
    peer = joined(root, repo, [{"prompt": "relay", "origin": "peer", "meta": True}], 0)
    peer["capture"]["status"] = "complete"
    assert report._origin_bucket(peer["prompt"]["origin"]) == "peer"
    m2 = joined(root, repo, [{"prompt": "fix alpha", "origin": "human", "tools": [edit]}], 0)
    m2["capture"]["status"] = "complete"
    hy = report.human_yield([m2, peer])
    assert hy["completed_human_tasks"] == 1 and hy["provisional_candidates"] == 0
    assert not any(k.startswith("link:") for k in hy["independent_blockers"]), hy
    assert hy["independent_blockers"], hy
    assert hy["independent_blockers"]["privacy:not_reached"] == 1
    # partition: one class per task, summing to the population
    def shaped(rules: str, trusted: bool, sufficient: bool = True) -> dict:
        x = copy.deepcopy(m2)
        x["funnel"] = {k: False for k in mf.FUNNEL}
        x["link"]["state"] = "linked"
        x["start_snapshot"]["state"] = "complete"
        x["observation"]["sufficient"] = sufficient
        x["boundary"] = {"trusted": trusted, "reason": None if trusted else "boundary_untested"}
        x["exclusions"] = [{"rule": r, "state": rules} for r in mf.RULES]
        return x
    pop = [shaped("no", False), shaped("yes", False), shaped("unknown", False), shaped("no", False, False),
           shaped("yes", True), shaped("unknown", True)]
    part = report.blocker_partition(pop)
    assert part["total"] == len(pop) and part["classes"] == {
        "boundary_only:cli_untested": 1, "boundary_plus_scope_excluded:cli_untested": 1,
        "boundary_plus_unresolved:cli_untested": 2, "scope_excluded": 1, "unresolved": 1}, part
    # F11: candidacy is one rule set. X7 (capture incomplete) or an insufficient observation
    # stops the funnel exactly where the partition stops counting a candidate, even when an
    # older tool recorded the funnel as passed.
    for x7, sufficient in (("unknown", True), ("yes", True), ("no", False)):
        x = shaped("no", True, sufficient)
        x["exclusions"] = [{"rule": r, "state": x7 if r == "X7" else "no"} for r in mf.RULES]
        x["funnel"] = {k: True for k in mf.FUNNEL}  # stale stored funnel
        f = mf.compute_funnel(x)
        assert not mf.provisional_candidate(f), (x7, sufficient, f)
        assert "candidate" not in report.blocker_partition([x])["classes"], (x7, sufficient)
    clear = shaped("no", True)
    clear["funnel"] = mf.compute_funnel(clear)
    assert clear["funnel"]["dependency_screen_clear"], clear["funnel"]


def test_checkpoint_one_classification_and_malformed_manifests(root: Path) -> None:
    """F10/F11: the checkpoint classifies every episode from the recomputed funnel, never a
    stale stored one, and a manifest missing fields it reads is counted and named, not fatal."""
    import report
    import rescreen
    repo = make_repo(root)
    edit = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    st = make_stores(root)
    stale = joined(root, repo, [{"prompt": "fix alpha", "origin": "human", "tools": [edit]}], 0)
    stale["observation"]["sufficient"] = False
    stale["exclusions"] = [{"rule": r, "state": "no"} for r in mf.RULES]
    stale["funnel"] = {k: True for k in mf.FUNNEL}  # recorded by an older tool
    store.write_manifest(st, stale)
    no_link = copy.deepcopy(stale)
    no_link["episode_id"] = "cap_nolink"
    del no_link["link"]
    store.write_manifest(st, no_link)
    store.write_manifest(st, {"episode_id": "cap_partial"})
    assert mf.shape_problem(stale) is None
    assert mf.shape_problem(no_link) and mf.shape_problem({"episode_id": "cap_partial"})
    assert not rescreen._well_formed(no_link), "a manifest without link reached refresh"
    ck = report.build_checkpoint(st, 1, tool_commit="0" * 40)
    assert ck["denominator"]["episodes_total"] == 1 and ck["denominator"]["manifests_malformed"] == 2, ck["denominator"]
    assert any("cap_nolink" in n and "cap_partial" in n for n in ck["notes"]), ck["notes"]
    hy = ck["human_yield"]
    assert hy["provisional_candidates"] == 0 and "candidate" not in hy["partition"]["classes"], hy
    assert hy["cumulative"]["dependency_screen_clear"] == 0, hy["cumulative"]
    assert ck["task_mix"]["provisional_candidates_by_task_class"] == {}, ck["task_mix"]
    # stale stored exclusions are re-evaluated (X7 is a function of capture status), and an
    # out-of-order stored funnel is still reported
    fresh = joined(root, repo, [{"prompt": "fix alpha", "origin": "human", "tools": [edit]}], 0)
    fresh["exclusions"] = [{"rule": r, "state": "yes"} for r in mf.RULES]
    fresh["funnel"] = {k: k == "privacy_cleared" for k in mf.FUNNEL}
    st2 = make_stores(root, "st2")
    store.write_manifest(st2, fresh)
    ck = report.build_checkpoint(st2, 1, tool_commit="0" * 40)
    assert "scope_excluded" not in ck["human_yield"]["partition"]["classes"], ck["human_yield"]["partition"]
    assert any("funnel order violated in 1" in n for n in ck["notes"]), ck["notes"]


def test_dot_segments_terminate(root: Path) -> None:
    """Regression: a path with more ``..`` than segments looped forever."""
    assert screen._resolve_dots("f:/../x") == "f:/x"
    assert screen._resolve_dots("/a/../../b/./c") == "/b/c"
    assert screen._resolve_dots("c:/a/b/../c") == "c:/a/c"
    assert screen._resolve_dots("//srv/share/a/../../../b") == "//srv/share/b"  # UNC anchor holds
    assert screen._resolve_dots("//srv/share") == "//srv/share"


def test_windows_path_forms(root: Path) -> None:
    """Extended (``\\\\?\\``, ``\\\\.\\``), UNC and drive-relative forms classify by where
    they point: an extended path into the repo is in it; a path on another drive is not."""
    class Idx:
        root = main_root = "c:/repo"

        def owner(self, p: str) -> None:
            return None
    cwd, cls = "c:/repo/sub", lambda p: screen.classify_path(p, Idx(), cwd, [])
    for p in ("\\\\?\\C:\\repo\\a.txt", "\\\\.\\C:\\repo\\a.txt", "//?/c:/repo/a.txt", "C:a.txt", "C:..\\x"):
        assert cls(p) is None, p
    for p in ("D:a.txt", "d:..\\repo\\a", "\\\\srv\\share\\a", "\\\\?\\UNC\\srv\\share\\a",
              "//srv/share/../../c:/repo/a", "C:..\\..\\x"):
        assert cls(p) == "external_path", (p, cls(p))


def test_root_resolution_without_marker_and_gone(root: Path) -> None:
    """The captured root resolves by path hash even when the checkout no longer carries
    the config marker; a removed worktree is reported as gone, not unresolved."""
    live = root / "repo" / ".claude" / "worktrees" / "lane"
    (live / "sub").mkdir(parents=True)
    (live / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    m = {"repo": {"root_sha256": pipeline.root_sha256(live)}}
    assert pipeline.resolve_repo_root(m, [str(live / "sub")]) == live
    assert not pipeline.captured_root_gone(m, [str(live / "sub")])
    gone = root / "repo" / ".claude" / "worktrees" / "removed"
    m = {"repo": {"root_sha256": pipeline.root_sha256(gone)}}
    assert pipeline.resolve_repo_root(m, [str(gone / "x")]) is None
    assert pipeline.captured_root_gone(m, [str(gone / "x")])
    m = {"repo": {"root_sha256": "0" * 64}}
    assert pipeline.resolve_repo_root(m, [str(live)]) is None
    assert not pipeline.captured_root_gone(m, [str(live)])


def test_invalidate_replaces_prior_rescreen_cause(root: Path) -> None:
    import rescreen
    m = {"observation": {"sufficient": True, "causes": ["rescreen_root_unresolved", "no_response"]},
         "deps": {c: {"required": "no", "evidence": []} for c in mf.DEP_CATEGORIES},
         "prior_context": {}}
    rescreen.invalidate(m, "rescreen_root_gone")
    assert m["observation"]["causes"] == ["no_response", "rescreen_root_gone"]
    assert all(m["deps"][c]["required"] == "unknown" for c in mf.DEP_CATEGORIES)


def test_attrib_flags_parse_any_path_form(root: Path) -> None:
    """Regression (N12): ``attrib`` prints the canonical ``X:\\`` path whatever form it was
    given; the flags are the letters before it, never a slice up to the first character of
    the caller's path (a lowercase drive or relative name landed inside the path text)."""
    tail = r"C:\Users\someone\store\Sub"
    assert store.parse_attrib_flags(" " * 21 + tail) == ""
    assert store.parse_attrib_flags("A" + " " * 20 + tail) == "A"
    assert store.parse_attrib_flags("             P       " + tail) == "P"
    assert store.parse_attrib_flags("A    SH      P  U    " + tail) == "ASHPU"
    assert store.parse_attrib_flags(r"A            \\srv\share\Sub") == "A"
    assert store.parse_attrib_flags("File not found - " + tail) is None
    assert store.parse_attrib_flags("") is None
    if os.name == "nt":
        d = root / "Users" / "Sub"
        d.mkdir(parents=True)
        for form in (str(d), str(d)[0].lower() + str(d)[1:]):
            assert store._windows_attrib_flags(Path(form)) == "", form
        assert store._windows_attrib_flags(root / "absent") is None


def test_lane_worktree_inside_root_is_other_session(root: Path) -> None:
    """Regression (N13): a lane's worktree sits inside the main checkout, so it passed the
    in-root test and counted as this repository's own edit."""
    class Idx:
        root = main_root = "c:/repo"

        def owner(self, p: str) -> None:
            return None
    cls = lambda p: screen.classify_path(p, Idx(), "c:/repo", [])
    assert cls("c:/repo/.claude/worktrees/lane-b/src/a.py") == "other_session"
    assert cls(".claude/worktrees/lane-b/src/a.py") == "other_session"
    assert cls("c:/repo/src/a.py") is None
    assert cls("c:/repo/.claude/settings.json") is None
    repo = make_repo(root)
    lane = repo / ".claude" / "worktrees" / "lane-b" / "src"
    lane.mkdir(parents=True)
    turns = [{"prompt": "fix alpha", "tools": [
        {"name": "Edit", "input": {"file_path": str(lane / "alpha.py"), "old_string": "1", "new_string": "2"}}]}]
    m = joined(root, repo, turns, 0)
    assert m["edited_files"] == [] and m["deps"]["other_session"]["observed"] == 1, m["deps"]["other_session"]


def test_glob_pattern_outside_root_is_observed(root: Path) -> None:
    """Regression (N14): a Glob whose ``pattern`` is absolute (or climbs out) searched
    outside the repository with nothing observed, so the screen called that sufficient."""
    assert screen._glob_anchor("C:/other/**/*.py", None) == "C:/other"
    assert screen._glob_anchor("../../other/*.md", None) == "../../other"
    assert screen._glob_anchor("src/**/*.py", "c:/repo") == "c:/repo/src"
    assert screen._glob_anchor("**/*.py", "c:/repo") == "c:/repo"
    assert screen._glob_anchor("**/*.py", None) == "."
    assert screen._glob_anchor("/**/*.py", "c:/repo") == "/"
    assert screen._glob_anchor("/*", None) == "/"
    repo = make_repo(root)
    other = root / "elsewhere"
    other.mkdir()
    for pattern, path in ((str(other / "**" / "*.py"), None), ("../elsewhere/*.py", None), ("**/*.py", str(other))):
        inp = {"pattern": pattern, **({"path": path} if path else {})}
        m = joined(root, repo, [{"prompt": "list files", "tools": [{"name": "Glob", "input": inp}]}], 0)
        assert m["deps"]["external_path"]["observed"] >= 1, (pattern, path, m["deps"]["external_path"])
        assert m["deps"]["external_path"]["required"] == "unknown"
    m = joined(root, repo, [{"prompt": "list files", "tools": [{"name": "Glob", "input": {"pattern": "src/**/*.py"}}]}], 0)
    assert m["deps"]["external_path"]["observed"] == 0 and m["deps"]["external_path"]["required"] == "no"


def test_exact_match_outside_window_is_not_joined(root: Path) -> None:
    """Regression (N15): the exact hash match had no time bound, so when the hook's own
    prompt was not in the transcript an older identical prompt was joined instead."""
    h = common.sha256_text
    ev = lambda i, ts: join.Event(i, "prompt", ts, {"sha256": h("continue"), "text": "continue", "uuid": str(i),
                                                   "origin": "human", "attachment": False})
    tr = join.Transcript(root / "t.jsonl", [ev(0, "2026-10-01T00:00:00Z")], 0, [], "s")
    assert join.find_turn(tr, h("continue"), "2026-10-01T00:00:30Z") == (0, "exact")
    assert join.find_turn(tr, h("continue"), "2026-10-01T03:00:00Z") is None
    tr.events.append(ev(1, "2026-10-01T02:59:00Z"))
    assert join.find_turn(tr, h("continue"), "2026-10-01T03:00:00Z") == (1, "exact")
    tr.events.append(ev(2, None))  # no timestamp: distance unknown, kept
    assert join.find_turn(tr, h("continue"), "2026-10-01T09:00:00Z") == (2, "exact")
    tr.events.append(ev(3, "2026-10-01T09:00:00Z"))
    assert join.find_turn(tr, h("continue"), "2026-10-01T09:00:00Z") == (3, "exact")


def test_isolation_write_success_survives_failed_cleanup(root: Path) -> None:
    """Regression (N16): a probe file that was created but could not be removed made the
    write attempt report ``denied`` — a successful forbidden write read as a pass."""
    import isolation

    class StickyPath(type(Path())):
        def unlink(self, missing_ok: bool = False) -> None:
            raise PermissionError(13, "cleanup denied")
    target = root / "tmp"
    target.mkdir()
    outcome = isolation._attempt("write", StickyPath(target))
    assert outcome.startswith("succeeded"), outcome
    left = list(target.iterdir())
    assert len(left) == 1 and left[0].name.startswith("isolation-probe-"), left
    left[0].unlink()
    assert isolation._attempt("write", target) == "succeeded" and list(target.iterdir()) == []


def test_prior_context_artifact_match_is_whole_segment(root: Path) -> None:
    """Regression (N17): ``a.py`` in the prompt matched an earlier edit of ``data.py`` by
    substring and flagged prior context as required."""
    repo = make_repo(root)
    (repo / "src" / "data.py").write_text("x = 1\n", encoding="utf-8")
    earlier = {"prompt": "write data", "tools": [{"name": "Write", "input": {"file_path": str(repo / "src" / "data.py"), "content": "x = 1\n"}}]}
    edit = {"name": "Edit", "input": {"file_path": str(repo / "src" / "alpha.py"), "old_string": "1", "new_string": "2"}}
    for prompt, want in (("in a.py rename x", "no"), ("in src/a.py rename x", "no"), ("in ata.py rename x", "no"),
                         ("in data.py rename x", "yes"), ("in src/data.py rename x", "yes"), ("in ./src/data.py rename x", "yes")):
        m = joined(root, repo, [earlier, {"prompt": prompt, "tools": [edit]}], 1)
        kinds = {e["kind"] for e in m["prior_context"]["prior_context_evidence"]}
        assert ("prompt_names_earlier_artifact" in kinds) == (want == "yes"), (prompt, kinds)
        assert m["prior_context"]["prior_conversation_required"] == want, (prompt, m["prior_context"])


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

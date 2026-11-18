#!/usr/bin/env python3
"""Unit tests for measure_relevance.py's transcript join and packet-JSON lookup.

No framework, plain checks (ctxdex-suite style, matching ctxcheck/test_ctxcheck.py).
Runs against throwaway tempfile fixtures only — never the real Claude Code projects directory
or a real .evidence-compiler store.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_relevance as mr  # noqa: E402

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(cond: bool, name: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED += 1
        FAILURES.append(name)
        print(f"FAIL  {name}")


def test_build_turns_counts_malformed_line_and_keeps_valid_data(tmp_path: Path) -> None:
    session_id = "test-session-0001"
    repo_root = "/srv/fixture-repo"
    store_root = repo_root
    packet_id = "ep_test0000000001"

    project_dir = tmp_path / "projects" / "fixture-project"
    project_dir.mkdir(parents=True)
    transcript = project_dir / f"{session_id}.jsonl"
    lines = [
        json.dumps({
            "type": "user",
            "timestamp": "2026-09-21T00:00:00Z",
            "note": f"context_brief marker {packet_id}",
        }),
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-09-21T00:00:01Z",
            "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": f"{repo_root}/lib/foo.py"}},
            ]},
        }),
        '{"type": "assistant", "timestamp": "2026-09-21T00:00:02Z", "message": {',  # malformed JSON
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-09-21T00:00:03Z",
            "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": f"{repo_root}/lib/bar.py"}},
            ]},
        }),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    packet = mr.Packet(
        packet_id=packet_id, store_root=store_root, repo_root=repo_root,
        created_at="2026-09-21T00:00:00Z", session_id=session_id,
        source_kind="user", traffic="candidate", rg_outcome="ok", cited={"lib/foo.py"},
    )

    original_projects_dir = mr.PROJECTS_DIR
    mr.PROJECTS_DIR = tmp_path / "projects"
    try:
        turns, issues = mr.build_turns([packet])
    finally:
        mr.PROJECTS_DIR = original_projects_dir

    check(issues["unreadable_transcript_line"] == 1, "malformed transcript line counted exactly once")
    check(packet_id in turns, "packet still gets a turn despite the malformed line")
    turn = turns.get(packet_id)
    check(turn is not None and turn.opened == ["lib/foo.py", "lib/bar.py"],
          "valid lines before and after the malformed one are both processed, in order")

    result = mr.score(packet.cited, turn)
    check(result is not None and result["hit"] is True, "scoring still runs correctly (no false inflation from the skip)")
    check(result is not None and result["recall"] == 0.5,
          "recall reflects exactly the two real opened files, not more")


def test_load_packet_json_present_and_absent(tmp_path: Path) -> None:
    store_root = str(tmp_path / "repo")
    packets_dir = Path(store_root, ".evidence-compiler", "packets")
    packets_dir.mkdir(parents=True)
    packet_id = "ep_test0000000002"
    (packets_dir / f"20260921-000000_{packet_id}.json").write_text(
        json.dumps({"task": {"extracted_symbols": ["foo"]}}), encoding="utf-8")

    present = mr.load_packet_json(store_root, f"*{packet_id}*.json")
    check(present is not None and present["task"]["extracted_symbols"] == ["foo"],
          "load_packet_json returns parsed JSON for a present packet")

    absent = mr.load_packet_json(store_root, "*ep_does_not_exist*.json")
    check(absent is None, "load_packet_json returns None (never raises) for an absent packet")


def test_inventory_nonzero_exit_warns_and_counts(tmp_path: Path) -> None:
    """SA11-CT-38: a non-zero `evidence review inventory` exit is a WARN and a counted issue, not empty classes."""
    import contextlib
    import io
    import subprocess

    store = tmp_path / "repo" / ".evidence-compiler"
    (store / "packets").mkdir(parents=True)
    (store / "packets" / "20260921-000000_ep_test0000000003.json").write_text(
        json.dumps({"packet_id": "ep_test0000000003", "identity": {}}), encoding="utf-8")

    def fake_run(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="evidence: store locked\n")

    orig_run, orig_globs = mr.subprocess.run, mr.STORE_GLOBS
    mr.subprocess.run, mr.STORE_GLOBS = fake_run, (str(store),)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            classes = mr.traffic_classes(str(tmp_path / "repo"))
            packets, issues = mr.load_packets()
    finally:
        mr.subprocess.run, mr.STORE_GLOBS = orig_run, orig_globs

    check(classes is None, "traffic_classes returns None (not empty classes) on a non-zero exit")
    check("WARN inventory exited 1" in err.getvalue() and "store locked" in err.getvalue(),
          "non-zero inventory exit WARNs with the exit code and stderr")
    check(issues["inventory_failed"] == 1 and len(packets) == 1,
          "load_packets counts inventory_failed and still loads the packet")


def _stub_compiler(root: Path) -> Path:
    """A throwaway evidence_compiler package with just enough surface for the replay CLIs to import."""
    pkg = root / "src" / "evidence_compiler"
    (pkg / "collectors").mkdir(parents=True)
    for rel in ("__init__.py", "collectors/__init__.py", "compiler.py", "ranking.py", "scoping.py"):
        (pkg / rel).write_text("", encoding="utf-8")
    (pkg / "collectors" / "base.py").write_text("class CollectorContext:\n    pass\n", encoding="utf-8")
    (pkg / "collectors" / "ripgrep.py").write_text("_MAX_MATCHES_PER_SYMBOL = 1\n", encoding="utf-8")
    return root / "src"


def _run_replay(module_name: str, argv: list[str], load_issues: Counter, packets: list | None = None) -> dict:
    import contextlib
    import importlib
    import io

    for name in [m for m in sys.modules if m == "evidence_compiler" or m.startswith("evidence_compiler.")]:
        del sys.modules[name]
    mod = importlib.import_module(module_name)
    orig = (mr.load_packets, mr.build_turns, sys.argv, list(sys.path))
    mr.load_packets = lambda: (list(packets or []), Counter(load_issues))
    mr.build_turns = lambda _pool: ({}, Counter({"unreadable_subagent": 1}))
    sys.argv = [module_name, *argv]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            mod.main()
    finally:
        mr.load_packets, mr.build_turns, sys.argv = orig[0], orig[1], orig[2]
        sys.path[:] = orig[3]
        for name in [m for m in sys.modules if m == "evidence_compiler" or m.startswith("evidence_compiler.")]:
            del sys.modules[name]
    return mod


def test_replays_surface_join_issues(tmp_path: Path) -> None:
    """SA11-CT-37: a corrupt packet or transcript dropped by the join makes a replay incomplete, never complete:true."""
    src = _stub_compiler(tmp_path)
    old = tmp_path / "old_ripgrep.py"
    old.write_text("", encoding="utf-8")
    out = tmp_path / "out"

    _run_replay("replay_rg", ["--old", str(old), "--new-src", str(src), "--out", str(out), "--label", "t"],
                Counter({"unreadable_packet": 1}))
    rg = json.loads((out / "replay_rg_t.json").read_text(encoding="utf-8"))["summary"]
    check(rg.get("join_issues") == {"unreadable_packet": 1}, "replay_rg surfaces load_packets issues")
    check(rg["complete"] is False, "replay_rg is incomplete when a packet was unreadable")

    _run_replay("replay_relevance", ["--src", str(src), "--label", "t", "--out", str(out)], Counter())
    rel = json.loads((out / "replay_relevance_t.json").read_text(encoding="utf-8"))["summary"]
    check(rel.get("join_issues") == {"unreadable_subagent": 1}, "replay_relevance surfaces build_turns issues")
    check(rel["complete"] is False, "replay_relevance is incomplete when a subagent transcript was unreadable")

    _run_replay("replay_rg", ["--old", str(old), "--new-src", str(src), "--out", str(out), "--label", "c"],
                Counter({"no_traffic_class": 2}))
    clean = json.loads((out / "replay_rg_c.json").read_text(encoding="utf-8"))["summary"]
    check(clean.get("join_issues") == {"no_traffic_class": 2} and clean["complete"] is True,
          "a coverage-only join issue is surfaced but does not mark data lost")


def _packet(packet_id: str, root: Path, traffic: str = "candidate", symbols: list[str] | None = None) -> "mr.Packet":
    """A packet whose repo and store are ``root`` (absent unless created); a packet JSON with
    ``symbols`` is written when ``symbols`` is given."""
    if symbols is not None:
        d = root / ".evidence-compiler" / "packets"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{packet_id}.json").write_text(json.dumps({"task": {"extracted_symbols": symbols}}), encoding="utf-8")
    return mr.Packet(packet_id=packet_id, store_root=str(root), repo_root=str(root), created_at="2026-09-21T00:00:00Z",
                     session_id=None, source_kind="user", traffic=traffic, rg_outcome="ok", cited=set())


def test_replays_count_every_skip(tmp_path: Path) -> None:
    """SA11-CT-66 residual: a packet a replay leaves out is counted under ``skipped`` with its
    reason (not a candidate, repository gone, no opened files, no symbols), never dropped silently;
    a filter skip is not data loss, so ``complete`` stays true."""
    src = _stub_compiler(tmp_path)
    old = tmp_path / "old_ripgrep.py"
    old.write_text("", encoding="utf-8")
    out = tmp_path / "out"
    repo = tmp_path / "repo"
    repo.mkdir()
    pkts = [_packet("ep_a", repo, traffic="no_brief"), _packet("ep_b", tmp_path / "gone"),
            _packet("ep_c", repo, symbols=[])]

    _run_replay("replay_rg", ["--old", str(old), "--new-src", str(src), "--out", str(out), "--label", "s"],
                Counter(), pkts)
    rg = json.loads((out / "replay_rg_s.json").read_text(encoding="utf-8"))["summary"]
    check(rg.get("skipped") == {"not_candidate": 1, "repo_root_missing": 1, "no_symbols": 1}
          and rg["complete"] is True, f"replay_rg counts each skipped packet by reason: {rg.get('skipped')}")

    _run_replay("replay_relevance", ["--src", str(src), "--label", "s", "--out", str(out)], Counter(), pkts)
    rel = json.loads((out / "replay_relevance_s.json").read_text(encoding="utf-8"))["summary"]
    check(rel.get("skipped") == {"not_candidate": 1, "repo_root_missing": 1, "no_opened_files": 1},
          f"replay_relevance counts pool exclusions and packets with nothing opened: {rel.get('skipped')}")
    _run_replay("replay_relevance", ["--src", str(src), "--label", "t", "--out", str(out),
                                     "--target", "ep_c=lib/x.py"], Counter(), pkts)
    rel = json.loads((out / "replay_relevance_t.json").read_text(encoding="utf-8"))["summary"]
    check(rel.get("skipped") == {"not_candidate": 1, "repo_root_missing": 1, "no_symbols": 1},
          f"replay_relevance counts a packet with no extracted symbols: {rel.get('skipped')}")


def test_compare_counts_unmatched_rows() -> None:
    """SA11-CT-66 residual: compare_replays counts the rows present in only one of the two
    replays instead of skipping them without a trace."""
    import compare_replays as cr

    def row(pid: str, f: str) -> dict:
        return {"packet_id": pid, "opened_file": f, "in_brief": [1, 1, 1], "in_filename_evidence": [0, 0, 0]}
    base = {"files": [row("p", "a.py"), row("p", "b.py")]}
    cfg = {"files": [row("p", "a.py"), row("q", "c.py"), row("q", "d.py")]}
    got = cr._flips(base, cfg, 3)
    check(got.get("unmatched") == {"base_only": 1, "config_only": 2}, f"rows in one replay only are counted: {got}")


def test_build_turns_counts_unattributed_subagents(tmp_path: Path) -> None:
    """SA11-CT-66 residual: a subagent transcript with no timestamp, or one that starts outside
    every packet window, is counted instead of skipped without a trace."""
    sid, pid = "test-session-0002", "ep_test0000000002"
    project = tmp_path / "projects" / "p"
    project.mkdir(parents=True)
    tr = project / f"{sid}.jsonl"
    tr.write_text(json.dumps({"type": "user", "timestamp": "2026-09-21T01:00:00Z",
                              "note": f"context_brief {pid}"}) + "\n", encoding="utf-8")
    subs = project / sid / "subagents"
    subs.mkdir(parents=True)
    (subs / "a.jsonl").write_text(json.dumps({"type": "assistant"}) + "\n", encoding="utf-8")
    (subs / "b.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-21T00:00:00Z"}) + "\n",
                                  encoding="utf-8")
    packet = mr.Packet(packet_id=pid, store_root="/srv/r", repo_root="/srv/r", created_at="2026-09-21T01:00:00Z",
                       session_id=sid, source_kind="user", traffic="candidate", rg_outcome="ok", cited=set())
    saved = mr.PROJECTS_DIR
    mr.PROJECTS_DIR = tmp_path / "projects"
    try:
        _, issues = mr.build_turns([packet])
    finally:
        mr.PROJECTS_DIR = saved
    check(issues["subagent_without_timestamp"] == 1 and issues["subagent_outside_packet_window"] == 1,
          f"unattributed subagent transcripts are counted: {dict(issues)}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        test_build_turns_counts_malformed_line_and_keeps_valid_data(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_load_packet_json_present_and_absent(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_inventory_nonzero_exit_warns_and_counts(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_replays_surface_join_issues(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_replays_count_every_skip(Path(td))
    test_compare_counts_unmatched_rows()
    with tempfile.TemporaryDirectory() as td:
        test_build_turns_counts_unattributed_subagents(Path(td))

    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILURES:
        print("Failures:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

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


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        test_build_turns_counts_malformed_line_and_keeps_valid_data(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_load_packet_json_present_and_absent(Path(td))

    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILURES:
        print("Failures:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

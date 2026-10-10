#!/usr/bin/env python3
"""Unit tests for build_episodes.py.

No framework, plain checks (ctxdex-suite style, matching ctxcheck/test_ctxcheck.py).
Runs against synthetic tempfile fixtures only — never a real Claude Code projects
directory, a real packet store, or a real archive. Requires ``evidence_compiler``
importable (its src/ on PYTHONPATH).

Synthetic example — contains no private repository data or production findings.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_episodes as be  # noqa: E402

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


# ---------------------------------------------------------------- fixtures

REPO = "/srv/fixture-repo"
SESSION_A = "00000000-0000-4000-8000-00000000000a"
SESSION_B = "00000000-0000-4000-8000-00000000000b"
SESSION_C = "00000000-0000-4000-8000-00000000000c"
SESSION_GONE = "00000000-0000-4000-8000-0000000000ff"


def packet(pid: str, session: str, prompt: str, created: str, *, selected: bool = True,
           source_kind: str = "human") -> dict[str, Any]:
    evidence = [{
        "id": f"ev_{pid[3:]}",
        "source_claim": {"kind": "git_meta", "statement": "HEAD 0123456789ab on branch main",
                         "references": []},
        "provenance": {"collector": "git", "command": "git rev-parse HEAD",
                       "captured_at": created, "source_revision": "0" * 40, "graph_hash": None,
                       "extra": {}},
        "authority": "authoritative", "freshness": "current", "confidence": 1.0,
        "compiler_assessment": {
            "relevance": "medium", "selected": selected, "final_score": 1.5,
            "components": {"direct_symbol": 0.0, "active_file": 0.0, "direct_dependency": 0.0,
                           "same_module": 0.0, "lexical_reference": 0.0, "convention": 0.0,
                           "freshness_bonus": 0.5, "authority_bonus": 1.0,
                           "provisional_penalty": 0.0, "duplication_penalty": 0.0},
            "selected_because": ["authoritative source"] if selected else [],
            "omitted_because": [] if selected else ["budget"],
        },
        "status": "usable",
    }]
    return {
        "schema_version": 1, "packet_id": pid, "created_at": created,
        "identity": {"repository_root": REPO, "session_id": session, "turn_id": None,
                     "worktree_id": None, "head": "0" * 40, "branch": "main", "head_state": "ok"},
        "correlation": {"prompt_hash": be.prompt_hash(prompt), "parent_packet_id": None},
        "task": {"raw_prompt_hash": be.prompt_hash(prompt), "intent": "unknown", "active_file": None,
                 "selection_range": None, "extracted_symbols": [], "source_kind": source_kind,
                 "symbol_details": []},
        "scope": {"confidence": "low", "sources": ["git_diff"]},
        "collectors_run": [{"name": "git", "status": "ok", "duration_ms": 1.0, "diagnostic": {}}],
        "evidence": evidence, "negative_evidence": [],
        "budget": {"min_tokens": 600, "default_tokens": 1000, "max_tokens": 1200,
                   "candidate_tokens": 17, "injected_tokens": 60 if selected else 0,
                   "omitted_evidence_ids": []},
        "validation": {"execution": [], "evidence_state": "UNVERIFIED"},
        "timing": {"total_ms": 1.0, "stages": {}},
    }


def brief_text(pkt: dict[str, Any]) -> str:
    return be.render_brief(be.EvidencePacket.from_dict(pkt)).text


def user(ts: str, content: Any, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "timestamp": ts, "uuid": f"u-{ts}", "message": {"role": "user", "content": content},
            **extra}


def brief(ts: str, session: str, text: str) -> dict[str, Any]:
    return {"type": "attachment", "timestamp": ts, "sessionId": session,
            "attachment": {"type": "hook_additional_context", "content": [text]}}


def assistant(ts: str, mid: str, tools: list[tuple[str, str]], output: int = 10) -> dict[str, Any]:
    return {"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "model": "fixture-model",
        "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                  "output_tokens": output},
        "content": [{"type": "tool_use", "id": tid, "name": name, "input": {"path": f"{REPO}/x"}}
                    for tid, name in tools]}}


def tool_result(ts: str, tid: str, is_error: bool) -> dict[str, Any]:
    return user(ts, [{"type": "tool_result", "tool_use_id": tid, "is_error": is_error}])


def queued(ts: str, prompt: Any, mode: str = "task-notification") -> dict[str, Any]:
    return {"type": "attachment", "timestamp": ts, "uuid": f"q-{ts}",
            "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": mode}}


def enqueue(ts: str, content: str) -> dict[str, Any]:
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": ts, "content": content}


def write_jsonl(path: Path, entries: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((e if isinstance(e, str) else json.dumps(e)) + "\n" for e in entries),
                    encoding="utf-8")


def write_packet(root: Path, pkt: dict[str, Any], sub: str = "") -> None:
    d = root / sub if sub else root
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pkt['packet_id']}.json").write_text(json.dumps(pkt, indent=2), encoding="utf-8")


def fixture(tmp: Path) -> tuple[list, list, list]:
    """Two packet sources, one transcript root, one cohort manifest."""
    src_a, src_b, troot = tmp / "src_a", tmp / "src_b", tmp / "transcripts"

    # Session A: a plain prompt with a brief, a slash command, a rewritten prompt.
    p1 = packet("ep_00000000000000a1", SESSION_A, "fix the parser", "2026-09-21T00:00:01.000000Z")
    slash = "<command-message>review</command-message>\n<command-name>/review</command-name>\n<command-args>src/x.py</command-args>"
    p2 = packet("ep_00000000000000a2", SESSION_A, "/review src/x.py", "2026-09-21T00:01:01.000000Z")
    p3 = packet("ep_00000000000000a3", SESSION_A, "the hook saw this text", "2026-09-21T00:02:01.000000Z",
                selected=False)
    write_jsonl(troot / "proj-a" / f"{SESSION_A}.jsonl", [
        user("2026-09-21T00:00:00.000Z", "fix the parser"),
        brief("2026-09-21T00:00:01.500Z", SESSION_A, brief_text(p1)),
        assistant("2026-09-21T00:00:02.000Z", "msg_1", [("tu_1", "Read")]),
        assistant("2026-09-21T00:00:02.100Z", "msg_1", [("tu_2", "Grep")], output=20),  # same message, later chunk
        tool_result("2026-09-21T00:00:03.000Z", "tu_1", False),
        tool_result("2026-09-21T00:00:03.100Z", "tu_2", True),
        "{not json",
        user("2026-09-21T00:01:00.000Z", slash),
        brief("2026-09-21T00:01:01.500Z", SESSION_A, brief_text(p2)),
        assistant("2026-09-21T00:01:02.000Z", "msg_2", []),
        user("2026-09-21T00:02:00.000Z", [{"type": "text", "text": "<system-reminder>x</system-reminder>"},
                                          {"type": "text", "text": "the app rewrote this"}]),
        assistant("2026-09-21T00:02:02.000Z", "msg_3", [("tu_3", "Bash")]),
    ])

    # Session B: queued prompts delivered as a batch, empty briefs, plus a meta prompt.
    p4 = packet("ep_00000000000000b1", SESSION_B, "<task-notification>one</task-notification>",
                "2026-09-21T01:00:01.000000Z", selected=False, source_kind="harness")
    p5 = packet("ep_00000000000000b2", SESSION_B, "<task-notification>two</task-notification>",
                "2026-09-21T01:00:01.500000Z", selected=False, source_kind="harness")
    p6 = packet("ep_00000000000000b3", SESSION_B, "Continue the run", "2026-09-21T01:05:01.000000Z")
    write_jsonl(troot / "proj-b" / f"{SESSION_B}.jsonl", [
        enqueue("2026-09-21T00:59:50.000Z", "<task-notification>one</task-notification>"),
        enqueue("2026-09-21T00:59:55.000Z", "<task-notification>two</task-notification>"),
        queued("2026-09-21T01:00:00.000Z", [{"type": "text", "text": "<task-notification>one</task-notification>"}]),
        queued("2026-09-21T01:00:00.500Z", "<task-notification>two</task-notification>"),
        assistant("2026-09-21T01:00:05.000Z", "msg_4", [("tu_4", "Read")]),
        user("2026-09-21T01:05:00.000Z", "Continue the run", isMeta=True),
        brief("2026-09-21T01:05:01.500Z", SESSION_B, brief_text(p6)),
        assistant("2026-09-21T01:05:02.000Z", "msg_5", []),
    ])

    # Session C: a brief is expected but never injected.
    p7 = packet("ep_00000000000000c1", SESSION_C, "look at this", "2026-09-21T02:00:01.000000Z")
    write_jsonl(troot / "proj-c" / f"{SESSION_C}.jsonl", [
        user("2026-09-21T02:00:00.000Z", "look at this"),
        assistant("2026-09-21T02:00:02.000Z", "msg_6", []),
    ])

    # Ambiguous: one brief copied into two files, neither named for the packet's session.
    p8 = packet("ep_00000000000000d1", SESSION_GONE, "shared", "2026-09-21T03:00:01.000000Z")
    for s in ("00000000-0000-4000-8000-0000000000d1", "00000000-0000-4000-8000-0000000000d2"):
        write_jsonl(troot / "proj-d" / f"{s}.jsonl", [
            user("2026-09-21T03:00:00.000Z", "shared"),
            brief("2026-09-21T03:00:01.500Z", "00000000-0000-4000-8000-0000000000d0", brief_text(p8)),
        ])

    p9 = packet("ep_00000000000000e1", SESSION_GONE, "no transcript", "2026-09-21T04:00:01.000000Z")
    conflict = packet("ep_00000000000000f1", SESSION_C, "conflict", "2026-09-21T05:00:01.000000Z")

    for p in (p1, p2, p3, p4, p5, p6, p7, p8, p9, conflict):
        write_packet(src_a, p, "repo-x")
    write_packet(src_b, p1)  # identical copy: one row, two sources
    write_packet(src_b, dict(conflict, created_at="2026-09-21T05:00:02.000000Z"))  # different bytes
    (src_a / "repo-x" / "ep_00000000000000f2.json").write_text("{broken", encoding="utf-8")

    manifest = tmp / "manifest.json"
    manifest.write_text(json.dumps({
        "cohort": {"evidence_compiler_commit": "abc1234"},
        "packets": [{"packet_id": p1["packet_id"], "traffic": "candidate"}],
        "lost_packets": {"packets": [{"packet_id": "ep_0000000000000099", "created_at": "2026-09-20T00:00:00Z",
                                      "traffic": "candidate", "transcripts": ["proj-z/x.jsonl"]}]},
    }), encoding="utf-8")
    return [("a", src_a), ("b", src_b)], [("t", troot)], [("c", manifest)]


def rows_by_id(out: Path) -> dict[str, dict[str, Any]]:
    rows = [json.loads(line) for line in (out / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]
    return {r["packet_id"]: r for r in rows}


# ---------------------------------------------------------------- tests

def test_unit_helpers() -> None:
    forms = be.prompt_forms("<command-message>x</command-message>\n<command-name>/x</command-name>\n<command-args></command-args>")
    check(forms[-1] == "/x", "prompt_forms: a slash command with no args reduces to its name")
    forms = be.prompt_forms("<command-name>/r</command-name><command-args>a b</command-args>")
    check(forms[-1] == "/r a b", "prompt_forms: a slash command keeps its args")
    check(be.prompt_forms("plain") == ["plain"], "prompt_forms: plain text has one form")
    check(be.tree_digest([("b", "2"), ("a", "1")]) == be.tree_digest([("a", "1"), ("b", "2")]),
          "tree_digest is order-independent")


def test_build(tmp: Path) -> None:
    sources, troots, cohorts = fixture(tmp)
    rec = be.build(sources, troots, cohorts, tmp / "out1")
    rows = rows_by_id(tmp / "out1")
    s = rec["summary"]

    r = rows["ep_00000000000000a1"]
    check(r["join_reason"] == "joined", "plain prompt with brief joins")
    check([x["source"] for x in r["sources"]] == ["a", "b"], "identical copies: one row, both sources listed")
    check(r["injected"]["found"] and r["injected"]["matches_render"], "brief found and equals render_brief")
    check(r["prompt"]["anchored_by"] == "hash" and r["prompt"]["hash_matches_packet"], "prompt anchored by hash")
    calls = r["observed"]["tool_calls"]
    check([c["name"] for c in calls] == ["Read", "Grep"], "tool calls in order, full turn")
    check([c["is_error"] for c in calls] == [False, True], "tool errors joined from results")
    cost = r["observed"]["cost"]
    check(cost["assistant_messages"] == 1 and cost["output_tokens"] == 20,
          "usage deduplicated by message.id, last chunk wins")
    check(r["cohort"] == "c" and r["cohort_status"] == "member" and r["compiler_commit"] == "abc1234",
          "cohort membership and commit from manifest")
    check(r["transcript"]["parse_errors"] == 1, "malformed transcript line counted, not fatal")
    check(r["observed"]["ended_by"] == "prompt", "turn ends at the next prompt")

    r = rows["ep_00000000000000a2"]
    check(r["prompt"]["hash_matches_packet"] and r["prompt"]["text"] == "/review src/x.py"
          and r["prompt"]["transcript_text"].startswith("<command-message>"),
          "slash command matched through its typed form; stored markup kept")
    check(rows["ep_00000000000000a1"]["prompt"]["prior_prompts"] == {}
          and r["prompt"]["prior_prompts"] == {"user": 1}, "earlier delivered prompts counted by origin")

    r = rows["ep_00000000000000a3"]
    check(r["join_reason"] == "joined" and r["injected"]["rendered_nonempty"] is False,
          "empty brief joins without a brief in the transcript")
    check(r["prompt"]["anchored_by"] == "time" and r["prompt"]["hash_matches_packet"] is False,
          "rewritten prompt: time fallback, mismatch reported")

    b1, b2 = rows["ep_00000000000000b1"], rows["ep_00000000000000b2"]
    check(b1["prompt"]["origin"] == "queued_command:task-notification",
          "queued prompt (content-block list) outranks its enqueue")
    check(b1["observed"]["cost"]["assistant_messages"] == 0 and b1["observed"]["ended_by"] == "prompt",
          "batched prompt: earlier turn empty, ended by the next prompt")
    check(b2["observed"]["cost"]["assistant_messages"] == 1, "batched prompt: last turn gets the response")

    r = rows["ep_00000000000000b3"]
    check(r["prompt"]["origin"] == "meta" and r["prompt"]["hash_matches_packet"],
          "meta entry without origin anchors a brief by hash")

    check(rows["ep_00000000000000c1"]["join_reason"] == "brief_not_found", "missing brief reported")
    check(rows["ep_00000000000000d1"]["join_reason"] == "ambiguous_transcript_session",
          "brief in two files of one root is ambiguous")
    check(rows["ep_00000000000000e1"]["join_reason"] == "missing_transcript", "no transcript reported")
    check(rows["ep_00000000000000f1"]["join_reason"] == "sha_conflict", "differing copies are a conflict")
    check(rows["ep_00000000000000f2"]["join_reason"] == "parse_failure", "malformed packet is data, not a crash")
    lost = rows["ep_0000000000000099"]
    check(lost["join_reason"] == "missing_packet" and lost["cohort_status"] == "lost"
          and lost["recorded_transcripts"] == ["proj-z/x.jsonl"], "lost packet keeps its row")

    check(set(s["join_reasons"]) == set(be.JOIN_REASONS) and sum(s["join_reasons"].values()) == len(rows),
          "every row has exactly one join reason")
    check(s["sha_conflicts"] == ["ep_00000000000000f1"], "summary lists conflicts")
    check("ep_00000000000000a3" in s["prompts"]["hash_mismatch"], "summary lists prompt hash mismatches")
    footprint_keys = {"hit", "coverage", "waste", "rediscovery", "gap", "head_start"}
    check(not any(footprint_keys & set(json.dumps(r)) for r in rows.values())
          and not any(k in r for r in rows.values() for k in footprint_keys),
          "rows hold no Footprints fields")

    be.build(sources, troots, cohorts, tmp / "out2")
    for name in ("episodes.jsonl", "build.json"):
        check((tmp / "out1" / name).read_bytes() == (tmp / "out2" / name).read_bytes(),
              f"two builds are byte-identical: {name}")


def test_root_precedence(tmp: Path) -> None:
    """Transcript roots take precedence in command-line order, not by name."""
    sources, troots, cohorts = fixture(tmp)
    (_, troot), = troots
    # A second root, named to sort BEFORE the first, holding copies of two session files:
    # one with a brief (matched by packet_id) and one without (matched by session only).
    other = tmp / "transcripts_copy"
    for proj, session in (("proj-a", SESSION_A), ("proj-c", SESSION_C)):
        (other / proj).mkdir(parents=True)
        (other / proj / f"{session}.jsonl").write_bytes((troot / proj / f"{session}.jsonl").read_bytes())
    roots = [("zz", troot), ("aa", other)]
    be.build(sources, roots, cohorts, tmp / "out-rank")
    rows = rows_by_id(tmp / "out-rank")
    check(rows["ep_00000000000000a1"]["join_reason"] == "joined"
          and rows["ep_00000000000000a1"]["transcript"]["source"] == "zz",
          "a brief in two roots is taken from the first root given, not the first by name")
    check(rows["ep_00000000000000c1"]["join_reason"] == "brief_not_found"
          and rows["ep_00000000000000c1"]["transcript"]["source"] == "zz",
          "a session in two roots is taken from the first root given")
    be.build(sources, roots[::-1], cohorts, tmp / "out-rank2")
    rows = rows_by_id(tmp / "out-rank2")
    check(rows["ep_00000000000000a1"]["transcript"]["source"] == "aa"
          and rows["ep_00000000000000c1"]["transcript"]["source"] == "aa",
          "reversing the roots reverses the choice")


def main() -> int:
    test_unit_helpers()
    with tempfile.TemporaryDirectory() as td:
        test_build(Path(td))
    with tempfile.TemporaryDirectory() as td:
        test_root_precedence(Path(td))

    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILURES:
        print("Failures:", ", ".join(FAILURES))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

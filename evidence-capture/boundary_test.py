#!/usr/bin/env python3
"""Ordering tests for the start-state boundary under one pinned Claude CLI (plan §2a).

Measures, never assumes:

  (a) whether ``UserPromptSubmit`` hooks finish before the first tool call and before
      the first assistant output reaches the client;
  (b) whether two hooks on that event run serially or concurrently;
  (c) whether a hook exceeding its timeout is killed or left running, and whether the
      turn proceeds meanwhile;
  (d) the delay between hook completion and the first tool call.

Each run uses a disposable git repository under a temporary directory, ``EVIDENCE_HOOK=0``
and ``EVIDENCE_CAPTURE=0``, ``--no-session-persistence``, ``--setting-sources ""`` (no user,
project or local settings, so no live hook runs) and ``--strict-mcp-config`` with no MCP
servers. Hooks come only from a ``--settings`` file this script writes. One short prompt
per run asks for a single shell command; nothing else is sent.

Output: ``boundary/<cli-version>.json`` with ``relied_upon``, ``results`` and raw timings.
Such a version-only record carries no client identity (executable hash and invocation
mode), so ``manifest.load_boundary`` never trusts it (``identity_untested``). It remains
evidence for print mode; an identity-bearing record comes from ``desktop_boundary_test.py``
and an owner acceptance.

Usage::

    python boundary_test.py --cli <path-to-claude-executable> [--model <id>] [--runs 3]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RELIED_UPON = ("a_hooks_finish_before_first_tool", "a_hooks_finish_before_first_output",
               "c_turn_waits_for_hook_within_timeout")
HOOK_SRC = r'''
import json, sys, time
from pathlib import Path
log, name, sleep_s = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
sys.stdin.read()
def mark(ev):
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"hook": name, "ev": ev, "t": time.time()}) + "\n")
mark("start")
time.sleep(sleep_s)
mark("end")
'''
TOOL_HOOK_SRC = r'''
import json, sys, time
from pathlib import Path
log = Path(sys.argv[1])
sys.stdin.read()
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps({"hook": "pretool", "ev": "tool", "t": time.time()}) + "\n")
'''
PROMPT = ("Use the Bash tool exactly once to run: echo boundary-probe . "
          "Do not write any text before the tool call. After it returns, reply with the single word done.")


def _cmd(py: Path, *args: str) -> str:
    quoted = " ".join(f'"{a}"' for a in (sys.executable, str(py), *args))
    return quoted.replace("\\", "/")


def _settings(work: Path, log: Path, hooks: list[tuple[str, float, int]]) -> Path:
    hook_py = work / "hook.py"
    tool_py = work / "tool_hook.py"
    hook_py.write_text(HOOK_SRC, encoding="utf-8")
    tool_py.write_text(TOOL_HOOK_SRC, encoding="utf-8")
    ups = [{"type": "command", "command": _cmd(hook_py, str(log), name, str(sleep_s)), "timeout": timeout}
           for name, sleep_s, timeout in hooks]
    settings = {"hooks": {
        "UserPromptSubmit": [{"hooks": ups}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": _cmd(tool_py, str(log)),
                                                       "timeout": 10}]}],
    }}
    p = work / "settings.json"
    p.write_text(json.dumps(settings, indent=1), encoding="utf-8")
    return p


def _repo(work: Path) -> Path:
    repo = work / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    (repo / "README.md").write_bytes(b"# boundary fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "start"], check=True, env=env)
    return repo


def run_once(cli: Path, model: str, hooks: list[tuple[str, float, int]], wait_after_s: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ec-boundary-") as tmp:
        work = Path(tmp)
        log = work / "events.jsonl"
        settings = _settings(work, log, hooks)
        repo = _repo(work)
        env = {**os.environ, "EVIDENCE_HOOK": "0", "EVIDENCE_CAPTURE": "0"}
        argv = [str(cli), "-p", PROMPT, "--model", model, "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", "--setting-sources", "", "--settings", str(settings),
                "--strict-mcp-config", "--allowedTools", "Bash", "--max-turns", "3"]
        t_submit = time.time()
        proc = subprocess.Popen(argv, cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        first_output: float | None = None
        first_tool_use: float | None = None
        kinds: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            now = time.time()
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kinds.append(f"{ev.get('type')}:{ev.get('subtype', '')}")
            if ev.get("type") == "assistant":
                first_output = first_output or now
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_use":
                        first_tool_use = first_tool_use or now
        proc.wait(timeout=300)
        t_exit = time.time()
        stderr = proc.stderr.read() if proc.stderr else ""
        time.sleep(wait_after_s)  # let a hook the client abandoned finish, if it was left running
        events = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return {"t_submit": t_submit, "t_exit": t_exit, "rc": proc.returncode, "first_output": first_output,
            "first_tool_stream": first_tool_use, "events": events, "stream_kinds": kinds[:12],
            "stderr_tail": stderr[-300:]}


def _ev(events: list[dict[str, Any]], hook: str, kind: str) -> float | None:
    for e in events:
        if e["hook"] == hook and e["ev"] == kind:
            return float(e["t"])
    return None


def analyse(order_runs: list[dict[str, Any]], timeout_run: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    before_tool, before_output, serial, delays = [], [], [], []
    for r in order_runs:
        ev = r["events"]
        ends = [t for t in (_ev(ev, "A", "end"), _ev(ev, "B", "end")) if t is not None]
        tool = _ev(ev, "pretool", "tool")
        last_end = max(ends) if len(ends) == 2 else None
        before_tool.append(bool(last_end and tool and last_end < tool))
        before_output.append(bool(last_end and r["first_output"] and last_end < r["first_output"]))
        a_s, a_e, b_s, b_e = (_ev(ev, "A", "start"), _ev(ev, "A", "end"), _ev(ev, "B", "start"), _ev(ev, "B", "end"))
        if None not in (a_s, a_e, b_s, b_e):
            serial.append(bool(b_s >= a_e or a_s >= b_e))
        if last_end and tool:
            delays.append(round((tool - last_end) * 1000))
    n = len(order_runs)
    results["a_hooks_finish_before_first_tool"] = {"passed": n > 0 and all(before_tool), "runs": before_tool}
    results["a_hooks_finish_before_first_output"] = {"passed": n > 0 and all(before_output), "runs": before_output}
    results["b_hooks_serial"] = {"passed": n > 0 and all(serial), "runs": serial,
                                 "note": "recorded only; capture does not rely on hook ordering between hooks"}
    results["d_hook_end_to_first_tool_ms"] = {"passed": bool(delays), "values": delays}

    ev = timeout_run["events"]
    started = _ev(ev, "slow", "start")
    ended = _ev(ev, "slow", "end")
    tool = _ev(ev, "pretool", "tool")
    turn_waited = bool(started and tool and tool - started >= timeout_run["timeout_s"] - 0.25)
    results["c_turn_waits_for_hook_within_timeout"] = {
        "passed": turn_waited,
        "hook_started": started is not None, "hook_finished_later": ended is not None,
        "hook_killed_or_abandoned": ended is None,
        "tool_after_hook_start_ms": round((tool - started) * 1000) if started and tool else None,
        "hook_end_after_tool": bool(ended and tool and ended > tool),
        "note": "passed = the first tool call did not happen before the hook timeout elapsed",
    }
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", required=True, type=Path)
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", type=Path, default=HERE / "boundary")
    args = ap.parse_args()
    version = subprocess.run([str(args.cli), "--version"], capture_output=True, text=True, timeout=60).stdout.split()[0]
    order_runs = [run_once(args.cli, args.model, [("A", 1.0, 30), ("B", 1.0, 30)], wait_after_s=0)
                  for _ in range(args.runs)]
    timeout_s = 3
    timeout_run = run_once(args.cli, args.model, [("slow", 8.0, timeout_s)], wait_after_s=8)
    timeout_run["timeout_s"] = timeout_s
    results = analyse(order_runs, timeout_run)
    rec = {"cli_version": version, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "mode": "print (-p, stream-json); interactive Desktop turns are not exercised by this harness",
           "model": args.model, "relied_upon": list(RELIED_UPON), "results": results,
           "raw": {"order_runs": order_runs, "timeout_run": timeout_run}}
    args.out.mkdir(exist_ok=True)
    out = args.out / f"{version}.json"
    out.write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v.get("passed") for k, v in results.items()}))
    print(out)
    return 0 if all(results[k]["passed"] for k in RELIED_UPON) else 1


if __name__ == "__main__":
    sys.exit(main())

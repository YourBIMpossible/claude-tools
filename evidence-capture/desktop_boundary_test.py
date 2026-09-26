#!/usr/bin/env python3
"""Start-boundary tests in the desktop client's CLI mode (SDK stream-json streaming).

The desktop client does not run the interactive terminal UI. It spawns the bundled CLI as
a long-lived headless process with ``--input-format stream-json --output-format
stream-json --await-initialize --permission-prompt-tool stdio`` and drives it over stdin
and stdout: an ``initialize`` control request, then one user message per turn, answering
``can_use_tool`` control requests. This harness drives the same binary the same way, so
unlike ``boundary_test.py`` (``-p`` with a prompt argument, one turn per process) it
exercises several turns in one process, partial-message streaming and the stdio
permission channel.

Measured, never assumed:

  (a) ``UserPromptSubmit`` hooks finish before the first tool call and before the first
      streamed model output of the turn, on every turn of a long-lived process;
  (b) hooks on one event run serially or concurrently; hooks from a project settings file
      and from inline ``--settings`` both run;
  (c) a hook over its timeout is killed or abandoned, and the turn waits until then;
  (d) hook end to first tool call, prompt submission to hook start;
  (e) hook input fields (``UserPromptSubmit`` and ``Stop``), and whether the transcript
      holds the turn's final assistant message when ``Stop`` runs;
  (f) whether a child launched detached from a hook (as the capture start hook launches
      maintenance) survives the hook's exit, a hook timeout kill and the CLI's exit;
  (g) the client identity every probe hook observed (``client_identity.observe``:
      executable, invocation mode), checked against the ``--cli`` executable's SHA-256.

Isolation: a disposable git repository under ``--out``; ``--setting-sources project,local``
(no user settings, so no live hook runs); ``--strict-mcp-config`` with no servers;
``EVIDENCE_HOOK=0``, ``EVIDENCE_CAPTURE=0`` and ``EC_CAPTURE_MAINTAIN=0``. Every inherited
``CLAUDE*`` and ``DESKTOP_*`` variable is removed from the CLI's environment (the names, not
the values, are recorded), so a harness launched from inside a client tests the mode the
CLI sets for itself, not the parent's. Sessions persist
(the desktop client persists them, and the ``Stop`` transcript check needs it); with
``--cleanup-transcripts`` the harness removes exactly the session files it created.

Not reproducible here and reported as such: the desktop host process itself, its inline
``--settings`` payload and ``initialize`` request (which may register SDK hook callbacks),
hooks from the user settings source, ``--resume`` of an existing session, and interactive
permission prompts answered by a person.

Output, in ``--out`` only: the full result, and a candidate boundary record
``<version>--<mode>.json`` whose ``identity`` is the tested executable hash, version and
mode, with ``accepted: null``. A candidate is never trusted. Accepting it (adding
``accepted: {at, by}`` and placing it in ``boundary/``) is the owner's decision.

Usage::

    python desktop_boundary_test.py --cli <claude executable> --out <dedicated dir> [--runs 3]
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))
import client_identity as ci  # noqa: E402

RELIED_UPON = ("a_hooks_finish_before_first_tool", "a_hooks_finish_before_first_output",
               "c_turn_waits_for_hook_within_timeout", "g_client_identity_observed")
SCRUB_PREFIXES = ("CLAUDE", "DESKTOP_")

HOOK_SRC = r'''
import json, os, subprocess, sys, time
from pathlib import Path
log, name, sleep_s, detach = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4]
raw = sys.stdin.read()
def mark(ev, **kw):
    with log.with_name(f"{log.stem}-{os.getpid()}{log.suffix}").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"hook": name, "ev": ev, "t": time.time(), "pid": os.getpid(), **kw}) + "\n")
try:
    payload = json.loads(raw)
except ValueError:
    payload = {"_unparsed": raw[:200]}
try:
    sys.path.insert(0, os.environ["EC_PROBE_PKG"])
    import client_identity as ci
    client = ci.persisted(ci.observe(dict(os.environ)))
except Exception as e:
    client = {"problem": "probe_error:" + type(e).__name__}
mark("start", payload=payload, client=client)
if detach != "-":
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    child = subprocess.Popen([sys.executable, detach, str(log), name], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=flags, start_new_session=os.name != "nt")
    mark("spawned", child=child.pid)
time.sleep(sleep_s)
mark("end")
'''
CHILD_SRC = r'''
import json, os, sys, time
from pathlib import Path
log, name = Path(sys.argv[1]), sys.argv[2]
def mark(ev):
    with log.with_name(f"{log.stem}-{os.getpid()}{log.suffix}").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"hook": "child-" + name, "ev": ev, "t": time.time(), "pid": os.getpid()}) + "\n")
mark("start")
end = time.time() + float(os.environ.get("EC_PROBE_CHILD_S", "20"))
while time.time() < end:
    time.sleep(0.5)
    mark("beat")
mark("end")
'''
TOOL_HOOK_SRC = r'''
import json, os, sys, time
from pathlib import Path
log = Path(sys.argv[1])
payload = json.loads(sys.stdin.read() or "{}")
with log.with_name(f"{log.stem}-{os.getpid()}{log.suffix}").open("a", encoding="utf-8") as f:
    f.write(json.dumps({"hook": "pretool", "ev": "tool", "t": time.time(),
                        "tool": payload.get("tool_name")}) + "\n")
'''
STOP_HOOK_SRC = r'''
import json, os, sys, time
from pathlib import Path
log = Path(sys.argv[1])
t = time.time()
payload = json.loads(sys.stdin.read() or "{}")
tx = payload.get("transcript_path")
info = {"tx_exists": False}
if isinstance(tx, str) and Path(tx).is_file():
    lines = Path(tx).read_text(encoding="utf-8", errors="replace").splitlines()
    types = []
    last_assistant_text = None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        types.append(e.get("type"))
        if e.get("type") == "assistant":
            for b in (e.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "text":
                    last_assistant_text = b.get("text")
    info = {"tx_exists": True, "tx_lines": len(lines), "tx_last_types": types[-3:],
            "tx_last_assistant_text": (last_assistant_text or "")[:40]}
with log.with_name(f"{log.stem}-{os.getpid()}{log.suffix}").open("a", encoding="utf-8") as f:
    f.write(json.dumps({"hook": "stop", "ev": "stop", "t": t, "payload": payload, **info}) + "\n")
'''
PROMPT = ("Use the Bash tool exactly once to run: echo boundary-probe-{n} . "
          "Do not write any text before the tool call. After it returns, reply with the single word done.")
FIXTURE_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _cmd(*args: str) -> str:
    return " ".join(f'"{a}"' for a in args).replace("\\", "/")


def _write_scripts(work: Path) -> dict[str, Path]:
    out = {}
    for name, src in (("hook", HOOK_SRC), ("child", CHILD_SRC), ("tool", TOOL_HOOK_SRC), ("stop", STOP_HOOK_SRC)):
        out[name] = work / f"{name}.py"
        out[name].write_text(src, encoding="utf-8")
    return out


def _ups_hook(py: dict[str, Path], log: Path, name: str, sleep_s: float, timeout: int, detach: bool) -> dict[str, Any]:
    return {"type": "command", "timeout": timeout,
            "command": _cmd(sys.executable, str(py["hook"]), str(log), name, str(sleep_s),
                            str(py["child"]) if detach else "-")}


def _repo(work: Path, project_settings: dict[str, Any]) -> Path:
    repo = work / "repo"
    repo.mkdir()
    env = {**os.environ, **FIXTURE_ENV}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    (repo / "README.md").write_bytes(b"# boundary fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "start"], check=True, env=env)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps(project_settings, indent=1), encoding="utf-8")
    return repo


class Session:
    """One CLI process driven like the desktop client drives it."""

    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        self.q: queue.Queue[tuple[float, dict[str, Any] | None]] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self._rid = 0

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            now = time.time()
            try:
                self.q.put((now, json.loads(line)))
            except ValueError:
                continue
        self.q.put((time.time(), None))

    def _read_err(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line)

    def send(self, obj: dict[str, Any]) -> float:
        assert self.proc.stdin is not None
        t = time.time()
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        return t

    def initialize(self, timeout: float = 60) -> dict[str, Any]:
        self._rid += 1
        rid = f"init_{self._rid}"
        self.send({"type": "control_request", "request_id": rid, "request": {"subtype": "initialize"}})
        end = time.time() + timeout
        while time.time() < end:
            try:
                _, ev = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if ev is None:
                raise RuntimeError("CLI exited during initialize: " + "".join(self.stderr)[-400:])
            if ev.get("type") == "control_response":
                return ev
        raise RuntimeError("no initialize response")

    def turn(self, text: str, timeout: float = 240) -> dict[str, Any]:
        t_submit = self.send({"type": "user", "session_id": "", "parent_tool_use_id": None,
                              "message": {"role": "user", "content": text}})
        rec: dict[str, Any] = {"t_submit": t_submit, "first_stream": None, "first_assistant": None,
                               "first_tool_stream": None, "replayed_user": None, "permission_requests": [],
                               "result": None, "session_id": None, "kinds": []}
        end = time.time() + timeout
        while time.time() < end:
            try:
                now, ev = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if ev is None:
                rec["exited"] = True
                break
            kind = f"{ev.get('type')}:{ev.get('subtype', '')}"
            if len(rec["kinds"]) < 40 and (not rec["kinds"] or rec["kinds"][-1] != kind):
                rec["kinds"].append(kind)
            rec["session_id"] = rec["session_id"] or ev.get("session_id")
            typ = ev.get("type")
            if typ == "user" and rec["replayed_user"] is None:
                rec["replayed_user"] = now
            elif typ == "stream_event":
                rec["first_stream"] = rec["first_stream"] or now
                se = ev.get("event") or {}
                block = se.get("content_block") or {}
                if se.get("type") == "content_block_start" and block.get("type") == "tool_use":
                    rec["first_tool_stream"] = rec["first_tool_stream"] or now
            elif typ == "assistant":
                rec["first_assistant"] = rec["first_assistant"] or now
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use":
                        rec["first_tool_stream"] = rec["first_tool_stream"] or now
            elif typ == "control_request":
                req = ev.get("request") or {}
                rec["permission_requests"].append({"t": now, "subtype": req.get("subtype"),
                                                   "tool": req.get("tool_name")})
                if req.get("subtype") == "can_use_tool":
                    self.send({"type": "control_response", "response": {
                        "subtype": "success", "request_id": ev.get("request_id"),
                        "response": {"behavior": "allow", "updatedInput": req.get("input") or {}}}})
                else:
                    self.send({"type": "control_response", "response": {
                        "subtype": "error", "request_id": ev.get("request_id"), "error": "not supported by probe"}})
            elif typ == "result":
                rec["result"] = now
                rec["result_subtype"] = ev.get("subtype")
                rec["is_error"] = ev.get("is_error")
                break
        return rec

    def close(self, timeout: float = 60) -> float:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=30)
        return time.time()


def _argv(cli: Path, model: str, inline: dict[str, Any], permission_mode: str, effort: str | None) -> list[str]:
    argv = [str(cli), "--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
            "--model", model, "--permission-prompt-tool", "stdio", "--permission-mode", permission_mode,
            "--include-partial-messages", "--await-initialize", "--thinking-display", "omitted",
            "--replay-user-messages", "--setting-sources=project,local", "--settings", json.dumps(inline),
            "--strict-mcp-config"]
    if effort:
        argv[argv.index("--model"):argv.index("--model")] = ["--effort", effort]
    return argv


def read_events(work: Path) -> list[dict[str, Any]]:
    """Every probe's events in time order. Each process appends to its own file: concurrent
    appends to one file interleave on Windows."""
    events = []
    for f in sorted(work.glob("events-*.jsonl")):
        events += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
    return sorted(events, key=lambda e: e["t"])


def scrubbed_names() -> list[str]:
    return sorted(k for k in os.environ if k.upper().startswith(SCRUB_PREFIXES))


def scrubbed_env() -> dict[str, str]:
    """The harness's environment without any inherited client variable."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(SCRUB_PREFIXES)}


def run_session(cli: Path, base: Path, model: str, turns: int, hooks_file: list[tuple[str, float, int, bool]],
                hooks_inline: list[tuple[str, float, int, bool]], permission_mode: str, effort: str | None,
                child_s: float, wait_after_s: float) -> dict[str, Any]:
    work = Path(tempfile.mkdtemp(prefix="ec-desktop-probe-", dir=base))
    log = work / "events.jsonl"
    py = _write_scripts(work)
    project = {"hooks": {
        "UserPromptSubmit": [{"hooks": [_ups_hook(py, log, *h) for h in hooks_file]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "timeout": 10,
                                                       "command": _cmd(sys.executable, str(py["tool"]), str(log))}]}],
        "Stop": [{"hooks": [{"type": "command", "timeout": 30,
                             "command": _cmd(sys.executable, str(py["stop"]), str(log))}]}],
    }}
    inline = {"hooks": {"UserPromptSubmit": [{"hooks": [_ups_hook(py, log, *h) for h in hooks_inline]}]}} \
        if hooks_inline else {}
    repo = _repo(work, project)
    env = {**scrubbed_env(), "EVIDENCE_HOOK": "0", "EVIDENCE_CAPTURE": "0", "EC_CAPTURE_MAINTAIN": "0",
           "EC_PROBE_CHILD_S": str(child_s), "EC_PROBE_PKG": str(PKG)}
    s = Session(_argv(cli, model, inline, permission_mode, effort), repo, env)
    t_spawn = time.time()
    init = s.initialize()
    t_init = time.time()
    recs = [s.turn(PROMPT.format(n=i + 1)) for i in range(turns)]
    t_exit = s.close()
    time.sleep(max(wait_after_s, t_spawn + child_s + 4 - time.time()) if child_s else wait_after_s)
    events = read_events(work)
    return {"work": str(work), "repo": str(repo), "rc": s.proc.returncode, "t_spawn": t_spawn, "t_init": t_init,
            "init_ok": (init.get("response") or {}).get("subtype") == "success", "turns": recs, "t_exit": t_exit,
            "events": events, "stderr_tail": "".join(s.stderr)[-600:]}


def _ms(a: float | None, b: float | None) -> int | None:
    return round((b - a) * 1000) if a is not None and b is not None else None


def _turn_events(run: dict[str, Any], i: int) -> list[dict[str, Any]]:
    """Events belonging to turn i: from its submission to the next turn's submission."""
    turns = run["turns"]
    lo = turns[i]["t_submit"]
    hi = turns[i + 1]["t_submit"] if i + 1 < len(turns) else float("inf")
    return [e for e in run["events"] if lo <= e["t"] < hi and not e["hook"].startswith("child-")]


def _first(evs: list[dict[str, Any]], hook: str, ev: str) -> dict[str, Any] | None:
    return next((e for e in evs if e["hook"] == hook and e["ev"] == ev), None)


def analyse(order_runs: list[dict[str, Any]], timeout_run: dict[str, Any], timeout_s: int,
            ups_names: list[str]) -> dict[str, Any]:
    before_tool, before_output, serial, both_sources, delays, invoke, per_turn = [], [], [], [], [], [], []
    ups_fields: set[str] = set()
    stop_fields: set[str] = set()
    stop_checks = []
    for run in order_runs:
        for i, t in enumerate(run["turns"]):
            evs = _turn_events(run, i)
            starts = {n: _first(evs, n, "start") for n in ups_names}
            ends = {n: _first(evs, n, "end") for n in ups_names}
            tool = _first(evs, "pretool", "tool")
            stop = _first(evs, "stop", "stop")
            both_sources.append(all(starts.values()))
            last_end = max((e["t"] for e in ends.values() if e), default=None) if all(ends.values()) else None
            first_start = min((e["t"] for e in starts.values() if e), default=None)
            before_tool.append(bool(last_end and tool and last_end < tool["t"]))
            first_out = t["first_stream"] or t["first_assistant"]
            before_output.append(bool(last_end and first_out and last_end < first_out))
            a, b = ups_names[0], ups_names[1]
            if starts[a] and ends[a] and starts[b] and ends[b]:
                serial.append(starts[b]["t"] >= ends[a]["t"] or starts[a]["t"] >= ends[b]["t"])
            if last_end and tool:
                delays.append(_ms(last_end, tool["t"]))
            invoke.append(_ms(t["t_submit"], first_start))
            for e in starts.values():
                if e:
                    ups_fields.update((e.get("payload") or {}).keys())
            if stop:
                stop_fields.update((stop.get("payload") or {}).keys())
                p = stop.get("payload") or {}
                stop_checks.append({"tx_exists": stop.get("tx_exists"), "tx_last_types": stop.get("tx_last_types"),
                                    "tx_last_assistant_text": stop.get("tx_last_assistant_text"),
                                    "stop_before_result_ms": _ms(stop["t"], t["result"]),
                                    "session_id_matches": p.get("session_id") == t["session_id"]})
            payload = (starts[a] or {}).get("payload") or {}
            per_turn.append({
                "turn": i + 1, "result": t.get("result_subtype"), "permission_requests": t["permission_requests"],
                "submit_to_hook_start_ms": invoke[-1], "hook_end_to_first_tool_ms": delays[-1] if last_end and tool else None,
                "hook_end_to_first_stream_ms": _ms(last_end, first_out), "replayed_user_ms": _ms(t["t_submit"], t["replayed_user"]),
                "prompt_matches": payload.get("prompt") == PROMPT.format(n=i + 1),
                "cwd_matches": Path(str(payload.get("cwd", ""))).resolve() == Path(run["repo"]).resolve(),
                "session_id_matches": payload.get("session_id") == t["session_id"],
                "permission_mode": payload.get("permission_mode"),
            })
    res: dict[str, Any] = {
        "a_hooks_finish_before_first_tool": {"passed": bool(before_tool) and all(before_tool), "runs": before_tool},
        "a_hooks_finish_before_first_output": {"passed": bool(before_output) and all(before_output), "runs": before_output,
                                               "note": "first output = first streamed partial message event"},
        "b_hooks_serial": {"passed": bool(serial) and all(serial), "runs": serial, "note": "recorded only"},
        "b_file_and_inline_hooks_both_run": {"passed": bool(both_sources) and all(both_sources), "runs": both_sources},
        "d_hook_end_to_first_tool_ms": {"values": delays},
        "d_submit_to_hook_start_ms": {"values": invoke},
        "e_ups_input_fields": sorted(ups_fields),
        "e_stop_input_fields": sorted(stop_fields),
        "e_stop_checks": stop_checks,
        "e_stop_sees_final_message": {"passed": bool(stop_checks) and all(
            c["tx_exists"] and (c["tx_last_assistant_text"] or "").strip().lower().startswith("done") for c in stop_checks)},
        "per_turn": per_turn,
    }
    for key in ("d_hook_end_to_first_tool_ms", "d_submit_to_hook_start_ms"):
        v = [x for x in res[key]["values"] if x is not None]
        if v:
            res[key].update({"min": min(v), "median": round(statistics.median(v)), "max": max(v)})

    # detached children spawned by a hook in the ordering runs
    child_ok = []
    for run in order_runs:
        evs = run["events"]
        for e in evs:
            if e["ev"] == "spawned":
                name = e["hook"]
                c_end = _first(evs, "child-" + name, "end")
                beats = [x["t"] for x in evs if x["hook"] == "child-" + name and x["ev"] == "beat"]
                child_ok.append({"hook": name, "child_started": _first(evs, "child-" + name, "start") is not None,
                                 "last_beat_after_cli_exit_ms": _ms(run["t_exit"], max(beats)) if beats else None,
                                 "child_finished": c_end is not None,
                                 "finished_after_cli_exit": bool(c_end and c_end["t"] > run["t_exit"])})
    res["f_detached_child_survives_hook_and_cli_exit"] = {
        "passed": bool(child_ok) and all(c["child_finished"] and c["finished_after_cli_exit"] for c in child_ok),
        "children": child_ok}

    evs = timeout_run["events"]
    t0 = timeout_run["turns"][0]
    started, ended = _first(evs, "slow", "start"), _first(evs, "slow", "end")
    tool = _first(evs, "pretool", "tool")
    c_end = _first(evs, "child-slow", "end")
    res["c_turn_waits_for_hook_within_timeout"] = {
        "passed": bool(started and tool and tool["t"] - started["t"] >= timeout_s - 0.25),
        "hook_killed_or_abandoned": ended is None, "hook_end_after_tool": bool(ended and tool and ended["t"] > tool["t"]),
        "tool_after_hook_start_ms": _ms(started["t"] if started else None, tool["t"] if tool else None),
        "result": t0.get("result_subtype"), "timeout_s": timeout_s,
    }
    beats = [x["t"] for x in evs if x["hook"] == "child-slow" and x["ev"] == "beat"]
    res["f_detached_child_survives_hook_timeout_kill"] = {
        "passed": c_end is not None, "child_started": _first(evs, "child-slow", "start") is not None,
        "last_beat_after_hook_start_ms": _ms(started["t"] if started else None, max(beats) if beats else None),
        "last_beat_after_cli_exit_ms": _ms(timeout_run["t_exit"], max(beats) if beats else None)}
    return res


def observations(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The client observation of every probe hook start (the pretool and stop probes do not observe)."""
    return [e.get("client") or {"problem": "not_recorded"} for run in runs for e in run["events"]
            if e["ev"] == "start" and not e["hook"].startswith("child-")]


def cleanup_transcripts(runs: list[dict[str, Any]]) -> list[str]:
    """Remove exactly the session files these runs created (transcript and sibling directory)."""
    removed = []
    for run in runs:
        for e in run["events"]:
            p = (e.get("payload") or {}) if e["hook"] == "stop" else {}
            tx, sid = p.get("transcript_path"), p.get("session_id")
            if not (isinstance(tx, str) and isinstance(sid, str) and Path(tx).name == f"{sid}.jsonl"):
                continue
            f = Path(tx)
            if f.is_file():
                f.unlink()
                removed.append(str(f))
            d = f.with_suffix("")
            if d.is_dir() and d.name == sid:
                shutil.rmtree(d)
                removed.append(str(d))
            parent = f.parent
            mem = parent / "memory"
            if mem.is_dir() and not any(mem.iterdir()) and [x.name for x in parent.iterdir()] == ["memory"]:
                mem.rmdir()
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                removed.append(str(parent))
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path, help="dedicated directory outside every repository")
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--turns", type=int, default=2)
    ap.add_argument("--permission-mode", default="auto")
    ap.add_argument("--effort", default=None)
    ap.add_argument("--cleanup-transcripts", action="store_true")
    args = ap.parse_args()
    out_dir = args.out.resolve()
    if out_dir == PKG or PKG in out_dir.parents:
        print("refusing: --out is inside the capture package (boundary records are placed by the owner)",
              file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    if subprocess.run(["git", "-C", str(args.out), "rev-parse"], capture_output=True).returncode == 0:
        print("refusing: --out is inside a git repository", file=sys.stderr)
        return 2
    if not args.cli.is_file():
        print("refusing: --cli is not a file", file=sys.stderr)
        return 2
    version = subprocess.run([str(args.cli), "--version"], capture_output=True, text=True, timeout=60).stdout.split()[0]
    order_runs = [run_session(args.cli, args.out, args.model, args.turns,
                              hooks_file=[("A", 1.0, 30, True)], hooks_inline=[("B", 1.0, 30, False)],
                              permission_mode=args.permission_mode, effort=args.effort, child_s=30, wait_after_s=5)
                  for _ in range(args.runs)]
    timeout_s = 3
    timeout_run = run_session(args.cli, args.out, args.model, 1, hooks_file=[("slow", 8.0, timeout_s, True)],
                              hooks_inline=[], permission_mode=args.permission_mode, effort=args.effort,
                              child_s=15, wait_after_s=5)
    res = analyse(order_runs, timeout_run, timeout_s, ["A", "B"])
    obs = observations(order_runs + [timeout_run])
    identity = ci.tested_identity(args.cli.resolve(), version, obs)
    res["g_client_identity_observed"] = {
        "passed": identity["problem"] is None, "problem": identity["problem"], "observations": len(obs),
        "modes": sorted({str(o.get("mode")) for o in obs}), "problems": sorted({str(o.get("problem")) for o in obs})}
    removed = cleanup_transcripts(order_runs + [timeout_run]) if args.cleanup_transcripts else []
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec = {"cli_version": version, "recorded_at": now,
           "mode": "desktop-style SDK streaming (stream-json in/out, await-initialize, stdio permission prompts)",
           "model": args.model, "permission_mode": args.permission_mode, "effort": args.effort,
           "scrubbed_env_names": scrubbed_names(), "identity": identity,
           "results": res, "transcripts_removed": removed,
           "raw": {"order_runs": order_runs, "timeout_run": timeout_run}}
    out = args.out / f"desktop-boundary-{version}.json"
    out.write_text(json.dumps(rec, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    candidate = {"cli_version": version, "recorded_at": now, "harness": "desktop_boundary_test",
                 "mode_description": rec["mode"], "model": args.model, "identity": identity, "accepted": None,
                 "relied_upon": list(RELIED_UPON), "results": {k: res[k] for k in RELIED_UPON},
                 "full_result": out.name}
    cand = args.out / ci.record_name(version, identity["mode"] or "unidentified")
    cand.write_text(json.dumps(candidate, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: v.get("passed") for k, v in res.items() if isinstance(v, dict) and "passed" in v}))
    print(json.dumps({k: identity[k] for k in ("cli_version", "mode", "problem", "observations")}))
    print(out)
    print(cand)
    return 0 if all(res[k]["passed"] for k in RELIED_UPON) else 1


if __name__ == "__main__":
    sys.exit(main())

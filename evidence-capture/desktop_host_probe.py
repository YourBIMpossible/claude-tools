#!/usr/bin/env python3
"""Start-boundary probe inside the desktop client itself (mode ``claude-desktop``).

``desktop_boundary_test.py`` drives the CLI headless and so tests the ``sdk-cli`` mode.
The desktop client launches the same executable with its own settings and initialize
request, which a harness cannot reproduce. This tool measures that mode where it runs:
a disposable repository carries project-local probe hooks, and a desktop session opened
in it answers a few synthetic prompts.

    python desktop_host_probe.py setup --out DIR
    python desktop_host_probe.py arm --out DIR --a-sleep 6     # next prompt: timeout turn
    python desktop_host_probe.py disarm --out DIR
    python desktop_host_probe.py analyse --out DIR --cli EXE

``setup`` builds, under ``DIR`` (outside every repository and scratchpad):

- ``repo/``: a git repository whose ``.claude/settings.json`` registers, for this project
  only, two ``UserPromptSubmit`` probes (A, whose sleep ``arm`` sets, and B), the real
  capture start hook, a ``PreToolUse`` probe, a ``Stop`` probe and the real capture end
  hook. Its ``env`` sets ``EVIDENCE_HOOK=0``, so the Evidence Compiler hook stays silent;
  the capture wrappers remove it and point capture at the synthetic stores.
- ``stores/``: synthetic capture stores; ``boundary/``: an empty record directory, so
  every episode fails closed (``mode_untested``) and the boundary verdict comes from here.
- ``probe/``: the hook scripts; ``events/``: one event file per hook process.

Probes record times, hook names, the client observation of ``client_identity.observe``
(executable path hash, mode, host version), the names (never values) of ``CLAUDE*``
variables, and a SHA-256 of the prompt. They record no prompt text, command line or
argument value. The ``Stop`` probe reads the transcript to time when the turn's final
assistant message appears.

``analyse`` writes ``host-probe-result.json`` and a candidate record
``<version>--<mode>.json`` with ``accepted: null`` to ``DIR``. Accepting it is the
owner's decision. Relied-upon properties, as in the harness:

- (a) every ``UserPromptSubmit`` hook ended before the turn's first tool call, and before
  its first assistant transcript entry (the desktop stream is not observable here, so the
  transcript timestamp stands in for first output);
- (c) in the armed turn, the turn waited for probe A until its timeout;
- (g) every probe observed one mode under the ``--cli`` executable.

Also recorded: latencies, whether the ``Stop`` transcript held the final message, and
each synthetic episode's join, observation causes and boundary reason.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))
import client_identity as ci  # noqa: E402
from dryrun import DryError, build_stores, check_out_dir  # noqa: E402
from store import Stores, scan_manifests  # noqa: E402

RELIED_UPON = ("a_hooks_finish_before_first_tool", "a_hooks_finish_before_first_output",
               "c_turn_waits_for_hook_within_timeout", "g_client_identity_observed")
UPS_NAMES = ("A", "B", "capstart")
A_TIMEOUT_S = 3
A_DEFAULT_SLEEP_S = 0.3
TURN_MARGIN_S = 2.0
STOP_POLL_S = 3.0
FIXTURE_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

_MARK = r'''
def mark(hook, ev, **kw):
    with (events / f"{hook}-{os.getpid()}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"hook": hook, "ev": ev, "t": time.time(), "pid": os.getpid(), **kw}) + "\n")
'''
UPS_SRC = r'''
import hashlib, json, os, sys, time
from pathlib import Path
t0 = time.time()
events, name, pkg, control = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
''' + _MARK + r'''
raw = sys.stdin.buffer.read()
try:
    payload = json.loads(raw.decode("utf-8-sig"))
except ValueError:
    payload = {}
prompt = payload.get("prompt")
try:
    sys.path.insert(0, pkg)
    import client_identity as ci
    client = ci.persisted(ci.observe(dict(os.environ)))
except Exception as e:
    client = {"problem": "probe_error:" + type(e).__name__}
sleep_s = 0.0
if name == "A":
    try:
        sleep_s = float(json.loads(control.read_text(encoding="utf-8"))["a_sleep"])
    except (OSError, ValueError, KeyError):
        sleep_s = 0.3
mark(name, "start", t_proc=t0, sleep_s=sleep_s, client=client,
     prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest() if isinstance(prompt, str) else None,
     session_id=payload.get("session_id"), hook_event_name=payload.get("hook_event_name"),
     payload_keys=sorted(payload), transcript_exists=Path(str(payload.get("transcript_path") or "")).is_file(),
     claude_env_names=sorted(k for k in os.environ if k.upper().startswith("CLAUDE")),
     evidence_hook=os.environ.get("EVIDENCE_HOOK"))
time.sleep(sleep_s)
mark(name, "end")
'''
TOOL_SRC = r'''
import json, os, sys, time
from pathlib import Path
events = Path(sys.argv[1])
''' + _MARK + r'''
payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
mark("pretool", "tool", tool=payload.get("tool_name"), session_id=payload.get("session_id"))
'''
STOP_SRC = r'''
import json, os, sys, time
from datetime import datetime
from pathlib import Path
t0 = time.time()
events, poll_s = Path(sys.argv[1]), float(sys.argv[2])
''' + _MARK + r'''
payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
tx = Path(str(payload.get("transcript_path") or ""))

def epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

def is_prompt(e):
    # A relayed (cross-session) message is stored with isMeta; it still starts a turn here.
    if e.get("type") != "user" or e.get("isSidechain"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return True
    return isinstance(c, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)

def state():
    if not tx.is_file():
        return {"tx_exists": False, "complete": False}
    entries = []
    for line in tx.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    idx = max((i for i, e in enumerate(entries) if is_prompt(e)), default=None)
    after = entries[idx + 1:] if idx is not None else []
    asst = [e for e in after if e.get("type") == "assistant" and not e.get("isSidechain")]
    kinds = [b.get("type") for b in ((asst[-1].get("message") or {}).get("content") or [])
             if isinstance(b, dict)] if asst else []
    return {"tx_exists": True, "complete": bool(asst) and "text" in kinds and "tool_use" not in kinds,
            "assistant_entries": len(asst), "last_kinds": kinds, "tail_types": [e.get("type") for e in entries[-3:]],
            "prompt_is_meta": bool(entries[idx].get("isMeta")) if idx is not None else None,
            "prompt_ts": epoch(entries[idx].get("timestamp")) if idx is not None else None,
            "first_assistant_ts": epoch(asst[0].get("timestamp")) if asst else None,
            "versions": sorted({str(e["version"]) for e in after + entries[idx:idx + 1] if e.get("version")})
            if idx is not None else []}

first = state()
last, t_complete = first, (t0 if first["complete"] else None)
while t_complete is None and time.time() - t0 < poll_s:
    time.sleep(0.1)
    last = state()
    if last["complete"]:
        t_complete = time.time()
mark("stop", "stop", t_proc=t0, session_id=payload.get("session_id"), payload_keys=sorted(payload),
     stop_hook_active=payload.get("stop_hook_active"), at_stop=first, final=last,
     complete_lag_ms=round((t_complete - t0) * 1000) if t_complete else None)
'''
CAPWRAP_SRC = r'''
import json, os, subprocess, sys, time
from pathlib import Path
events, which, pkg, meta, content, bdir = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
''' + _MARK + r'''
raw = sys.stdin.buffer.read()
hook = "cap" + which
mark(hook, "start")
env = dict(os.environ)
for k in ("EVIDENCE_HOOK", "EVIDENCE_CAPTURE"):
    env.pop(k, None)
env.update({"EC_CAPTURE_META": meta, "EC_CAPTURE_CONTENT": content, "EC_CAPTURE_BOUNDARY_DIRS": bdir,
            "EC_CAPTURE_MAINTAIN": "0"})
try:
    r = subprocess.run([sys.executable, str(Path(pkg) / f"capture_{which}.py")], input=raw, env=env,
                       capture_output=True, timeout=50)
    mark(hook, "end", rc=r.returncode, stdout_bytes=len(r.stdout), stderr_bytes=len(r.stderr))
except subprocess.TimeoutExpired:
    mark(hook, "end", rc=None, timed_out=True)
'''


def _cmd(*args: str) -> str:
    return " ".join(f'"{a}"' for a in args).replace("\\", "/")


def settings(out: Path, py: str) -> dict[str, Any]:
    probe, ev, st = out / "probe", str(out / "events"), stores_of(out)
    control = str(out / "control.json")

    def hook(timeout: int, *args: str) -> dict[str, Any]:
        return {"type": "command", "timeout": timeout, "command": _cmd(py, *args)}

    cap = (str(PKG), str(st.meta), str(st.content), str(out / "boundary"))
    return {
        "env": {"EVIDENCE_HOOK": "0"},
        "hooks": {
            "UserPromptSubmit": [{"hooks": [
                hook(A_TIMEOUT_S, str(probe / "ups.py"), ev, "A", str(PKG), control),
                hook(10, str(probe / "ups.py"), ev, "B", str(PKG), control),
                hook(15, str(probe / "capwrap.py"), ev, "start", *cap)]}],
            "PreToolUse": [{"matcher": "*", "hooks": [hook(5, str(probe / "tool.py"), ev)]}],
            "Stop": [{"hooks": [hook(10, str(probe / "stop.py"), ev, str(STOP_POLL_S)),
                                hook(60, str(probe / "capwrap.py"), ev, "end", *cap)]}],
        },
    }


def stores_of(out: Path) -> Stores:
    return Stores(out / "stores" / "capture-meta", out / "stores" / "capture-content")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env={**os.environ, **FIXTURE_ENV})


def setup(out: Path) -> Path:
    out = check_out_dir(out)
    if (out / "repo").exists():
        raise DryError("already set up; use a new directory")
    probe = out / "probe"
    probe.mkdir()
    for name, src in (("ups", UPS_SRC), ("tool", TOOL_SRC), ("stop", STOP_SRC), ("capwrap", CAPWRAP_SRC)):
        (probe / f"{name}.py").write_text(src, encoding="utf-8")
    (out / "events").mkdir()
    (out / "boundary").mkdir()
    build_stores(out / "stores")
    arm(out, A_DEFAULT_SLEEP_S)
    repo = out / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "README.md").write_text("# desktop host probe (synthetic)\n", encoding="utf-8")
    (repo / ".evidence-compiler").mkdir()
    (repo / ".evidence-compiler" / "config.yaml").write_text("capture:\n  enabled: true\n", encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps(settings(out, sys.executable), indent=1) + "\n",
                                                    encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "probe fixture")
    return repo


def arm(out: Path, a_sleep: float) -> None:
    (out / "control.json").write_text(json.dumps({"a_sleep": a_sleep}) + "\n", encoding="utf-8")


def read_events(out: Path) -> list[dict[str, Any]]:
    evs: list[dict[str, Any]] = []
    for f in sorted((out / "events").glob("*.jsonl")):
        evs += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
    return sorted(evs, key=lambda e: e["t"])


def _ms(a: float | None, b: float | None) -> int | None:
    return round((b - a) * 1000) if a is not None and b is not None else None


def _first(evs: list[dict[str, Any]], hook: str, ev: str) -> dict[str, Any] | None:
    return next((e for e in evs if e["hook"] == hook and e["ev"] == ev), None)


def split_turns(evs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """One window per prompt: from shortly before probe A's start to shortly before the next."""
    starts = [e["t"] for e in evs if e["hook"] == "A" and e["ev"] == "start"]
    bounds = [t - TURN_MARGIN_S for t in starts] + [float("inf")]
    return [[e for e in evs if bounds[i] <= e["t"] < bounds[i + 1]] for i in range(len(starts))]


def _stats(values: list[int | None]) -> dict[str, Any]:
    v = [x for x in values if x is not None]
    return {"values": v, **({"min": min(v), "median": round(statistics.median(v)), "max": max(v)} if v else {})}


def analyse_turns(turns: list[list[dict[str, Any]]]) -> dict[str, Any]:
    per_turn, before_tool, before_output, timeout_turns = [], [], [], []
    for i, evs in enumerate(turns):
        starts = {n: _first(evs, n, "start") for n in UPS_NAMES}
        ends = {n: _first(evs, n, "end") for n in UPS_NAMES}
        a = starts["A"] or {}
        armed = float(a.get("sleep_s") or 0) > A_TIMEOUT_S
        tool = _first(evs, "pretool", "tool")
        stop = _first(evs, "stop", "stop")
        at_stop = (stop or {}).get("at_stop") or {}
        final = (stop or {}).get("final") or {}
        first_asst = final.get("first_assistant_ts") or at_stop.get("first_assistant_ts")
        prompt_ts = final.get("prompt_ts") or at_stop.get("prompt_ts")
        first_start = min((e["t"] for e in starts.values() if e), default=None)
        all_ended = all(ends.values())
        last_end = max(e["t"] for e in ends.values() if e) if all_ended else None
        row = {
            "turn": i + 1, "armed_timeout": armed, "ups_started": sorted(n for n, e in starts.items() if e),
            "ups_ended": sorted(n for n, e in ends.items() if e), "first_tool": (tool or {}).get("tool"),
            "prompt_to_hook_start_ms": _ms(prompt_ts, first_start),
            "ups_span_ms": _ms(first_start, last_end),
            "capstart_ms": _ms((starts["capstart"] or {}).get("t"), (ends["capstart"] or {}).get("t")),
            "capstart_rc": (ends["capstart"] or {}).get("rc"),
            "hook_end_to_first_tool_ms": _ms(last_end, tool["t"]) if tool else None,
            "hook_end_to_first_assistant_ms": _ms(last_end, first_asst),
            "stop_seen": stop is not None, "stop_complete_at_stop": at_stop.get("complete"),
            "prompt_is_meta": at_stop.get("prompt_is_meta"),
            "stop_complete_lag_ms": (stop or {}).get("complete_lag_ms"),
            "stop_last_kinds": at_stop.get("last_kinds"), "stop_tail_types": at_stop.get("tail_types"),
            "evidence_hook_env": a.get("evidence_hook"), "prompt_sha256": a.get("prompt_sha256"),
            "session_id_consistent": len({e.get("session_id") for e in evs if e.get("session_id")}) <= 1,
        }
        capend = _first(evs, "capend", "end")
        row["capend_ms"] = _ms((_first(evs, "capend", "start") or {}).get("t"), (capend or {}).get("t"))
        row["capend_rc"] = (capend or {}).get("rc")
        if armed:
            first_after = min((x for x in ((tool or {}).get("t"), first_asst) if x is not None), default=None)
            waited = bool(a.get("t") and first_after and first_after - a["t"] >= A_TIMEOUT_S - 0.25)
            timeout_turns.append({"turn": i + 1, "passed": waited and ends["A"] is None and stop is not None,
                                  "a_killed": ends["A"] is None, "first_activity_after_a_start_ms":
                                  _ms(a.get("t"), first_after), "turn_completed": stop is not None})
        else:
            if all_ended and tool:
                before_tool.append(last_end < tool["t"])
            if all_ended and first_asst:
                before_output.append(last_end < first_asst)
        per_turn.append(row)
    stop_rows = [r for r in per_turn if r["stop_seen"]]
    return {
        "turns": len(turns),
        "a_hooks_finish_before_first_tool": {"passed": bool(before_tool) and all(before_tool), "runs": before_tool},
        "a_hooks_finish_before_first_output": {
            "passed": bool(before_output) and all(before_output), "runs": before_output,
            "note": "first output = first assistant transcript entry of the turn (the desktop stream is not observable)"},
        "c_turn_waits_for_hook_within_timeout": {
            "passed": bool(timeout_turns) and all(t["passed"] for t in timeout_turns), "turns": timeout_turns,
            "timeout_s": A_TIMEOUT_S},
        "d_latency_ms": {k: _stats([r[k] for r in per_turn if not r["armed_timeout"]])
                         for k in ("prompt_to_hook_start_ms", "ups_span_ms", "capstart_ms", "hook_end_to_first_tool_ms",
                                   "hook_end_to_first_assistant_ms", "capend_ms")},
        "e_stop_sees_final_message": {
            "passed": bool(stop_rows) and all(r["stop_complete_at_stop"] for r in stop_rows),
            "at_stop": [r["stop_complete_at_stop"] for r in stop_rows],
            "lag_ms": [r["stop_complete_lag_ms"] for r in stop_rows], "note": "recorded; capture reconciles below"},
        "per_turn": per_turn,
    }


def episodes(stores: Stores) -> list[dict[str, Any]]:
    scan = scan_manifests(stores)
    out: list[dict[str, Any]] = [{"episode_id": None, "prompt_sha256": None, "manifest": name,
                                  "status": "unreadable"}
                                 for name in scan.unreadable]
    for m in scan.manifests:
        cap = m.get("capture") or {}
        out.append({"episode_id": m.get("episode_id"), "prompt_sha256": (m.get("prompt") or {}).get("sha256"),
                    "status": cap.get("status"), "errors": cap.get("errors"), "join": cap.get("join"),
                    "start_ms": cap.get("start_ms"), "end_ms": cap.get("end_ms"), "ended_by": m.get("ended_by"),
                    "snapshot": (m.get("start_snapshot") or {}).get("state"),
                    "snapshot_reason": (m.get("start_snapshot") or {}).get("reason"),
                    "observation": m.get("observation"), "boundary": m.get("boundary"),
                    "client": {k: (m.get("client") or {}).get(k) for k in ("mode", "problem", "host_version")}})
    return out


def analyse(out: Path, cli: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    evs = read_events(out)
    res = analyse_turns(split_turns(evs))
    obs = [e.get("client") or {"problem": "not_recorded"} for e in evs if e["hook"] in ("A", "B") and e["ev"] == "start"]
    versions = sorted({v for e in evs if e["hook"] == "stop" for v in ((e.get("final") or {}).get("versions") or [])})
    version = versions[0] if len(versions) == 1 else None
    identity = ci.tested_identity(cli.resolve(), version or "unknown", obs)
    if version is None and identity["problem"] is None:
        identity["problem"] = "version_not_single:" + ",".join(versions)
        identity["mode"] = None
    res["g_client_identity_observed"] = {
        "passed": identity["problem"] is None, "problem": identity["problem"], "observations": len(obs),
        "modes": sorted({str(o.get("mode")) for o in obs}), "problems": sorted({str(o.get("problem")) for o in obs}),
        "versions": versions}
    eps = episodes(stores_of(out))
    by_prompt = {r["prompt_sha256"]: r["turn"] for r in res["per_turn"] if r["prompt_sha256"]}
    for ep in eps:
        ep["turn"] = by_prompt.get(ep["prompt_sha256"])
    res["episodes"] = eps
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    full = {"harness": "desktop_host_probe", "recorded_at": now, "identity": identity, "results": res}
    candidate = {"cli_version": version, "recorded_at": now, "harness": "desktop_host_probe",
                 "mode_description": "desktop client session with project-local probe hooks",
                 "identity": identity, "accepted": None, "relied_upon": list(RELIED_UPON),
                 "results": {k: res[k] for k in RELIED_UPON}, "full_result": "host-probe-result.json"}
    return full, candidate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("setup", "arm", "disarm", "analyse"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=Path, required=True)
        if name == "arm":
            p.add_argument("--a-sleep", type=float, required=True)
        if name == "analyse":
            p.add_argument("--cli", type=Path, required=True)
    args = ap.parse_args(argv)
    out = args.out.resolve()
    if args.cmd == "setup":
        try:
            print(setup(out))
        except DryError as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 2
        return 0
    if not (out / "repo" / ".claude" / "settings.json").is_file():
        print("refusing: --out is not a host-probe directory", file=sys.stderr)
        return 2
    if args.cmd in ("arm", "disarm"):
        arm(out, args.a_sleep if args.cmd == "arm" else A_DEFAULT_SLEEP_S)
        return 0
    if not args.cli.is_file():
        print("refusing: --cli is not a file", file=sys.stderr)
        return 2
    full, candidate = analyse(out, args.cli)
    (out / "host-probe-result.json").write_text(json.dumps(full, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    cand = out / ci.record_name(candidate["cli_version"] or "unknown", full["identity"]["mode"] or "unidentified")
    cand.write_text(json.dumps(candidate, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    r = full["results"]
    print(json.dumps({k: r[k]["passed"] for k in (*RELIED_UPON, "e_stop_sees_final_message")}))
    print(json.dumps({k: full["identity"][k] for k in ("cli_version", "mode", "problem", "observations")}))
    print(cand)
    return 0 if all(r[k]["passed"] for k in RELIED_UPON) else 1


if __name__ == "__main__":
    sys.exit(main())

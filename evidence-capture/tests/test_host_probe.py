#!/usr/bin/env python3
"""The desktop host probe (``desktop_host_probe``): layout, probe scripts, analysis.

The probe scripts run here as plain subprocesses with synthetic hook input, under this
runner's own interpreter as the client (``CLAUDE_PID``). Everything lives in temporary
directories; the real home, stores and boundary records are never touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
import client_identity as ci  # noqa: E402
import desktop_host_probe as hp  # noqa: E402
import dryrun  # noqa: E402
from common import iso, now_utc  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def _hook_env(tdir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "EVIDENCE_", "EC_CAPTURE"))}
    image = ci.pid_image(os.getpid())
    assert image, "cannot resolve this process's image"
    env.update({ci.ENV_PID: str(os.getpid()), ci.ENV_EXECPATH: image, ci.ENV_MODE: dryrun.SYNTHETIC_MODE,
                "EVIDENCE_HOOK": "0", "EC_CAPTURE_TRANSCRIPTS": str(tdir)})
    return env


def _run_hooks(out: Path, event: str, payload: dict[str, object], env: dict[str, str]) -> None:
    """Run every hook the probe settings register for ``event``, in order, like the client."""
    settings = json.loads((out / "repo" / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for group in settings["hooks"][event]:
        for h in group["hooks"]:
            # the command line is the probe's own, as the client runs it: through a shell
            r = subprocess.run(h["command"], shell=True, input=json.dumps(payload).encode(), env=env,
                               capture_output=True, timeout=60)
            assert r.returncode == 0 and not r.stdout, (h["command"], r.returncode, r.stdout, r.stderr[-400:])


def test_setup_layout(case: Path) -> None:
    out = case / "probe-out"
    repo = hp.setup(out)
    settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"] == {"EVIDENCE_HOOK": "0"}
    assert [h["timeout"] for h in settings["hooks"]["UserPromptSubmit"][0]["hooks"]] == [hp.A_TIMEOUT_S, 10, 15]
    assert set(settings["hooks"]) == {"UserPromptSubmit", "PreToolUse", "Stop"}
    assert not list((out / "boundary").iterdir())
    st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    assert st.returncode == 0 and not st.stdout.strip(), st.stdout
    try:
        hp.setup(out)
        raise AssertionError("a second setup must refuse")
    except dryrun.DryError:
        pass
    try:
        hp.setup(repo / "nested")
        raise AssertionError("setup inside a repository must refuse")
    except dryrun.DryError:
        pass


def test_arm_disarm(case: Path) -> None:
    out = case / "probe-out"
    hp.setup(out)
    assert hp.main(["arm", "--out", str(out), "--a-sleep", "6"]) == 0
    assert json.loads((out / "control.json").read_text(encoding="utf-8")) == {"a_sleep": 6.0}
    assert hp.main(["disarm", "--out", str(out)]) == 0
    assert json.loads((out / "control.json").read_text(encoding="utf-8")) == {"a_sleep": hp.A_DEFAULT_SLEEP_S}
    assert hp.main(["arm", "--out", str(case / "not-a-probe"), "--a-sleep", "1"]) == 2


def test_probe_hooks_end_to_end(case: Path) -> None:
    """One synthetic turn through every registered hook: identity, capture, Stop view."""
    out = case / "probe-out"
    repo = hp.setup(out)
    tdir = case / "transcripts"
    env = _hook_env(tdir)
    sid = str(uuid.uuid4())
    tp = dryrun.write_transcript(tdir, sid, dryrun.transcript_entries(sid, repo, dryrun.PROMPT, None,
                                                                       iso(now_utc()), edit=False))
    base = {"session_id": sid, "transcript_path": str(tp), "cwd": str(repo)}
    _run_hooks(out, "UserPromptSubmit", {**base, "hook_event_name": "UserPromptSubmit", "prompt": dryrun.PROMPT}, env)
    _run_hooks(out, "PreToolUse", {**base, "hook_event_name": "PreToolUse", "tool_name": "Read"}, env)
    _run_hooks(out, "Stop", {**base, "hook_event_name": "Stop", "stop_hook_active": False}, env)

    full, cand = hp.analyse(out, Path(ci.pid_image(os.getpid()) or ""))
    r = full["results"]
    assert r["turns"] == 1, r["turns"]
    row = r["per_turn"][0]
    assert row["ups_started"] == ["A", "B", "capstart"] and row["ups_ended"] == ["A", "B", "capstart"], row
    assert row["capstart_rc"] == 0 and row["capend_rc"] == 0, row
    assert row["evidence_hook_env"] == "0" and row["first_tool"] == "Read", row
    assert row["stop_complete_at_stop"] is True and row["stop_complete_lag_ms"] == 0, row
    assert r["a_hooks_finish_before_first_tool"]["passed"], r["a_hooks_finish_before_first_tool"]
    assert not r["c_turn_waits_for_hook_within_timeout"]["passed"]  # no armed turn yet
    assert r["g_client_identity_observed"]["passed"], r["g_client_identity_observed"]
    assert full["identity"]["mode"] == dryrun.SYNTHETIC_MODE and cand["cli_version"] == dryrun.CLI_VERSION
    assert cand["accepted"] is None and cand["relied_upon"] == list(hp.RELIED_UPON)
    (ep,) = r["episodes"]
    assert ep["turn"] == 1 and ep["join"]["state"] == "joined" and ep["join"]["by"] == "end", ep
    assert ep["boundary"]["trusted"] is False and ep["boundary"]["reason"] == "boundary_untested", ep["boundary"]
    assert "no_response" not in ep["observation"]["causes"], ep["observation"]
    blob = b"".join(f.read_bytes() for f in (out / "events").iterdir())
    assert dryrun.PROMPT.encode() not in blob, "probe events must not hold prompt text"


def _ev(hook: str, ev: str, t: float, **kw: object) -> dict[str, object]:
    return {"hook": hook, "ev": ev, "t": t, **kw}


def _turn(t: float, *, a_sleep: float = 0.3, a_end: bool = True, tool_at: float | None = 1.0,
          asst_at: float | None = 0.9, complete: bool = True) -> list[dict[str, object]]:
    evs = [_ev("A", "start", t, sleep_s=a_sleep, prompt_sha256=f"p{t}", session_id="s"),
           _ev("B", "start", t + 0.01, session_id="s"), _ev("capstart", "start", t + 0.02),
           _ev("B", "end", t + 0.3), _ev("capstart", "end", t + 0.4, rc=0)]
    if a_end:
        evs.append(_ev("A", "end", t + a_sleep))
    if tool_at is not None:
        evs.append(_ev("pretool", "tool", t + tool_at, tool="Bash", session_id="s"))
    view = {"complete": complete, "prompt_ts": t - 0.05, "first_assistant_ts": t + asst_at if asst_at else None}
    evs.append(_ev("stop", "stop", t + 8, session_id="s", at_stop=view, final={**view, "versions": ["9.9.9"]},
                   complete_lag_ms=0 if complete else None))
    return evs


def test_analyse_turns(case: Path) -> None:
    del case
    evs = _turn(100.0) + _turn(200.0, tool_at=None) + _turn(300.0, a_sleep=6, a_end=False, tool_at=3.2, asst_at=3.1)
    r = hp.analyse_turns(hp.split_turns(sorted(evs, key=lambda e: e["t"])))
    assert r["turns"] == 3
    assert r["a_hooks_finish_before_first_tool"] == {"passed": True, "runs": [True]}, r
    assert r["a_hooks_finish_before_first_output"]["runs"] == [True, True], r
    assert r["c_turn_waits_for_hook_within_timeout"]["passed"], r["c_turn_waits_for_hook_within_timeout"]
    assert r["e_stop_sees_final_message"]["passed"]
    assert r["d_latency_ms"]["prompt_to_hook_start_ms"]["values"] == [50, 50]

    early = _turn(100.0, tool_at=0.35)  # a tool call while the capture start hook (ends at +0.4) is running
    r = hp.analyse_turns(hp.split_turns(sorted(early, key=lambda e: e["t"])))
    assert r["a_hooks_finish_before_first_tool"] == {"passed": False, "runs": [False]}, r

    short = _turn(300.0, a_sleep=6, a_end=False, tool_at=1.0, asst_at=0.9)  # the turn did not wait
    r = hp.analyse_turns(hp.split_turns(sorted(short, key=lambda e: e["t"])))
    assert not r["c_turn_waits_for_hook_within_timeout"]["passed"]

    stale = _turn(100.0, complete=False)
    r = hp.analyse_turns(hp.split_turns(sorted(stale, key=lambda e: e["t"])))
    assert not r["e_stop_sees_final_message"]["passed"] and r["e_stop_sees_final_message"]["at_stop"] == [False]


TESTS = [test_setup_layout, test_arm_disarm, test_probe_hooks_end_to_end, test_analyse_turns]


def main() -> int:
    only = set(sys.argv[1:])
    with tempfile.TemporaryDirectory(prefix="ec-host-probe-test-") as tmp:
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

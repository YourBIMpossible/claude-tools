#!/usr/bin/env python3
"""Client identity: executable hash, CLI version and invocation mode (``client_identity``).

The synthetic client is this runner's own interpreter: the hook environment names this
process in ``CLAUDE_PID``, and the boundary records are written for its executable. The
fixtures live in temporary directories; the real home and stores are never touched.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
import client_identity as ci  # noqa: E402
import dryrun  # noqa: E402
import manifest as mf  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
from common import iso, now_utc, read_json, write_json_atomic  # noqa: E402
from test_hooks import PROMPT, Fixture, Skip, load_inconclusive  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []
V = dryrun.CLI_VERSION
MODE = dryrun.SYNTHETIC_MODE


def _self_image() -> str:
    image = ci.pid_image(os.getpid())
    assert image, "cannot resolve this process's image"
    return image


def _live_env(**extra: str) -> dict[str, str]:
    return {ci.ENV_PID: str(os.getpid()), ci.ENV_MODE: MODE, **extra}


def _client() -> dict:
    obs = ci.observe(_live_env())
    assert obs["problem"] is None, obs
    got = ci.resolve(ci.persisted(obs), _live_env(), None)
    assert got.get("sha256"), got
    return got


def _record(d: Path, *, sha: str, mode: str = MODE, accepted: bool = True, version: str = V,
            passed: bool = True) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    rec = {"cli_version": version, "recorded_at": iso(now_utc()),
           "identity": {"sha256": sha, "cli_version": version, "mode": mode, "problem": None},
           "accepted": {"at": iso(now_utc()), "by": "test"} if accepted else None,
           "relied_upon": ["p"], "results": {"p": {"passed": passed}}}
    p = d / ci.record_name(version, mode)
    write_json_atomic(p, rec)
    return p


# --------------------------------------------------------------------------- tests

def test_end_to_end_trusted_and_no_path(root: Path) -> None:
    fx = Fixture(root)
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    fx.end(sid, fx.transcript(sid, PROMPT, pid, ts))
    m = fx.episode(sid)
    ident = read_json(fx.boundary / ci.record_name(V, MODE))["identity"]
    assert m["client"]["sha256"] == ident["sha256"] and m["client"]["mode"] == MODE, m["client"]
    if (why := load_inconclusive(m)) is not None:
        raise Skip(f"inconclusive under load, {why}")
    assert m["boundary"]["trusted"] is True, m["boundary"]
    assert m["boundary"]["client"]["sha256"] == ident["sha256"]
    text = store.manifest_path(fx.stores, m["episode_id"]).read_text(encoding="utf-8")
    image = _self_image()
    for needle in (image, Path(image).parent.as_posix(), json.dumps(image)[1:-1]):
        assert needle.lower() not in text.lower(), "the manifest names the client's path"
    assert "_path" not in m["client"]
    cache = read_json(fx.stores.meta / ci.CACHE_NAME)
    assert list(cache["entries"].values()) == [ident["sha256"]]
    assert ci.CACHE_NAME in (fx.stores.meta / ".gitignore").read_text(encoding="utf-8")


def test_unidentified_client_fails_closed_without_failing_capture(root: Path) -> None:
    fx = Fixture(root)
    cases = {"pid_missing": {k: v for k, v in fx.env.items() if k != ci.ENV_PID},
             "execpath_mismatch": {**fx.env, ci.ENV_EXECPATH: str(fx.root / "other.exe")},
             "mode_malformed": {**fx.env, ci.ENV_MODE: "bad mode!"}}
    for problem, env in cases.items():
        sid = str(uuid.uuid4())
        pid, ts = fx.start(sid, env=env)
        fx.end(sid, fx.transcript(sid, PROMPT, pid, ts), env=env)
        m = fx.episode(sid)
        assert m["client"]["problem"] == problem, (problem, m["client"])
        if (why := load_inconclusive(m)) is not None:  # the over-budget branch: next test, by construction
            raise Skip(f"inconclusive under load ({problem}), {why}")
        assert m["capture"]["status"] == "complete", (problem, m["capture"])
        b = m["boundary"]
        assert not b["trusted"] and b["reason"] == f"identity_unverified:{problem}", (problem, b)
        assert report._boundary_bucket(b["reason"]) == "cli_untested"
        assert not m["funnel"]["start_snapshot_complete"]


class _TrippableClock:
    """``time`` as pipeline sees it: ``perf_counter`` reads 0 until tripped, then a value
    past the start budget; everything else is the real module."""

    def __init__(self) -> None:
        self.tripped = False

    def perf_counter(self) -> float:
        return (pipeline.START_BUDGET_MS + 500) / 1000 if self.tripped else 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def test_over_budget_start_keeps_identity_verdict(root: Path) -> None:
    """N19, by construction: a start that runs over ``START_BUDGET_MS`` (the clock is
    tripped after the snapshot, so the snapshot itself keeps its budget) records
    ``start_over_budget`` and ends ``partial``, never ``failed``; the client problem and
    the untrusted boundary are unchanged by it, and the review is held on X7 (which is
    what an over-budget run under load looks like to the wall-clock tests)."""
    try:
        import yaml  # noqa: F401
    except ImportError as exc:  # the fixture repo carries config.yaml: every capture would add config:yaml_unavailable
        raise Skip("PyYAML missing (pip install pyyaml): the capture errors could not be pinned to the budget") from exc
    fx = Fixture(root)
    env = {k: v for k, v in fx.env.items() if k != ci.ENV_PID}
    sid = str(uuid.uuid4())
    ts = iso(now_utc())
    clock = _TrippableClock()
    real_time, real_snapshot = pipeline.time, pipeline.take_snapshot

    def snapshot_then_trip(*args: Any, **kw: Any) -> Any:
        out = real_snapshot(*args, **kw)
        clock.tripped = True
        return out
    pipeline.time, pipeline.take_snapshot = clock, snapshot_then_trip  # type: ignore[assignment]
    try:
        res = pipeline.run_start({"session_id": sid, "prompt": PROMPT, "cwd": str(fx.repo)}, env, now=ts)
    finally:
        pipeline.time, pipeline.take_snapshot = real_time, real_snapshot  # type: ignore[assignment]
    assert res["action"] == "captured" and res["start_ms"] > pipeline.START_BUDGET_MS, res
    m = fx.episode(sid)
    assert m["start_snapshot"]["state"] == "complete", m["start_snapshot"]
    assert m["capture"]["errors"] == ["start_over_budget"], m["capture"]
    tp = fx.transcript(sid, PROMPT, None, ts)
    res = pipeline.run_end({"session_id": sid, "transcript_path": str(tp), "cwd": str(fx.repo)}, env)
    assert res["action"] == "ended" and res["status"] == "partial", res
    m = fx.episode(sid)
    assert m["capture"]["status"] == "partial" and m["capture"]["errors"] == ["start_over_budget"], m["capture"]
    assert m["client"]["problem"] == "pid_missing", m["client"]
    b = m["boundary"]
    assert not b["trusted"] and b["reason"] == "identity_unverified:pid_missing", b
    assert m["review"]["state"] == "held" and m["review"]["reason"] == "unknown:X7", m["review"]
    assert load_inconclusive(m) is not None, "the wall-clock tests must recognise this shape as load"


def test_observe(root: Path) -> None:
    obs = ci.observe(_live_env())
    assert obs["problem"] is None and obs["mode"] == MODE and obs["sha256"] is None
    assert obs["path_sha256"] == ci.path_sha256(_self_image()) and obs["size"] > 0
    assert ci.observe({ci.ENV_PID: str(os.getpid())})["mode"] == ci.MODE_UNSET
    assert ci.observe({ci.ENV_MODE: MODE})["problem"] == "pid_missing"
    assert ci.observe(_live_env(**{ci.ENV_PID: "x1"}))["problem"] == "pid_missing"
    assert ci.observe(_live_env(**{ci.ENV_EXECPATH: str(root / "x.exe")}))["problem"] == "execpath_mismatch"
    assert ci.observe(_live_env(**{ci.ENV_EXECPATH: _self_image()}))["problem"] is None
    assert ci.observe(_live_env(**{ci.ENV_MODE: "a/b"}))["problem"] == "mode_malformed"
    assert ci.observe(_live_env(**{ci.ENV_HOST_VERSION: "1.2 ; x"}))["host_version"] is None
    assert ci.observe(_live_env(**{ci.ENV_HOST_VERSION: "2.9.1"}))["host_version"] == "2.9.1"


def test_resolve_cache_and_drift(root: Path) -> None:
    start = ci.persisted(ci.observe(_live_env()))
    cache = root / ci.CACHE_NAME
    assert ci.resolve(start, None, cache)["problem"] == "unverifiable", "no cache, no live client"
    live = ci.resolve(start, _live_env(), cache)
    assert live["sha256"] and live["problem"] is None
    assert ci.resolve(start, None, cache)["sha256"] == live["sha256"], "the cache serves a clientless pass"
    assert ci.resolve(start, {}, None)["problem"] == "unverifiable"
    moved = {**start, "mtime_ns": start["mtime_ns"] - 1}
    assert ci.resolve(moved, _live_env(), None)["problem"] == "exe_changed"
    other = {**start, "path_sha256": "0" * 64}
    assert ci.resolve(other, _live_env(), None)["problem"] == "unverifiable"
    assert ci.resolve(None, _live_env(), None)["problem"] == "not_observed"
    assert ci.resolve({**start, "problem": "pid_missing"}, _live_env(), None)["problem"] == "pid_missing"
    (root / "bad.json").write_text("{", encoding="utf-8")
    assert ci.cache_lookup(root / "bad.json", start) is None


def test_load_boundary_reasons(root: Path) -> None:
    client = _client()
    d = root / "b"
    assert mf.load_boundary(None, [d], client)["reason"] == "cli_unknown"
    assert mf.load_boundary(V, [d], client)["reason"] == "boundary_untested"
    # a version-only record predates identity
    d.mkdir()
    write_json_atomic(d / f"{V}.json", {"cli_version": V, "relied_upon": ["p"], "results": {"p": {"passed": True}}})
    assert mf.load_boundary(V, [d], client)["reason"] == "identity_untested"
    assert mf.load_boundary(V, [d], None)["reason"] == "identity_unknown"
    assert mf.load_boundary(V, [d], {"sha256": None, "problem": "not_observed"})["reason"] == "identity_unknown"
    assert mf.load_boundary(V, [d], {**client, "problem": "exe_changed"})["reason"] == "identity_unverified:exe_changed"
    _record(d, sha=client["sha256"], mode="other-mode")
    assert mf.load_boundary(V, [d], client)["reason"] == "identity_untested"
    (d / f"{V}.json").unlink()
    assert mf.load_boundary(V, [d], client)["reason"] == "mode_untested"
    p = _record(d, sha="f" * 64)
    assert mf.load_boundary(V, [d], client)["reason"] == "identity_changed"
    _record(d, sha=client["sha256"], accepted=False)
    assert mf.load_boundary(V, [d], client)["reason"] == "boundary_unaccepted"
    _record(d, sha=client["sha256"], passed=False)
    assert mf.load_boundary(V, [d], client)["reason"] == "boundary_failed:p"
    rec = read_json(p)
    rec["identity"]["mode"] = "other-mode"
    write_json_atomic(p, rec)
    assert mf.load_boundary(V, [d], client)["reason"] == "boundary_record_mismatch"
    p.write_text("{", encoding="utf-8")
    assert mf.load_boundary(V, [d], client)["reason"] == "boundary_file_unreadable"
    _record(d, sha=client["sha256"])
    b = mf.load_boundary(V, [d], client)
    assert b["trusted"] is True and b["accepted_at"] and b["client"]["mode"] == MODE, b
    for reason in ("identity_untested", "mode_untested", "identity_changed", "boundary_unaccepted",
                   "identity_unknown", "identity_unverified:exe_changed", "boundary_record_mismatch"):
        assert report._boundary_bucket(reason) == "cli_untested"


def test_acceptance_takes_effect_in_maintenance(root: Path) -> None:
    fx = Fixture(root)
    rec_path = fx.boundary / ci.record_name(V, MODE)
    rec = read_json(rec_path)
    write_json_atomic(rec_path, {**rec, "accepted": None})
    sid = str(uuid.uuid4())
    pid, ts = fx.start(sid)
    fx.end(sid, fx.transcript(sid, PROMPT, pid, ts))
    m = fx.episode(sid)
    assert m["boundary"]["reason"] == "boundary_unaccepted" and not m["funnel"]["start_snapshot_complete"]
    write_json_atomic(rec_path, rec)
    # the scheduled backstop has no client: the hash comes from the cache the end hook filled
    env = {k: v for k, v in fx.env.items() if k not in (ci.ENV_PID, ci.ENV_MODE)}
    pipeline.run_maintain(fx.stores, env)
    m = fx.episode(sid)
    assert m["boundary"]["trusted"] is True, m["boundary"]
    assert m["funnel"]["start_snapshot_complete"]


def test_tested_identity(root: Path) -> None:
    cli = Path(_self_image())
    good = ci.persisted(ci.observe(_live_env()))
    t = ci.tested_identity(cli, V, [good, good])
    assert t["problem"] is None and t["mode"] == MODE and t["sha256"] == ci.hash_file(str(cli))[0], t
    assert t["observations"] == 2
    assert ci.tested_identity(cli, V, [])["problem"] == "not_observed"
    assert ci.tested_identity(cli, V, [good, {**good, "mode": "x"}])["problem"] == "mode_inconsistent"
    assert ci.tested_identity(cli, V, [{**good, "path_sha256": "0" * 64}])["problem"] == \
        "hooks_ran_under_other_executable"
    assert ci.tested_identity(cli, V, [{**good, "problem": "pid_missing"}])["problem"] == "observation:pid_missing"
    assert ci.tested_identity(root / "missing.exe", V, [good])["problem"] == "cli_unreadable"
    assert ci.record_name(V, MODE) == f"{V}--{MODE}.json"


def test_no_command_line_or_secret_reads(root: Path) -> None:
    src = (PKG / "client_identity.py").read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
    for banned in ("argv", "GetCommandLine", "cmdline", "CommandLine", "environ[", "TOKEN", "SOCKET", "OAUTH",
                   "psutil", "wmic", "Win32_Process"):
        assert banned not in code, banned
    envs = set(re.findall(r'"(CLAUDE[A-Z_]*)"', code))
    assert envs == {ci.ENV_PID, ci.ENV_EXECPATH, ci.ENV_MODE, ci.ENV_HOST_VERSION}, envs


TESTS = [test_end_to_end_trusted_and_no_path, test_unidentified_client_fails_closed_without_failing_capture,
         test_over_budget_start_keeps_identity_verdict, test_observe, test_resolve_cache_and_drift, test_load_boundary_reasons,
         test_acceptance_takes_effect_in_maintenance, test_tested_identity, test_no_command_line_or_secret_reads]


def main() -> int:
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-identity-test-") as tmp:
        for fn in TESTS:
            if only and fn.__name__ not in only:
                continue
            case = Path(tmp) / fn.__name__
            case.mkdir()
            try:
                fn(case)
                PASSED.append(fn.__name__)
            except Skip as exc:
                SKIPPED.append(fn.__name__)
                print(f"SKIP {fn.__name__}: {exc}")
            except Exception:  # noqa: BLE001 — plain-check runner
                FAILED.append(fn.__name__)
                print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    for name in PASSED:
        print(f"ok   {name}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

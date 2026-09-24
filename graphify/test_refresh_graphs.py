"""Regression tests for Refresh-Graphs.ps1 failure accounting — CLI-level: run
the shipped script under pwsh against synthetic targets in a temp dir, assert
the exit code, health.json, the log, and which graph-meta.json sidecars exist.

Only the external `graphify` executable is faked (GRAPHIFY_EXE). The stats read
is real: PYTHON_EXE is this interpreter, and the stats-failed target carries a
malformed graph.json, so the script's own embedded json.loads is what fails.

Synthetic targets arrive via a temp graphify.local.json (GRAPHIFY_CONFIG); the
shipped script runs verbatim. Config-failure cases assert the fail-closed path
(graphify is never invoked); relative-path cases run from a working directory
outside the config folder, as a scheduled task does. Run: python test_refresh_graphs.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("Refresh-Graphs.ps1")

# Fake graphify: `extract` exits 3 inside a scan dir named extract-fail, every
# other call succeeds without touching graph.json.
FAKE_GRAPHIFY = """\
if ($args[0] -eq 'extract' -and (Split-Path -Leaf (Get-Location).Path) -eq 'extract-fail') { exit 3 }
exit 0
"""

PREV_SUCCESS = "2026-01-01T00:00:00Z"

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" -- {detail}" if detail and not cond else ""))


def make_target(root, name, graph_text):
    scan = root / name
    (scan / "graphify-out").mkdir(parents=True)
    (scan / "graphify-out" / "graph.json").write_text(graph_text, encoding="utf-8")
    return scan


# Fake graphify for fail-closed cases: any invocation leaves a marker file.
MARKER_GRAPHIFY = """Set-Content -Path $env:GRAPHIFY_TEST_MARKER -Value "invoked $args"
exit 0
"""


def clean_env(**over):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRAPHIFY_") and k != "PYTHON_EXE"}
    env.update(over)
    return env


def run_script(pwsh, env, cwd=None):
    return subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT)],
                          capture_output=True, text=True, timeout=120, env=env, cwd=cwd)


def config_failure_cases(pwsh):
    """Missing / invalid config must fail closed: exit 1, FATAL log line,
    config_status + config_error in health.json, prior last_success_at
    preserved, and graphify never invoked."""
    cases = {
        "missing": (None, "missing"),
        "placeholder": (json.dumps({"targets": [{"name": "x", "scan": "<path-to-backend>"}]}), "invalid"),
        "no-targets": (json.dumps({"targets": []}), "invalid"),
        "bad-json": ("{ nope", "invalid-json"),
        "not-an-object": ("[]", "invalid-json"),
        "bad-name": (json.dumps({"targets": [{"name": "has space/slash", "scan": "."}]}), "invalid"),
        "drive-relative": (json.dumps({"targets": [{"name": "x", "scan": "C" + ":rel"}]}), "invalid"),
        "placeholder-dashboard": (json.dumps({"targets": [{"name": "x", "scan": "."}],
                                              "dashboard_dirs": ["<path-to-dashboard>"]}), "invalid"),
        "embedded-placeholder": (json.dumps({"targets": [{"name": "x", "scan": "repos/<path-to-backend>"}]}), "invalid"),
        # pwsh 7 would unwrap this one-element array into an object; 5.1 would not.
        "wrapped-object": (json.dumps([{"targets": [{"name": "x", "scan": "."}]}]), "invalid-json"),
    }
    if "powershell" in os.path.basename(pwsh).lower():
        # .NET Framework rejects '|' in paths; that must be a config error, not a crash.
        cases["invalid-path-char"] = (json.dumps({"targets": [{"name": "x", "scan": "a|b"}]}), "invalid")
    for label, (body, status) in cases.items():
        tmp = Path(tempfile.mkdtemp(prefix=f"refresh-graphs-cfg-{label}-"))
        try:
            config = tmp / "graphify.local.json"
            if body is not None:
                config.write_text(body, encoding="utf-8")
            (tmp / "health.json").write_text(json.dumps({"last_success_at": PREV_SUCCESS}), encoding="utf-8")
            fake = tmp / "marker-graphify.ps1"
            fake.write_text(MARKER_GRAPHIFY, encoding="utf-8")
            marker = tmp / "graphify-invoked.txt"
            env = clean_env(GRAPHIFY_ROOT=str(tmp), GRAPHIFY_CONFIG=str(config), GRAPHIFY_EXE=str(fake),
                            PYTHON_EXE=sys.executable, GRAPHIFY_TEST_MARKER=str(marker))
            p = run_script(pwsh, env)
            check(f"8 config {label}: exits 1", p.returncode == 1, f"rc={p.returncode} stderr={p.stderr.strip()[:300]}")
            health = json.loads((tmp / "health.json").read_text(encoding="utf-8-sig"))
            run = health.get("last_run") or {}
            check(f"8 config {label}: config_error recorded", bool(run.get("config_error")) and run.get("exit_code") == 1,
                  f"run={run}")
            check(f"8 config {label}: config_status {status}", run.get("config_status") == status,
                  f"got {run.get('config_status')}")
            check(f"8 config {label}: refresh did not run (graphify never invoked)", not marker.exists())
            check(f"8 config {label}: last_success_at kept", health.get("last_success_at") == PREV_SUCCESS)
            log = (tmp / "refresh-log.txt").read_text(encoding="utf-8-sig")
            check(f"8 config {label}: FATAL config logged", "FATAL config (" in log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def relative_path_cases(pwsh):
    """Relative config paths resolve against the config file's folder, not the
    (scheduled-task) working directory, and are recorded absolute."""
    tmp = Path(tempfile.mkdtemp(prefix="refresh-graphs-relpath-"))
    try:
        cfg_dir = tmp / "cfg"
        elsewhere = tmp / "elsewhere"
        elsewhere.mkdir(parents=True)
        good_graph = json.dumps({"built_at_commit": "", "nodes": [{"id": "a", "community": 0}], "links": []})
        make_target(cfg_dir / "scans", "rel", good_graph)
        (cfg_dir / "tools").mkdir(parents=True)
        (cfg_dir / "tools" / "fake-graphify.ps1").write_text(FAKE_GRAPHIFY, encoding="utf-8")
        config = cfg_dir / "graphify.local.json"
        config.write_text(json.dumps({
            "targets": [{"name": "rel", "scan": "scans/rel", "repo": "scans"}],
            "graphify_exe": "tools/fake-graphify.ps1",
            "python_exe": sys.executable,
        }), encoding="utf-8")
        env = clean_env(GRAPHIFY_ROOT=str(tmp), GRAPHIFY_CONFIG=str(config))
        p = run_script(pwsh, env, cwd=str(elsewhere))
        check("9 relative: exits 0 from a foreign cwd", p.returncode == 0,
              f"rc={p.returncode} stderr={p.stderr.strip()[:300]}")
        health = json.loads((tmp / "health.json").read_text(encoding="utf-8-sig"))
        rec = (health.get("last_run") or {}).get("targets") or [{}]
        expected = os.path.abspath(cfg_dir / "scans" / "rel")
        check("9 relative: scan recorded absolute, anchored at the config folder",
              rec[0].get("scan") == expected, f"got {rec[0].get('scan')} want {expected}")
        check("9 relative: relative graphify_exe resolved (target ok)", rec[0].get("ok") is True, f"rec={rec[0]}")
        check("9 relative: graph-meta.json written in the anchored scan dir",
              (cfg_dir / "scans" / "rel" / "graphify-out" / "graph-meta.json").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    # GRAPHIFY_TEST_SHELL=powershell.exe re-runs the suite under Windows PowerShell 5.1.
    pwsh = shutil.which(os.environ.get("GRAPHIFY_TEST_SHELL", "pwsh"))
    if not pwsh:
        print("FAIL: pwsh not found on PATH -- this suite needs PowerShell 7")
        sys.exit(1)

    tmp = Path(tempfile.mkdtemp(prefix="refresh-graphs-test-"))
    try:
        good_graph = json.dumps({
            "built_at_commit": "",
            "nodes": [{"id": "a", "community": 0}, {"id": "b", "community": 1}, {"id": "c", "community": 1}],
            "links": [{"source": "a", "target": "b"}],
        })
        scans = {
            "stats-fail": make_target(tmp, "stats-fail", "{ this is not json"),
            "good": make_target(tmp, "good", good_graph),
            "extract-fail": make_target(tmp, "extract-fail", good_graph),
        }

        config = tmp / "graphify.local.json"
        config.write_text(json.dumps({
            "targets": [{"name": n, "scan": str(s), "repo": str(tmp)} for n, s in scans.items()],
        }), encoding="utf-8")

        fake = tmp / "fake-graphify.ps1"
        fake.write_text(FAKE_GRAPHIFY, encoding="utf-8")

        # Prior health: a successful run whose last_success_at must survive.
        (tmp / "health.json").write_text(json.dumps({
            "last_success_at": PREV_SUCCESS,
            "last_run": {"targets": [{"name": "good", "nodes": 3}]},
        }), encoding="utf-8")

        # Env vars stay valid explicit overrides of the config's tool paths.
        env = clean_env(GRAPHIFY_ROOT=str(tmp), GRAPHIFY_CONFIG=str(config),
                        GRAPHIFY_EXE=str(fake), PYTHON_EXE=sys.executable)
        p = run_script(pwsh, env)
        check("1 run: exits 1 when any target fails", p.returncode == 1,
              f"rc={p.returncode} stderr={p.stderr.strip()[:300]}")

        health = json.loads((tmp / "health.json").read_text(encoding="utf-8-sig"))
        run = health["last_run"]
        recs = run["targets"]
        by_name = {}
        for r in recs:
            by_name.setdefault(r["name"], []).append(r)

        check("2 run record: exit_code 1", run["exit_code"] == 1, f"got {run['exit_code']}")
        check("2 run record: one record per target", len(recs) == 3 and all(len(v) == 1 for v in by_name.values()),
              f"names={[r['name'] for r in recs]}")
        check("2 last_success_at: prior value kept, not re-stamped",
              health["last_success_at"] == PREV_SUCCESS, f"got {health['last_success_at']}")

        sf = by_name.get("stats-fail", [{}])[0]
        check("3 stats-fail: exit_code -2", sf.get("exit_code") == -2, f"got {sf.get('exit_code')}")
        check("3 stats-fail: ok false", sf.get("ok") is False, f"got {sf.get('ok')}")
        check("3 stats-fail: no stats recorded", sf.get("nodes") is None)
        check("3 stats-fail: ended_at stamped", bool(sf.get("ended_at")))
        check("3 stats-fail: no graph-meta.json sidecar",
              not (scans["stats-fail"] / "graphify-out" / "graph-meta.json").exists())

        ef = by_name.get("extract-fail", [{}])[0]
        check("4 extract-fail: exit_code is graphify's code", ef.get("exit_code") == 3, f"got {ef.get('exit_code')}")
        check("4 extract-fail: ok false", ef.get("ok") is False)
        check("4 extract-fail: no graph-meta.json sidecar",
              not (scans["extract-fail"] / "graphify-out" / "graph-meta.json").exists())

        gd = by_name.get("good", [{}])[0]
        check("5 good sibling: ok true, exit_code 0", gd.get("ok") is True and gd.get("exit_code") == 0,
              f"ok={gd.get('ok')} exit={gd.get('exit_code')}")
        check("5 good sibling: stats from graph.json", (gd.get("nodes"), gd.get("edges"), gd.get("communities")) == (3, 1, 2),
              f"got {(gd.get('nodes'), gd.get('edges'), gd.get('communities'))}")
        check("5 good sibling: graph-meta.json sidecar written",
              (scans["good"] / "graphify-out" / "graph-meta.json").exists())

        log = (tmp / "refresh-log.txt").read_text(encoding="utf-8-sig").splitlines()
        check("6 log: FAIL line for stats-fail",
              any(l.startswith("FAIL stats-fail: could not read stats") for l in log))
        check("6 log: no OK line for stats-fail", not any(l.startswith("stats-fail: OK") for l in log))
        check("6 log: no OK line for extract-fail", not any(l.startswith("extract-fail: OK") for l in log))
        check("6 log: OK line for good", any(l.startswith("good: OK") for l in log))
        check("6 log: run counts exactly 2 failures", any(l.endswith("refresh done (2 failed) ===") for l in log),
              f"tail={log[-1:]}")

        hist = (tmp / "health-history.jsonl").read_text(encoding="utf-8-sig").splitlines()
        check("7 history: one line appended with exit_code 1",
              len(hist) == 1 and json.loads(hist[0])["exit_code"] == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    config_failure_cases(pwsh)
    relative_path_cases(pwsh)

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()

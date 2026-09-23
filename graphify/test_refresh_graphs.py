"""Regression tests for Refresh-Graphs.ps1 failure accounting — CLI-level: run
the shipped script under pwsh against synthetic targets in a temp dir, assert
the exit code, health.json, the log, and which graph-meta.json sidecars exist.

Only the external `graphify` executable is faked (GRAPHIFY_EXE). The stats read
is real: PYTHON_EXE is this interpreter, and the stats-failed target carries a
malformed graph.json, so the script's own embedded json.loads is what fails.

The script's hardcoded $targets block is swapped for synthetic targets in a
temp copy; every other line runs verbatim. Run: python test_refresh_graphs.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("Refresh-Graphs.ps1")
TARGETS_BLOCK = re.compile(r"^\$targets = @\(\r?\n.*?^\)\r?$", re.M | re.S)

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


def ps_quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def main():
    pwsh = shutil.which("pwsh")
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

        source = SCRIPT.read_text(encoding="utf-8-sig")
        targets = "$targets = @(\n" + ",\n".join(
            f"    @{{ Name = '{n}'; Scan = {ps_quote(s)}; Repo = {ps_quote(tmp)} }}" for n, s in scans.items()
        ) + "\n)"
        patched, n_subs = TARGETS_BLOCK.subn(lambda _m: targets, source)
        check("0 setup: $targets block found exactly once", n_subs == 1, f"subs={n_subs}")
        if n_subs != 1:
            raise SystemExit(1)
        script = tmp / "Refresh-Graphs.ps1"
        script.write_text(patched, encoding="utf-8")

        fake = tmp / "fake-graphify.ps1"
        fake.write_text(FAKE_GRAPHIFY, encoding="utf-8")

        # Prior health: a successful run whose last_success_at must survive.
        (tmp / "health.json").write_text(json.dumps({
            "last_success_at": PREV_SUCCESS,
            "last_run": {"targets": [{"name": "good", "nodes": 3}]},
        }), encoding="utf-8")

        env = dict(os.environ, GRAPHIFY_ROOT=str(tmp), GRAPHIFY_EXE=str(fake), PYTHON_EXE=sys.executable)
        p = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
                           capture_output=True, text=True, timeout=120, env=env)
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

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()

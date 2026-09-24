"""Regression tests for Check-GraphifyHealth.ps1 config-failure handling and the
public dashboard boundary — CLI-level: run the shipped script under pwsh in a
temp dir and assert exit code, local alerts.json, and the PUBLIC
graphify-health.js.

Windows-only / network calls are stubbed by defining same-named functions in
the calling scope (functions shadow cmdlets; the script itself runs verbatim):
Get-ScheduledTaskInfo (task result from STUB_TASK_RESULT), Invoke-RestMethod
(PyPI answer = STUB_PYPI_VERSION), Add-Type (toast off). python_exe is a fake
script printing FAKE_VERSION, so the published installed_version proves which
python the health check actually used.

Every run executes from a working directory OUTSIDE the config folder, so
relative config paths are exercised the way a scheduled task sees them.
Run: python test_health_check.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).with_name("Check-GraphifyHealth.ps1")
FAKE_VERSION = "9.9.9"
# Sentinels that only ever live in LOCAL state; none may reach the public file.
PARSER_SENTINEL = "XYZZY-PARSER-TEXT"
DRIVE = "Q" + ":" + "\\"                      # built, so the boundary check never sees a literal
LOCAL_PATH = DRIVE + "secret" + "\\" + "graphify.local.json"

PUBLIC_TOP = {"checked_at", "status", "installed_version", "latest_version", "version_checked",
              "version_source", "update_available", "last_success_at", "task_last_result",
              "task_next_run", "alerts", "targets"}
PUBLIC_ALERT = {"severity", "code", "message", "action", "context"}
PUBLIC_TARGET = {"name", "ok", "exit_code", "nodes", "edges", "communities", "node_drift_pct",
                 "drift_warning", "head_match", "ended_at"}
STUB_TOP = {"checked_at", "status", "alerts", "targets"}

HARNESS = r"""
function global:Get-ScheduledTaskInfo { [CmdletBinding()] param($TaskName)
    if ($env:STUB_TASK_RESULT -eq 'missing') { throw 'no such task' }
    [pscustomobject]@{ LastTaskResult = [int]$env:STUB_TASK_RESULT; NextRunTime = $null } }
function global:Invoke-RestMethod { [CmdletBinding()] param($Uri, $TimeoutSec, [switch]$UseBasicParsing)
    [pscustomobject]@{ info = [pscustomobject]@{ version = $env:STUB_PYPI_VERSION } } }
function global:Add-Type { [CmdletBinding()] param($AssemblyName) throw 'toast stubbed' }
& $env:HEALTH_SCRIPT
exit $LASTEXITCODE
"""

results = []


def check(name, cond, detail=""):
    results.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" -- {detail}" if detail and not cond else ""))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


RECENT = iso(datetime.now(timezone.utc) - timedelta(days=1))


class Env:
    """One isolated run directory: <tmp>/cfg holds config + tools + dashboard,
    <tmp>/state is GRAPHIFY_ROOT, <tmp>/elsewhere is the process cwd."""

    def __init__(self, label):
        self.tmp = Path(tempfile.mkdtemp(prefix=f"graphify-health-{label}-"))
        self.cfg_dir = self.tmp / "cfg"
        self.state = self.tmp / "state"
        self.cwd = self.tmp / "elsewhere"
        self.dash = self.cfg_dir / "dash"
        for d in (self.cfg_dir / "tools", self.state, self.cwd, self.dash):
            d.mkdir(parents=True)
        (self.cfg_dir / "tools" / "fake-python.ps1").write_text(f"Write-Output '{FAKE_VERSION}'\n", encoding="utf-8")
        self.config = self.cfg_dir / "graphify.local.json"
        self.js = self.dash / "graphify-health.js"
        # A stale public "ok" from an earlier healthy day: must never survive a current failure.
        self.js.write_text('window.GRAPHIFY_HEALTH = {"status":"ok","alerts":[],"targets":[]};', encoding="utf-8")

    def write_config(self, body):
        self.config.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")

    def write_health(self, doc):
        (self.state / "health.json").write_text(json.dumps(doc), encoding="utf-8")

    def run(self, task_result="1", extra_env=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GRAPHIFY_") and k not in ("PYTHON_EXE",)}
        env.update(GRAPHIFY_ROOT=str(self.state), GRAPHIFY_CONFIG=str(self.config),
                   HEALTH_SCRIPT=str(SCRIPT), STUB_TASK_RESULT=task_result, STUB_PYPI_VERSION=FAKE_VERSION)
        env.update(extra_env or {})
        return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", HARNESS],
                              capture_output=True, text=True, timeout=120, env=env, cwd=str(self.cwd))

    def alerts(self):
        return json.loads((self.state / "alerts.json").read_text(encoding="utf-8-sig"))

    def public(self):
        text = self.js.read_text(encoding="utf-8-sig").strip()
        m = re.fullmatch(r"window\.GRAPHIFY_HEALTH = (.*);", text, re.S)
        return text, (json.loads(m.group(1)) if m else None)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def valid_config(**over):
    body = {"targets": [{"name": "backend", "scan": "scans/backend", "repo": "scans"}],
            "python_exe": "tools/fake-python.ps1", "dashboard_dirs": ["dash"]}
    body.update(over)
    return body


def config_abort_health():
    """What Refresh-Graphs.ps1 writes after a config abort (local detail included)."""
    return {"updated_at": RECENT, "installed_version": "", "drift_threshold_pct": 15,
            "last_success_at": RECENT,
            "last_run": {"started_at": RECENT, "ended_at": RECENT, "exit_code": 1,
                         "config_status": "invalid-json",
                         "config_error": f"graphify config '{LOCAL_PATH}' is not valid JSON: {PARSER_SENTINEL}",
                         "targets": []}}


def assert_public_clean(label, e, text, doc, stub=False):
    check(f"{label}: public file parses", doc is not None, text[:200])
    if doc is None:
        return
    check(f"{label}: public top-level keys are the allowlist",
          set(doc) == (STUB_TOP if stub else PUBLIC_TOP), f"keys={sorted(doc)}")
    for a in doc.get("alerts", []):
        check(f"{label}: alert '{a.get('code')}' keys are the allowlist", set(a) == PUBLIC_ALERT, f"keys={sorted(a)}")
    for t in doc.get("targets", []):
        check(f"{label}: target '{t.get('name')}' keys are the allowlist", set(t) == PUBLIC_TARGET, f"keys={sorted(t)}")
    leaks = {
        "drive path": re.search(r"[A-Za-z]:(\\\\|/)", text),
        "backslash": "\\\\" in text,
        "posix home": re.search(r"/(home|Users)/", text),
        "temp dir": str(e.tmp) in text or json.dumps(str(e.tmp))[1:-1] in text,
        "parser/exception text": PARSER_SENTINEL in text or "not valid JSON" in text,
        "local config message": "graphify config '" in text,
        "raw keys": re.search(r'"(scan|repo|python_exe|graphify_exe|config_error|config_status|detail|dashboard_dirs)"\s*:', text),
    }
    for what, hit in leaks.items():
        check(f"{label}: public has no {what}", not hit)


def error_alerts(doc):
    return [a["code"] for a in doc["alerts"] if a["severity"] == "error"]


def case_config_failure(label, body, task_result="1"):
    """Parseable-but-invalid (or unparseable with last-known-good) config: one
    authoritative config-invalid alert, current error published, no stale ok."""
    e = Env(label)
    try:
        e.write_config(body)
        e.write_health(config_abort_health())
        p = e.run(task_result=task_result)
        check(f"{label}: exits 1", p.returncode == 1, f"rc={p.returncode} err={p.stderr.strip()[:300]}")
        local = e.alerts()
        codes = error_alerts(local)
        check(f"{label}: exactly one error alert, config-invalid", codes == ["config-invalid"], f"codes={codes}")
        cfg_alert = next(a for a in local["alerts"] if a["code"] == "config-invalid")
        check(f"{label}: local detail keeps diagnostics", "config " in cfg_alert["detail"] and PARSER_SENTINEL in cfg_alert["detail"],
              cfg_alert["detail"][:200])
        check(f"{label}: task exit 1 folded in as context", "0x1" in cfg_alert["context"], cfg_alert["context"])
        text, doc = e.public()
        assert_public_clean(label, e, text, doc)
        if doc:
            check(f"{label}: public status is error (no stale ok)", doc["status"] == "error", doc.get("status"))
            check(f"{label}: public has exactly one error alert, config-invalid", error_alerts(doc) == ["config-invalid"],
                  f"{error_alerts(doc)}")
            check(f"{label}: public config message is the generic one",
                  doc["alerts"][0]["message"] == "Graphify local configuration is missing or invalid.")
        return e, doc
    except Exception:
        e.close()
        raise


def main():
    # -- placeholder target: settings (python, relative dashboard dir) still resolve
    e, doc = case_config_failure("placeholder", valid_config(targets=[{"name": "x", "scan": "<path-to-backend>"}]))
    check("placeholder: configured python_exe used although targets invalid",
          bool(doc) and doc["installed_version"] == FAKE_VERSION, f"{doc and doc['installed_version']}")
    e.close()

    # -- empty target list
    e, doc = case_config_failure("empty-targets", valid_config(targets=[]))
    check("empty-targets: configured python_exe used", bool(doc) and doc["installed_version"] == FAKE_VERSION)
    e.close()

    # -- genuinely separate failure keeps its own alert
    e = Env("separate-task-failure")
    try:
        e.write_config(valid_config(targets=[]))
        e.write_health(config_abort_health())
        e.run(task_result="2")
        codes = error_alerts(e.alerts())
        check("separate-task-failure: task exit 2 stays its own alert",
              sorted(codes) == ["config-invalid", "task-result"], f"codes={codes}")
    finally:
        e.close()

    # -- task exit 1 that was NOT a config abort (a target failed) is not folded
    e = Env("exit1-not-config")
    try:
        e.write_config(valid_config(targets=[]))
        e.write_health({"installed_version": FAKE_VERSION, "last_success_at": RECENT,
                        "last_run": {"exit_code": 1, "ended_at": RECENT, "targets": [
                            {"name": "backend", "ok": False, "exit_code": 3}]}})
        e.run(task_result="1")
        local = e.alerts()
        codes = sorted(error_alerts(local))
        check("exit1-not-config: task exit 1 stays its own alert when the refresh was not a config abort",
              codes == ["config-invalid", "refresh-failed", "target-failed", "task-result"], f"codes={codes}")
        cfg_alert = next(a for a in local["alerts"] if a["code"] == "config-invalid")
        check("exit1-not-config: config alert not mislabelled as the abort", cfg_alert["context"] == "")
    finally:
        e.close()

    # -- invalid JSON: last-known-good settings from an earlier parseable run
    e = Env("invalid-json")
    try:
        other = e.tmp / "other-dash"
        other.mkdir()
        e.write_config(valid_config())
        # A valid run saves last-known-good from the FILE, not a one-off env override ...
        e.run(task_result="0", extra_env={"GRAPHIFY_DASHBOARD_DIRS": str(other)})
        # ... and a parseable-but-invalid config never overwrites it.
        e.write_config(valid_config(dashboard_dirs=["<path-to-dashboard>"]))
        e.write_health(config_abort_health())
        e.run()
        # A dashboard_dirs entry that fails validation falls back to last-known-good:
        # the stale public "ok" is replaced by the current error.
        text, doc = e.public()
        assert_public_clean("bad-dashboard-dirs", e, text, doc)
        check("bad-dashboard-dirs: current error published via last-known-good dir",
              bool(doc) and doc["status"] == "error" and error_alerts(doc) == ["config-invalid"], f"{doc}")
        e.js.write_text('window.GRAPHIFY_HEALTH = {"status":"ok","alerts":[],"targets":[]};', encoding="utf-8")
        e.write_config("{ nope " + PARSER_SENTINEL)
        p = e.run()
        check("invalid-json: exits 1", p.returncode == 1)
        codes = error_alerts(e.alerts())
        check("invalid-json: exactly one error alert, config-invalid", codes == ["config-invalid"], f"codes={codes}")
        text, doc = e.public()
        assert_public_clean("invalid-json", e, text, doc)
        check("invalid-json: current error published via last-known-good", bool(doc) and doc["status"] == "error")
        check("invalid-json: last-known-good python used", bool(doc) and doc["installed_version"] == FAKE_VERSION)
        lkg = json.loads((e.state / "publish-settings.lkg.json").read_text(encoding="utf-8-sig"))
        check("invalid-json: last-known-good holds the file's dashboard dir, not the env override",
              [os.path.realpath(d) for d in lkg.get("dashboard_dirs") or []] == [os.path.realpath(e.dash)],
              f"lkg={lkg}")
    finally:
        e.close()

    # -- missing config, no safe source: fail visibly, publish nothing invented
    e = Env("missing-no-source")
    try:
        e.js.unlink()
        e.write_health(config_abort_health())
        p = e.run()
        check("missing-no-source: exits 1", p.returncode == 1)
        codes = error_alerts(e.alerts())
        check("missing-no-source: exactly one error alert, config-invalid", codes == ["config-invalid"], f"codes={codes}")
        check("missing-no-source: no dashboard file invented", not e.js.exists())
        log = (e.state / "health-check-log.txt").read_text(encoding="utf-8-sig")
        check("missing-no-source: log says status NOT published", "NOT published" in log)
    finally:
        e.close()

    # -- missing config, env override names the dashboard dir
    e = Env("missing-env-dash")
    try:
        e.write_health(config_abort_health())
        p = e.run(extra_env={"GRAPHIFY_DASHBOARD_DIRS": str(e.dash)})
        check("missing-env-dash: exits 1", p.returncode == 1)
        text, doc = e.public()
        assert_public_clean("missing-env-dash", e, text, doc)
        check("missing-env-dash: public status error, one config-invalid",
              bool(doc) and doc["status"] == "error" and error_alerts(doc) == ["config-invalid"],
              f"{doc and error_alerts(doc)}")
    finally:
        e.close()

    # -- recovered config after a prior config abort: generic re-run alert, nothing local leaks
    e = Env("recovered")
    try:
        (e.cfg_dir / "scans" / "backend").mkdir(parents=True)
        e.write_config(valid_config())
        e.write_health(config_abort_health())
        p = e.run()
        codes = error_alerts(e.alerts())
        check("recovered: exactly one error alert, refresh-config", codes == ["refresh-config"], f"codes={codes}")
        text, doc = e.public()
        assert_public_clean("recovered", e, text, doc)
        if doc:
            check("recovered: prior config_error does not leak", PARSER_SENTINEL not in text)
            check("recovered: task exit 1 folded into the refresh-config alert",
                  "0x1" in doc["alerts"][0]["context"], doc["alerts"][0]["context"])
    finally:
        e.close()

    # -- healthy run: status ok, targets projected to the allowlist (no scan/repo/SHAs)
    e = Env("healthy")
    try:
        (e.cfg_dir / "scans" / "backend").mkdir(parents=True)
        e.write_config(valid_config())
        sha = "a" * 40
        e.write_health({"updated_at": RECENT, "installed_version": FAKE_VERSION, "drift_threshold_pct": 15,
                        "last_success_at": RECENT,
                        "last_run": {"started_at": RECENT, "ended_at": RECENT, "exit_code": 0, "targets": [
                            {"name": "backend", "scan": DRIVE + "repo" + "\\" + "backend", "started_at": RECENT,
                             "ended_at": RECENT, "ok": True, "exit_code": 0, "nodes": 10, "edges": 5,
                             "communities": 2, "prev_nodes": 10, "node_drift_pct": 0.0, "drift_warning": False,
                             "built_at_commit": sha, "repo_head": sha, "head_match": True,
                             "code_only": True, "force": False, "pythonhashseed": "0"}]}})
        p = e.run(task_result="0")
        check("healthy: exits 0", p.returncode == 0, f"rc={p.returncode} err={p.stderr.strip()[:300]}")
        text, doc = e.public()
        assert_public_clean("healthy", e, text, doc)
        if doc:
            check("healthy: status ok, zero alerts", doc["status"] == "ok" and doc["alerts"] == [], f"{doc['alerts']}")
            t = doc["targets"][0] if doc["targets"] else {}
            check("healthy: target metrics published", (t.get("name"), t.get("nodes"), t.get("ok")) == ("backend", 10, True))
            check("healthy: no commit SHAs published", sha not in text)
        local = e.alerts()
        check("healthy: local alerts.json keeps the scan path", local["targets"][0]["scan"].startswith(DRIVE))
    finally:
        e.close()

    # -- local values never reach public alert text: bad target names, SHAs, versions
    e = Env("sanitized-text")
    try:
        (e.cfg_dir / "scans" / "backend").mkdir(parents=True)
        e.write_config(valid_config())
        sha = "b" * 40
        e.write_health({"installed_version": FAKE_VERSION, "last_success_at": RECENT, "drift_threshold_pct": 15,
                        "last_run": {"exit_code": 0, "ended_at": RECENT, "targets": [
                            {"name": DRIVE + "evil", "ok": False, "exit_code": 3},
                            {"name": "my private repo", "ok": True, "exit_code": 0, "head_match": False,
                             "built_at_commit": sha, "repo_head": "c" * 40, "drift_warning": True,
                             "node_drift_pct": 40.0, "prev_nodes": 10, "nodes": 14}]}})
        p = e.run(task_result="0", extra_env={"STUB_PYPI_VERSION": "private notes/" + "x"})
        check("sanitized-text: exits 1 (target failed)", p.returncode == 1)
        text, doc = e.public()
        assert_public_clean("sanitized-text", e, text, doc)
        if doc:
            check("sanitized-text: real alerts published, not the guard stub",
                  sorted(a["code"] for a in doc["alerts"]) == ["count-drift", "head-behind", "target-failed"],
                  f"{[a['code'] for a in doc['alerts']]}")
            check("sanitized-text: bad target names replaced in messages", "evil" not in text
                  and "private repo" not in text and "Refresh failed for target (exit 3)." in text)
            check("sanitized-text: no commit SHA in any message", sha[:8] not in text and "c" * 8 not in text)
            check("sanitized-text: non-version PyPI answer never published",
                  "private notes" not in text and doc["latest_version"] == "" and not doc["update_available"])
        local = e.alerts()
        hb = next(a for a in local["alerts"] if a["code"] == "head-behind")
        check("sanitized-text: SHAs kept in the local detail", sha in hb["detail"])
    finally:
        e.close()

    # -- leak guard unit: every rule trips on its leak class, the stub is clean
    probes = {"drive": DRIVE + "x", "fwd-drive": "Q" + ":" + "/x", "unc": "//" + "srv/share",
              "posix": "/" + "opt/x", "home": "~" + "/x", "parent": ".." + "/repo", "urlenc": "Q%3" + "A%5C" + "x"}
    script = (". $env:PUBLIC_PS1\n"
              "$out = [ordered]@{}\n"
              "foreach ($p in ($env:PROBES | ConvertFrom-Json).PSObject.Properties) {\n"
              "  $doc = [ordered]@{ status = 'ok'; alerts = @([ordered]@{ severity = 'warn'; code = 'x'; message = $p.Value }); targets = @() }\n"
              "  $json = ConvertTo-Json (ConvertTo-GraphifyPublicHealth -Doc $doc) -Depth 6 -Compress\n"
              "  $out[$p.Name] = @(Test-GraphifyPublicSafe -Json $json).Count }\n"
              "$stub = ConvertTo-Json (New-GraphifyPublicRejectedStub -CheckedAt '2026-01-01T00:00:00Z') -Depth 6 -Compress\n"
              "$out['stub'] = @(Test-GraphifyPublicSafe -Json $stub).Count\n"
              "$out | ConvertTo-Json -Compress\n")
    env = dict(os.environ, PUBLIC_PS1=str(SCRIPT.with_name("GraphifyPublic.ps1")), PROBES=json.dumps(probes))
    p = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=120, env=env)
    try:
        hits = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        hits = {}
    for name in probes:
        check(f"guard: '{name}' leak class is rejected", hits.get(name, 0) > 0, f"{hits} {p.stderr[:200]}")
    check("guard: the rejection stub passes its own guard", hits.get("stub") == 0, f"{hits}")

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} checks passed")
    sys.exit(0 if passed == len(results) else 1)


# GRAPHIFY_TEST_SHELL=powershell.exe re-runs the suite under Windows PowerShell 5.1.
PWSH = shutil.which(os.environ.get("GRAPHIFY_TEST_SHELL", "pwsh"))
if __name__ == "__main__":
    if not PWSH:
        print("FAIL: pwsh not found on PATH -- this suite needs PowerShell 7")
        sys.exit(1)
    main()

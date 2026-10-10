"""Scheduled maintenance and expiry backstop.

The start hook launches maintenance as a detached child at most once a minute, so
without the backstop, expiry depends on future prompts and on that child surviving
(a hook killed at its timeout takes its detached child with it; measured on the desktop
client's CLI). The backstop is a plain process for the operating system's scheduler:
no prompt, no hook, no model. Each run

  1. checks both stores,
  2. runs the same maintenance pass (waiting for a maintenance child that holds the lock),
  3. verifies, read-only, that nothing past its expiry is left without a valid seal,
  4. records the outcome where a broken store cannot hide it: a status file beside the
     user config (``EC_CAPTURE_BACKSTOP_STATUS`` overrides), a line in the metadata
     store's ``capture.log`` when that is writable, standard error, and the exit code
     the scheduler shows as the task's last result.

Exit codes: 0 ok, 3 warnings (review errors, episodes skipped because another process
held their lock, undated content, capture-failure streaks), 4 failure (stores unconfigured
or unusable, lock never released, maintenance error, an episode, pending record or
manifest maintenance could not handle, a failed metadata commit, expiry error, overdue
content left); ``pipeline.maintain_findings`` judges the maintenance pass itself.
``health`` turns an old last success into a failure, so a scheduler that silently stopped
running the backstop shows up in the checkpoint report.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pipeline
from common import iso, now_utc, parse_iso, read_json, write_json_atomic
from review import overdue
from store import USER_CONFIG, Stores, log_line, resolve_stores

ENV_STATUS = "EC_CAPTURE_BACKSTOP_STATUS"
STATUS_NAME = "evidence-capture-backstop.json"
LOCK_WAIT_S = 300
LOCK_POLL_S = 2.0
STALE_AFTER_H = 48
HISTORY_MAX = 30
EXIT_OK, EXIT_WARN, EXIT_FAIL = 0, 3, 4
TASK_START = "03:30"


def status_path(env: dict[str, str]) -> Path:
    return Path(env[ENV_STATUS]) if env.get(ENV_STATUS) else USER_CONFIG.with_name(STATUS_NAME)


def _evaluate(stores: Stores, env: dict[str, str], now: str, lock_wait_s: float,
              problems: list[str], warnings: list[str]) -> dict[str, Any]:
    check = pipeline.full_store_check(stores)
    if check["content"]:
        problems.append("content_store_unusable: " + "; ".join(check["content"]))
    if check["meta"]:
        problems.append("meta_store_problems: " + "; ".join(check["meta"]))
    deadline = time.monotonic() + lock_wait_s
    while True:
        counts = pipeline.run_maintain(stores, env, now=now)
        if counts.get("action") != "skipped" or time.monotonic() >= deadline:
            break
        time.sleep(LOCK_POLL_S)
    if counts.get("action") == "skipped":
        problems.append("maintenance_lock_held")
    found, warned = pipeline.maintain_findings(counts)
    problems.extend(found)
    warnings.extend(warned)
    if stores.content.is_dir():
        left = overdue(stores, now=now)
        if left:
            problems.append(f"overdue_remaining: {len(left)}")
            counts["overdue"] = left[:20]
    return counts


def run_backstop(env: dict[str, str], *, now: str | None = None, status: Path | None = None,
                 lock_wait_s: float = LOCK_WAIT_S, config: Path = USER_CONFIG) -> dict[str, Any]:
    """One backstop pass. Never raises; the returned record carries ``status`` and ``exit``."""
    t0 = time.monotonic()
    now = now or iso(now_utc())
    status = status or status_path(env)
    problems: list[str] = []
    warnings: list[str] = []
    counts: dict[str, Any] = {}
    stores: Stores | None = None
    try:
        stores = resolve_stores(env, config)
        if stores is None:
            problems.append("stores_unconfigured")
        elif not stores.meta.is_dir():
            problems.append("meta_store_missing")
        else:
            counts = _evaluate(stores, env, now, lock_wait_s, problems, warnings)
    except Exception as exc:  # noqa: BLE001 — the backstop reports every failure instead of raising
        problems.append(f"backstop_error: {type(exc).__name__}: {exc}")
    state = "fail" if problems else "warn" if warnings else "ok"
    rec: dict[str, Any] = {"at": now, "status": state, "problems": problems, "warnings": warnings,
                           "counts": counts, "elapsed_ms": round((time.monotonic() - t0) * 1000),
                           "exit": {"ok": EXIT_OK, "warn": EXIT_WARN, "fail": EXIT_FAIL}[state]}
    _record(rec, status, stores)
    return rec


def _record(rec: dict[str, Any], status: Path, stores: Stores | None) -> None:
    line = f"backstop {rec['status']}: " + ("; ".join(rec["problems"] + rec["warnings"]) or "ok")
    try:
        prev = read_json(status) if status.is_file() else {}
    except (OSError, ValueError):
        prev = {}
    history = ([{k: rec[k] for k in ("at", "status", "problems", "warnings")}] + list(prev.get("history") or []))
    doc = {"last_run": rec["at"], "last_status": rec["status"],
           "last_ok": rec["at"] if rec["status"] != "fail" else prev.get("last_ok"),
           "consecutive_failures": int(prev.get("consecutive_failures", 0)) + 1 if rec["status"] == "fail" else 0,
           "history": history[:HISTORY_MAX]}
    try:
        status.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(status, doc)
    except OSError as exc:
        rec["problems"].append(f"status_unwritable: {type(exc).__name__}")
        rec.update(status="fail", exit=EXIT_FAIL)
        line = f"backstop fail: {'; '.join(rec['problems'])}"
    rec["status_file"] = str(status)
    if stores is not None and stores.meta.is_dir():
        try:
            log_line(stores, line)
        except OSError:
            pass
    if rec["status"] != "ok":
        print(line, file=sys.stderr)


def health(status: Path, *, now: str | None = None, stale_after_h: float = STALE_AFTER_H,
           failure_times: list[str] | None = None) -> dict[str, Any]:
    """The backstop's standing, for the checkpoint report: ``ok``, ``warn``, ``fail``,
    ``stale`` (no success within ``stale_after_h``) or ``never_run``.

    ``failure_times`` are the start times of recorded capture failures. Failures after the
    last pass were never judged by it, so they cap the standing: one or more is at most
    ``warn``, ``FAILURE_STREAK`` or more is ``fail``; a stale "ok" never hides refusals."""
    now_dt = parse_iso(now or iso(now_utc()))
    assert now_dt is not None
    try:
        doc = read_json(status) if status.is_file() else None
    except (OSError, ValueError):
        return {"state": "fail", "reason": "status file unreadable"}
    if not doc:
        return {"state": "never_run"}
    last_ok = parse_iso(doc.get("last_ok"))
    if last_ok is None or now_dt - last_ok > timedelta(hours=stale_after_h):
        return {"state": "stale", "last_ok": doc.get("last_ok"), "last_status": doc.get("last_status"),
                "consecutive_failures": doc.get("consecutive_failures", 0)}
    out = {"state": doc.get("last_status", "fail"), "last_ok": doc.get("last_ok"),
           "consecutive_failures": doc.get("consecutive_failures", 0)}
    last_run = parse_iso(doc.get("last_run")) or last_ok
    unseen = [t for t in failure_times or [] if (ts := parse_iso(t)) is not None and ts > last_run]
    if unseen:
        out["capture_failures_since_last_run"] = len(unseen)
        worst = "fail" if len(unseen) >= pipeline.FAILURE_STREAK else "warn"
        rank = {"ok": 0, "warn": 1, "fail": 2}
        if rank.get(worst, 2) > rank.get(out["state"], 2):
            out["state"] = worst
    return out


def task_xml(start: str = TASK_START) -> str:
    """Task Scheduler definition for the backstop: daily, catch-up after a missed run,
    the signed-in user's own token (no stored password), no elevation, one instance,
    30-minute limit. Printed for review; this module never registers it."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    exe = windowless if windowless.is_file() else exe
    script = Path(__file__).resolve().with_name("capture.py")
    args = f'"{script}" backstop'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Evidence capture maintenance and expiry backstop (capture.py backstop)</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{escape(start)}:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(exe))}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(script.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def main_backstop(env: dict[str, str] | None = None) -> int:
    rec = run_backstop(dict(os.environ) if env is None else env)
    return int(rec["exit"])

#!/usr/bin/env python3
"""``UserPromptSubmit`` hook: capture the start of an episode (plan §9).

Fail-open: whatever happens, nothing is printed and the exit code is 0, so the prompt
is never blocked or altered. A failure is one line in the capture log (or the temp
fallback log). Maintenance is launched detached after the start has been recorded.
"""
from __future__ import annotations

import os
import sys


def _log_import_failure(hook: str, exc: BaseException, env: dict[str, str]) -> None:
    """The one line an import failure leaves: in the capture log through ``store`` when that
    imports, else in the temp fallback log ``store.log_line`` falls back to. Never raises."""
    message = f"{hook}: import failed: {type(exc).__name__}: {exc}"
    try:
        from store import log_line, resolve_stores

        log_line(resolve_stores(env), message)
        return
    except Exception:  # noqa: BLE001 — store itself may be what failed to import
        pass
    try:
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tmp = env.get("TEMP") or env.get("TMPDIR") or "."
        with open(os.path.join(tmp, "ec-capture-failures.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:  # noqa: BLE001 — fail-open (plan §9): no log is still exit 0
        pass


def main() -> int:
    env = dict(os.environ)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import json

        import pipeline
        from store import log_line, resolve_stores
    except Exception as exc:  # noqa: BLE001 — an import failure must not reach the agent
        _log_import_failure("start", exc, env)
        return 0
    try:
        if pipeline.disabled(env):
            return 0
        raw = sys.stdin.buffer.read()
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            log_line(resolve_stores(env), "start: hook input is not JSON")
            return 0
        if not isinstance(payload, dict):
            log_line(resolve_stores(env), "start: hook input is not an object")
            return 0
        result = pipeline.run_start(payload, env)
        stores = result.get("stores")
        if stores is not None and pipeline.maintenance_due(stores):
            pipeline.spawn_maintenance(env)
    except Exception as exc:  # noqa: BLE001 — fail-open (plan §9)
        try:
            log_line(resolve_stores(env), f"start: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.exit(code)

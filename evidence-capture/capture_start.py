#!/usr/bin/env python3
"""``UserPromptSubmit`` hook: capture the start of an episode (plan §9).

Fail-open: whatever happens, nothing is printed and the exit code is 0, so the prompt
is never blocked or altered. A failure is one line in the capture log (or the temp
fallback log). Maintenance is launched detached after the start has been recorded.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    env = dict(os.environ)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import json

        import pipeline
        from store import log_line, resolve_stores
    except Exception:  # noqa: BLE001 — an import failure must not reach the agent
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

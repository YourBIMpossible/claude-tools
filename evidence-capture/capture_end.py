#!/usr/bin/env python3
"""``Stop`` hook: join, link, screen and review the session's open episode (plan §9).

Fail-open: nothing is printed and the exit code is 0 whatever happens; a failure is one
line in the capture log. An episode this hook cannot finish is finished by maintenance.
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
            log_line(resolve_stores(env), "end: hook input is not JSON")
            return 0
        if not isinstance(payload, dict):
            log_line(resolve_stores(env), "end: hook input is not an object")
            return 0
        pipeline.run_end(payload, env)
    except Exception as exc:  # noqa: BLE001 — fail-open (plan §9)
        try:
            log_line(resolve_stores(env), f"end: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.exit(code)

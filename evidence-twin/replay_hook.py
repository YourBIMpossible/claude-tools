#!/usr/bin/env python3
"""UserPromptSubmit replay hook for Twin runs.

    python replay_hook.py <brief-file | ->

Emits the brief in the file through ``additionalContext``, exactly as the
Evidence Compiler adapter does (same JSON shape, UTF-8, ``ensure_ascii=False``).
With ``-`` (the no-brief arm) it emits nothing, so both arms run the same hook.
Reads and discards the hook's stdin. Fail-open: any error emits nothing and
exits 0, like the production adapter.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    try:
        sys.stdin.read()
    except (OSError, ValueError):
        pass
    try:
        if len(argv) != 1 or argv[0] == "-":
            return 0
        brief = Path(argv[0]).read_text(encoding="utf-8")
        if not brief:
            return 0
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": brief}}
        sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - fail-open, as in production
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

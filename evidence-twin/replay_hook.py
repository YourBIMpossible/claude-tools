#!/usr/bin/env python3
"""UserPromptSubmit replay hook for Twin runs.

    python replay_hook.py <brief-file | ->

Emits the brief in the file through ``additionalContext``, exactly as the
Evidence Compiler adapter does (same JSON shape, UTF-8, ``ensure_ascii=False``).
With ``-`` (the no-brief arm) it emits nothing, so both arms run the same hook.
Reads and discards the hook's stdin. Fail-open: any error emits nothing and
exits 0, like the production adapter.

Once the brief is written to stdout the hook leaves ``<brief-file>.delivered``
holding the sha256 of the emitted brief (SA11-CT-40): a fail-open hook that
emitted nothing leaves no marker, and twin marks that run invalid. A marker that
cannot be written is fail-open too.

"Delivered" means *emitted by the hook*, nothing more. The marker proves this
process wrote that exact brief to its stdout; it does not prove the CLI accepted
the hook output or that the brief reached the model's context. It catches a hook
that silently did nothing, not a CLI that dropped what the hook gave it.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

DELIVERED_SUFFIX = ".delivered"


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
        Path(argv[0] + DELIVERED_SUFFIX).write_text(hashlib.sha256(brief.encode("utf-8")).hexdigest() + "\n",
                                                    encoding="utf-8")
    except Exception:  # noqa: BLE001 - fail-open, as in production
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

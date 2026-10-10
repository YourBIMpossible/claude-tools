#!/usr/bin/env python3
"""A zero-cost stand-in for ``claude -p`` for Twin dry runs and tests.

Accepts the harness's command line, reads the target file named in the
``FAKE_CLAUDE_TARGET`` environment variable, appends one line to it (a real edit,
so the archived diff is not empty) and prints a stream-json transcript of that
turn: init, a Read, an Edit, and a result. Token counts are fixed and small.
``FAKE_CLAUDE_ERROR=1`` emits an errored result instead, like an auth failure.
No network, no model, no session files.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 20}


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    model = argv[argv.index("--model") + 1] if "--model" in argv else "unknown"
    target = os.environ.get("FAKE_CLAUDE_TARGET", "README.md")
    path = Path.cwd() / target
    if os.environ.get("FAKE_CLAUDE_ERROR"):  # an auth failure as the real CLI reports it
        emit({"type": "system", "subtype": "init", "cwd": str(Path.cwd()), "model": model,
              "claude_code_version": "fake-0"})
        emit({"type": "assistant", "parent_tool_use_id": None, "message": {
            "id": "msg_fake_err", "model": "<synthetic>", "content": [{"type": "text", "text": "401"}]}})
        emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "usage": {},
              "terminal_reason": "api_error", "api_error_status": 401})
        return 0
    emit({"type": "system", "subtype": "init", "cwd": str(Path.cwd()), "model": model,
          "claude_code_version": "fake-0", "tools": ["Read", "Edit"], "mcp_servers": [], "skills": [],
          "agents": [], "memory_paths": [], "permissionMode": "dontAsk",
          "plugins": [{"name": "telemetry", "path": "builtin", "source": "telemetry@builtin"}]})
    emit({"type": "assistant", "parent_tool_use_id": None, "message": {
        "id": "msg_fake_1", "model": model, "usage": USAGE,
        "content": [{"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": str(path)}}]}})
    emit({"type": "user", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "(file)", "is_error": False}]}})
    with path.open("a", encoding="utf-8") as fh:
        fh.write("# fake edit\n")
    emit({"type": "assistant", "parent_tool_use_id": None, "message": {
        "id": "msg_fake_2", "model": model, "usage": USAGE,
        "content": [{"type": "tool_use", "id": "tu_2", "name": "Edit",
                     "input": {"file_path": str(path), "old_string": "", "new_string": "# fake edit"}}]}})
    emit({"type": "user", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu_2", "content": "ok", "is_error": False}]}})
    emit({"type": "assistant", "parent_tool_use_id": None, "message": {
        "id": "msg_fake_3", "model": model, "usage": USAGE, "content": [{"type": "text", "text": "Done."}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0,
          "usage": USAGE})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

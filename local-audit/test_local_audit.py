#!/usr/bin/env python3
"""Guard test: the local-audit lane is census/report-only by default.

Proves three things without needing Ollama or a target repo:
  1. `local_audit.MODE == "census"` and the model instruction carries the
     explicit census-only directive.
  2. Neither tool source contains a remediation/apply code path (no --fix/--apply
     flag, no repository-mutating git subcommand).
  3. Neither tool opens a file for writing *inside a target repo* path.

Run: `python test_local_audit.py`  (exit 0 = guard holds, 1 = violated)
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    mark = "ok " if cond else "FAIL"
    print(f"  {mark} {msg}")
    if not cond:
        FAILS.append(msg)


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # safe: entrypoints are guarded by __main__
    return mod


def main() -> int:
    la = load("local_audit")

    # 1. Declared mode + model instruction.
    check(getattr(la, "MODE", None) == "census", 'local_audit.MODE == "census"')
    check("Census and report only" in la.CENSUS_ONLY,
          "CENSUS_ONLY states 'Census and report only'")
    check(la.CENSUS_ONLY in la.SYSTEM,
          "SYSTEM prompt embeds the census-only directive")
    check(re.search(r"do not produce\b.*\b(edits|patches)", la.SYSTEM, re.I | re.S) is not None,
          "SYSTEM forbids emitting edits/patches")

    # 2 & 3. Static source checks on both tools.
    # Mutating git subcommands and remediation flags must not appear in source.
    forbidden_git = re.compile(
        r"""["']\s*(commit|apply|add|push|reset|checkout|restore|rm|revert|stash|
            clean|merge|rebase)\b""", re.X)
    # A *defined* remediation flag (argparse), not the word in a comment/docstring.
    forbidden_flags = re.compile(
        r"""add_argument\(\s*["']--(fix|apply|write|remediate|auto-fix)\b""", re.X)
    for fname in ("local_audit", "slop_prepass"):
        src = (HERE / f"{fname}.py").read_text(encoding="utf-8")
        # strip comments/docstrings-ish: keep it simple, scan raw but allow the
        # word inside prose by requiring the git-token to be a quoted arg.
        gituse = [m.group(0) for m in forbidden_git.finditer(src)]
        check(not gituse, f"{fname}.py has no repo-mutating git subcommand {gituse or ''}")
        flaguse = [m.group(0) for m in forbidden_flags.finditer(src)]
        check(not flaguse, f"{fname}.py defines no remediation flag {flaguse or ''}")
        # No write-mode file open whose path is built from the target repo.
        bad_write = re.search(r"\(\s*repo\b[^)]*\)\s*\.(write_text|open\([^)]*['\"][wax])", src)
        check(bad_write is None, f"{fname}.py never opens a target-repo file for writing")

    print(f"\n{'PASS' if not FAILS else 'FAIL'}: census-only guard "
          f"({'held' if not FAILS else f'{len(FAILS)} violation(s)'})")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

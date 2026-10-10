#!/usr/bin/env python3
"""Evidence capture command line (plan §11 step 4).

  init                 create the stores named by the environment or the user config
  maintain [--quiet]   run maintenance now (the start hook launches it detached);
                       exit 0 ok, 3 warn, 4 fail, judged as the backstop judges it
  report               write the next §12 checkpoint (JSON and markdown) and commit it
  fn-audit sheet       write the next blind FN-audit sheet (plan §5.3)
  fn-audit score K     score sealed sheet K
  fn-audit amended K --note TEXT --tool-commit SHA
                       record that the screen was amended after audit K
  admit EPISODE        first-batch admission check for one episode
  expire               delete expired snapshots and payloads now
  backstop [--health | --task-xml PATH]
                       scheduled maintenance and expiry backstop (exit 0 ok, 3 warn, 4 fail);
                       --task-xml writes a Task Scheduler definition for review only
  dry --out DIR [-n N] run the real hooks against synthetic fixtures in DIR only

Stores come from ``EC_CAPTURE_META``/``EC_CAPTURE_CONTENT`` or the user config file
``evidence-capture.json`` in the client's settings directory. Nothing here registers a
hook; enabling capture is a separate, owner-approved step.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit  # noqa: E402
import pipeline  # noqa: E402
from report import write_checkpoint  # noqa: E402
from review import expire  # noqa: E402
from store import Stores, init_meta_repo, init_stores, resolve_stores  # noqa: E402


def _stores(env: dict[str, str]) -> Stores:
    st = resolve_stores(env)
    if st is None:
        raise SystemExit("capture stores are not configured (EC_CAPTURE_META / EC_CAPTURE_CONTENT or user config)")
    return st


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="capture.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    mp = sub.add_parser("maintain")
    mp.add_argument("--quiet", action="store_true")
    sub.add_parser("report")
    sub.add_parser("rescreen", help="re-join and re-screen every joined episode; commit a corrections record")
    fa = sub.add_parser("fn-audit")
    fsub = fa.add_subparsers(dest="action", required=True)
    fsub.add_parser("sheet")
    sc = fsub.add_parser("score")
    sc.add_argument("k", type=int)
    am = fsub.add_parser("amended")
    am.add_argument("k", type=int)
    am.add_argument("--note", required=True)
    am.add_argument("--tool-commit", required=True)
    ad = sub.add_parser("admit")
    ad.add_argument("episode")
    sub.add_parser("expire")
    bp = sub.add_parser("backstop")
    bp.add_argument("--health", action="store_true", help="print the backstop's standing and exit")
    bp.add_argument("--task-xml", type=Path, help="write a Task Scheduler definition for review; never registers it")
    dp = sub.add_parser("dry")
    dp.add_argument("--out", required=True, type=Path)
    dp.add_argument("-n", type=int, default=10)
    args = ap.parse_args(argv)
    env = dict(os.environ)

    if args.cmd == "dry":
        import dryrun
        try:
            _print(dryrun.run_dry(args.out, args.n))
        except dryrun.DryError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "backstop":
        import backstop
        if args.task_xml:
            args.task_xml.write_text(backstop.task_xml(), encoding="utf-16")
            print(args.task_xml)
            return 0
        if args.health:
            st = _stores(env)
            _print(backstop.health(backstop.status_path(env), failure_times=pipeline.capture_failure_times(st, env)))
            return 0
        rec = backstop.run_backstop(env)
        _print(rec)
        return int(rec["exit"])
    st = _stores(env)
    if args.cmd == "init":
        init_stores(st)
        init_meta_repo(st)
        _print(pipeline.full_store_check(st))
        return 0
    if args.cmd == "maintain":
        res = pipeline.run_maintain(st, env)
        problems, warnings = pipeline.maintain_findings(res)
        res.update(status="fail" if problems else "warn" if warnings else "ok", problems=problems, warnings=warnings)
        if not args.quiet:
            _print(res)
        import backstop
        return backstop.EXIT_FAIL if problems else backstop.EXIT_WARN if warnings else backstop.EXIT_OK
    if args.cmd == "report":
        commit, _ = pipeline.tool_identity()
        import backstop
        path, ck = write_checkpoint(st, tool_commit=commit, backstop=backstop.health(
            backstop.status_path(env), failure_times=pipeline.capture_failure_times(st, env)))
        _print({"checkpoint": str(path), "episodes": ck["denominator"]["episodes_total"],
                "provisional_candidates": ck["funnel"]["provisional_candidates"]})
        return 0
    if args.cmd == "rescreen":
        import rescreen
        res = rescreen.run_rescreen(st, env)
        _print({k: v for k, v in res.items() if k != "changed"} | {"changed": len(res.get("changed", []))})
        return 0 if res.get("action") != "skipped" else 1
    if args.cmd == "fn-audit":
        try:
            if args.action == "sheet":
                res = audit.build_sheet(st)
            elif args.action == "score":
                res = audit.score(st, args.k)
            else:
                res = audit.record_amendment(st, args.k, args.note, args.tool_commit)
        except audit.AuditError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        _print(res)
        return 0
    if args.cmd == "admit":
        res = audit.admission_check(st, args.episode)
        _print(res)
        return 0 if res["admitted"] else 1
    if args.cmd == "expire":
        _print(expire(st))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

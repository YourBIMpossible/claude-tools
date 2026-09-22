"""Replay stored Evidence Compiler packets through two ripgrep collector versions.

Read-only. For each human-candidate packet whose repository still exists, the
packet's extracted symbols are searched against the *current* checkout by the
old collector (twice, to measure run-to-run noise) and the new one. Reports
completion outcomes, p50/p95 latency, and retained-file-set differences.

Usage:
  py replay_rg.py --old <old ripgrep.py> --new-src <worktree>/src --out <dir> [--label idle] [--budget-ms 1500]

Caveat: repositories have moved since packets were captured, so this compares
the two versions against each other on today's code, not against history.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import measure_relevance as mr  # noqa: E402


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new-src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="idle")
    ap.add_argument("--budget-ms", type=int, default=1500)
    args = ap.parse_args()

    sys.path.insert(0, args.new_src)
    from evidence_compiler.collectors.base import CollectorContext
    from evidence_compiler.collectors import ripgrep as new_rg

    # The old module uses relative imports; load it inside the same package.
    old_rg = _load_module("evidence_compiler.collectors._ripgrep_old", args.old)

    packets, _ = mr.load_packets()
    rows = []
    issues: Counter = Counter()
    for p in packets:
        if p.traffic != "candidate" or not Path(p.repo_root).is_dir():
            continue
        raw = mr.load_packet_json(p.store_root, f"*{p.packet_id}*.json")
        if raw is None:
            issues["missing_packet_json"] += 1
            continue
        symbols = raw.get("task", {}).get("extracted_symbols") or []
        if not symbols:
            continue

        def run(mod):
            ctx = CollectorContext(
                repository_root=p.repo_root, cwd=p.repo_root, prompt_text="", prompt_hash="replay",
                timeout_ms=args.budget_ms, extracted_symbols=symbols, config={},
            )
            t = time.perf_counter()
            res = mod.RipgrepCollector().collect(ctx)
            ms = (time.perf_counter() - t) * 1000
            files = {c.references[0].rsplit(":", 1)[0] for c in res.items if c.references}
            return {"status": res.status, "ms": ms, "files": files,
                    "timeout": res.diagnostic.get("symbols_timeout", 0),
                    "capped": res.diagnostic.get("symbols_not_searched", 0),
                    "matched": res.diagnostic.get("symbols_matched", 0)}

        o1, o2, n = run(old_rg), run(old_rg), run(new_rg)
        rows.append({
            "packet_id": p.packet_id, "repo": Path(p.repo_root).name, "symbols": len(symbols),
            "old_status": o1["status"], "new_status": n["status"],
            "old_ms": round(o1["ms"], 1), "new_ms": round(n["ms"], 1),
            "old_timeout_symbols": o1["timeout"], "new_timeout_symbols": n["timeout"],
            "old_matched": o1["matched"], "new_matched": n["matched"],
            "old_not_searched": o1["capped"], "new_not_searched": n["capped"],
            "old_vs_old_same_files": o1["files"] == o2["files"],
            "old_vs_new_same_files": o1["files"] == n["files"],
            "new_files_missing_from_both_old": len(o1["files"] | o2["files"]) and len((o1["files"] | o2["files"]) - n["files"]),
        })

    summary = {
        "label": args.label, "budget_ms": args.budget_ms, "packets": len(rows),
        "old_status": dict(Counter(r["old_status"] for r in rows)),
        "new_status": dict(Counter(r["new_status"] for r in rows)),
        "old_ms_p50": _pct([r["old_ms"] for r in rows], 0.5), "old_ms_p95": _pct([r["old_ms"] for r in rows], 0.95),
        "new_ms_p50": _pct([r["new_ms"] for r in rows], 0.5), "new_ms_p95": _pct([r["new_ms"] for r in rows], 0.95),
        "packets_with_timeouts_old": sum(1 for r in rows if r["old_timeout_symbols"]),
        "packets_with_timeouts_new": sum(1 for r in rows if r["new_timeout_symbols"]),
        "symbols_timeout_old": sum(r["old_timeout_symbols"] for r in rows),
        "symbols_timeout_new": sum(r["new_timeout_symbols"] for r in rows),
        "symbols_not_searched_old": sum(r["old_not_searched"] for r in rows),
        "symbols_not_searched_new": sum(r["new_not_searched"] for r in rows),
        "symbols_matched_old": sum(r["old_matched"] for r in rows),
        "symbols_matched_new": sum(r["new_matched"] for r in rows),
        "file_sets_equal_old_vs_old": sum(r["old_vs_old_same_files"] for r in rows),
        "file_sets_equal_old_vs_new": sum(r["old_vs_new_same_files"] for r in rows),
        "issues": dict(issues),
        "complete": not issues,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"replay_rg_{args.label}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

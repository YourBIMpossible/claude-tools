"""Measure graphify retrieval recall for a fixed query set and record a baseline.

Why this exists: the reranker is deferred until there is a number to beat.
Tuning retrieval without a metric is guessing, so this script produces the
metric first and stores it as a versioned baseline.

What it measures, per query:
  hit          - did any expected source file appear in the returned nodes,
                 within the token budget the agent would actually use
  rank         - 1-based position of the first expected file (None on a miss)
  found/expect - how many of the expected files came back (multi-file queries)

Aggregates are reported overall AND split by phrasing tag. The lexical half is
easy (question words match the filename); the intent half is the honest signal.

Usage:
  python measure_recall.py                       # measure, print, write baseline
  python measure_recall.py --compare <file.json> # measure and diff vs a baseline
  python measure_recall.py --budget 4000         # non-default retrieval budget
  python measure_recall.py --no-write            # print only

Ground truth is validated against graph.json before any query runs: a typo'd
path would otherwise show up as a retrieval failure and send you debugging the
wrong thing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Bring your own query set (see recall/README.md for the schema). Not tracked.
QUERIES = Path(os.environ.get("RECALL_QUERIES", HERE / "queries.json"))
# Resolve graphify from PATH; override with $env:GRAPHIFY_EXE if not on PATH.
GRAPHIFY = os.environ.get("GRAPHIFY_EXE", "graphify")

# Example graphify NODE line:
#   "NODE MyClass.Method() [src=my.module/MyClass.cs loc=L17 community=core]"
NODE_RE = re.compile(r"^NODE\s+(?P<label>.*?)\s+\[src=(?P<src>[^\s\]]+)")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_spec() -> dict:
    return json.loads(QUERIES.read_text(encoding="utf-8"))


def graph_source_files(graph_path: Path) -> set[str]:
    g = json.loads(graph_path.read_text(encoding="utf-8"))
    return {n["source_file"] for n in g["nodes"] if n.get("source_file")}


def run_query_raw(repo: Path, question: str, budget: int) -> str:
    """Run `graphify query` and return its raw stdout, unparsed."""
    env = dict(os.environ)
    env["GRAPHIFY_QUERY_LOG_DISABLE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    proc = subprocess.run(
        [str(GRAPHIFY), "query", question, "--budget", str(budget)],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"graphify query failed ({proc.returncode}) for {question!r}: "
            f"{proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def parse_nodes(stdout: str) -> list[dict]:
    """Parse NODE lines in output order. NOT de-duplicated by file."""
    nodes = []
    for line in stdout.splitlines():
        m = NODE_RE.match(line.strip())
        if m:
            nodes.append({"label": m.group("label"), "src": m.group("src")})
    return nodes


def is_truncated(stdout: str) -> bool:
    return "TRUNCATED" in stdout


def dedup_files(nodes: list[dict]) -> list[str]:
    """Collapse a node list to first-occurrence-order unique source files."""
    ordered: list[str] = []
    for n in nodes:
        if n["src"] not in ordered:
            ordered.append(n["src"])
    return ordered


def run_query(repo: Path, question: str, budget: int) -> list[str]:
    """Return the ordered list of source files graphify surfaced for a question."""
    return dedup_files(parse_nodes(run_query_raw(repo, question, budget)))


def aggregate(rows: list[dict]) -> dict:
    """Roll up hit/rank/found/expected rows into hit-rate, recall, MRR, median rank.

    Rows need only `id`, `hit`, `rank`, `found`, `expected` — callers may carry
    extra keys (phrasing, question, ...) and they're ignored here.
    """
    if not rows:
        return {"queries": 0}
    hit = [r for r in rows if r["hit"]]
    ranks = [r["rank"] for r in hit]
    return {
        "queries": len(rows),
        "hit_rate": round(len(hit) / len(rows), 3),
        "file_recall": round(
            sum(r["found"] for r in rows) / sum(r["expected"] for r in rows), 3
        ),
        "mrr": round(sum(1 / r for r in ranks) / len(rows), 3) if ranks else 0.0,
        "median_rank": (sorted(ranks)[len(ranks) // 2] if ranks else None),
        "misses": [r["id"] for r in rows if not r["hit"]],
    }


def measure(spec: dict, budget: int) -> dict:
    repo = Path(spec["repo"])
    graph = repo / spec["graph"]

    known = graph_source_files(graph)
    bad = sorted(
        {p for q in spec["queries"] for p in q["expect"] if p not in known}
    )
    if bad:
        raise SystemExit(
            "Ground truth references files that are not in the graph:\n  "
            + "\n  ".join(bad)
            + "\nFix csharp-queries.json (or rebuild the graph) before measuring."
        )

    results = []
    for q in spec["queries"]:
        got = run_query(repo, q["question"], budget)
        pos = {f: i + 1 for i, f in enumerate(got)}
        hits = [f for f in q["expect"] if f in pos]
        ranks = sorted(pos[f] for f in hits)
        results.append(
            {
                "id": q["id"],
                "phrasing": q["phrasing"],
                "question": q["question"],
                "expect": q["expect"],
                "returned": len(got),
                "hit": bool(hits),
                "rank": ranks[0] if ranks else None,
                "found": len(hits),
                "expected": len(q["expect"]),
                "missed": [f for f in q["expect"] if f not in pos],
            }
        )

    graph_doc = json.loads(graph.read_text(encoding="utf-8"))
    return {
        "measured_at": now_iso(),
        "set_version": spec["set_version"],
        "budget": budget,
        "repo": str(repo),
        "graph_nodes": len(graph_doc["nodes"]),
        "graph_built_at_commit": graph_doc.get("built_at_commit"),
        "overall": aggregate(results),
        "by_phrasing": {
            tag: aggregate([r for r in results if r["phrasing"] == tag])
            for tag in sorted({r["phrasing"] for r in results})
        },
        "results": results,
    }


def fmt(a: dict) -> str:
    return (
        f"hit {a['hit_rate']:.3f}  file-recall {a['file_recall']:.3f}  "
        f"MRR {a['mrr']:.3f}  median rank {a['median_rank']}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=2000)
    ap.add_argument("--compare", type=Path)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    spec = load_spec()
    b = measure(spec, args.budget)

    print(f"graph {b['graph_nodes']} nodes @ {(b['graph_built_at_commit'] or '?')[:8]}"
          f"  budget {b['budget']}  set v{b['set_version']}")
    print(f"  overall  {fmt(b['overall'])}  ({b['overall']['queries']} queries)")
    for tag, a in b["by_phrasing"].items():
        print(f"  {tag:<8} {fmt(a)}  ({a['queries']} queries)")
    if b["overall"]["misses"]:
        print("  misses: " + ", ".join(b["overall"]["misses"]))

    if args.compare:
        old = json.loads(args.compare.read_text(encoding="utf-8"))
        if old["set_version"] != b["set_version"]:
            print(
                f"\n! set_version {old['set_version']} -> {b['set_version']}: "
                "the query set changed, so these numbers are not comparable."
            )
        print(f"\nvs {args.compare.name} ({old['measured_at']}):")
        for scope in ("overall", *b["by_phrasing"]):
            o = old["overall"] if scope == "overall" else old["by_phrasing"].get(scope)
            n = b["overall"] if scope == "overall" else b["by_phrasing"][scope]
            if not o:
                continue
            for k in ("hit_rate", "file_recall", "mrr"):
                d = n[k] - o[k]
                print(f"  {scope:<8} {k:<11} {o[k]:.3f} -> {n[k]:.3f}  {d:+.3f}")

    if not args.no_write:
        out = args.out or HERE / f"baseline-v{b['set_version']}-budget{b['budget']}.json"
        out.write_text(json.dumps(b, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

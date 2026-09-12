"""Reranker experiment: BM25 over identifier tokens, intent queries only, 2000-budget.

Scope, per the recorded decision: this evaluates a lexical (BM25) reranker
against the baseline's 11 "intent" queries at the 2000-token budget -- the
constrained condition where BASELINE.md showed the correct file already
inside the traversal but ranked too low to survive truncation. The 13
"lexical" queries are excluded: BASELINE.md showed their failures don't move
with budget at all (0.615 hit rate at both 2000 and 8000 tokens), so they're
a seeding/extraction problem a reranker cannot touch by construction.

Why BM25 and not embeddings: graph.json carries no prose per node (checked --
the full field set for code nodes is label/source_file/source_location/
community/community_name/metadata.namespace/type; edge relations are
structural verbs like calls/imports/contains, not behavioral vocabulary).
The only text available to rerank against is identifier-derived: label, file
path, community name. BM25 is the standard second-stage reranker for exactly
that kind of sparse lexical corpus, needs no model/network/new dependency,
and -- importantly -- keeps this experiment a genuinely different technique
from the embeddings/RRF approach that was deferred pending evidence like
this. It's the honest lower bound: if lexical reranking doesn't move the
intent rows, that's the evidence for whether embeddings are worth reaching
for next, not an assumption.

Method:
  1. For each intent query, ask graphify for the raw node list at the 2000
     budget (this is the exact list the current pipeline ships). Its length
     is `node_cap` -- how many raw NODE lines fit in 2000 tokens for THIS
     query, per graphify's own truncation, not an assumed tokens/node ratio.
  2. Ask graphify for the (near-)full candidate pool at a much larger budget.
     Same seeds, same BFS order -- budget only changes the truncation point
     (verified: node_cap-sized prefix of the pool matches the 2000-budget
     list exactly, or this script says so).
  3. Score every pooled node against the query with BM25 (k1=1.5, b=0.75)
     over tokenized label+source_file+community. Stable-sort by score so a
     0.0-vs-0.0 tie keeps the original BFS order -- the reranker only moves
     nodes it has actual lexical signal for.
  4. Truncate the reranked pool to the SAME node_cap and compare hit/rank/MRR
     against the untouched 2000-budget baseline. Same query, same effective
     budget, only the ordering differs.

A query only tests the reranker if the expected file is somewhere in the
full pool at all (`reachable_in_pool`). If it isn't, no amount of reordering
can produce a hit -- that's a seeding failure wearing an intent-query
disguise, not a ranking failure, and BASELINE.md already flagged the same
pattern for the lexical set. Both the all-11 and reachable-only aggregates
are reported so this distinction is visible instead of silently depressing
the "did reranking help" number.

Ground truth (expect: [...] source-file paths) is frozen at set_version 1.
This script never edits csharp-queries.json.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from measure_recall import (  # noqa: E402
    aggregate,
    dedup_files,
    graph_source_files,
    load_spec,
    now_iso,
    parse_nodes,
    run_query_raw,
)

TARGET_BUDGET = 2000
POOL_BUDGET = 30000
POOL_RETRY_BUDGET = 150000

_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "for", "and", "or", "that", "this", "these", "those",
    "it", "its", "with", "as", "at", "by", "from", "how", "what", "when",
    "where", "which", "who", "whom", "does", "do", "did", "every", "each",
    "into", "out", "up", "down", "not", "no", "so", "than", "then", "there",
    "here", "if", "just",
}
_BOUNDARY = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    toks = []
    for part in _BOUNDARY.split(text):
        if not part:
            continue
        for sub in _CAMEL.split(part):
            if sub:
                toks.append(sub.lower())
    return [t for t in toks if len(t) > 1 and t not in _STOP]


def node_doc(node: dict) -> list[str]:
    return tokenize(f"{node.get('label', '')} {node.get('src', '')}")


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.n = len(docs)
        self.k1 = k1
        self.b = b
        self.doc_len = [len(d) for d in docs]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.tf = [{} for _ in docs]
        df: dict[str, int] = {}
        for i, d in enumerate(docs):
            for t in d:
                self.tf[i][t] = self.tf[i].get(t, 0) + 1
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        self.idf = {
            t: math.log((self.n - n + 0.5) / (n + 0.5) + 1) for t, n in df.items()
        }

    def score(self, query_tokens: list[str], i: int) -> float:
        tf = self.tf[i]
        if not tf:
            return 0.0
        dl = self.doc_len[i]
        s = 0.0
        for t in query_tokens:
            f = tf.get(t)
            if not f:
                continue
            idf = self.idf.get(t, 0.0)
            s += idf * (f * (self.k1 + 1)) / (
                f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
            )
        return s


def full_pool(repo: Path, question: str) -> tuple[list[dict], bool]:
    stdout = run_query_raw(repo, question, POOL_BUDGET)
    if "TRUNCATED" in stdout:
        stdout = run_query_raw(repo, question, POOL_RETRY_BUDGET)
    return parse_nodes(stdout), ("TRUNCATED" in stdout)


def score_row(expect: list[str], files: list[str], qid: str) -> dict:
    pos = {f: i + 1 for i, f in enumerate(files)}
    hits = [f for f in expect if f in pos]
    ranks = sorted(pos[f] for f in hits)
    return {
        "id": qid,
        "found": len(hits),
        "expected": len(expect),
        "hit": bool(hits),
        "rank": ranks[0] if ranks else None,
        "missed": [f for f in expect if f not in pos],
    }


def main() -> int:
    spec = load_spec()
    repo = Path(spec["repo"])
    graph = repo / spec["graph"]

    known = graph_source_files(graph)
    intent = [q for q in spec["queries"] if q["phrasing"] == "intent"]
    bad = sorted({p for q in intent for p in q["expect"] if p not in known})
    if bad:
        raise SystemExit(
            "Ground truth references files not in the graph:\n  " + "\n  ".join(bad)
        )

    rows = []
    order_mismatches = []
    for q in intent:
        raw2000 = parse_nodes(run_query_raw(repo, q["question"], TARGET_BUDGET))
        node_cap = len(raw2000)
        baseline_files = dedup_files(raw2000)

        pool, pool_truncated = full_pool(repo, q["question"])
        pool_files = dedup_files(pool)
        reachable = any(f in pool_files for f in q["expect"])

        # Determinism sanity check: the 2000-budget list should be exactly the
        # node_cap-length prefix of the larger-budget pool (same seeds/BFS
        # order; budget only moves the truncation point).
        if [n["src"] for n in pool[:node_cap]] != [n["src"] for n in raw2000]:
            order_mismatches.append(q["id"])

        docs = [node_doc(n) for n in pool]
        bm25 = BM25(docs)
        qtok = tokenize(q["question"])
        order = sorted(range(len(pool)), key=lambda i: -bm25.score(qtok, i))
        reranked_files = dedup_files([pool[i] for i in order[:node_cap]])

        b_row = score_row(q["expect"], baseline_files, q["id"])
        r_row = score_row(q["expect"], reranked_files, q["id"])
        rows.append({
            "id": q["id"],
            "question": q["question"],
            "expect": q["expect"],
            "node_cap": node_cap,
            "pool_size": len(pool),
            "pool_truncated": pool_truncated,
            "reachable_in_pool": reachable,
            "baseline": b_row,
            "reranked": r_row,
        })
        flag = "" if reachable else "  [unreachable at any budget]"
        print(
            f"  {q['id']}: cap={node_cap:>3} pool={len(pool):>4}  "
            f"baseline rank={str(b_row['rank']):>4}  reranked rank={str(r_row['rank']):>4}{flag}"
        )

    baseline_all = aggregate([r["baseline"] for r in rows])
    reranked_all = aggregate([r["reranked"] for r in rows])
    reach_rows = [r for r in rows if r["reachable_in_pool"]]
    baseline_reach = aggregate([r["baseline"] for r in reach_rows])
    reranked_reach = aggregate([r["reranked"] for r in reach_rows])

    regressions = [r["id"] for r in rows if r["baseline"]["hit"] and not r["reranked"]["hit"]]
    improved = [
        r["id"] for r in rows
        if (not r["baseline"]["hit"] and r["reranked"]["hit"])
        or (r["baseline"]["hit"] and r["reranked"]["hit"] and r["reranked"]["rank"] < r["baseline"]["rank"])
    ]
    unchanged_hits = [
        r["id"] for r in rows
        if r["baseline"]["hit"] and r["reranked"]["hit"] and r["reranked"]["rank"] == r["baseline"]["rank"]
    ]

    doc = {
        "measured_at": now_iso(),
        "set_version": spec["set_version"],
        "budget": TARGET_BUDGET,
        "method": "BM25 k1=1.5 b=0.75 over tokenize(label + source_file); "
                  "stable sort (zero-score ties keep original BFS order); "
                  "capped to each query's own graphify-truncation node count at 2000 tokens.",
        "queries": len(rows),
        "unreachable_queries": [r["id"] for r in rows if not r["reachable_in_pool"]],
        "order_mismatches": order_mismatches,
        "baseline_all_11": baseline_all,
        "reranked_all_11": reranked_all,
        "baseline_reachable_only": baseline_reach,
        "reranked_reachable_only": reranked_reach,
        "regressions": regressions,
        "improved": improved,
        "unchanged_hits": unchanged_hits,
        "rows": rows,
    }

    out = HERE / "rerank-experiment-v1.json"
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    print(f"\nintent queries: {len(rows)}  unreachable: {len(doc['unreachable_queries'])}"
          f"  ({', '.join(doc['unreachable_queries']) or 'none'})")
    if order_mismatches:
        print(f"! determinism check failed for: {', '.join(order_mismatches)}")
    print(f"\nall 11 intent queries:")
    print(f"  baseline  hit {baseline_all['hit_rate']:.3f}  MRR {baseline_all['mrr']:.3f}  median rank {baseline_all['median_rank']}")
    print(f"  reranked  hit {reranked_all['hit_rate']:.3f}  MRR {reranked_all['mrr']:.3f}  median rank {reranked_all['median_rank']}")
    print(f"\nreachable-in-pool only ({len(reach_rows)} queries):")
    print(f"  baseline  hit {baseline_reach['hit_rate']:.3f}  MRR {baseline_reach['mrr']:.3f}  median rank {baseline_reach['median_rank']}")
    print(f"  reranked  hit {reranked_reach['hit_rate']:.3f}  MRR {reranked_reach['mrr']:.3f}  median rank {reranked_reach['median_rank']}")
    print(f"\nimproved: {improved or 'none'}")
    print(f"regressions: {regressions or 'none'}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

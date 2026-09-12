# C# retrieval recall — baseline v1

Measured 2026-08-07 against the Add-Ins graph (6,634 nodes, built at `5160510d`,
graphify 0.9.36, `extract --code-only`, `PYTHONHASHSEED=0`).

Query set: [csharp-queries.json](csharp-queries.json) — 24 questions, 13 tagged
`lexical` (question words appear in the target filename) and 11 tagged `intent`
(phrased the way a person describes the behaviour, filename tokens deliberately
avoided). Ground truth is source-file paths, validated against `graph.json`
before any query runs.

Runner: [measure_recall.py](measure_recall.py). Re-measure with
`python measure_recall.py --compare baseline-v1-budget2000.json`.

## Numbers

| scope | budget | hit rate | file recall | MRR | median rank |
|---|---|---|---|---|---|
| overall | 2000 | 0.500 | 0.483 | 0.273 | 4 |
| lexical | 2000 | 0.615 | 0.588 | 0.392 | 2 |
| intent  | 2000 | 0.364 | 0.333 | 0.133 | 6 |
| overall | 8000 | 0.625 | 0.586 | 0.278 | 4 |
| lexical | 8000 | 0.615 | 0.588 | 0.392 | 2 |
| intent  | 8000 | 0.636 | 0.583 | 0.143 | 22 |

`hit rate` = queries where at least one expected file came back.
`file recall` = fraction of all expected files recovered (multi-file aware).
`MRR` = mean reciprocal rank of the first correct file, averaged over all queries.

## What the two budgets separate

Raising the budget 4× is a clean discriminator, because it changes only how much
of the traversal survives truncation — not what the traversal reaches.

**Intent queries are a ranking problem.** Hit rate goes 0.364 → 0.636 and the
median rank of a hit lands at 22. The correct files are already in the traversal;
they sit below the 2000-token cut. This is exactly the failure a reranker exists
to fix, and it is the half of the set worth optimising.

**Lexical misses are a seeding problem.** Hit rate is identical at both budgets
(0.615), so no amount of extra context recovers them. Probing the seeds shows
why — the entity extractor picks the wrong start nodes and the traversal then
finds only a handful of nodes total:

| query | seeds chosen | nodes found |
|---|---|---|
| "how are sheets sorted" | `Sheets`, `SortedSet` | 22 |
| "which code fixes family parameters" | `Family`, two unrelated test methods | 6 |

A reranker cannot help here: it can only reorder what traversal returned. These
need better seed selection (or a lexical/symbol prefilter), which is a different
piece of work.

## Known weakness in the ground truth

`q12` ("where are print sets synchronized") is scored a miss, but its seeds are
`PrintSetSyncPlan` / `PrintSetSyncPlanner` — the actual implementation. The query
set only lists `SyncPrintSetsCommand.cs`, the thin command wrapper. That is a
ground-truth gap, not a retrieval failure, and probably applies to other
command/implementation splits too.

It is deliberately left uncorrected in v1. Widening `expect` after seeing the
results would tune the metric to the tool. Fix it as a considered pass over the
whole set, bump `set_version`, and re-baseline — never mid-comparison.

## Reranker experiment (v1)

A BM25 lexical reranker was evaluated against the 11 intent queries at the
2000-token budget: hit rate 0.364 → 0.455, MRR 0.133 → 0.155, zero
regressions — kept. 4 of 11 intent queries are unreachable at any budget
(seeding failures, same class as the lexical misses above); 2 more have zero
token overlap with their target and mark the ceiling of what lexical
reranking can do. Full writeup: [RERANK-EXPERIMENT.md](RERANK-EXPERIMENT.md).

## Rules for using this baseline

- Compare only against the same `set_version`, the same budget, and the same
  graph build. All three are recorded in each baseline file.
- A retrieval change earns its keep on the **intent** rows. Overall recall is
  flattered by the lexical half and will move for reasons that have nothing to do
  with ranking quality.
- Rebuilding the graph after a large refactor changes the denominator. Re-measure
  the baseline before judging anything against it.

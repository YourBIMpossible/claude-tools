# graphify recall harness

A small, generic way to put a number on graphify's retrieval quality so tuning
(e.g. a reranker) is measured instead of guessed. The method is repo-agnostic —
bring your own query set and run it against your own graph.

## Files

- `measure_recall.py` — runs a fixed query set through `graphify query`, records
  per-query hit / rank / found-vs-expected, and writes a versioned baseline.
- `rerank_bm25.py` — an optional BM25 rerank pass over the returned nodes, to
  compare against the raw retrieval baseline.

Neither script contains or ships any repo-specific data. The query set and the
generated baselines are yours and are git-ignored.

## Bring your own query set

Create a `queries.json` (or point `RECALL_QUERIES` at one) shaped like:

```json
{
  "queries": [
    {
      "id": "q01",
      "phrasing": "intent",
      "question": "which module validates uploaded files",
      "expect": ["my.module/UploadValidator.ext"]
    }
  ]
}
```

`expect` lists ground-truth **source-file paths** (not node labels — labels change
shape between graphify versions; file paths are stable).

## Run

```bash
# graphify must be on PATH, or set GRAPHIFY_EXE to its full path
export RECALL_QUERIES=./queries.json
python measure_recall.py            # writes a baseline
python rerank_bm25.py               # optional rerank comparison
```

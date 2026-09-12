# Reranker experiment v1 — BM25 on intent queries, 2000-token budget

Scope per the recorded decision: evaluate a reranker against the 11 `intent`
queries only, at the 2000-token budget graphify actually ships with. The 13
`lexical` queries are excluded — [BASELINE.md](BASELINE.md) showed their hit
rate is identical at 2000 and 8000 tokens (0.615 both), so they fail at the
seeding stage, before any reranker gets a candidate list to reorder.

Ground truth identity: **source-file paths**, not node labels — approved as
the benchmark's ground-truth identity because labels are an unstable
implementation detail across graphify node-ID/schema changes (proven by the
0.9.x change) while paths are the durable artifact a developer retrieves.
This is **benchmark set version 1**. `q12`'s known ground-truth gap (command
wrapper listed, not the `PrintSetSyncPlanner` implementation that actually
answers it) is left uncorrected — fixing it after seeing results would
contaminate the baseline. Nothing in this experiment touched
`csharp-queries.json`; `set_version` stays `1`.

Runner: [rerank_bm25.py](rerank_bm25.py) (imports the query/parsing plumbing
from [measure_recall.py](measure_recall.py), which was refactored to expose
raw per-node results — behavior-preserving, reverified against the existing
baseline before this ran). Full data: [rerank-experiment-v1.json](rerank-experiment-v1.json).

## Why BM25, not embeddings

`graph.json` carries no prose per node — the complete field set for a code
node is `label`, `source_file`, `source_location`, `community`,
`community_name`, `metadata.namespace`, `type`; edge relations are structural
verbs (`calls`, `imports`, `contains`), not behavioral vocabulary. The only
text available to rerank against is identifier-derived. BM25 is the standard
second-stage reranker for exactly that kind of sparse lexical corpus — no
model, no network call, no new dependency (hand-rolled, stdlib only) — and
staying lexical keeps this a genuinely different technique from the
embeddings/RRF approach still deferred. If lexical reranking doesn't move a
query, that's evidence for whether embeddings are worth reaching for next,
not an assumption baked into the experiment design.

## Method

For each intent query:

1. Pull the raw node list graphify actually returns at the 2000-token budget.
   Its length is `node_cap` — how many raw NODE lines fit in 2000 tokens for
   *this* query, per graphify's own truncation, not an assumed tokens/node
   ratio.
2. Pull the (near-)full candidate pool at a 30,000-token budget (retried at
   150,000 if still truncated — never triggered; largest pool was 457 nodes).
3. Score every pooled node with BM25 (k1=1.5, b=0.75) over
   `tokenize(label + source_file)` against the tokenized question. Stable
   sort: a 0-vs-0 score tie keeps the original BFS order, so the reranker
   only moves nodes it has actual lexical signal for for — it can't invent
   an improvement out of noise.
4. Truncate the reranked pool to the same `node_cap` and compare hit / rank
   against the untouched 2000-budget baseline. Same query, same effective
   budget, only the ordering differs.

**Determinism check**: the 2000-budget node list should be exactly the
`node_cap`-length prefix of the larger-budget pool, since budget only moves
graphify's truncation point, not seed selection or BFS order. Verified equal
for all 11 queries (`order_mismatches: []` in the JSON) — the comparison is
sound.

A query only tests the reranker if its expected file is somewhere in the
full pool at all (`reachable_in_pool`). If it isn't, no reordering can
produce a hit — that's a seeding failure wearing an intent-query disguise,
the same failure mode BASELINE.md already documented for the lexical set.
Both all-11 and reachable-only aggregates are reported below so this doesn't
silently depress the "did reranking help" number.

## Results

| scope | n | baseline hit | reranked hit | baseline MRR | reranked MRR | median rank (both) |
|---|---|---|---|---|---|---|
| all 11 intent | 11 | 0.364 | **0.455** | 0.133 | **0.155** | 6 |
| reachable-in-pool only | 7 | 0.571 | **0.714** | 0.208 | **0.243** | 6 |

**Regressions: none.** No query that hit at baseline missed after reranking,
and no hit got worse. The stable tiebreak worked as designed — `q21` and
`q23` were already well-ranked (baseline rank 6 and 1) and stayed exactly
there.

**Improved (3 of 7 reachable queries):**

| id | question | baseline rank | reranked rank |
|---|---|---|---|
| q18 | "write the schedule data out to a spreadsheet file" | 24 | **7** |
| q06 | "what runs when Revit starts up and builds the toolbar" | miss | **17 (hit)** |
| q09 | "find elements that appear more than once in the model" | 4 | **3** |

**Unreachable at any budget (4 of 11 — a seeding failure, not a ranking one):**
q13, q15, q20, q24. These queries' expected files never appear in the
candidate pool even at 30,000 tokens; no reranker changes that.

**Reachable but still missed (2 of 11):** q02 and q04. Traced past the
`node_cap` truncation to their position in the *fully* reranked, uncapped
order: `q02`'s target ranks 23rd of 60 unique files reached, `q04`'s ranks
28th of 44. Both have **zero token overlap** with their target's identifiers
— `q02` ("a user picks existing views and copies them into a new sector with
a suffix" → `Tool2DuplicateCollectionCommand.cs`) shares no word with
{tool, duplicate, collection, command}. BM25 correctly scores these 0.0 and
leaves them at their original BFS position; there's no lexical signal to
exploit. This is the concrete shape of the ceiling: paraphrase far enough
from the identifier vocabulary and lexical reranking has nothing to work
with.

## Verdict: keep

Against the standing bar ("keep it only if it improves the measured
retrieval result"): hit rate, file recall, and MRR all improve on the
intent rows at the deployed 2000-token budget, with zero regressions
observed. **Keep the BM25 reranker.**

Scoped explicitly: this result justifies keeping the reranking *technique*
for the cases it can address. It is not yet wired into graphify's own query
path — `rerank_bm25.py` is currently a standalone evaluation harness, not a
production retrieval step. Turning it into one (e.g. a thin wrapper that
pulls a wide pool and reranks before handing results to whatever consumes
`graphify query`) is a separate, small follow-on and is flagged rather than
done here, since it changes a live path rather than just measuring it.

The two failure classes it *can't* address are now evidenced, not assumed:
- **Unreachable queries (seeding)** — same family as the lexical seeding
  failures in BASELINE.md. A different fix: better seed/entity selection or
  a symbol prefilter, not reranking.
- **Zero-overlap reachable queries (paraphrase ceiling)** — the case for
  embeddings, if it's worth pursuing: q02/q04 show concretely that when a
  question shares no vocabulary with the target's identifiers, lexical
  scoring has nothing to grip. This is the evidence the embeddings/RRF tier
  was deferred pending.

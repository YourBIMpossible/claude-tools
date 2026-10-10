# evidence-episodes

Offline, read-only episode builder for Evidence Compiler measurement. Joins
persisted `EvidencePacket`s to the Claude Code transcripts that received them
and writes one factual row per `packet_id`. Never in the prompt path.

```
python build_episodes.py \
    --source w3=<packets-dir> --source live=<packets-dir> \
    --transcripts w3=<transcripts-dir> --transcripts live=<transcripts-dir> \
    [--cohort w3=<manifest.json>] --out <dir>
```

Requires `evidence_compiler` importable (its `src/` on `PYTHONPATH`). Uses its
`EvidencePacket.from_dict`, `classify_traffic`, `render_brief` and `prompt_hash`,
so traffic class and brief text are the compiler's own, not re-derived.

## Output

- `episodes.jsonl` — one row per packet, sorted by `packet_id`, canonical JSON.
- `build.json` — builder sha, compiler version, sha256 of every input tree and
  of `episodes.jsonl`, and the summary (join reasons, brief-found rate, prompt
  anchoring, turn endings; overall, per source, per cohort).

Identical inputs give byte-identical output: no timestamps, paths relative to
their named roots. Freeze growing inputs (a live packet store, live transcripts)
before building, or the build describes a moving target.

## Rows are facts only

`identity`, `retrieved`, `injected`, `prompt`, `observed`, `cost`. No hit,
coverage, waste or usefulness is computed here; scoring belongs downstream.

Every row carries exactly one `join_reason`: `joined`, `missing_packet` (named
in a cohort's lost list; the row is kept), `missing_transcript`,
`brief_not_found`, `ambiguous_transcript_session`, `sha_conflict` (copies of a
packet differ across sources), `parse_failure`, `other`.

## How a turn is located

1. The brief: a `hook_additional_context` attachment whose text equals
   `render_brief(packet)`. An empty brief is never injected, so its absence is
   expected, not a miss.
2. The prompt: the event whose text hashes to the packet's prompt hash. Prompt
   events are user entries, `queued_command` attachments (string or
   content-block prompts) and, as lower-ranked candidates, queue enqueues and
   meta entries. A slash command matches through its typed form
   (`/name args`). `prompt.anchored_by` records how: `hash`, `brief_order`
   (no hash, placed by its brief), or `time` (nearest delivered prompt at or
   before packet creation — the app rewrote the text, e.g. pasted content).
   `hash_matches_packet: false` rows are listed in the summary.
   `prompt.prior_prompts` counts the delivered prompts earlier in the same
   transcript, by origin (`{}` for a session's first prompt).
3. The window: from the anchor to the next prompt, the next packet's start, or
   end of transcript (`observed.ended_by`). Queued prompts delivered as a batch
   share one response: earlier rows end at the next prompt with no assistant
   message, which the summary counts as `turns_without_assistant`.

## Tests

```
python test_build_episodes.py
```

Synthetic tempfile fixtures only; covers every join reason, prompt forms,
enqueue-vs-delivery precedence, usage dedupe by `message.id`, batched turns,
the time fallback, and byte-identical rebuilds.

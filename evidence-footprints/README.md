# evidence-footprints

Footprints v1: **retrieval and observed use** per Evidence Compiler episode.
Reads the rows from `evidence-episodes/build_episodes.py` and compares what each
brief cited with what the agent then touched. It measures whether the agent went
where the brief pointed, not whether the brief helped. Offline and read-only;
never in the prompt path.

```
python footprints.py --episodes <episodes.jsonl> --out <dir> [--no-git]
```

No dependencies beyond the standard library and `git` on `PATH`.

## Which rows are scored

Joined rows with an observed turn that has at least one assistant message.
Queued prompts delivered as a batch share one response, which the episode
builder records on the batch's last row; the other rows of the batch get status
`no_assistant_response` instead of a zero-work score.

## Sets

| Set | Holds |
|---|---|
| B (brief) | paths referenced by selected items (`path:line` → `path`) |
| O (omitted) | paths referenced only by retrieved items that were not selected |
| T (touched) | files read, searched in, run or written, by native tools and by shell commands |
| E (edited) | native `Edit`/`Write`/`MultiEdit`/`NotebookEdit` targets plus shell write targets (not `git add`) |

Paths are repo-relative, lowercase, forward-slash. The worktree root and the
main checkout root are both stripped; paths under any other root (another
repository, a scratch directory, home) are counted in `outside_repo` and not
scored. Two paths match when equal or when one ends with `/` plus the other, so
a path relative to a subdirectory matches its repo-relative form.

## Metrics

`null` whenever the denominator is empty.

| Metric | Definition |
|---|---|
| hit | B and T share a file |
| coverage | \|T ∩ B\| / \|T\| |
| waste | \|B − T\| / \|B\| |
| rediscovery | searches whose query names a brief file (basename, or a stem of 4+ characters) or a selected lexical-match symbol, over all searches |
| gap | \|E − B\| / \|E\|, split into `gap_in_packet` (the file was retrieved but omitted) and `gap_unretrieved` |
| head_start | 1 − (call index of the first touch of a file in E ∩ B) / calls; 0 when E ∩ B is empty |
| calls_to_target, first_target_cited | calls before the first touch of any edited file, and whether that file was cited |
| recall@k, mrr | Agent Retrieval Bench definitions: retrieved items ranked by `final_score`, relevant = E |
| brief_strings, brief_strings_reused | selected-statement snippets of 16+ characters absent from the prompt, and how many appear in a later tool input |

Every metric in hit, coverage, waste, gap and head_start has a **shuffled
baseline**: the same turn scored against the brief of another episode from the
same repository (shifts 1, 2, 3, 5, 8 in `created_at` order). Lift = actual −
baseline. Raw rates are inflated by files an agent opens every turn; lift is the
number to read.

Slices: `intent` (prompt class), `anchored` (`prompt_symbol` in scope sources),
`capped` (ripgrep hit its match cap), `stem` (filename-stem items present),
`traffic`, `delegated` (the turn used `Agent`/`Task`, whose inner calls are not
in the transcript's main thread).

## Shell commands

`shell_paths.py` splits Bash and PowerShell commands into segments and gives
every path a role from its verb: read (`cat`, `sed`, `Get-Content`, …), search
(`grep`, `rg`, `Select-String`, `find`, … with the query separated), git
(`show`/`diff`/`log`/`blame` read; `add`/`rm`/`checkout`/`restore` write), write
(`cp`, `rm`, `Set-Content`, redirects), exec (interpreters and scripts), nav.
Command substitutions are parsed as commands; heredoc and here-string bodies are
dropped, with quoted path literals inside them kept as role `script`.

A segment that has a path-like token but an unknown verb, or unbalanced quotes,
is a **miss**. Misses are logged in `unparsed.jsonl` and never guessed at. The
miss rate (misses / path-bearing segments) is published in `summary.json`; at
10% or more, fix the extractor before reading any score.

## Facts

Each episode lists stable fact IDs, `f_` + sha1(`kind|value`)[:12], for
`cited_path`, `omitted_path`, `native_touch` (`role:path`), `shell_path`
(`role:path`) and `brief_string`.

## Twin candidate pool

`pool.jsonl` holds every scored episode with the verdict of each mechanical
filter from the measurement plan (S1.3):

| Filter | Pass when |
|---|---|
| standalone | the prompt is human and the first delivered prompt of its session (`prior_prompts` empty) |
| clean_tree | HEAD resolved and the git collector saw 0 dirty files |
| edited_repo_file | E is non-empty |
| calls_to_target | more than 3 calls before the first touch of an edited file |
| dependencies_tracked | every file read before being written exists at HEAD (`git ls-tree`, read-only; unknown with `--no-git` or an unreadable HEAD) |

`eligible` is true only when every filter is true; unknown never passes. The
summary gives the funnel in this order and the count eligible if any one
filter were dropped.

## Blind labeling sheets

```
python blind_sheets.py make --episodes <episodes.jsonl> --transcripts <source>=<dir> ...                             --seed <int> [--per-stratum 5] --out <dir>
python blind_sheets.py seal --labels <dir>/labels.json --out <dir>/seal.json
```

`make` draws a seeded sample from scored episodes that edited a repo file and had a
non-empty brief, `--per-stratum` from anchored and from unanchored, and shuffles
them under blind IDs `b01`, `b02`, …. Each sheet shows the prompt, the brief, the
tool calls, the agent's main-thread text in the turn, and what this extractor
recorded (edited and touched files). No metric, rule verdict, packet ID or
timestamp. The blind-ID key goes to `key/key.json`; `manifest.json` records the
input hashes, the seed and each sheet's sha256. Same inputs and seed give
byte-identical output.

`seal` refuses an incomplete labels file (`extractor` agree/disagree, with a note
on disagree; `outcome` from the six-way tree) and records its sha256.

## Output

- `footprints.jsonl` — one row per episode row, sorted by `packet_id`; unscored rows carry `status`.
- `unparsed.jsonl`, `pool.jsonl`, `summary.json`, `distributions.md` (one page).

Identical inputs give byte-identical outputs. `summary.json` records the sha256
of the episodes file and of both scripts.

## Tests

```
python test_footprints.py
python test_blind_sheets.py
```

Synthetic tempfile fixtures only (a throwaway git repository for the pool check).

# local-audit — local, read-only code-audit lane

Decorrelated second-opinion reviewer running entirely on this machine (Ollama +
RTX 5080). It is NOT the primary audit lane — that stays with the Claude-side
skills (`/audit`, `/slop-audit`, `/revit-functionality-audit`) and the Monday
scheduled audits. This lane exists because a different model family has
different blind spots than the model that wrote the diff. Never scheduled;
interactive use only (decision 2026-08-08).

## Pieces

| Piece | Where | Job |
|---|---|---|
| `slop_prepass.py` (this repo) | run via CLI | Deterministic discovery: greps a git repo for swallowed-failure patterns and success-summary counters. Output = candidate table, not findings. |
| Continue profile `Local Repo Audit` | `~/.continue/config.yaml` | Classification + cross-file reasoning via `qwen3-coder-32k` (Ollama, 32k ctx baked). Read-only rules; `/audit`, `/audit-silent`, `/audit-security`, `/audit-contract`, `/audit-counters` slash prompts. |

## Workflow

```bash
python slop_prepass.py <repo-root> [--since <ref>] > candidates.md
python slop_prepass.py --self-test
```

Then in VS Code (Continue sidebar, `Local Repo Audit` config selected):
paste/attach the candidate rows for the area under review plus the relevant
files (`@file`), and run `/audit` or a focused variant. The model classifies
candidates and traces cross-file paths; it does not brute-force discovery —
that is the pre-pass's job and it is free.

## MODE: Census and report only

This lane **discovers, classifies, and reports** — it never edits, patches,
stages, commits, reverts, or runs commands against the audited repo. Its output
(a findings report) is written **outside** the target repo, under `out/`.
Remediation is **out of scope by default**: applying any fix is a separate,
explicit, human action. There is no `--fix` / `--apply` mode, and adding one
must be a deliberate, opt-in change — enforced by `test_local_audit.py`.

## Ground rules

- All model traffic local (`localhost:11434`). No cloud, no telemetry additions.
- Read-only / census-only (above). The model is instructed to emit findings as
  text only; treat every finding as HYPOTHESIS until verified.
- Do not schedule unattended runs of this lane — a weaker model's misses go
  unnoticed without a human in the loop.

Guard test: `python test_local_audit.py` (proves census-only default: mode flag,
model instruction wording, and no repo-write / fix-apply code path).

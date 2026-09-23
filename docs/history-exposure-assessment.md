# Historical exposure assessment — initial public commit `351a644`

Evidence-based, non-sensitive assessment of what the **already-public** initial
commit `351a644ca6717e7dbbfc28de1db387b51c6f54a4` exposed before PR #1 removed or
anonymized it on the current tip. This document **refers to sensitive files by
path and category only** — it reproduces no secret values, no full private
endpoints, no private source/class inventories, no topology detail, and no
customer data.

This is an **assessment**. No history rewrite, credential rotation, or
GitHub cache-removal request is performed here; those remain owner decisions.

## Owner-decision block (read first)

> **Decided 2026-09-23: Option A — retain history, no rewrite.** A full-ref
> mirror scan found no credential, private key, or personal/customer data. See
> [2026-09-23__history-retention-decision.md](2026-09-23__history-retention-decision.md).

Based on the evidence below (no real secret values, no customer/client data —
only architecture/privacy metadata):

- **Option A — Accept historical architecture disclosure; no rewrite.**
  *Technically applicable.* Lowest effort. The disclosed material is structural
  (route/topology/name/path metadata), not credentials or data. Residual risk is
  informational only.
- **Option B — Rewrite public history and pursue GitHub cache removal.**
  *Technically applicable.* Removes the material from commit `351a644` in the
  GitHub-hosted history and lets you ask GitHub to purge cached views. Costs a
  force-push / history rewrite (needs separate explicit approval) and invalidates
  the baseline SHA for anyone who already cloned.
- **Option C — Treat as a credential/data incident; rotate/revoke + remove history.**
  **Not applicable.** No real credentials, tokens, keys, passwords, customer, or
  regulated data were found in the exposed tree (evidence below). There is nothing
  to rotate or revoke.

**Recommendation input (not a decision):** the evidence supports A or B. B is only
warranted if the *structural* disclosure (private route/service-name/path
metadata) is itself considered sensitive enough to justify a history rewrite of a
repo that currently has **0 forks** and a ~9.5-hour public exposure window.

## Three distinct removal states (do not conflate)

| State | Status |
|---|---|
| Removed from **current `main`** | ✅ Done (PR #1, merge `deeb728`). Current tip has none of the listed files. |
| Removed from **Git history** | ❌ Not done. Content still exists at commit `351a644` and every commit before the PR-1 deletions. A corrective commit does not rewrite history. |
| Removed from **third-party caches / forks** | ❌ Not done / not applicable. GitHub may cache old commit views; 0 forks exist today, but a cache/clone made during the exposure window is outside this repo's control. |

## Exposure summary

- Public exposure window: repo created `2026-09-12T08:39:27Z`; hardening merged to
  `main` at `2026-09-12T18:02:54Z` (~9.5 hours). Baseline `351a644` remains in
  history.
- Public forks at assessment time: **0** (`repos/.../forks` → empty; `forkCount`
  = 0). No fork owners contacted (out of scope).
- Secret scan of the baseline tree (Gitleaks 8.18.4, default rules, redacted):
  **4 findings, all in `ctxdex/test_ctxdex_gate.py`** — the intentional
  **synthetic** secret-gate vectors (AWS docs-style example key, placeholder PEM,
  dummy connection strings). **Zero** findings in any removed private file.
- Targeted scan of every removed file for assigned secret values
  (`secret|token|password|api_key|...= <12+ char value>`) and email addresses:
  **0 and 0** across all eight files.

**Actual secret values publicly exposed: NO.**
**Customer / client / regulated data publicly exposed: NO.**
**Classification: architecture / privacy disclosure (no credential-data incident).**

## Per-artifact assessment

```yaml
- artifact_path: ctxcheck/configs/bimpossible.toml
  public_commit_range: 351a644 .. (removed in PR #1, merge deeb728)
  exposure_category:
    - private endpoint or API-route structure
    - private service/deployment topology
    - secret variable name only
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: >
    Highest-severity item. Encoded a private product's route/endpoint structure,
    API-router prefixes, docker service topology, and secret env-var NAMES. No
    secret values. Structural disclosure only; rewrite is an owner judgment call.

- artifact_path: ctxcheck/configs/<private-profile>.toml
  public_commit_range: 351a644 .. (removed in PR #1)
  exposure_category:
    - private source file/class/module inventory
    - local filesystem path
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Private repo inventory plus local machine paths. No values, no data.

- artifact_path: ctxcheck/configs/memory.toml
  public_commit_range: 351a644 .. (removed in PR #1)
  exposure_category:
    - local filesystem path
    - private endpoint or API-route structure
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Local machine path and a private target definition. No values.

- artifact_path: ctxcheck/audits/2026-08-10__slop-audit.md
  public_commit_range: 351a644 .. (removed in PR #1)
  exposure_category:
    - private benchmark/derived metadata
    - local filesystem path
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Generated operational audit output over private source; local paths.

- artifact_path: ctxcheck/audits/2026-08-31__slop-audit.md
  public_commit_range: 351a644 .. (removed in PR #1)
  exposure_category:
    - private benchmark/derived metadata
    - local filesystem path
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Same class as the 08-10 audit.

- artifact_path: ctxdex/audits/2026-08-10__slop-audit.md
  public_commit_range: 351a644 .. (removed in PR #1)
  exposure_category:
    - private benchmark/derived metadata
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Generated operational output derived from private source.

- artifact_path: graphify/recall/BASELINE.md
  public_commit_range: 351a644 .. (removed in PR #1)
  exposure_category:
    - private benchmark/derived metadata
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Recall benchmark numbers derived from a private corpus.

- artifact_path: graphify/recall/RERANK-EXPERIMENT.md
  public_commit_range: 351a644 .. (removed in PR #1)
  exposure_category:
    - private benchmark/derived metadata
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: >
    Benchmark writeup derived from private source. Distinct from the retained
    generic method file graphify/recall/rerank_bm25.py (public BM25 rerank method,
    no private data — intentionally kept).

# Anonymized (not deleted) — content changed on current tip, prior form still in history.
- artifact_path: graphify/Refresh-Graphs.ps1
  public_commit_range: 351a644 .. (anonymized in PR #1)
  exposure_category: [local filesystem path, private endpoint or API-route structure]
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Machine paths + private scan targets; now parameterized. Old form in history.

- artifact_path: graphify/Check-GraphifyHealth.ps1
  public_commit_range: 351a644 .. (anonymized in PR #1)
  exposure_category: [local filesystem path]
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Machine paths + private dashboard dirs; now env-driven. Old form in history.

- artifact_path: graphify/recall/measure_recall.py
  public_commit_range: 351a644 .. (anonymized in PR #1)
  exposure_category: [local filesystem path]
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Machine path + private example; now PATH/env with a generic example.

- artifact_path: local-audit/audit-repo.cmd
  public_commit_range: 351a644 .. (anonymized in PR #1)
  exposure_category: [private endpoint or API-route structure]
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Default scanned a named private repo; default now the current dir.

- artifact_path: local-audit/full-audit.cmd
  public_commit_range: 351a644 .. (anonymized in PR #1)
  exposure_category: [private endpoint or API-route structure]
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Same as audit-repo.cmd.

- artifact_path: local-audit/README.md
  public_commit_range: 351a644 .. (anonymized in PR #1)
  exposure_category: [local filesystem path]
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Absolute self-paths; now relative.

- artifact_path: ctxdex/README.md
  public_commit_range: 351a644 .. (anonymized in PR #1)
  exposure_category: [local filesystem path]
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Pointer to a legacy private-workspace path; dropped, public attribution kept.

- artifact_path: ctxcheck/README.md
  public_commit_range: 351a644 .. (anonymized in PR #1)
  exposure_category: [private source file/class/module inventory]
  actual_secret_value_present: no
  customer_or_client_data_present: no
  publicly_reachable_before_removal: yes
  current_tip_removed_or_anonymized: yes
  rotation_or_revocation_required: no
  history_rewrite_recommended: owner_decision_required
  rationale: Private ADR reference; genericized.
```

## Not an exposure (retained intentionally)

- `ctxdex/test_ctxdex_gate.py` — the only Gitleaks hits in the baseline (4) are
  here and are **synthetic** vectors proving the tool refuses to index
  credentials. Not private material; kept and allowlisted.
- `graphify/recall/rerank_bm25.py` — generic public BM25 rerank method; no private
  data. Distinct from the removed `RERANK-EXPERIMENT.md`.

## Conclusion

- Classification: **architecture / privacy disclosure** in Git history at
  `351a644`.
- Actual secret values found: **no**. Customer/client data found: **no**.
- Credential rotation/revocation: **not required** (nothing to rotate).
- History rewrite: **owner decision** — Option A (accept) or Option B (rewrite +
  cache-removal request). Option C is not applicable.
- Whatever the owner chooses, current `main` is already clean; the decision is
  solely about the pre-hardening commit(s) still present in history.

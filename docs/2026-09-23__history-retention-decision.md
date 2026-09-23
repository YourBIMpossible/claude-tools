# History retention decision — 2026-09-23

**Decision: history rewrite NOT REQUIRED. Public Git history is retained as-is.**

This record closes the owner-decision block in
[history-exposure-assessment.md](history-exposure-assessment.md) (Option A). It
contains category counts only — no paths from private machines, no private
identifiers, no matched literals.

## Scope of the scan

- Source: a fresh `git clone --mirror` of the public repository (not the working
  checkout), so every reachable ref was included: `main`, both open Dependabot
  branches, and every `refs/pull/*/head` and `refs/pull/*/merge` ref
  (11 refs, 30 commits, 134 unique blobs).
- Detector 1: Gitleaks 8.18.4, default rules, git mode with `--log-opts=--all`,
  run from the mirror (no repo `.gitleaks.toml`, so nothing allowlisted).
- Detector 2: the enhanced `tools/pre_publish_check.py` rule set (13 content
  rules + 14 forbidden-path classes) applied to every line of every reachable blob.
- Detector 3: an assigned-secret heuristic (`secret|token|password|api_key|
  private_key` assigned a 12+ character value).

## Results by category

| Category | Line matches | Distinct paths | Classification |
|---|---:|---:|---|
| Gitleaks findings | 4 | 1 | Synthetic test vectors in the ctxdex secret-gate suite (placeholder key/PEM); not credentials |
| Assigned-secret heuristic | 0 | 0 | — |
| `windows-drive-path` | 77 | 23 | Local-machine path disclosure |
| `private-identifier` | 77 | 16 | Private repo/workspace name disclosure |
| `claude-worktree-path` | 5 | 2 | Local tooling path disclosure |
| `claude-user-home` | 4 | 3 | Local tooling path disclosure |
| `worktree-autoname` | 2 | 1 | Local tooling metadata |
| `internal-state-path` | 2 | 1 | Local tooling metadata |
| `private-source-path` | 2 | 2 | Private architecture metadata (class/file names) |
| `user-home-path` | 1 | 1 | Local-machine path disclosure |
| `private-namespace` | 1 | 1 | Private architecture metadata |
| `email-address` | 1 | 1 | Synthetic `user:pass@host` connection-string vector (same test suite) |
| `unc-path`, `private-ip`, `private-git-remote` | 0 | 0 | — |
| Forbidden file classes (DBs, binaries, `.env`, logs, state, reports) | 0 | 0 | — |

## Classification against the rewrite trigger

| Trigger | Found |
|---|---|
| Secret, credential, token, or API key | **No** |
| Private key | **No** (placeholder PEM test vector only) |
| Regulated personal data or customer/client data | **No** |

Every historical finding is architecture/privacy metadata (local paths, private
repository names, internal tooling metadata, private class/namespace names).
None meets the rewrite trigger, so no force-push, ref rewrite, tag deletion, or
GitHub cache-removal request is made. Nothing requires rotation or revocation.

## Residual risk accepted

Anyone with a clone can still read the pre-hardening metadata in old commits.
This is informational only: it describes how the maintainer's machine and private
repositories are laid out, and grants no access to any of them. The current tree
is enforced clean by `tools/pre_publish_check.py` in CI and in
`tools/release-check.ps1`.

## Reopen condition

Re-run this classification, and treat rewrite as required, if a later scan of any
reachable ref finds a real credential, private key, or personal/customer data.

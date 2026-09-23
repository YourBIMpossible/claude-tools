# Publication-hygiene closeout — 2026-09-23

| Item | Value |
|---|---|
| Base (`origin/main`) | `e2d9cb2` |
| Branch | `security/publication-hygiene-closeout` |
| PR / merge | recorded in the post-merge section below |

## 1. Current-tree inventory and dispositions

Inventory: every tracked file, every line, under the enhanced rule set.
42 findings across 18 files before remediation. After remediation there are
0 findings and 3 line-scoped exceptions.

| Category | Findings | Disposition |
|---|---:|---|
| Drive-letter paths in policy examples (PR template, CONTRIBUTING, public-boundary) | 4 | Reworded to the category ("drive-letter, UNC, or user-home paths") |
| Drive-letter paths naming the working checkout / recovery bundles (publication-readiness) | 4 | Replaced with "the working checkout" / `<recovery-dir>/…` |
| Legacy private-workspace name (public-boundary, publication-readiness, history assessment) | 6 | Replaced with "legacy private-workspace" |
| Private repo name (plugin repo) in `.gitignore` and `Refresh-Graphs.ps1` comments | 3 | Replaced with "plugin" |
| Private profile repo name (ctxcheck, ctxdex code/README, removed-artifact rows) | 6 | Replaced with a generic description / `<private-profile>` placeholder |
| Placeholder drive paths in `Refresh-Graphs.ps1` example targets | 4 | `<path-to-…>` placeholders (the `$targets` block shape is unchanged) |
| Workstation store-glob default in `measure_relevance.py` | 3 | Now derived from the checkout location, with an `EVIDENCE_STORE_ROOT` override; the worktree glob kept as a scoped exception |
| Functional worktree-prefix regex in `measure_relevance.py` | 1 | Scoped exception |
| Synthetic drive fixture and docstring in `test_measure_relevance.py` | 2 | Non-drive fixture root; docstring reworded |
| Claude user-home paths (skillspector README, SkillTargets comments) | 3 | Reworded / `<USERPROFILE>` placeholder |
| Drive-root output bug description (skillspector README + script, release-check comment) | 3 | Reworded without a drive literal |
| Synthetic `user:pass@host` vector in the ctxdex secret-gate test | 1 | Scoped exception (also in `.gitleaks.toml`) |
| Checker's own test literals | 4 | Assembled at runtime; not exempted |

The previous whole-file `MARKER_ALLOWLIST` (10 files, including the checker, the
policy docs and the ctxdex test) is removed. Every tracked file is scanned.

## 2. Checker rules (`tools/pre_publish_check.py`)

The checker reports every match, redacted, with file:line, rule and line hash. It
exits 1 on any finding, stale exception or invalid config. There is no warn mode.

| Rule | Detects |
|---|---|
| `windows-drive-path` | Any drive letter, `\` or `/`, not part of a URL scheme or word |
| `unc-path` | Double-backslash host/share network paths |
| `user-home-path` | `/home/<user>/`, `/Users/<user>/` |
| `claude-user-home` | `~`, `$HOME`, `%USERPROFILE%`, `$env:USERPROFILE` + `.claude`, either separator |
| `claude-worktree-path` | `.claude` + `worktrees`, either separator |
| `worktree-autoname` | Generated `word-word-<6 hex>` worktree/branch names |
| `internal-state-path` | `.tools` + `state` |
| `private-namespace`, `private-source-path` | Private product namespace and command-class/source paths (carried over) |
| `email-address`, `private-ip`, `private-git-remote` | Carried over |
| `private-identifier` | Tokens whose normalized contiguous sub-joins hash into `tools/private-identifiers.sha256` (19 entries; the names are not published) |
| 14 forbidden file classes | Carried over unchanged |

Exceptions are listed in `tools/public-boundary-exceptions.json`. Each one is
bound to a path, a rule and the SHA-256 of a single line, and carries a reason.
If the line is edited, its exception stops applying. An unused exception fails
the run. Three exceptions are in use:
- the synthetic connection string (1);
- the functional worktree glob and the worktree-prefix regex (2).

No exception covers any line remediated above.

## 3. Test matrix (`tools/test_pre_publish_check.py` — 54 checks)

Each case runs the checker as a subprocess against a throwaway git repo built from
synthetic content only.

| Case | Asserted |
|---|---|
| Backslash drive path | exit 1, 1 finding, file:line, literal redacted |
| Forward-slash drive path | exit 1, 1 finding, literal redacted |
| Multiple violations in one file | all 4 reported; distinct line numbers |
| UNC path | exit 1, literal redacted |
| Claude user-home, tilde and USERPROFILE forms | exit 1 each |
| Claude worktree path, forward and backslash | exit 1 each |
| `/home/<user>/` | exit 1, redacted |
| Generated worktree name | exit 1 |
| Private identifier (exact, and case/separator variant inside a path) | exit 1, name never printed |
| Public-safe relative paths, URLs, `a:b` | exit 0, PASS |
| Identifier parts alone | exit 0 |
| Scoped exception | suppresses exactly its line; not other lines, not an edited line, not another path, not another rule |
| Stale exception / reasonless exception | exit 1 |
| Forbidden file class | exit 1 |

Wired into `.github/workflows/ci.yml`, `tools/release-check.ps1` and
`CONTRIBUTING.md`. `evidence-relevance/test_measure_relevance.py` is wired in as
well, because this change touches that module.

## 4. Historical scan and decision

Mirror clone of the public repo: 11 refs (including all PR refs), 30 commits,
134 unique blobs.

| Detector | Result |
|---|---|
| Gitleaks, `--log-opts=--all`, no allowlist | 4 findings — the synthetic ctxdex vectors only |
| Assigned-secret heuristic | 0 |
| Enhanced boundary rules | 172 line matches, all path/name/architecture metadata (per-rule counts in the decision record) |
| Forbidden file classes | 0 |

Decision: **history rewrite NOT REQUIRED.** No credential, private key or
personal/customer data was found in any reachable ref. The record is in
`docs/2026-09-23__history-retention-decision.md`. It closes the owner-decision
block in `docs/history-exposure-assessment.md`. No force-push, ref rewrite, tag
deletion or GitHub Support contact was made.

## 5. Local validation (branch tip, before push)

| Gate | Result |
|---|---|
| `tools/pre_publish_check.py` | PASS (98 files, 13 content rules, 19 identifiers, 3 exceptions) |
| `tools/test_pre_publish_check.py` | 54/54 |
| ctxcheck / ctxdex / local-audit guard / slop_prepass | 49/49, 43/43, held, 11/11 |
| graphify refresh tests / evidence-relevance tests | 22/22, 7/7 |
| `py_compile`, `git diff --check` | clean |
| Gitleaks `--no-git` | no leaks |
| `tools/release-check.ps1` | All release gates passed |

## 6. GitHub CI, merge and post-merge verification

A follow-up commit records these once the merge lands.

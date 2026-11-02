# Contributing to claude-tools

This is a public, **source-only**, local-first toolbelt. Contributions are welcome,
but the top priority is that nothing private ever enters the repository.

## The one hard rule: keep the public boundary intact

Before every push, run the gate. It is exactly what CI's manual audit (`ci.yml`) runs on Linux and Windows:

```bash
python tools/gate.py     # boundary + machine paths + every test suite + gitleaks; ends GATE PASS or GATE FAIL
```

It needs gitleaks 8.18.4 (`$GITLEAKS_BIN`, PATH, the profile's `tools/git_gate.py`
install, or `bin/`: see [bin/README.md](bin/README.md)); without it the gate fails.

Turn on the repo's git hooks once per clone (the profile's bootstrap does it), so the
gate runs on every push and a staged secret is caught at commit time:

```bash
git config core.hooksPath .githooks
```

`.githooks/pre-push` runs the gate on each pushed commit (checked out apart from your
working copy) and blocks the push unless it passes, printing each failing step's own
report. `.githooks/pre-commit` runs gitleaks on the staged diff. A vetted false
positive goes in the scanner's allowlist (`.machine-paths-allow.json`,
`tools/public-boundary-exceptions.json`, `.gitleaks.toml`), never a bypass.

Never attach to a commit, issue, or PR: secrets, tokens, keys, `.env` files;
customer/client/financial data; private source trees or architecture metadata
(routes, service topology, internal class/command names, secret env-var names);
generated reports, indexes, or databases; local-machine paths (drive-letter,
UNC, or user-home), private hostnames/IPs, or personal emails. See
[docs/public-boundary.md](docs/public-boundary.md) for the full allowed/forbidden
policy and the examples/fixtures rules.

If you spot a questionable file, **do not silently delete it** — open an issue
describing the file and why it is in doubt, and leave classification to a
maintainer. If you find private content that reached a *published* commit,
report it privately (see [SECURITY.md](SECURITY.md)); removing it in a new commit
does not remove it from Git history or third-party caches.

## House style

- Single-file **stdlib** Python (3.11+) or a thin PowerShell wrapper. No new
  runtime dependencies without discussion.
- Every tool ships a CLI-subprocess test suite; tests use **synthetic inputs
  only** — never point tests, examples, or fixtures at a real private repo.
- Configs live in the tool's own folder; target repos are never modified
  (except `ctxcheck`'s `commands` category, which runs only its declared commands).
- Severity model where it applies: declared claims that don't hold FAIL;
  scan-discovered issues WARN.
- Audit/analysis tooling stays **read-only / report-only by default**; any
  remediation must be explicit opt-in (see the local-audit README).

## Tests

Every suite is listed in `TEST_SUITES` in [tools/gate.py](tools/gate.py), and the gate
runs them all (`tools/test_gate.py` fails if a tracked `test_*.py` is missing from the
list). A suite is a plain script that exits non-zero on a failure; run one alone with
`python <suite>`. The graphify suites need pwsh (PowerShell 7); the scheduling suite
runs only on Windows; the evidence-episodes suite needs evidence-compiler installed at
the commit `tools/gate.py` pins. The gate prints a line for each suite it cannot run
here. CI's `ci.yml` runs the same gate on manual dispatch only
(`gh workflow run ci.yml --ref <branch>`). The automatic PR check, `secret-scan.yml`
(when a PR opens or leaves draft, never on later pushes), runs the boundary check and
gitleaks; `machine-paths.yml` fires the same way.

## Licensing of contributions

This project is licensed under the [Apache License 2.0](LICENSE). By submitting a
contribution, you agree that it is licensed under Apache-2.0 as an intentional
contribution under Section 5 of that license, and you affirm you have the right
to submit it. Do not add third-party code under an incompatible license, and
carry any required upstream copyright/`NOTICE` attribution with anything you
adapt. See [docs/license-decision.md](docs/license-decision.md) for the
provenance basis of the license choice.

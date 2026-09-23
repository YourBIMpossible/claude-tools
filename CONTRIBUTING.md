# Contributing to claude-tools

This is a public, **source-only**, local-first toolbelt. Contributions are welcome,
but the top priority is that nothing private ever enters the repository.

## The one hard rule: keep the public boundary intact

Before every push, scan the **committed tree** (never trust `.gitignore` alone):

```bash
pwsh tools/release-check.ps1          # gitleaks + boundary + tests, all in one
```

or the individual gates:

```bash
bin/gitleaks.exe detect --source . --no-banner   # requires the gitleaks download
python tools/pre_publish_check.py                # forbidden-file + private-marker scan
```

Never attach to a commit, issue, or PR: secrets, tokens, keys, `.env` files;
customer/client/financial data; private source trees or architecture metadata
(routes, service topology, internal class/command names, secret env-var names);
generated reports, indexes, or databases; local-machine paths (`F:\…`,
`C:\Users\…`), private hostnames/IPs, or personal emails. See
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

```bash
python ctxcheck/test_ctxcheck.py
python ctxdex/test_ctxdex_gate.py
python graphify/test_refresh_graphs.py   # needs pwsh (PowerShell 7)
```

CI (PR + manual dispatch only) runs these plus the boundary and secret scans.

## Licensing of contributions

This project is licensed under the [Apache License 2.0](LICENSE). By submitting a
contribution, you agree that it is licensed under Apache-2.0 as an intentional
contribution under Section 5 of that license, and you affirm you have the right
to submit it. Do not add third-party code under an incompatible license, and
carry any required upstream copyright/`NOTICE` attribution with anything you
adapt. See [docs/license-decision.md](docs/license-decision.md) for the
provenance basis of the license choice.

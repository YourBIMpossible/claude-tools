<!-- Thanks for contributing. Keep the public boundary intact — see CONTRIBUTING.md. -->

## What & why

<!-- One or two sentences. Link any related issue. -->

## Pre-push boundary gate (required)

- [ ] Ran `pwsh tools/release-check.ps1` (or the individual gates) and it passed.
- [ ] No secrets, `.env`, credentials, or keys added.
- [ ] No private source, routes/endpoints, service topology, secret env-var
      names, local-machine paths (`F:\…`, `C:\Users\…`), private hosts/IPs, or
      personal emails added (see `docs/public-boundary.md`).
- [ ] Any new tool/example/fixture uses **synthetic** data only and is labelled.
- [ ] Audit/analysis code stays **census/report-only** (no default remediation).

## Notes

<!-- Anything reviewers should know. If this touches licensing, see docs/license-decision.md. -->

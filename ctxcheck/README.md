# ctxcheck — local repo reality-check CLI

Validates that a repo's documented claims still match reality. Single-file
stdlib Python (3.11+, needs `tomllib`), no network, read-only against target
repos except the `commands` category, which runs exactly the commands its own
config declares.

```bash
py ctxcheck.py run <name>            # configs/<name>.toml
py ctxcheck.py run <repo-dir>        # uses <repo>/.ctxcheck.toml
py ctxcheck.py run --config x.toml --repo <dir>
```

Categories: `files` `refs` `env` `compose` `endpoints` `links` `commands`
`staleness` — filter with `--only`, machine output with `--json`, promote
WARNs to failures with `--strict`, show passes with `--verbose`.

Severity model: **declared** claims that don't hold FAIL (exit 1);
**scan-discovered** issues (doc path refs, commit SHAs, staleness) WARN
(exit 0 unless `--strict`) because scans have false positives.

Configs for real repos live in `configs/` here — target repos are not
touched. Test suite: `py test_ctxcheck.py` or `py -m pytest -q` (same 48 CLI-level
checks over a fixture repo; pytest collects `test_ctxcheck_suite`).

Scope guard: per claude-profile `docs/adr/0001`, this tool stays a
reality-checker — no context manifests, no MCP wrapper, no retrieval-eval
machinery.

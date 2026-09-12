# License decision — RESOLVED: Apache-2.0

**Status: LICENSED. `LICENSE` (Apache License 2.0) added at the repo root on
2026-09-12, with a `NOTICE` file.** This supersedes the prior
"decision required / all-rights-reserved" state.

## Decision

- **License chosen:** Apache License 2.0.
- **Why:** permissive license suited to public developer tooling, with an
  express patent grant and `NOTICE`-file support for attribution.
- **Copyright line:** `Copyright 2026 YourBIMpossible` (see `NOTICE` and the
  `LICENSE` appendix).

## Provenance verification (basis for the choice)

Verified against the merged `main` tree on 2026-09-12 before adding the license:

| Check | Result |
|---|---|
| All tracked source first-party? | Yes — no copied/adapted-from markers, no third-party copyright/SPDX/license notices in any tracked file. |
| Python dependencies | **stdlib only** (argparse, json, sqlite3, subprocess, pathlib, re, urllib, tomllib, …). No `requirements.txt` / `setup.py` / `pyproject.toml` / `Pipfile`. |
| `skillspector/src` tracked? | **No.** Only first-party wrapper scripts (`Run-…`, `Update-…`, `SkillTargets.ps1`) and README are tracked; the upstream project is cloned at setup under its own license. |
| `skillspector/baselines/*` | First-party SkillSpector scan metadata only — `version`, `rules: []`, and `fingerprints` (truncated content hashes + rule IDs + file names + review notes). **No third-party source text.** |
| Third-party binaries (`bin/*.exe`) | Not tracked; downloaded at setup under their own licenses (see `bin/README.md`). |
| `graphifyy` (PyPI) | Installed at setup under its own license; not redistributed. |

**Conclusion:** all tracked material is licensable by the repository owner and no
incompatible third-party code is distributed → Apache-2.0 is applicable and was
added.

## Third-party components (separately licensed — unchanged by this choice)

- **SkillSpector** — upstream project, cloned into `skillspector/src/` at setup;
  not vendored here. Governed by its own upstream license.
- **graphifyy** — PyPI package installed at setup; its own license applies.
- **gitleaks / trivy** (`bin/`) — downloaded at setup; their own licenses apply.

This repository's Apache-2.0 grant covers only the first-party tool source and
documentation tracked here. See [NOTICE](../NOTICE).

# LICENSE — DECISION REQUIRED (owner)

**Status: unlicensed. This repository currently ships no `LICENSE` file and no
license header in any source file.**

Under default copyright law, "no license" means **all rights reserved**: the
public can view and fork on GitHub (per the GitHub Terms of Service), but no one
is granted permission to use, copy, modify, or redistribute the code. That is
almost certainly *not* the intent for a public toolbelt, but the intended license
was never stated, so this hardening pass **did not invent one**.

## What needs an owner decision

Pick one and add the corresponding `LICENSE` file at the repo root (and, if you
want, an SPDX header line in each source file):

| Option | Effect |
|---|---|
| **MIT** | Simplest permissive; keep copyright + license notice. Common for small tool repos. |
| **Apache-2.0** | Permissive + explicit patent grant + `NOTICE` support. |
| **BSD-3-Clause** | Permissive; adds a no-endorsement clause. |
| **GPL-3.0 / MPL-2.0** | Copyleft (share-alike). Only if you want derivatives kept open. |
| **Keep unlicensed** | Deliberate all-rights-reserved. State it explicitly so it is a choice, not an oversight. |

## Third-party components are separately licensed (not affected by this choice)

- **skillspector** (`skillspector/src/`) — NVIDIA's project, **not vendored here**;
  cloned at setup under its own upstream license. This repo's license does not
  cover it.
- **graphifyy** — installed from PyPI at setup; its own license applies.
- **gitleaks / trivy** (`bin/`) — downloaded at setup; their own licenses apply.

Only the first-party tool source in this repo (ctxcheck, ctxdex, the graphify
ops scripts, local-audit, the skillspector *wrapper* scripts, `tools/`) is
covered by whatever license the owner chooses.

## How to resolve

1. Decide the license (or decide to stay unlicensed on purpose).
2. Add `LICENSE` at the repo root; optionally add SPDX headers.
3. Delete this file, or replace it with a one-line pointer to the chosen license.

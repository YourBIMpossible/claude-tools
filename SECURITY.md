# Security policy

## Scope

`claude-tools` is a collection of **local-first** developer tools. They run on
your own machine against your own repositories; there is no hosted service, no
account, and no network egress except where a tool explicitly fetches a URL you
pass it (`ctxdex index <url>`) or checks PyPI for a graphify update (notify-only).

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — do not open a public
issue for anything exploitable.

- Preferred: GitHub **"Report a vulnerability"** (private security advisory) on
  this repository's **Security** tab.
  > Owner note: enable *Settings → Code security → Private vulnerability
  > reporting* so this channel is available. Until then, contact the owner
  > through their GitHub profile.

Include: affected tool/file, version or commit, reproduction steps, and impact.
Please allow a reasonable window for a fix before any public disclosure.

## Reporting exposed private content

This repo has a strict public-content boundary
([docs/public-boundary.md](docs/public-boundary.md)). If you find private data
(a real secret, a private repo path, internal endpoint/route names, customer
data) in the **current tree or in Git history**, report it through the private
channel above rather than a public issue. Note that removing such content in a
new commit does **not** remove it from Git history or third-party caches — a
history-rewrite / credential-rotation decision belongs to the owner.

## What these tools do and don't do

- **Read-only by default.** The audit/reality-check tools report; they do not
  modify target repos. `ctxcheck`'s `commands` category runs only the commands
  its own config declares. Any remediation behavior is explicit opt-in.
- **No telemetry.** Nothing phones home.
- **Secret handling.** `ctxdex` refuses to index content that looks like
  credentials (its `test_ctxdex_gate.py` suite asserts this). The synthetic
  secret vectors in that test file are deliberate fakes, not real secrets.
- **Third-party binaries** (`bin/`) and the **skillspector** upstream project are
  downloaded/cloned at setup, pinned, and never committed — verify them against
  their upstream checksums (see `bin/README.md`, `skillspector/README.md`).

## Supported versions

This is a rolling toolbelt with no formal release cadence; fixes land on `main`.

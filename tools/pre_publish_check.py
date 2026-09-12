#!/usr/bin/env python3
"""Public-boundary pre-publish check for claude-tools.

Scans the COMMITTED git tree (git ls-files) for two classes of problem:

  1. Forbidden file classes  — reports/state/indexes/DBs/binaries/venvs/logs/
     caches/.env and other things that must never be tracked in this public repo.
  2. High-signal private markers — local-machine paths, private project/product
     identifiers, private hosts/IPs, personal emails.

Evidence is the committed tree, never .gitignore alone. Exit 0 = clean, 1 = findings.
This is a boundary check, NOT a secret scanner — run Gitleaks as well (see docs).
"""
from __future__ import annotations

import re
import subprocess
import sys

# Files whose whole job is to describe the policy: they legitimately name the
# forbidden terms. They are still scanned for real secrets by Gitleaks.
MARKER_ALLOWLIST = {
    "docs/public-boundary.md",
    "docs/publication-readiness.md",
    "docs/license-decision.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "tools/pre_publish_check.py",
    ".gitleaks.toml",
    # Synthetic secret-gate test vectors (fake AWS key, PEM, connection strings).
    # These exist to prove ctxdex REFUSES to index credentials. Also scoped in
    # .gitleaks.toml. The suite asserts the tool rejects every vector.
    "ctxdex/test_ctxdex_gate.py",
}

# Forbidden tracked-path patterns (path is enough — content need not be read).
FORBIDDEN_PATHS = [
    (r"(^|/)reports/", "private/generated reports"),
    (r"(^|/)state/", "cross-session state store"),
    (r"\.exe$", "third-party/vendored binary"),
    (r"\.dll$", "third-party/vendored binary"),
    (r"\.db$|\.sqlite3?$", "database / index"),
    (r"(^|/)venv/|(^|/)\.venv/", "virtualenv"),
    (r"__pycache__|\.pyc$", "python cache"),
    (r"(^|/)node_modules/", "node deps"),
    (r"\.env($|\.)", "environment file"),
    (r"\.log$", "log file"),
    (r"(^|/)_backups?/|(^|/)backups?/", "backup/export dir"),
    (r"(^|/)skillspector/src/", "vendored upstream project"),
    (r"(^|/)graphify/backups/", "private graph exports"),
    (r"budget.*\.json$|csharp-queries\.json$", "private recall fixture"),
]

# High-signal private markers in tracked file CONTENT.
MARKER_PATTERNS = [
    (r"[A-Za-z]:\\Users\\", "windows user-home path"),
    (r"F:\\", "local F: drive path"),
    (r"\bBIMpossible\.[A-Za-z]", "private product namespace"),
    (r"/Commands/[A-Za-z0-9_]+\.cs", "private source path"),
    (r"\b[A-Za-z0-9_]+Command\.cs\b", "private command class"),
    (r"\bAI-Dev\b|\bAI-Server\b|\bAI-Dashboard\b", "legacy/private project dir"),
    (r"[a-zA-Z0-9._%+-]+@(?!example\.com|anthropic\.com)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
     "email address"),
    (r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+", "private IP"),
    (r"git@[a-zA-Z0-9.-]+:", "private git remote"),
]


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def main() -> int:
    files = tracked_files()
    path_hits: list[str] = []
    marker_hits: list[str] = []

    for f in files:
        for pat, why in FORBIDDEN_PATHS:
            if re.search(pat, f):
                path_hits.append(f"{f}  [{why}]")
                break

    for f in files:
        if f in MARKER_ALLOWLIST:
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for pat, why in MARKER_PATTERNS:
            m = re.search(pat, text)
            if m:
                # line number of first hit
                line = text[: m.start()].count("\n") + 1
                marker_hits.append(f"{f}:{line}  [{why}]  {m.group(0)[:60]}")

    print(f"pre-publish check: {len(files)} tracked files")
    if path_hits:
        print("\nFORBIDDEN FILE CLASSES:")
        for h in path_hits:
            print(f"  - {h}")
    if marker_hits:
        print("\nPRIVATE MARKERS IN CONTENT:")
        for h in marker_hits:
            print(f"  - {h}")

    if path_hits or marker_hits:
        print(f"\nFAIL: {len(path_hits)} path finding(s), {len(marker_hits)} marker finding(s).")
        return 1
    print("PASS: no forbidden files or private markers in the committed tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

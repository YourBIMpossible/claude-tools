#!/usr/bin/env python3
"""Public-boundary pre-publish check for claude-tools.

Scans the COMMITTED git tree (git ls-files) for two classes of problem:

  1. Forbidden file classes  — reports/state/indexes/DBs/binaries/venvs/logs/
     caches/.env and other things that must never be tracked in this public repo.
  2. Private markers in content — local-machine paths (any drive letter, either
     separator, UNC, user homes), Claude Code local-state paths, generated
     worktree names, internal state paths, private identifiers, private
     hosts/IPs, personal emails.

Every match in every file is reported (file:line, rule, redacted excerpt, line
hash) — the private literal itself is never echoed. Evidence is the committed
tree, never .gitignore alone. No file is exempt, this one included.
Exit 0 = clean, 1 = findings, stale exception, or invalid config. There is no
warning-only mode. This is a boundary check, NOT a secret scanner — run Gitleaks too.

Repo-local config (read from the repo root, both optional):
  tools/private-identifiers.sha256    one SHA-256 per line of a normalized private
      identifier (repo/workspace/user names). Hashed so the public list does not
      itself disclose the names. Normalization: lowercase, runs of [-_.] -> "-".
      Add one:  python tools/pre_publish_check.py --hash-identifier <name>
  tools/public-boundary-exceptions.json   narrowly scoped exceptions, each
      {"path", "rule", "line_sha256", "reason"}. An exception covers exactly one
      rule on one line whose stripped text hashes to line_sha256; editing that
      line voids it. An exception that matches nothing fails the run.
      Print a line's hash:  python tools/pre_publish_check.py --hash-line "<text>"
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

IDENTIFIERS_FILE = "tools/private-identifiers.sha256"
EXCEPTIONS_FILE = "tools/public-boundary-exceptions.json"

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

SEP = r"[\\/]"

# (rule id, pattern, description). Matched per line; every match is reported.
MARKER_RULES = [
    ("windows-drive-path",
     r"(?<![A-Za-z0-9])[A-Za-z]:" + SEP + r"(?=[A-Za-z0-9_.$~%*<-]|" + SEP + r")",
     "absolute drive-letter path (either separator)"),
    ("unc-path",
     r"(?<![\\\w])\\\\[A-Za-z0-9._$-]+\\[A-Za-z0-9._$-]",
     "UNC / network path"),
    ("user-home-path",
     r"(?<![\w.])/(?:home|Users)/[A-Za-z_][\w.-]*/",
     "absolute user-home path"),
    ("claude-user-home",
     r"(?:~|\$HOME|\$\{HOME\}|%USERPROFILE%|\$env:USERPROFILE)" + SEP + r"\.claude\b",
     "Claude Code user-home state path"),
    ("claude-worktree-path",
     r"\.claude" + SEP + r"worktrees\b",
     "Claude Code worktree path"),
    ("worktree-autoname",
     r"\b[a-z]+(?:-[a-z]+){1,5}-(?=[0-9a-f]*\d)[0-9a-f]{6}\b",
     "generated worktree/branch name"),
    ("internal-state-path",
     r"\.tools" + SEP + r"state\b",
     "internal cross-session state path"),
    ("private-namespace",
     r"\bBIMpossible\.[A-Za-z]",
     "private product namespace"),
    ("private-source-path",
     r"/Commands/[A-Za-z0-9_]+\.cs|\b[A-Za-z0-9_]+Command\.cs\b",
     "private source path / command class"),
    ("email-address",
     r"[a-zA-Z0-9._%+-]+@(?!example\.com|anthropic\.com)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
     "email address"),
    ("private-ip",
     r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+",
     "private IP"),
    ("private-git-remote",
     r"git@[a-zA-Z0-9.-]+:",
     "private git remote"),
]
COMPILED = [(rid, re.compile(p), why) for rid, p, why in MARKER_RULES]
RULE_IDS = {rid for rid, _, _ in MARKER_RULES} | {"private-identifier"}

TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")
MAX_ID_PARTS = 5


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_identifier(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower()).strip("-")


def identifier_candidates(token: str) -> set[str]:
    """Every contiguous run of 1..MAX_ID_PARTS parts of a token, normalized."""
    parts = [p for p in re.split(r"[-_.]+", token.lower()) if p]
    out = set()
    for i in range(len(parts)):
        for j in range(i + 1, min(len(parts), i + MAX_ID_PARTS) + 1):
            out.add("-".join(parts[i:j]))
    return out


def redact(s: str) -> str:
    s = s.strip()
    return (s[:3] if len(s) > 4 else s[:1]) + "..."


def load_identifiers(root: Path) -> set[str]:
    p = root / IDENTIFIERS_FILE
    if not p.exists():
        return set()
    out = set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        h = ln.split("#", 1)[0].strip().lower()
        if h:
            if not re.fullmatch(r"[0-9a-f]{64}", h):
                raise ValueError(f"{IDENTIFIERS_FILE}: not a SHA-256 entry: {h[:16]}")
            out.add(h)
    return out


def load_exceptions(root: Path) -> list[dict]:
    p = root / EXCEPTIONS_FILE
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{EXCEPTIONS_FILE}: top level must be a list")
    for i, e in enumerate(data):
        missing = {"path", "rule", "line_sha256", "reason"} - set(e)
        if missing:
            raise ValueError(f"{EXCEPTIONS_FILE}[{i}]: missing {sorted(missing)}")
        if e["rule"] not in RULE_IDS:
            raise ValueError(f"{EXCEPTIONS_FILE}[{i}]: unknown rule {e['rule']!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(e["line_sha256"])):
            raise ValueError(f"{EXCEPTIONS_FILE}[{i}]: line_sha256 is not a SHA-256")
        if not str(e["reason"]).strip():
            raise ValueError(f"{EXCEPTIONS_FILE}[{i}]: empty reason")
    return data


def scan_line(line: str, ids: set[str]):
    """Yield (rule, redacted excerpt) for every match on one line."""
    for rid, rx, _ in COMPILED:
        for m in rx.finditer(line):
            yield rid, redact(m.group(0))
    if ids:
        for m in TOKEN_RE.finditer(line):
            for cand in identifier_candidates(m.group(0)):
                h = sha256(cand)
                if h in ids:
                    yield "private-identifier", f"id:{h[:12]}"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          check=True).stdout


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--hash-identifier":
        print(sha256(normalize_identifier(argv[1])))
        return 0
    if len(argv) == 2 and argv[0] == "--hash-line":
        print(sha256(argv[1].strip()))
        return 0
    if argv:
        print("usage: pre_publish_check.py [--hash-identifier NAME | --hash-line TEXT]")
        return 2

    root = Path(git("rev-parse", "--show-toplevel").strip())
    try:
        ids = load_identifiers(root)
        exceptions = load_exceptions(root)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1
    exc_keys = {(e["path"], e["rule"], e["line_sha256"]) for e in exceptions}
    used: set[tuple] = set()

    files = [ln for ln in git("-C", str(root), "ls-files").splitlines() if ln.strip()]
    path_hits: list[str] = []
    marker_hits: list[str] = []

    for f in files:
        for pat, why in FORBIDDEN_PATHS:
            if re.search(pat, f):
                path_hits.append(f"{f}  [{why}]")
                break

    for f in files:
        try:
            text = (root / f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            line_hash = None
            for rid, excerpt in scan_line(line, ids):
                line_hash = line_hash or sha256(line.strip())
                key = (f, rid, line_hash)
                if key in exc_keys:
                    used.add(key)
                    continue
                marker_hits.append(
                    f"{f}:{n}  [{rid}]  {excerpt}  line_sha256={line_hash[:16]}")

    stale = [f"{k[0]}  [{k[1]}]  line_sha256={k[2][:16]}"
             for k in sorted(exc_keys - used)]

    print(f"pre-publish check: {len(files)} tracked files, {len(COMPILED) + 1} "
          f"content rules, {len(ids)} private identifiers, "
          f"{len(exceptions)} scoped exceptions")
    if path_hits:
        print("\nFORBIDDEN FILE CLASSES:")
        for h in path_hits:
            print(f"  - {h}")
    if marker_hits:
        print("\nPRIVATE MARKERS IN CONTENT:")
        for h in marker_hits:
            print(f"  - {h}")
    if stale:
        print("\nSTALE EXCEPTIONS (match nothing - remove or re-hash):")
        for h in stale:
            print(f"  - {h}")

    if path_hits or marker_hits or stale:
        print(f"\nFAIL: {len(path_hits)} path finding(s), {len(marker_hits)} marker "
              f"finding(s), {len(stale)} stale exception(s).")
        return 1
    print("PASS: no forbidden files or private markers in the committed tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Public-boundary pre-publish check for claude-tools.

Scans the tracked git content for two classes of problem. The content read is the
git objects, never the working copy: every blob in the index (what is committed or
about to be) plus every HEAD blob the index no longer matches (committed content a
local edit would otherwise hide). Findings in a HEAD-only blob are labeled path@HEAD.

  1. Forbidden file classes  — reports/state/indexes/DBs/binaries/venvs/logs/
     caches/.env and other things that must never be tracked in this public repo.
  2. Private markers in content — local-machine paths (any drive letter, either
     separator, UNC, user homes), Claude Code local-state paths, generated
     worktree names, internal state paths, private identifiers, private
     hosts/IPs, personal emails.

Every match in every file is reported (file:line, rule, redacted excerpt, line
hash) — the private literal itself is never echoed. Evidence is the git tree,
never .gitignore alone. No file is exempt, this one included. Content is decoded
by BOM (UTF-8/16/32) or as strict UTF-8; a blob that decodes as neither, or that
git cannot produce, is itself a finding (undecodable-content / unreadable-blob) —
nothing is skipped or partially decoded.
Exit 0 = clean, 1 = findings, stale exception, or invalid config. There is no
warning-only mode. This is a boundary check, NOT a secret scanner — run Gitleaks too.

Repo-local config (read from the index like all content, both optional; a config
file present on disk but not tracked is a CONFIG ERROR, never silently ignored):
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

import codecs
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
    (r"(^|/)graphify/(graphify\.local(?!\.example\.json$)[^/]*\.json|publish-settings\.lkg\.json|alerts\.json"
     r"|health\.json|health-history\.jsonl|pypi-version-cache\.json|[^/]*-log\.txt)$",
     "graphify machine-local config / runtime state"),
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


def load_identifiers(text: str | None) -> set[str]:
    if text is None:
        return set()
    out = set()
    for ln in text.splitlines():
        h = ln.split("#", 1)[0].strip().lower()
        if h:
            if not re.fullmatch(r"[0-9a-f]{64}", h):
                raise ValueError(f"{IDENTIFIERS_FILE}: not a SHA-256 entry: {h[:16]}")
            out.add(h)
    return out


def load_exceptions(text: str | None) -> list[dict]:
    if text is None:
        return []
    data = json.loads(text)
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


# UTF-32 LE before UTF-16 LE: they share the FF FE prefix.
BOMS = [(codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16")]


def decode(data: bytes) -> str | None:
    """Text of a blob, or None when it is neither BOM-marked Unicode nor strict UTF-8.
    A NUL means binary or BOM-less UTF-16/32 (ASCII in UTF-16 is valid UTF-8 with a
    NUL between every character, which no marker regex would ever match)."""
    for bom, enc in BOMS:
        if data.startswith(bom):
            try:
                text = data.decode(enc)
            except UnicodeDecodeError:
                return None
            return None if "\0" in text else text
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def tracked_blobs(root: Path) -> list[tuple[str, str, str]]:
    """(path, label, blob sha) for every index blob, plus every HEAD blob the index
    does not hold at that path. Gitlinks (submodules) carry no content here."""
    out: list[tuple[str, str, str]] = []
    index: dict[str, str] = {}
    for rec in git("-C", str(root), "ls-files", "-s", "-z").split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, sha, _stage = meta.split()
        if mode == "160000" or index.get(path) == sha:
            continue
        index[path] = sha
        out.append((path, path, sha))
    has_head = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", "HEAD"],
                              capture_output=True).returncode == 0
    if has_head:
        for rec in git("-C", str(root), "ls-tree", "-r", "-z", "HEAD").split("\0"):
            if not rec:
                continue
            meta, path = rec.split("\t", 1)
            _mode, kind, sha = meta.split()
            if kind == "blob" and index.get(path) != sha:
                out.append((path, f"{path}@HEAD", sha))
    return out


def read_blobs(root: Path, shas: list[str]) -> dict[str, bytes | None]:
    """Blob bytes by sha from one `git cat-file --batch`; None = git could not produce it."""
    unique = list(dict.fromkeys(shas))
    if not unique:
        return {}
    raw = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
                         input="".join(s + "\n" for s in unique).encode("ascii"),
                         capture_output=True, check=True).stdout
    out: dict[str, bytes | None] = {}
    pos = 0
    for sha in unique:
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].decode("ascii").split()
        pos = nl + 1
        if len(header) == 3 and header[1] == "blob":
            size = int(header[2])
            out[sha] = raw[pos:pos + size]
            pos += size + 1
        else:
            out[sha] = None
    return out


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
    blobs = tracked_blobs(root)
    contents = read_blobs(root, [sha for _, _, sha in blobs])
    index_sha = {path: sha for path, label, sha in blobs if path == label}

    def config_text(rel: str) -> str | None:
        if rel in index_sha:
            data = contents[index_sha[rel]]
            text = decode(data) if data is not None else None
            if text is None:
                raise ValueError(f"{rel}: not readable as text from the index")
            return text
        if (root / rel).exists():
            raise ValueError(f"{rel}: present on disk but not tracked - add it or remove it")
        return None

    try:
        ids = load_identifiers(config_text(IDENTIFIERS_FILE))
        exceptions = load_exceptions(config_text(EXCEPTIONS_FILE))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1
    exc_keys = {(e["path"], e["rule"], e["line_sha256"]) for e in exceptions}
    used: set[tuple] = set()

    files = sorted({path for path, _, _ in blobs})
    path_hits: list[str] = []
    marker_hits: list[str] = []
    content_hits: list[str] = []

    for f in files:
        for pat, why in FORBIDDEN_PATHS:
            if re.search(pat, f):
                path_hits.append(f"{f}  [{why}]")
                break

    for f, label, sha in blobs:
        data = contents[sha]
        if data is None:
            content_hits.append(f"{label}  [unreadable-blob]  {sha[:12]}")
            continue
        text = decode(data)
        if text is None:
            content_hits.append(f"{label}  [undecodable-content]  neither BOM-marked "
                                f"Unicode nor strict UTF-8, so it cannot be scanned")
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
                    f"{label}:{n}  [{rid}]  {excerpt}  line_sha256={line_hash[:16]}")

    stale = [f"{k[0]}  [{k[1]}]  line_sha256={k[2][:16]}"
             for k in sorted(exc_keys - used)]

    print(f"pre-publish check: {len(files)} tracked files ({len(blobs)} blobs scanned), "
          f"{len(COMPILED) + 1} content rules, {len(ids)} private identifiers, "
          f"{len(exceptions)} scoped exceptions")
    if path_hits:
        print("\nFORBIDDEN FILE CLASSES:")
        for h in path_hits:
            print(f"  - {h}")
    if marker_hits:
        print("\nPRIVATE MARKERS IN CONTENT:")
        for h in marker_hits:
            print(f"  - {h}")
    if content_hits:
        print("\nUNSCANNABLE CONTENT:")
        for h in content_hits:
            print(f"  - {h}")
    if stale:
        print("\nSTALE EXCEPTIONS (match nothing - remove or re-hash):")
        for h in stale:
            print(f"  - {h}")

    if path_hits or marker_hits or content_hits or stale:
        print(f"\nFAIL: {len(path_hits)} path finding(s), {len(marker_hits)} marker "
              f"finding(s), {len(content_hits)} unscannable file(s), "
              f"{len(stale)} stale exception(s).")
        return 1
    print("PASS: no forbidden files or private markers in the tracked tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

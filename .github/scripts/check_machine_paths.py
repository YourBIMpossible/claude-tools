#!/usr/bin/env python3
"""Fail when a tracked file contains an absolute, machine-specific path.

A committed absolute path (a drive-letter path, a Git-Bash drive path, a user-home
path, a network share) only works on the machine that wrote it; every other clone
gets a broken default, a dead link or a silently wrong instruction. This guard
keeps such paths out of a repository. It is self-contained (Python 3.9+, standard
library only) so any repo can adopt it as-is: copy this file, optionally add an
allowlist, and run it in CI and locally.

Usage:
  python check_machine_paths.py                 # scan the git repo containing the cwd
  python check_machine_paths.py --root DIR      # scan another repo
  python check_machine_paths.py --self-test     # run the built-in rule cases

What is scanned: every file ``git ls-files`` lists (tracked or staged), read from the
working tree. Files with a NUL byte in their first 8 KiB are treated as binary and
skipped (counted in the summary). Nothing else is exempt unless the allowlist says so.

Rules (each match is one finding):
  drive-path   a Windows drive-letter path: a letter, a colon, then either separator
  msys-path    an MSYS / Git-Bash / WSL drive path: a one-letter root directory,
               optionally under mnt, followed by more path
  home-path    a POSIX user-home path: a named directory under home or Users
               (the CI runner's own home is exempt)
  unc-path     a UNC network path: two backslashes, a server, a backslash, a share
The self-test below shows concrete examples of each.

Allowlist (optional): ``.machine-paths-allow.json`` at the repo root (``--allow`` to
override), a JSON list of entries::

  {"path": "vendor/**", "match": "optional regex", "reason": "why this is not a machine path"}

``path`` is a glob over repo-relative POSIX paths (``*`` stays within one directory,
``**`` crosses directories). ``match``, when given, must be found in the offending
line for the entry to apply; without it the entry covers every finding in the
matched files. ``reason`` is required. An entry that covers no finding fails the
run, so the list cannot silently outlive what it was written for.

Exit status: 0 clean, 1 findings or a stale allowlist entry, 2 usage/config error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ALLOW_FILE = ".machine-paths-allow.json"
BINARY_SNIFF = 8192

SEP = r"[\\/]"
RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("drive-path",
     re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:" + SEP + r"+(?=[A-Za-z0-9_.$~%-])"),
     "Windows drive-letter path"),
    ("msys-path",
     re.compile(r"(?<![\w.:/~$@-])/(?:mnt/)?[A-Za-z]/(?=[A-Za-z0-9_.~-])"),
     "MSYS / Git-Bash / WSL drive path"),
    ("home-path",
     re.compile(r"(?<![\w.~$-])/(?:home|Users)/(?!runner/)[A-Za-z_][\w.-]*/"),
     "POSIX user-home path"),
    ("unc-path",
     re.compile(r"(?<![\\\w])\\\\[A-Za-z0-9_$-][A-Za-z0-9._$-]*\\[A-Za-z0-9._$-]"),
     "UNC network path"),
]


class ConfigError(Exception):
    pass


def glob_to_regex(glob: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(r".*")
            i += 2
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def load_allowlist(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path.name}: unreadable or not JSON ({exc})") from exc
    if not isinstance(data, list):
        raise ConfigError(f"{path.name}: must be a JSON list of entries")
    entries = []
    for n, e in enumerate(data, 1):
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not e["path"].strip():
            raise ConfigError(f"{path.name} entry {n}: 'path' (non-empty string) is required")
        if not isinstance(e.get("reason"), str) or not e["reason"].strip():
            raise ConfigError(f"{path.name} entry {n} ({e['path']}): 'reason' is required")
        match = e.get("match")
        if match is not None and not isinstance(match, str):
            raise ConfigError(f"{path.name} entry {n} ({e['path']}): 'match' must be a string")
        try:
            match_re = re.compile(match) if match else None
        except re.error as exc:
            raise ConfigError(f"{path.name} entry {n} ({e['path']}): bad 'match' regex: {exc}") from exc
        entries.append({"n": n, "path": e["path"], "path_re": glob_to_regex(e["path"]),
                        "match_re": match_re, "used": 0})
    return entries


REGEX_CLASS_LETTERS = set("dwsbDWSB")


def scan_line(line: str) -> list[tuple[str, str]]:
    hits = []
    for rid, rx, _ in RULES:
        for m in rx.finditer(line):
            # A regex class escape (a backslash, then d/w/s/b) before a colon is not a drive letter.
            if (rid == "drive-path" and m.start() > 0 and line[m.start() - 1] == "\\"
                    and line[m.start()] in REGEX_CLASS_LETTERS):
                continue
            hits.append((rid, m.group(0)))
    return hits


def tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    except FileNotFoundError as exc:
        raise ConfigError("git not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise ConfigError(f"not a git repository: {root} ({exc.stderr.decode(errors='replace').strip()})") from exc
    return sorted(p for p in out.decode("utf-8", errors="surrogateescape").split("\0") if p)


def check(root: Path, allow_path: Path) -> int:
    allow = load_allowlist(allow_path)
    findings, binary, scanned = [], 0, 0
    for rel in tracked_files(root):
        full = root / rel
        try:
            data = full.read_bytes()
        except FileNotFoundError:
            continue  # deleted in the working tree but still in the index
        except OSError as exc:
            findings.append(f"{rel}: unreadable: {exc.strerror}")
            continue
        if b"\0" in data[:BINARY_SNIFF]:
            binary += 1
            continue
        scanned += 1
        text = data.decode("utf-8", errors="replace")
        candidates = [e for e in allow if e["path_re"].match(rel)]
        for lineno, line in enumerate(text.splitlines(), 1):
            hits = scan_line(line)
            if not hits:
                continue
            covered = None
            for e in candidates:
                if e["match_re"] is None or e["match_re"].search(line):
                    covered = e
                    break
            if covered is not None:
                covered["used"] += len(hits)
                continue
            for rid, text_hit in hits:
                excerpt = line.strip()
                if len(excerpt) > 160:
                    excerpt = excerpt[:157] + "..."
                findings.append(f"{rel}:{lineno}: {rid} ({text_hit!r}): {excerpt}")
    stale = [e for e in allow if e["used"] == 0]
    for line in findings:
        print(line)
    for e in stale:
        print(f"{allow_path.name}: entry {e['n']} ({e['path']}) covers nothing - remove it")
    print(f"machine-paths: {scanned} text files scanned, {binary} binary skipped, "
          f"{len(allow)} allowlist entries, {len(findings)} findings, {len(stale)} stale entries")
    if findings or stale:
        print("FAIL: replace machine paths with paths derived at runtime (repo root, home folder, "
              "per-machine config) or add a justified allowlist entry.")
        return 1
    print("PASS: no machine-specific absolute paths.")
    return 0


# Fixtures are written with markers so that this file holds no literal machine path
# (it must pass this guard, and any stricter boundary check, without exceptions):
# <D> = a drive letter and colon, <M> = an MSYS drive root, <H> = the POSIX home root,
# <U> = a UNC prefix, <C> = the Claude Code state directory name, <CL> = a colon.
_MARKERS = {"<D>": "C" + ":", "<d>": "f" + ":", "<M>": "/" + "c", "<H>": "/" + "home",
            "<UH>": "/" + "Users", "<U>": "\\" + "\\", "<C>": "." + "claude", "<CL>": ":"}


def _expand(line: str) -> str:
    for k, v in _MARKERS.items():
        line = line.replace(k, v)
    return line


SELF_TEST = [
    # (line, expected rule ids)
    (r"see <D>\src\repo\file.md", ["drive-path"]),
    ("open <d>/Tools/reports", ["drive-path"]),
    (r'"<D>\Users\name\x"', ["drive-path"]),
    ("file:///<D>/dir/y", ["drive-path"]),
    ("https://example.com/a/b", []),
    ("regex [A-Za-z]:[\\/] stays clean", []),
    ("ratio 12:30/45", []),
    ("ls <M>/src/repo", ["msys-path"]),
    ("cd /mnt<M>/work", ["msys-path"]),
    ("a path like and/or x/y/z", []),
    ("~/<C>/hooks/x.py", []),
    ("$HOME/<C>", []),
    ("<H>/alice/.bashrc", ["home-path"]),
    ("<UH>/bob/Library/x", ["home-path"]),
    ("<H>/runner/work/repo", []),
    (r"<U>server\share\x", ["unc-path"]),
    (r"printf 'a\n'", []),
    ("{repos}/claude-tools/reports", []),
    (r"regex T\d\d<CL>\d\d<CL>\d\dZ and \w<CL>\s", []),
    (r"extended \\?\<D>\x", ["drive-path"]),
]


def self_test() -> int:
    bad = 0
    for line, want in SELF_TEST:
        got = [rid for rid, _ in scan_line(_expand(line))]
        if got != want:
            bad += 1
            print(f"self-test FAIL: {line!r}: expected {want}, got {got}")
    for glob, path, want in [("vendor/**", "vendor/a/b.py", True), ("*.md", "a/b.md", False),
                             ("**/*.md", "a/b.md", True), ("**/*.md", "b.md", True),
                             ("hooks/tests/*.py", "hooks/tests/x/y.py", False)]:
        if bool(glob_to_regex(glob).match(path)) != want:
            bad += 1
            print(f"self-test FAIL: glob {glob!r} vs {path!r}: expected {want}")
    total = len(SELF_TEST) + 5
    for name, ok in _repo_cases():
        total += 1
        if not ok:
            bad += 1
            print(f"self-test FAIL: repo case: {name}")
    print(f"self-test: {total - bad}/{total} cases pass")
    return 1 if bad else 0


def _repo_cases() -> list[tuple[str, bool]]:
    """End-to-end: a throwaway git repo, scanned with and without an allowlist."""
    import contextlib
    import io
    import tempfile

    def run(root: Path) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                return check(root, root / ALLOW_FILE)
            except ConfigError:
                return 2

    cases = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if subprocess.run(["git", "init", "-q", str(root)], capture_output=True).returncode != 0:
            return [("git init", False)]
        (root / "clean.md").write_text("Reports go to {reports}/x.md and ~/notes.\n", encoding="utf-8")
        (root / "fixture.py").write_text(_expand('P = "<D>/fixture/x"\n'), encoding="utf-8")
        (root / "blob.bin").write_bytes(b"\0" + _expand("<D>/x").encode())
        subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True)
        cases.append(("finding fails the run", run(root) == 1))
        allow = [{"path": "*.py", "match": "fixture", "reason": "synthetic fixture"}]
        (root / ALLOW_FILE).write_text(json.dumps(allow), encoding="utf-8")
        cases.append(("allowlisted finding passes", run(root) == 0))
        allow.append({"path": "docs/**", "reason": "covers nothing"})
        (root / ALLOW_FILE).write_text(json.dumps(allow), encoding="utf-8")
        cases.append(("stale entry fails the run", run(root) == 1))
        (root / ALLOW_FILE).write_text(json.dumps([{"path": "*.py"}]), encoding="utf-8")
        cases.append(("entry without reason is a config error", run(root) == 2))
    return cases


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, help="repository root (default: the git repo containing the cwd)")
    ap.add_argument("--allow", type=Path, help=f"allowlist file (default: <root>/{ALLOW_FILE})")
    ap.add_argument("--self-test", action="store_true", help="run the built-in rule cases and exit")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # findings quote file text; never die on a console codec
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.self_test:
        return self_test()
    try:
        if args.root is None:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
            if top.returncode != 0:
                raise ConfigError("not inside a git repository; pass --root")
            root = Path(top.stdout.strip())
        else:
            root = args.root.resolve()
        return check(root, args.allow or root / ALLOW_FILE)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

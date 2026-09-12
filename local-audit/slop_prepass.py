#!/usr/bin/env python3
"""slop_prepass.py — deterministic pattern pre-pass for the local audit lane.

Scans a git repo for candidate "reports success, quietly did nothing" sites:
swallowed failures, fire-and-forget async, and success-summary counters. Output
is a candidate list for an LLM (or human) to CLASSIFY — a hit here is not a
finding, it is a place worth reading. Discovery is deterministic; judgment is not
this script's job.

Usage:
  python slop_prepass.py <repo-root> [--since REF] [--json] [--max-snippet N]
  python slop_prepass.py --self-test

Read-only: never writes inside the target repo. Requires git on PATH.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# pattern name -> (regex, note). Grouped per language family by file extension.
PATTERNS: dict[str, list[tuple[str, re.Pattern[str], str]]] = {
    "python": [
        ("bare-except", re.compile(r"^\s*except\s*:"), "bare except"),
        ("except-pass", re.compile(r"^\s*except\b[^:]*:\s*(pass\b|\.\.\.)"), "except with pass/ellipsis on the same line"),
        ("suppress", re.compile(r"contextlib\.suppress\(|with\s+suppress\("), "contextlib.suppress"),
        ("errors-ignore", re.compile(r"errors\s*=\s*[\"']ignore[\"']"), "decode/encode errors silently ignored"),
        ("subprocess-nocheck", re.compile(r"subprocess\.(run|call)\((?![^)\n]*check\s*=\s*True)"), "subprocess without check=True (return code may be unread)"),
    ],
    "jsts": [
        ("empty-catch", re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}"), "empty catch block"),
        ("comment-only-catch", re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*(//[^\n]*|/\*[^*]*\*/)\s*\}"), "comment-only catch block"),
        ("empty-catch-cb", re.compile(r"\.catch\(\s*(\(\s*\w*\s*\)|\w+)?\s*=>\s*\{\s*\}\s*\)"), "empty .catch() handler"),
        ("then-no-catch", re.compile(r"\.then\([^;\n]*\);"), "promise chain terminated at .then (verify a .catch/await exists upstream)"),
        ("void-promise", re.compile(r"^\s*void\s+[A-Za-z_$][\w$.]*\s*\("), "explicitly discarded promise"),
    ],
    "csharp": [
        ("empty-catch", re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}"), "empty catch block"),
        ("async-void", re.compile(r"\basync\s+void\s+\w+\s*\("), "async void (exceptions vaporize)"),
        ("unawaited-task", re.compile(r"^\s*(?:_\s*=\s*)?Task\.(Run|Factory\.StartNew)\("), "fire-and-forget Task (verify the return is observed)"),
    ],
    "powershell": [
        ("silently-continue", re.compile(r"-ErrorAction\s+SilentlyContinue", re.IGNORECASE), "-ErrorAction SilentlyContinue"),
        ("empty-catch", re.compile(r"catch\s*\{\s*\}"), "empty catch block"),
        ("ignore-native-exit", re.compile(r"\|\s*Out-Null\s*$"), "output discarded (verify $LASTEXITCODE is read after native calls)"),
    ],
}

# language-agnostic: success-summary strings whose totals check 2 must trace
COUNTER = re.compile(
    r"(?:print|log|logger|console\.|Write-Host|Write-Output|echo|toast|message|status)"
    r"[^\n]{0,80}?"
    r"(?:succeeded|success|processed|completed|updated|synced|done|all\s+clean|\bok\b)",
    re.IGNORECASE,
)

EXT_LANG = {
    ".py": "python",
    ".js": "jsts", ".jsx": "jsts", ".ts": "jsts", ".tsx": "jsts", ".mjs": "jsts", ".cjs": "jsts",
    ".cs": "csharp",
    ".ps1": "powershell", ".psm1": "powershell",
}

SKIP_DIR_PARTS = {"node_modules", "dist", "build", ".git", "vendor", "__pycache__", "bin", "obj", ".next"}
MAX_FILE_BYTES = 2_000_000


def tracked_files(repo: Path, since: str | None) -> list[Path]:
    if since:
        cmd = ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=d", since]
    else:
        cmd = ["git", "-C", str(repo), "ls-files"]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        sys.exit(f"git failed ({' '.join(cmd)}): {out.stderr.strip()}")
    files = []
    for line in out.stdout.splitlines():
        p = repo / line
        if not p.is_file() or p.suffix.lower() not in EXT_LANG:
            continue
        if any(part in SKIP_DIR_PARTS for part in p.parts):
            continue
        files.append(p)
    return files


def is_testish(rel: str) -> bool:
    low = rel.lower()
    return any(t in low for t in ("test", "spec", "fixture", "mock"))


def scan_file(repo: Path, path: Path, max_snippet: int) -> list[dict]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lang = EXT_LANG[path.suffix.lower()]
    rel = path.relative_to(repo).as_posix()
    hits: list[dict] = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        for name, rx, note in PATTERNS[lang]:
            if rx.search(line):
                hits.append({"file": rel, "line": i, "check": "swallow", "pattern": name,
                             "note": note, "snippet": line.strip()[:max_snippet]})
        if COUNTER.search(line):
            hits.append({"file": rel, "line": i, "check": "counter", "pattern": "success-summary",
                         "note": "trace every skip/error path into this total", "snippet": line.strip()[:max_snippet]})
    # python: except-header line whose entire body is a lone pass/ellipsis on the next line
    if lang == "python":
        for i, line in enumerate(lines[:-1], 1):
            if re.match(r"^\s*except\b[^:]*:\s*(#[^\n]*)?$", line) and lines[i].strip() in ("pass", "..."):
                if not any(h["line"] == i and h["check"] == "swallow" for h in hits):
                    hits.append({"file": rel, "line": i, "check": "swallow", "pattern": "except-pass-nextline",
                                 "note": "except body is a lone pass/ellipsis", "snippet": line.strip()[:max_snippet]})
    # multi-line empty/comment-only catch bodies that single-line regexes miss
    if lang in ("jsts", "csharp"):
        for m in re.finditer(r"catch\s*(\([^)]*\))?\s*\{([^{}]*)\}", text):
            body = re.sub(r"//[^\n]*|/\*.*?\*/", "", m.group(2), flags=re.DOTALL).strip()
            if not body:
                ln = text.count("\n", 0, m.start()) + 1
                if not any(h["line"] == ln and h["check"] == "swallow" for h in hits):
                    hits.append({"file": rel, "line": ln, "check": "swallow", "pattern": "empty-catch-multiline",
                                 "note": "catch body empty across lines", "snippet": " ".join(m.group(0).split())[:max_snippet]})
    for h in hits:
        h["testish"] = is_testish(rel)
    return hits


def render_markdown(repo: Path, since: str | None, hits: list[dict]) -> str:
    out = [f"# Slop pre-pass — {repo}", ""]
    out.append(f"Window: {'diff since ' + since if since else 'all tracked files'}. "
               f"{len(hits)} candidate sites. Candidates need CLASSIFICATION, they are not findings.")
    out.append("")
    for check, title in (("swallow", "Swallowed-failure candidates"), ("counter", "Success-summary counters")):
        rows = [h for h in hits if h["check"] == check]
        out.append(f"## {title} ({len(rows)})")
        out.append("")
        if rows:
            out.append("| file:line | pattern | test-ish | snippet |")
            out.append("|---|---|---|---|")
            for h in rows:
                snip = h["snippet"].replace("|", "\\|")
                out.append(f"| {h['file']}:{h['line']} | {h['pattern']} | {'y' if h['testish'] else ''} | `{snip}` |")
        out.append("")
    return "\n".join(out)


def self_test() -> int:
    cases = [
        ("python", "except:", "bare-except", True),
        ("python", "    except ValueError: pass", "except-pass", True),
        ("python", "    except ValueError as e:", "except-pass", False),
        ("python", "subprocess.run(cmd)", "subprocess-nocheck", True),
        ("python", "subprocess.run(cmd, check=True)", "subprocess-nocheck", False),
        ("jsts", "} catch (e) {}", "empty-catch", True),
        ("jsts", "promise.then(handle);", "then-no-catch", True),
        ("csharp", "async void OnClick(object s) {", "async-void", True),
        ("csharp", "public async Task Save() {", "async-void", False),
        ("powershell", "Remove-Item $p -ErrorAction SilentlyContinue", "silently-continue", True),
    ]
    failed = 0
    for lang, line, pattern, expect in cases:
        rx = next(r for n, r, _ in PATTERNS[lang] if n == pattern)
        got = bool(rx.search(line))
        if got != expect:
            failed += 1
            print(f"FAIL [{lang}/{pattern}] expect={expect} got={got}: {line}")
    counter_yes = 'log.info(f"{n} rooms processed")'
    counter_no = "total = a + b"
    if not COUNTER.search(counter_yes) or COUNTER.search(counter_no):
        failed += 1
        print("FAIL counter heuristic")
    print(f"self-test: {len(cases) + 1 - failed}/{len(cases) + 1} passed")
    return 1 if failed else 0


def main() -> int:
    # Windows consoles default to cp1252; snippets contain arbitrary source text
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", nargs="?", help="repo root to scan")
    ap.add_argument("--since", help="git ref: scan only files changed since this ref")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    ap.add_argument("--max-snippet", type=int, default=160)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.repo:
        ap.error("repo path required (or --self-test)")
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        sys.exit(f"not a git repo root: {repo}")

    hits: list[dict] = []
    for f in tracked_files(repo, args.since):
        hits.extend(scan_file(repo, f, args.max_snippet))
    hits.sort(key=lambda h: (h["file"], h["line"]))

    if args.json:
        print(json.dumps({"repo": str(repo), "since": args.since, "candidates": hits}, indent=1))
    else:
        print(render_markdown(repo, args.since, hits))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""local_audit.py — batch local second-opinion audit.

Orchestrates the whole local-audit lane in one command: runs the deterministic
pre-pass over a repo, then hands each flagged file to the local Ollama model for
CLASSIFICATION, and assembles one combined findings report.

The findings come from the LOCAL model (a decorrelated second opinion), not from
whoever runs this. Every finding is a HYPOTHESIS — worth checking, not a verdict.

Usage:
  python local_audit.py <repo-root> [--since REF] [--full]
                        [--model qwen3-coder-32k:latest] [--max-files N]
                        [--host http://localhost:11434] [--out DIR]

Read-only on the target repo. The report is written under --out (default the
local-audit out/ folder), never inside the audited repo. On-demand only — do NOT
schedule this (a weak model's misses go unnoticed unattended).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREPASS = HERE / "slop_prepass.py"
MAX_FILE_CHARS = 48_000

SYSTEM = (
    "READ-ONLY audit assistant. Findings only. Every finding must have: file and "
    "line range, a concrete failure scenario (inputs/state that produce the wrong "
    "outcome), severity (CRITICAL/HIGH/MEDIUM/LOW), why existing tests miss it, and "
    "the smallest proving test. Anything without a failure scenario is not a finding. "
    "No style nits, no generic advice, no code restatement, no praise. Label every "
    "finding HYPOTHESIS unless you can point to the exact code path that proves it."
)

LENS = (
    "Audit the provided code for defects that report success while quietly doing "
    "nothing. Use the pre-pass candidate rows as starting points but read the "
    "surrounding code before deciding. Three checks: (1) swallowed failures — empty/"
    "broad catches, unread return codes, floating promises, async-void; (2) success "
    "counters or status that exclude skipped/failed items; (3) tests that exercise "
    "code with no production callers. For each real issue, name what dies, who is "
    "never told, and what the user sees instead. If a candidate is fine, say so in one "
    "line and move on."
)


def run_prepass(repo: Path, since: str | None) -> list[dict]:
    cmd = [sys.executable, str(PREPASS), str(repo), "--json"]
    if since:
        cmd += ["--since", since]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        sys.exit(f"pre-pass failed:\n{out.stderr.strip()}")
    return json.loads(out.stdout)["candidates"]


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """Collapse candidates that share a (line, pattern) — duplicates bloat the prompt
    and push the local model toward repetition loops."""
    seen: set[tuple[int, str]] = set()
    out: list[dict] = []
    for c in sorted(rows, key=lambda r: (r["line"], r["pattern"])):
        key = (c["line"], c["pattern"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def ollama_chat(host: str, model: str, file_rel: str, source: str, rows: list[dict], timeout: int) -> str:
    table = "\n".join(f"- L{c['line']} [{c['pattern']}] {c['snippet']}" for c in rows)
    truncated = len(source) > MAX_FILE_CHARS
    if truncated:
        source = source[:MAX_FILE_CHARS] + "\n… (file truncated for context window)"
    user = (
        f"{LENS}\n\nPre-pass candidates in this file:\n{table}\n\n"
        f"File: {file_rel}{' (TRUNCATED)' if truncated else ''}\n```\n{source}\n```"
    )
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "stream": False,
        # repeat_penalty/repeat_last_n suppress the loop where a small model emits the
        # same finding dozens of times until it hits num_predict and truncates.
        "options": {"temperature": 0.2, "num_predict": 2500,
                    "repeat_penalty": 1.3, "repeat_last_n": 256},
    }).encode("utf-8")
    req = urllib.request.Request(f"{host}/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["message"]["content"].strip()


def check_ollama(host: str, model: str) -> None:
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=10) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        sys.exit(f"Ollama not reachable at {host} ({e}). Start Ollama and retry.")
    names = {m["name"] for m in tags.get("models", [])}
    if model not in names:
        sys.exit(f"Model '{model}' not installed. Have: {', '.join(sorted(names)) or '(none)'}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo")
    ap.add_argument("--since", help="git ref: audit only files changed since this ref")
    ap.add_argument("--full", action="store_true", help="ignore --since; audit all flagged files")
    ap.add_argument("--model", default="qwen3-coder-32k:latest")
    ap.add_argument("--max-files", type=int, default=25, help="cap files sent to the model (default 25)")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        sys.exit(f"not a git repo root: {repo}")
    since = None if args.full else args.since
    check_ollama(args.host, args.model)

    candidates = run_prepass(repo, since)
    by_file: dict[str, list[dict]] = {}
    for c in candidates:
        if c.get("testish"):
            continue  # test files: the pre-pass flags them but they are not shipped code
        by_file.setdefault(c["file"], []).append(c)
    if not by_file:
        print("No non-test candidates found in scope — nothing to audit.")
        return 0

    ranked = sorted(by_file.items(), key=lambda kv: len(kv[1]), reverse=True)
    selected = ranked[: args.max_files]
    skipped = ranked[args.max_files:]

    print(f"Auditing {len(selected)} of {len(ranked)} flagged files with {args.model} "
          f"(~1 min each) ...", flush=True)

    results: list[tuple[str, int, str]] = []
    for i, (rel, rows) in enumerate(selected, 1):
        print(f"  [{i}/{len(selected)}] {rel} ({len(rows)} candidates)", flush=True)
        try:
            source = (repo / rel).read_text(encoding="utf-8", errors="replace")
            findings = ollama_chat(args.host, args.model, rel, source, dedupe_rows(rows), args.timeout)
        except (OSError, urllib.error.URLError, TimeoutError, KeyError) as e:
            findings = f"**ERROR auditing this file:** {e}"
        results.append((rel, len(rows), findings))

    now = _dt.datetime.now()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"{repo.name}-local-audit-{now:%Y-%m-%d_%H%M%S}.md"

    lines = [
        f"# Local audit (second opinion) — {repo.name}",
        "",
        f"- Repo: `{repo}`",
        f"- Model: `{args.model}` (local, {args.host})",
        f"- Scope: {'full' if args.full else (f'changed since {since}' if since else 'all tracked files')}",
        f"- Files audited: {len(selected)} of {len(ranked)} flagged "
        f"(non-test); cap --max-files={args.max_files}",
        f"- Generated: {now:%Y-%m-%d %H:%M:%S}",
        "",
        "> Findings are the LOCAL model's — a decorrelated second opinion. Every one is a "
        "HYPOTHESIS: worth checking, not a verdict. This lane is a backup to the primary "
        "Claude-side audits, not a replacement.",
        "",
    ]
    if skipped:
        lines += [
            f"## Skipped for the file cap ({len(skipped)}) — re-run with a higher --max-files to include",
            "",
            *[f"- {rel} ({len(rows)} candidates)" for rel, rows in skipped],
            "",
        ]
    lines += ["---", ""]
    for rel, n, findings in results:
        lines += [f"## {rel} ({n} candidates)", "", findings, ""]

    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {report}")
    print(f"Audited {len(selected)} files; {len(skipped)} skipped for the cap.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

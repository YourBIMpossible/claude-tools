#!/usr/bin/env python3
"""ctxcheck - local repo reality-check CLI.

Validates that a repo's documented claims still match reality:

  files      required docs/anchor files exist
  refs       paths referenced from markdown docs still exist
  env        declared env var names appear in their declaration files
  compose    declared docker-compose service names exist in the compose file
  endpoints  route strings appear in the declared source globs
  links      commit SHAs resolve in a known repo; ADR references resolve to files
  commands   a claimed command actually runs in the declared environment
  staleness  context files older than a threshold are flagged for review

Single file, stdlib only. Config is TOML (tomllib), either <repo>/.ctxcheck.toml
or a named config under <this-dir>/configs/<name>.toml.

Severity model: anything the config DECLARES must hold -> FAIL when it doesn't.
Anything ctxcheck DISCOVERS by scanning (doc path refs, commit SHAs) -> WARN,
because scans have false positives. staleness is always WARN (flag for review,
not a defect). Exit 0 = no FAIL, exit 1 = FAILs (or WARNs under --strict),
exit 2 = usage/config error.

Deliberately out of scope (see claude-profile docs/adr/0001): context manifests,
MCP wrappers, retrieval-eval machinery. ctxcheck reads and reports; it never
indexes, rewrites, or serves anything.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob as globmod
import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
CATEGORIES = (
    "files", "refs", "env", "compose", "endpoints", "links", "commands", "staleness",
)

# --- result collection ------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def add(self, status: str, category: str, message: str) -> None:
        self.results.append({"status": status, "category": category, "message": message})

    def counts(self) -> dict:
        c = {PASS: 0, WARN: 0, FAIL: 0}
        for r in self.results:
            c[r["status"]] += 1
        return c


# --- helpers ----------------------------------------------------------------


def norm(p: str) -> str:
    return p.replace("\\", "/")


def resolve_ref(token: str, repo: Path, doc_dir: Path) -> bool:
    """Does a path-looking token resolve to something real?"""
    t = token.strip().strip('"').strip("'")
    # strip line-number suffixes like path.py:42 (windows drive colon is position 1)
    m = re.match(r"^(.*?):(\d+)$", t)
    if m and len(m.group(1)) > 2:
        t = m.group(1)
    p = Path(t)
    if p.is_absolute():
        return p.exists()
    return (repo / t).exists() or (doc_dir / t).exists()


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_MD_LINK_RE = re.compile(r"\]\(([^)\s#]+)")
_CODE_TOKEN_RE = re.compile(r"`([^`\n]+)`")


def looks_like_path(tok: str) -> bool:
    t = tok.strip()
    if not t or len(t) > 500 or " " in t or "\t" in t:
        return False
    if _SCHEME_RE.match(t):
        return False
    if any(ch in t for ch in "*?<>|$(){}"):
        return False
    if "/" not in t and "\\" not in t:
        return False
    # command-flag fragments and bare separators
    if t.startswith("-") or t in ("/", "\\"):
        return False
    last = norm(t).rstrip("/").rsplit("/", 1)[-1]
    # require an extension in the last segment, or an explicit trailing slash
    return ("." in last and not last.startswith(".git")) or t.endswith(("/", "\\"))


def extract_path_refs(text: str) -> list[tuple[int, str]]:
    """(line_number, token) candidates that look like local paths."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        for rx in (_MD_LINK_RE, _CODE_TOKEN_RE):
            for m in rx.finditer(line):
                tok = m.group(1)
                if looks_like_path(tok):
                    out.append((i, tok))
    return out


def git_ok(repo: Path) -> bool:
    return (repo / ".git").exists()


def git_commit_exists(repo: Path, sha: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def git_last_commit_ts(repo: Path, path: Path) -> float | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%ct", "--", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        s = r.stdout.strip()
        return float(s) if r.returncode == 0 and s else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def glob_repo(repo: Path, patterns: list[str]) -> list[Path]:
    seen: dict[Path, None] = {}
    for pat in patterns:
        for hit in globmod.glob(str(repo / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen[p] = None
    return list(seen)


# --- checks -----------------------------------------------------------------


def check_files(cfg: dict, repo: Path, rep: Report) -> None:
    for item in cfg.get("require", []):
        if globmod.has_magic(item):
            hits = globmod.glob(str(repo / item), recursive=True)
            if hits:
                rep.add(PASS, "files", f"exists: {item} ({len(hits)} match(es))")
            else:
                rep.add(FAIL, "files", f"missing required file(s): {item}")
        elif (repo / item).exists() or Path(item).is_absolute() and Path(item).exists():
            rep.add(PASS, "files", f"exists: {item}")
        else:
            rep.add(FAIL, "files", f"missing required file: {item}")


_WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+)\]\]")


def extract_md_link_targets(text: str) -> list[tuple[int, str]]:
    """(line_number, target) for every markdown link target - no path-shape
    filter, because in strict scans the doc's links ARE declared claims."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in _MD_LINK_RE.finditer(line):
            tok = m.group(1)
            if not _SCHEME_RE.match(tok) and not tok.startswith("#"):
                out.append((i, tok))
    return out


def check_refs(cfg: dict, repo: Path, rep: Report) -> None:
    for item in cfg.get("require", []):
        p = Path(item)
        ok = p.exists() if p.is_absolute() else (repo / item).exists()
        rep.add(PASS if ok else FAIL, "refs",
                f"declared ref {'exists' if ok else 'MISSING'}: {item}")
    ignore = cfg.get("ignore", [])
    # scan_strict: an index/registry doc whose markdown links are declared
    # claims - every link target must exist, misses FAIL (not WARN).
    for doc in glob_repo(repo, cfg.get("scan_strict", [])):
        text = doc.read_text(encoding="utf-8", errors="replace")
        rel_doc = norm(str(doc.relative_to(repo)))
        broken = 0
        for line_no, tok in extract_md_link_targets(text):
            if any(fnmatch.fnmatch(tok, pat) or fnmatch.fnmatch(norm(tok), pat)
                   for pat in ignore):
                continue
            if not resolve_ref(tok, repo, doc.parent):
                rep.add(FAIL, "refs",
                        f"{rel_doc}:{line_no} index claims missing target: {tok}")
                broken += 1
        if not broken:
            rep.add(PASS, "refs", f"strict-scanned {rel_doc}: all link targets exist")
    # wikilinks: [[name]] -> name.md next to the doc (or at repo root).
    # Forward links to not-yet-written notes are legitimate, so misses WARN.
    for doc in glob_repo(repo, cfg.get("wikilinks", [])):
        text = doc.read_text(encoding="utf-8", errors="replace")
        rel_doc = norm(str(doc.relative_to(repo)))
        broken = 0
        for i, line in enumerate(text.splitlines(), 1):
            for m in _WIKILINK_RE.finditer(line):
                name = m.group(1).strip()
                if not (doc.parent / f"{name}.md").exists() \
                        and not (repo / f"{name}.md").exists():
                    rep.add(WARN, "refs",
                            f"{rel_doc}:{i} wikilink [[{name}]] has no {name}.md yet")
                    broken += 1
        if not broken:
            rep.add(PASS, "refs", f"wikilinks in {rel_doc} all resolve")
    for doc in glob_repo(repo, cfg.get("scan", [])):
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            rep.add(WARN, "refs", f"unreadable doc {doc}: {e}")
            continue
        rel_doc = norm(str(doc.relative_to(repo)))
        broken = 0
        for line_no, tok in extract_path_refs(text):
            if any(fnmatch.fnmatch(tok, pat) or fnmatch.fnmatch(norm(tok), pat)
                   for pat in ignore):
                continue
            if not resolve_ref(tok, repo, doc.parent):
                rep.add(WARN, "refs", f"{rel_doc}:{line_no} references missing path: {tok}")
                broken += 1
        if not broken:
            rep.add(PASS, "refs", f"scanned {rel_doc}: all path refs resolve")


def check_env(cfg: dict, repo: Path, rep: Report) -> None:
    declared_in = cfg.get("declared_in", [])
    texts: dict[str, str] = {}
    for f in declared_in:
        p = repo / f
        if p.exists():
            texts[f] = p.read_text(encoding="utf-8", errors="replace")
        else:
            rep.add(FAIL, "env", f"declaration file missing: {f}")
    for name in cfg.get("names", []):
        rx = re.compile(rf"\b{re.escape(name)}\b")
        hits = [f for f, t in texts.items() if rx.search(t)]
        if hits:
            rep.add(PASS, "env", f"{name} declared in {', '.join(hits)}")
        else:
            rep.add(FAIL, "env", f"env var {name} not declared in any of: "
                                 f"{', '.join(declared_in) or '(none)'}")


def compose_service_names(text: str) -> list[str]:
    """Naive top-level `services:` block parse - service names only."""
    names: list[str] = []
    in_services = False
    svc_indent: int | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            in_services = raw.strip() == "services:"
            svc_indent = None
            continue
        if in_services:
            if svc_indent is None:
                svc_indent = indent
            if indent == svc_indent:
                m = re.match(r"^([A-Za-z0-9._-]+):\s*(#.*)?$", raw.strip())
                if m:
                    names.append(m.group(1))
    return names


def check_compose(cfg_list: list[dict], repo: Path, rep: Report) -> None:
    for cfg in cfg_list:
        f = cfg.get("file", "docker-compose.yml")
        p = repo / f
        if not p.exists():
            rep.add(FAIL, "compose", f"compose file missing: {f}")
            continue
        found = compose_service_names(p.read_text(encoding="utf-8", errors="replace"))
        for svc in cfg.get("services", []):
            if svc in found:
                rep.add(PASS, "compose", f"service '{svc}' defined in {f}")
            else:
                rep.add(FAIL, "compose", f"service '{svc}' NOT defined in {f} "
                                         f"(found: {', '.join(found) or 'none'})")


def check_endpoints(cfg_list: list[dict], repo: Path, rep: Report) -> None:
    for cfg in cfg_list:
        files = glob_repo(repo, cfg.get("appear_in", []))
        label = cfg.get("label", ", ".join(cfg.get("appear_in", [])))
        if not files:
            rep.add(FAIL, "endpoints", f"no files match appear_in globs: {label}")
            continue
        for route in cfg.get("routes", []):
            hit = next((f for f in files
                        if route in f.read_text(encoding="utf-8", errors="replace")), None)
            if hit:
                rep.add(PASS, "endpoints",
                        f"route '{route}' found in {norm(str(hit.relative_to(repo)))}")
            else:
                rep.add(FAIL, "endpoints",
                        f"route '{route}' appears in NO file matching: {label}")


_COMMIT_RE = re.compile(r"`([0-9a-f]{7,40})`")
_ADR_RE = re.compile(r"(?:ADR[- ]?(\d{4})|(?:docs/)?adr/(\d{4})[\w.-]*)", re.I)


def check_links(cfg: dict, repo: Path, rep: Report) -> None:
    adr_dir_raw = cfg.get("adr_dir", "")
    adr_dir = Path(adr_dir_raw) if adr_dir_raw else None
    if adr_dir is not None and not adr_dir.is_absolute():
        adr_dir = repo / adr_dir
    commit_repos = [Path(c) if Path(c).is_absolute() else repo / c
                    for c in cfg.get("commit_repos", ["."])]
    commit_repos = [c for c in commit_repos if git_ok(c)]
    check_commits = cfg.get("check_commits", True)

    for doc in glob_repo(repo, cfg.get("docs", [])):
        text = doc.read_text(encoding="utf-8", errors="replace")
        rel_doc = norm(str(doc.relative_to(repo)))
        issues = 0
        if check_commits and commit_repos:
            for sha in dict.fromkeys(_COMMIT_RE.findall(text)):
                if any(c.isdigit() for c in sha) is False:
                    continue  # all-letter hex words ("deadbee") are usually prose
                if not any(git_commit_exists(r, sha) for r in commit_repos):
                    rep.add(WARN, "links",
                            f"{rel_doc}: commit `{sha}` not found in any known repo")
                    issues += 1
        if adr_dir is not None:
            for m in _ADR_RE.finditer(text):
                num = m.group(1) or m.group(2)
                if not globmod.glob(str(adr_dir / f"{num}-*.md")) \
                        and not (adr_dir / f"{num}.md").exists():
                    rep.add(WARN, "links",
                            f"{rel_doc}: ADR {num} has no file under {norm(str(adr_dir))}")
                    issues += 1
        if not issues:
            rep.add(PASS, "links", f"{rel_doc}: all commit/ADR references resolve")


def check_commands(cfg_list: list[dict], repo: Path, rep: Report) -> None:
    for cfg in cfg_list:
        cmd = cfg.get("run", "")
        if not cmd:
            rep.add(FAIL, "commands", "config entry with no 'run' value")
            continue
        cwd = repo / cfg.get("cwd", ".")
        timeout = cfg.get("timeout", 60)
        expect = cfg.get("expect_exit", 0)
        try:
            # shell=True is intentional: 'run' comes from ctxcheck's own local
            # config (trusted, owner-authored — never remote/user input), and
            # claimed commands must run exactly as a human would type them,
            # including Windows .cmd shims (npm, npx) and pipelines.
            r = subprocess.run(cmd, shell=True, cwd=str(cwd),
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            rep.add(FAIL, "commands", f"timed out after {timeout}s: {cmd}")
            continue
        except OSError as e:
            rep.add(FAIL, "commands", f"could not launch: {cmd} ({e})")
            continue
        if r.returncode == expect:
            rep.add(PASS, "commands", f"ran ok (exit {r.returncode}): {cmd}")
        else:
            tail = (r.stderr or r.stdout).strip().splitlines()
            detail = f" | {tail[-1][:200]}" if tail else ""
            rep.add(FAIL, "commands",
                    f"exit {r.returncode} (expected {expect}): {cmd}{detail}")


def check_staleness(cfg: dict, repo: Path, rep: Report) -> None:
    max_age_days = cfg.get("max_age_days", 90)
    cutoff = time.time() - max_age_days * 86400
    use_git = git_ok(repo)
    fresh = 0
    for f in glob_repo(repo, cfg.get("paths", [])):
        ts = git_last_commit_ts(repo, f) if use_git else None
        if ts is None:
            ts = f.stat().st_mtime
        if ts < cutoff:
            age = int((time.time() - ts) / 86400)
            rep.add(WARN, "staleness",
                    f"{norm(str(f.relative_to(repo)))} last touched {age}d ago "
                    f"(threshold {max_age_days}d) - review for staleness")
        else:
            fresh += 1
    rep.add(PASS, "staleness", f"{fresh} file(s) within {max_age_days}d threshold")


# --- config / CLI -----------------------------------------------------------


def find_config(target: str | None, config_opt: str | None, repo_opt: str | None) -> Path:
    if config_opt:
        return Path(config_opt)
    if target:
        p = Path(target)
        if p.suffix == ".toml":
            return p
        if p.is_dir():
            return p / ".ctxcheck.toml"
        named = SCRIPT_DIR / "configs" / f"{target}.toml"
        if named.exists():
            return named
        raise SystemExit(f"ctxcheck: no config named '{target}' "
                         f"(looked for {named}) and it is not a path")
    if repo_opt:
        return Path(repo_opt) / ".ctxcheck.toml"
    return Path.cwd() / ".ctxcheck.toml"


def run(args: argparse.Namespace) -> int:
    cfg_path = find_config(args.target, args.config, args.repo)
    if not cfg_path.exists():
        print(f"ctxcheck: config not found: {cfg_path}", file=sys.stderr)
        return 2
    try:
        with open(cfg_path, "rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        print(f"ctxcheck: bad TOML in {cfg_path}: {e}", file=sys.stderr)
        return 2

    repo = Path(args.repo or cfg.get("repo") or cfg_path.parent)
    if not repo.is_dir():
        print(f"ctxcheck: repo dir not found: {repo}", file=sys.stderr)
        return 2

    only = set(args.only.split(",")) if args.only else set(CATEGORIES)
    unknown = only - set(CATEGORIES)
    if unknown:
        print(f"ctxcheck: unknown categories: {', '.join(sorted(unknown))}",
              file=sys.stderr)
        return 2

    rep = Report()
    if "files" in only and "files" in cfg:
        check_files(cfg["files"], repo, rep)
    if "refs" in only and "refs" in cfg:
        check_refs(cfg["refs"], repo, rep)
    if "env" in only and "env" in cfg:
        check_env(cfg["env"], repo, rep)
    if "compose" in only and "compose" in cfg:
        check_compose(cfg["compose"], repo, rep)
    if "endpoints" in only and "endpoints" in cfg:
        check_endpoints(cfg["endpoints"], repo, rep)
    if "links" in only and "links" in cfg:
        check_links(cfg["links"], repo, rep)
    if "commands" in only and "commands" in cfg:
        check_commands(cfg["commands"], repo, rep)
    if "staleness" in only and "staleness" in cfg:
        check_staleness(cfg["staleness"], repo, rep)

    counts = rep.counts()
    if args.json:
        print(json.dumps({"repo": str(repo), "config": str(cfg_path),
                          "summary": counts, "results": rep.results}, indent=2))
    else:
        for r in rep.results:
            if r["status"] != PASS or args.verbose:
                print(f"[{r['status']}] {r['category']}: {r['message']}")
        print(f"ctxcheck {repo.name}: {counts[PASS]} pass, "
              f"{counts[WARN]} warn, {counts[FAIL]} fail")

    if counts[FAIL] or (args.strict and counts[WARN]):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ctxcheck",
        description="Local repo reality-check: do documented claims still match reality?",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run", help="run checks from a config")
    rp.add_argument("target", nargs="?",
                    help="named config (configs/<name>.toml), a .toml path, "
                         "or a repo dir containing .ctxcheck.toml")
    rp.add_argument("--repo", help="repo root override")
    rp.add_argument("--config", help="explicit config file path")
    rp.add_argument("--only", help="comma-separated category filter: "
                                   + ",".join(CATEGORIES))
    rp.add_argument("--json", action="store_true", help="machine-readable output")
    rp.add_argument("--strict", action="store_true", help="WARNs also fail the run")
    rp.add_argument("--verbose", action="store_true", help="show PASS lines too")
    rp.set_defaults(func=run)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

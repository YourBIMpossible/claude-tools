#!/usr/bin/env python3
"""CLI-level test suite for ctxcheck - builds a throwaway fixture repo and runs
the real CLI as a subprocess, ctxdex-suite style. No framework, plain checks."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CTXCHECK = str(Path(__file__).resolve().parent / "ctxcheck.py")

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(cond: bool, name: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED += 1
        FAILURES.append(name)
        print(f"FAIL  {name}")


def run(*argv: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, CTXCHECK, *argv],
                          capture_output=True, text=True, timeout=120, cwd=cwd)


def git(repo: Path, *argv: str, env: dict | None = None) -> None:
    import os
    e = dict(os.environ)
    if env:
        e.update(env)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", *argv],
                   capture_output=True, text=True, timeout=30, env=e, check=True)


def build_fixture(root: Path) -> Path:
    repo = root / "fixture-repo"
    repo.mkdir()
    (repo / "README.md").write_text(
        "# Fixture\nSee [the script](scripts/build.py) and `docs/arch.md`.\n"
        "Broken: [gone](scripts/gone.py) and `missing/dir/file.txt`.\n"
        "Ignored: `bash://20260101-000000` style URIs.\n"
        "Line-ref works: `scripts/build.py:12`.\n"
        "Range, not a path: `origin/main..HEAD` and `abc1234...feature/x`.\n",
        encoding="utf-8")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "build.py").write_text("print('build')\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "arch.md").write_text("architecture\n", encoding="utf-8")
    (repo / "docs" / "adr").mkdir()
    (repo / "docs" / "adr" / "0001-first-decision.md").write_text("# 0001\n",
                                                                 encoding="utf-8")
    (repo / ".env.example").write_text("API_KEY_ID=\nDB_URL=\n", encoding="utf-8")
    (repo / "docker-compose.yml").write_text(
        "version: '3'\n"
        "services:\n"
        "  backend:\n"
        "    image: x\n"
        "  frontend:\n"
        "    image: y\n"
        "volumes:\n"
        "  data:\n",
        encoding="utf-8")
    (repo / "backend").mkdir()
    (repo / "backend" / "app.py").write_text(
        "ROUTES = ['/api/projects', '/api/health']\n", encoding="utf-8")
    (repo / "memory").mkdir()
    (repo / "memory" / "good-note.md").write_text(
        "A note linking [[other-note]] and forward [[future-note]].\n",
        encoding="utf-8")
    (repo / "memory" / "other-note.md").write_text("linked target\n", encoding="utf-8")
    (repo / "memory" / "INDEX.md").write_text(
        "- [Good](good-note.md) — exists\n"
        "- [Other](other-note.md) — exists\n"
        "- [Ghost](deleted-note.md) — index rot\n"
        "- [Web](https://example.com) — external, skipped\n",
        encoding="utf-8")
    (repo / "notes").mkdir()
    (repo / "notes" / "old-note.md").write_text("ancient context\n", encoding="utf-8")
    (repo / "notes" / "new-note.md").write_text("fresh context\n", encoding="utf-8")

    git(repo, "init", "-q")
    git(repo, "add", "notes/old-note.md")
    git(repo, "commit", "-q", "-m", "old note",
        env={"GIT_AUTHOR_DATE": "2020-01-01T00:00:00",
             "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"})
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def head_sha(repo: Path) -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=15)
    return r.stdout.strip()


def write_cfg(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def run_checks() -> list[str]:
    """Run every check once; return the names of the failing ones."""
    global PASSED, FAILED
    PASSED = FAILED = 0
    FAILURES.clear()
    tmp = Path(tempfile.mkdtemp(prefix="ctxcheck-test-"))
    try:
        repo = build_fixture(tmp)
        sha = head_sha(repo)

        # link-fixture doc: one good commit, one bogus, one good ADR, one missing ADR
        (repo / "docs" / "linked.md").write_text(
            f"Landed in `{sha[:9]}`. Bogus: `1234567890abcdef1234`.\n"
            "Per ADR-0001 and ADR-0042.\n", encoding="utf-8")

        # --- happy-path config -------------------------------------------
        good = write_cfg(tmp / "good.toml", f"""
repo = {json.dumps(str(repo))}

[files]
require = ["README.md", "docs/*.md"]

[refs]
scan = ["README.md"]
ignore = ["bash://*"]
require = ["scripts/build.py"]

[env]
names = ["API_KEY_ID", "DB_URL"]
declared_in = [".env.example"]

[[compose]]
file = "docker-compose.yml"
services = ["backend", "frontend"]

[[endpoints]]
routes = ["/api/projects", "/api/health"]
appear_in = ["backend/**/*.py"]

[links]
docs = ["docs/linked.md"]
adr_dir = "docs/adr"

[[commands]]
run = "{Path(sys.executable).as_posix()} -c \\"print('hi')\\""
timeout = 30

[staleness]
paths = ["notes/*.md"]
max_age_days = 90
""")
        r = run("run", str(good), "--json")
        check(r.returncode == 0, "happy config exits 0 (WARNs allowed)")
        data = json.loads(r.stdout)
        check(data["summary"]["FAIL"] == 0, "happy config has zero FAILs")
        msgs = [x["message"] for x in data["results"]]
        stat = {x["message"]: x["status"] for x in data["results"]}

        check(any("exists: README.md" in m for m in msgs), "files: README passes")
        check(any("docs/*.md" in m and "match" in m for m in msgs), "files: glob require matches")
        check(any("scripts/build.py" in m and "exists" in m for m in msgs),
              "refs: declared ref passes")
        check(any("scripts/gone.py" in m for m in msgs), "refs: broken md link warned")
        check(any("missing/dir/file.txt" in m for m in msgs), "refs: broken code-ref warned")
        check(not any("bash://" in m and "missing" in m for m in msgs),
              "refs: ignore pattern suppresses bash:// URIs")
        check(not any("build.py:12" in m for m in msgs), "refs: path:line suffix resolves")
        check(not any("origin/main..HEAD" in m or "abc1234...feature/x" in m for m in msgs),
              "refs: git revision ranges are not treated as paths")
        check(any(m.startswith("API_KEY_ID declared") for m in msgs), "env: declared name passes")
        check(any("service 'backend' defined" in m for m in msgs), "compose: backend found")
        check(not any("'data'" in m for m in msgs), "compose: volumes not parsed as services")
        check(any("route '/api/projects' found" in m for m in msgs), "endpoints: route found")
        check(any(sha[:9] in m and stat[m] == "PASS" for m in msgs) is False
              and not any(f"commit `{sha[:9]}`" in m for m in msgs),
              "links: real commit not warned")
        check(any("1234567890abcdef1234" in m for m in msgs), "links: bogus commit warned")
        check(not any("ADR 0001" in m for m in msgs), "links: existing ADR resolves")
        check(any("ADR 0042" in m for m in msgs), "links: missing ADR warned")
        check(any("ran ok" in m for m in msgs), "commands: claimed command runs")
        check(any("old-note.md" in m and "review for staleness" in m for m in msgs),
              "staleness: old file flagged")
        check(not any("new-note.md" in m for m in msgs), "staleness: fresh file not flagged")
        warn_msgs = [x["message"] for x in data["results"] if x["status"] == "WARN"]
        check(all(x["status"] == "WARN" for x in data["results"]
                  if "references missing path" in x["message"]),
              "refs: scan misses are WARN not FAIL")
        check(len(warn_msgs) >= 4, "expected WARNs present")

        # --- strict index scan + wikilinks (memory-lint behaviors) ---------
        mem = write_cfg(tmp / "mem.toml", f"""
repo = {json.dumps(str(repo))}

[refs]
scan_strict = ["memory/INDEX.md"]
wikilinks = ["memory/*.md"]
""")
        r = run("run", str(mem), "--json")
        data = json.loads(r.stdout)
        mmsgs = {x["message"]: x["status"] for x in data["results"]}
        check(r.returncode == 1, "index rot makes strict scan exit 1")
        check(any("deleted-note.md" in m and s == "FAIL" for m, s in mmsgs.items()),
              "scan_strict: missing index target FAILs")
        check(not any("https://example.com" in m for m in mmsgs),
              "scan_strict: external links skipped")
        check(not any("other-note.md" in m and s != "PASS" for m, s in mmsgs.items()),
              "scan_strict: existing extensionless-dir link target passes")
        check(any("[[future-note]]" in m and s == "WARN" for m, s in mmsgs.items()),
              "wikilinks: forward link WARNs, not FAILs")
        check(not any("[[other-note]]" in m for m in mmsgs),
              "wikilinks: resolving link not flagged")

        # strict mode: WARNs flip exit to 1
        r = run("run", str(good), "--strict")
        check(r.returncode == 1, "--strict makes WARNs fail the run")

        # --only filter
        r = run("run", str(good), "--only", "files", "--json")
        data = json.loads(r.stdout)
        check(all(x["category"] == "files" for x in data["results"]),
              "--only files filters categories")
        r = run("run", str(good), "--only", "nonsense")
        check(r.returncode == 2, "unknown --only category exits 2")

        # --- failing config ----------------------------------------------
        bad = write_cfg(tmp / "bad.toml", f"""
repo = {json.dumps(str(repo))}

[files]
require = ["DOES-NOT-EXIST.md"]

[refs]
require = ["scripts/also-gone.py"]

[env]
names = ["NOT_DECLARED_ANYWHERE"]
declared_in = [".env.example", "no-such-file.env"]

[[compose]]
file = "docker-compose.yml"
services = ["database"]

[[endpoints]]
routes = ["/api/never"]
appear_in = ["backend/**/*.py"]

[[endpoints]]
routes = ["/x"]
appear_in = ["no-dir/**/*.zz"]

[[commands]]
run = "{Path(sys.executable).as_posix()} -c \\"import sys; sys.exit(3)\\""

[[commands]]
run = "{Path(sys.executable).as_posix()} -c \\"import sys; sys.exit(5)\\""
expect_exit = 5

[[commands]]
run = "{Path(sys.executable).as_posix()} -c \\"import time; time.sleep(30)\\""
timeout = 2
""")
        r = run("run", str(bad), "--json")
        check(r.returncode == 1, "failing config exits 1")
        data = json.loads(r.stdout)
        fails = [x["message"] for x in data["results"] if x["status"] == "FAIL"]
        check(any("DOES-NOT-EXIST.md" in m for m in fails), "files: missing required FAILs")
        check(any("also-gone.py" in m for m in fails), "refs: declared missing FAILs")
        check(any("NOT_DECLARED_ANYWHERE" in m for m in fails), "env: undeclared name FAILs")
        check(any("no-such-file.env" in m for m in fails), "env: missing declaration file FAILs")
        check(any("'database' NOT defined" in m and "backend" in m for m in fails),
              "compose: absent service FAILs and lists found")
        check(any("/api/never" in m for m in fails), "endpoints: absent route FAILs")
        check(any("no files match" in m for m in fails), "endpoints: dead glob FAILs")
        check(any("exit 3 (expected 0)" in m for m in fails), "commands: wrong exit FAILs")
        check(any("timed out after 2s" in m for m in fails), "commands: timeout FAILs")
        passes = [x["message"] for x in data["results"] if x["status"] == "PASS"]
        check(any("exit 5" in m for m in passes), "commands: expect_exit honored")

        # --- CLI / config plumbing ---------------------------------------
        r = run("run", str(tmp / "nope.toml"))
        check(r.returncode == 2, "missing config exits 2")
        broken = write_cfg(tmp / "broken.toml", "not [ valid toml ===")
        r = run("run", str(broken))
        check(r.returncode == 2, "bad TOML exits 2")

        # in-repo .ctxcheck.toml discovered from repo dir target
        write_cfg(repo / ".ctxcheck.toml", "[files]\nrequire = [\"README.md\"]\n")
        r = run("run", str(repo))
        check(r.returncode == 0, "repo-dir target finds .ctxcheck.toml")

        # human output has the summary line
        r = run("run", str(good))
        check("pass," in r.stdout and "warn," in r.stdout and "fail" in r.stdout,
              "human output prints summary line")
        check(r.stdout.count("[PASS]") == 0, "human output hides PASS by default")
        r = run("run", str(good), "--verbose")
        check("[PASS]" in r.stdout, "--verbose shows PASS lines")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return list(FAILURES)


def test_ctxcheck_suite() -> None:
    """pytest entry point - the same 48 checks as `python test_ctxcheck.py`."""
    failures = run_checks()
    assert PASSED > 0, "suite ran zero checks"
    assert not failures, "failing checks:\n  " + "\n  ".join(failures)


def main() -> int:
    return 1 if run_checks() else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tests for tools/pre_publish_check.py.

No framework, plain checks (ctxdex-suite style). Each case builds a throwaway git
repo with synthetic content only, runs the checker as a subprocess (the way CI
and release-check do) and asserts the exit code and redacted report lines.
Violating literals are assembled at runtime so this file itself stays clean.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

CHECKER = Path(__file__).resolve().parent / "pre_publish_check.py"
BS = "\\"
DRIVE = "Q:"                        # synthetic drive letter
SYNTHETIC_ID = "acme-hidden-repo"   # synthetic private identifier

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED += 1
        FAILURES.append(name)
        print(f"FAIL  {name}\n{detail}")


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def write(root: Path, rel: str, content: str | bytes) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")


def run_checker(root: Path) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(CHECKER)], cwd=root,
                       capture_output=True, text=True, encoding="utf-8")
    return r.returncode, r.stdout + r.stderr


def run_repo(files: dict[str, str | bytes], *, identifiers: list[str] | None = None,
             exceptions: list[dict] | None = None) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        if identifiers is not None:
            files = {**files, "tools/private-identifiers.sha256":
                     "".join(sha(i) + "\n" for i in identifiers)}
        if exceptions is not None:
            files = {**files, "tools/public-boundary-exceptions.json": json.dumps(exceptions)}
        for rel, content in files.items():
            write(root, rel, content)
        git(root, "add", "--", *files)
        return run_checker(root)


def run_committed(committed: dict[str, str], edits: dict[str, str], *,
                  stage_edits: bool) -> tuple[int, str]:
    """Commit `committed`, then overwrite files in the working copy (optionally staged)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        for rel, text in committed.items():
            write(root, rel, text)
        git(root, "add", "--", *committed)
        git(root, "-c", "user.name=t", "-c", "user.email=t@example.com",
            "commit", "-q", "-m", "c")
        for rel, text in edits.items():
            write(root, rel, text)
        if stage_edits:
            git(root, "add", "--", *edits)
        return run_checker(root)


def findings(out: str, rule: str) -> list[str]:
    return [ln for ln in out.splitlines() if f"[{rule}]" in ln]


def case(name: str, text: str, rule: str, *, count: int = 1,
         identifiers: list[str] | None = None, leak: str | None = None) -> None:
    code, out = run_repo({"doc.md": text}, identifiers=identifiers)
    hits = findings(out, rule)
    check(f"{name}: exit nonzero", code == 1, out)
    check(f"{name}: {count} [{rule}] finding(s) reported", len(hits) == count, out)
    check(f"{name}: file:line reported", all(h.strip().startswith("- doc.md:") for h in hits), out)
    if leak:
        check(f"{name}: literal redacted from output", leak not in out, out)


def main() -> int:
    case("backslash drive path", f"data in {DRIVE}{BS}Private{BS}notes.txt\n",
         "windows-drive-path", leak="Private")
    case("forward-slash drive path", f"see {DRIVE}/Private/notes.txt\n",
         "windows-drive-path", leak="Private")
    case("multiple violations in one file all reported",
         f"a {DRIVE}{BS}one\nb {DRIVE}/two and {DRIVE}{BS}three\n\nc {DRIVE}/four\n",
         "windows-drive-path", count=4)
    code, out = run_repo({"doc.md": f"a {DRIVE}{BS}one\nb {DRIVE}/two\n"})
    lines = sorted(h.split()[1] for h in findings(out, "windows-drive-path"))
    check("multiple violations carry distinct line numbers",
          lines == ["doc.md:1", "doc.md:2"], out)
    case("UNC path", f"share at {BS}{BS}fileserver{BS}team{BS}plans\n", "unc-path",
         leak="fileserver")
    case("tilde claude home", "state lives in ~" + "/.claude/projects\n", "claude-user-home")
    case("USERPROFILE claude home", f"%USERPROFILE%{BS}.claude{BS}skills\n",
         "claude-user-home")
    case("claude worktree path (forward)", "cd .claude" + "/worktrees/lane-x\n",
         "claude-worktree-path")
    case("claude worktree path (backslash)", f".claude{BS}worktrees{BS}lane-x\n",
         "claude-worktree-path")
    case("user home path", "cache at /home" + "/someone/.cache/x\n", "user-home-path",
         leak="someone")
    case("generated worktree name", "branch claude/brave-otter" + "-4f2a9c merged\n",
         "worktree-autoname")
    case("private identifier (exact)", f"clone {SYNTHETIC_ID} first\n",
         "private-identifier", identifiers=[SYNTHETIC_ID], leak=SYNTHETIC_ID)
    case("private identifier (case/separator variant, inside a path)",
         "open ../Acme_Hidden.Repo/README.md\n", "private-identifier",
         identifiers=[SYNTHETIC_ID], leak="Acme_Hidden")

    code, out = run_repo({"doc.md": "See docs/public-boundary.md and ./tools/x.py, "
                          "https://example.com/a, ../sibling/file.txt, a:b ratio.\n"},
                         identifiers=[SYNTHETIC_ID])
    check("public-safe relative paths/URLs: exit 0", code == 0, out)
    check("public-safe relative paths/URLs: PASS line", "PASS" in out, out)
    code, out = run_repo({"doc.md": "the acme product and a hidden repo\n"},
                         identifiers=[SYNTHETIC_ID])
    check("identifier parts alone do not trigger", code == 0, out)

    bad_line = f"fixture {DRIVE}{BS}fixture{BS}path"
    exc = {"path": "doc.md", "rule": "windows-drive-path",
           "line_sha256": sha(bad_line), "reason": "synthetic fixture"}
    code, out = run_repo({"doc.md": bad_line + "\n"}, exceptions=[exc])
    check("scoped exception suppresses exactly its line", code == 0, out)
    code, out = run_repo({"doc.md": bad_line + "\n" + f"other {DRIVE}{BS}x\n"},
                         exceptions=[exc])
    check("scoped exception does not cover other lines",
          code == 1 and len(findings(out, "windows-drive-path")) == 1, out)
    code, out = run_repo({"doc.md": bad_line + f" {BS}{BS}srv{BS}share\n"}, exceptions=[exc])
    check("scoped exception does not cover the line after an edit", code == 1, out)
    code, out = run_repo({"other.md": bad_line + "\n"}, exceptions=[exc])
    check("scoped exception is path-bound", code == 1, out)
    code, out = run_repo({"doc.md": bad_line + "\n"},
                         exceptions=[{**exc, "rule": "unc-path"}])
    check("scoped exception is rule-bound (and the unused one is stale)",
          code == 1 and "STALE" in out, out)
    code, out = run_repo({"doc.md": "clean\n"}, exceptions=[exc])
    check("stale exception fails", code == 1 and "STALE" in out, out)
    code, out = run_repo({"doc.md": "clean\n"}, exceptions=[{**exc, "reason": " "}])
    check("exception without a reason is rejected", code == 1 and "CONFIG ERROR" in out, out)

    marker = f"see {DRIVE}{BS}Private{BS}notes.txt\n"
    for name, data in (("utf-16-le", marker.encode("utf-16")),   # PS 5.1 Out-File default
                       ("utf-16-be", codecs.BOM_UTF16_BE + marker.encode("utf-16-be")),
                       ("utf-32", marker.encode("utf-32")),
                       ("utf-8-sig", marker.encode("utf-8-sig"))):
        code, out = run_repo({"doc.md": data})
        check(f"{name} content (BOM) is decoded and scanned",
              code == 1 and len(findings(out, "windows-drive-path")) == 1, out)
    code, out = run_repo({"doc.md": marker.encode("utf-16-le")})
    check("BOM-less UTF-16 is an undecodable-content finding, not a skip",
          code == 1 and len(findings(out, "undecodable-content")) == 1, out)
    code, out = run_repo({"doc.md": b"ok\n\xff\xfe\xfd not utf-8\n"})
    check("invalid UTF-8 is an undecodable-content finding",
          code == 1 and len(findings(out, "undecodable-content")) == 1
          and "unscannable" in out, out)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        git(root, "update-index", "--add", "--info-only", "--cacheinfo",
            "100644," + "ab" * 20 + ",ghost.md")
        code, out = run_checker(root)
    check("an index blob git cannot produce is an unreadable-blob finding",
          code == 1 and len(findings(out, "unreadable-blob")) == 1, out)
    code, out = run_committed({"doc.md": marker}, {"doc.md": "clean\n"}, stage_edits=False)
    check("unstaged clean working copy does not hide a committed marker",
          code == 1 and any(h.strip().startswith("- doc.md:1") for h in
                            findings(out, "windows-drive-path")), out)
    code, out = run_committed({"doc.md": marker}, {"doc.md": "clean\n"}, stage_edits=True)
    check("staged clean edit still reports the committed marker as path@HEAD",
          code == 1 and any(h.strip().startswith("- doc.md@HEAD:1") for h in
                            findings(out, "windows-drive-path")), out)
    code, out = run_committed({"doc.md": "clean\n"}, {"doc.md": marker}, stage_edits=False)
    check("unstaged working-copy text is not the published tree", code == 0, out)
    code, out = run_committed({"doc.md": "clean\n"}, {"doc.md": marker}, stage_edits=True)
    check("staged marker is caught before commit", code == 1, out)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        write(root, "doc.md", marker)
        write(root, "tools/public-boundary-exceptions.json", json.dumps([exc]))
        git(root, "add", "--", "doc.md")
        code, out = run_checker(root)
    check("untracked exceptions file is a config error, never silently applied",
          code == 1 and "CONFIG ERROR" in out, out)

    code, out = run_repo({"state/queue.yaml": "x: 1\n"})
    check("forbidden file class fails", code == 1 and "FORBIDDEN" in out, out)
    for local in ("graphify/graphify.local.json", "graphify/graphify.local.bak.json",
                  "graphify/publish-settings.lkg.json", "graphify/alerts.json", "graphify/refresh-log.txt"):
        code, out = run_repo({local: "{}\n"})
        check(f"graphify local state is forbidden: {local}", code == 1 and "FORBIDDEN" in out, out)
    code, out = run_repo({"graphify/graphify.local.example.json": "{}\n"})
    check("graphify config template stays publishable", code == 0, out)

    print(f"\n{PASSED} passed, {FAILED} failed")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

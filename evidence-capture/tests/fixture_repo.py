#!/usr/bin/env python3
"""Synthetic source repositories for the clone-builder proofs (plan §7).

``build_source`` makes a repository with a short pre-start history, hands it to an
``at_start`` callback (which may dirty it and take the start snapshot), then records the
post-start history a replay must never see:

* ``post_commits`` commits on ``main`` beyond the start (79 by default, matching the
  real failure), one adding a file whose content is a sentinel and one rewriting a
  pre-start file with another sentinel;
* a post-start branch ``feature`` carrying a sentinel;
* an annotated tag (sentinel in its message) and a lightweight tag on post-start commits;
* a reflog-only commit (committed, then reset away) carrying a sentinel;
* a stash whose content carries a sentinel;
* everything repacked, so the source object store is a real pack.

The post-start history is written with one ``git fast-import`` so the fixture costs a
handful of process spawns, not hundreds. Synthetic example — contains no private
repository data.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Callable

EPOCH = 1767225600  # 2026-01-01T00:00:00Z
IDENT = "t <t@example.invalid>"
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def git(repo: Path, *args: str, input_: bytes | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=input_, capture_output=True, env=GIT_ENV)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "replace")


def _data(text: str) -> bytes:
    raw = text.encode("utf-8")
    return b"data %d\n" % len(raw) + raw + b"\n"


def base_repo(root: Path, name: str = "source", *, symlink: bool = False) -> Path:
    """Pre-start history: three commits, a nested file, an ignore rule, optionally a symlink."""
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_bytes(b"*.log\n")
    (repo / "README.md").write_bytes(b"# fixture\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pre 1")
    (repo / "src").mkdir()
    (repo / "src" / "alpha.py").write_bytes(b"def alpha(x):\n    return x + 1\n\n\n"
                                            b"def beta(y):\n    return y * 2\n")
    (repo / "pre.txt").write_bytes(b"pre-start content\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pre 2")
    (repo / "notes.txt").write_bytes(b"one\ntwo\nthree\n")
    git(repo, "add", "-A")
    if symlink:
        sha = git(repo, "hash-object", "-w", "--stdin", input_=b"README.md").strip()
        git(repo, "update-index", "--add", "--cacheinfo", f"120000,{sha},link")
    git(repo, "commit", "-q", "-m", "pre 3")
    if symlink:
        git(repo, "checkout-index", "-f", "link")
    return repo


def add_post_start(repo: Path, start: str, post_commits: int = 79) -> list[str]:
    """Write the post-start history described in the module docstring; return the sentinels."""
    tag = uuid.uuid4().hex
    s = {k: f"SENTINEL-{k}-{tag}" for k in ("new", "rewrite", "branch", "tag", "orphan", "stash")}
    git(repo, "reset", "-q", "--hard", start)
    git(repo, "clean", "-q", "-fdx")
    stream = bytearray()
    t = EPOCH
    for i in range(1, post_commits + 1):
        t += 60
        stream += b"commit refs/heads/main\nmark :%d\ncommitter %s %d +0000\n" % (i, IDENT.encode(), t)
        stream += _data(f"post-start commit {i}")
        if i == 1:
            stream += b"from " + start.encode() + b"\n"
        stream += b"M 100644 inline log.txt\n" + _data("\n".join(f"line {j}" for j in range(1, i + 1)))
        if i == 5:
            stream += b"M 100644 inline new.txt\n" + _data(s["new"])
        if i == 10:
            stream += b"M 100644 inline pre.txt\n" + _data(s["rewrite"])
    t += 60
    stream += b"commit refs/heads/feature\nmark :9001\ncommitter %s %d +0000\n" % (IDENT.encode(), t)
    stream += _data("post-start branch") + b"from " + start.encode() + b"\n"
    stream += b"M 100644 inline feature.txt\n" + _data(s["branch"])
    stream += b"tag v-post\nfrom :%d\ntagger %s %d +0000\n" % (post_commits, IDENT.encode(), t)
    stream += _data(f"annotated post-start tag {s['tag']}")
    stream += b"reset refs/tags/lt-post\nfrom :%d\n\n" % max(1, post_commits // 2)
    git(repo, "fast-import", "--quiet", "--force", input_=bytes(stream))
    git(repo, "reset", "-q", "--hard", "main")

    (repo / "orphan.txt").write_bytes(s["orphan"].encode())
    git(repo, "add", "orphan.txt")
    git(repo, "commit", "-q", "-m", "reflog-only commit")
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    (repo / "README.md").write_bytes(("# fixture\n" + s["stash"] + "\n").encode())
    git(repo, "stash", "push", "-q", "-m", "fixture stash")
    git(repo, "repack", "-a", "-d", "-q")
    return list(s.values())


def build_source(root: Path, at_start: Callable[[Path, str], None] | None = None, *, post_commits: int = 79,
                 symlink: bool = False, name: str = "source") -> tuple[Path, str, list[str]]:
    """``(repo, start_sha, sentinels)``; ``at_start(repo, start_sha)`` runs at the start."""
    repo = base_repo(root, name, symlink=symlink)
    start = git(repo, "rev-parse", "HEAD").strip()
    if at_start is not None:
        at_start(repo, start)
    sentinels = add_post_start(repo, start, post_commits)
    return repo, start, sentinels

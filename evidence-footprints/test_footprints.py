#!/usr/bin/env python3
"""Unit tests for footprints.py and shell_paths.py.

No framework, plain checks (ctxdex-suite style, matching ctxcheck/test_ctxcheck.py).
Runs against synthetic tempfile fixtures only — never a real episode file, packet
store, transcript, or repository.

Synthetic example — contains no private repository data or production findings.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import footprints as fpm  # noqa: E402
import shell_paths as sp  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(cond: bool, name: str) -> None:
    (PASSED if cond else FAILED).append(name)
    print(("  ok  " if cond else "  FAIL ") + name)


def paths(cmd: str) -> list[tuple[str, str]]:
    segs, script = sp.parse_command(cmd)
    return [(u.role, u.path) for s in segs for u in s.paths] + [(u.role, u.path) for u in script]


def queries(cmd: str) -> list[str]:
    return [q for s in sp.parse_command(cmd)[0] for q in s.queries]


def kinds(cmd: str) -> list[str]:
    return [s.kind for s in sp.parse_command(cmd)[0]]


def test_shell_paths() -> None:
    check(paths("grep -rn parse_row src/app.py") == [("search", "src/app.py")]
          and queries("grep -rn parse_row src/app.py") == ["parse_row"],
          "grep: bundled flags, first positional is the query")
    check(paths("grep -n -A 3 needle notes.md") == [("search", "notes.md")],
          "grep: -A consumes its value")
    check(paths("cat a.py b.py | head -n 5") == [("read", "a.py"), ("read", "b.py")],
          "cat reads every positional; head -n value is not a path")
    check(paths("sed -n '1,20p' src/x.py") == [("read", "src/x.py")], "sed: the script is not a path")
    check(paths("git show HEAD:src/x.py") == [("read", "src/x.py")], "git show rev:path")
    check(paths("git log --oneline 1a2b3c4..HEAD -- src/y.py") == [("read", "src/y.py")],
          "git log: paths after --, the range is not a path")
    check(paths("git add docs/n.md") == [("write", "docs/n.md")], "git add is a write role")
    check(paths("Get-Content -Path .\\src\\z.ps1 -TotalCount 5") == [("read", "./src/z.ps1")],
          "Get-Content -Path, backslashes normalized")
    check(queries("Select-String -Pattern Needle -Path lib/a.cs") == ["Needle"]
          and paths("Select-String -Pattern Needle -Path lib/a.cs") == [("search", "lib/a.cs")],
          "Select-String: -Pattern is a query, -Path a path")
    check(paths("python tool.py > out/report.txt") == [("write", "out/report.txt"), ("exec", "tool.py")],
          "redirect target is a write; script is exec")
    check(paths("echo hi 2>&1 > /dev/null") == [], "fd duplication and /dev/null are not paths")
    check(paths("x=$(cat conf/a.json); echo $x") == [("read", "conf/a.json")],
          "command substitution parsed as its own command")
    check(paths("$n = (Get-Content data/b.txt).Length") == [("read", "data/b.txt")],
          "PowerShell parenthesized group parsed as its own command")
    check(paths("python - <<'EOF'\nopen('pkg/c.py').read()\nEOF") == [("script", "pkg/c.py")],
          "heredoc body literal reported with role script")
    check(kinds("git commit -m @'\nsubject line with src/d.py\n'@") == ["git"],
          "mid-line PowerShell here-string body dropped")
    check(kinds("frobnicate src/e.py") == ["miss"], "unknown verb with a path is a miss")
    check(kinds('cat "src/f.py') == ["miss"], "unbalanced quote is a miss")
    check(kinds("# cat src/g.py") == ["comment"], "comment line has no paths")
    check(not sp.is_filelike("7c5b90d..head") and not sp.is_filelike("a/*.py,b/*.py")
          and sp.is_filelike("docs/../x.md") and sp.is_filelike("docker/.env")
          and sp.is_filelike("app/[id]/page.tsx") and not sp.is_filelike("src/[ab].py"),
          "is_filelike rejects git ranges and glob lists, keeps real files")


REPO = "/srv/fixture-repo"
WT = REPO + "/.claude/worktrees/w1"


def item(iid: str, score: float, selected: bool, refs: list[str], statement: str = "",
         kind: str = "lexical_match") -> dict:
    return {"id": iid, "final_score": score, "selected": selected, "references": refs,
            "statement": statement, "kind": kind}


def call(seq: int, name: str, inp: dict) -> dict:
    return {"seq": seq, "name": name, "input": inp, "is_error": False}


def row(pid: str, created: str, items: list[dict], calls: list[dict], *, head: str = "0" * 40,
        prompt: str = "fix the parser", prior: dict | None = None, dirty: int = 0,
        root: str = WT) -> dict:
    return {
        "packet_id": pid, "join_reason": "joined", "traffic": "candidate",
        "identity": {"repository_root": root, "created_at": created, "head": head,
                     "head_state": "resolved", "intent": "debugging"},
        "retrieved": {"items": items, "scope": {"sources": ["prompt_symbol", "git_diff"]},
                      "collectors_run": [
                          {"name": "git", "diagnostic": {"dirty_count": dirty}},
                          {"name": "ripgrep", "diagnostic": {"truncated": False,
                                                             "filename_stem": {"items": 1}}}]},
        "observed": {"tool_calls": calls},
        "prompt": {"text": prompt, "origin": "user", "prior_prompts": {} if prior is None else prior},
    }


def episode_a(head: str) -> dict:
    items = [item("ev_a", 5.0, True, ["src/a.py:10"], "parse_row at src/a.py:10  |  def parse_row(line, strict=True):"),
             item("ev_b", 3.0, False, ["src/b.py:4"]),
             item("ev_g", 1.0, False, [], kind="git_meta")]
    calls = [call(0, "Grep", {"pattern": "parse_row", "path": "src"}),
             call(1, "Read", {"file_path": REPO + "/src/a.py"}),
             call(2, "Bash", {"command": "cat src/c.py && frobnicate src/q.py"}),
             call(3, "Read", {"file_path": WT + "/src/b.py"}),
             call(4, "Edit", {"file_path": WT + "/src/b.py", "old_string": "x", "new_string": "def parse_row(line, strict=True):"}),
             call(5, "Agent", {"prompt": "check"}),
             call(6, "Read", {"file_path": "/home/someone/scratch/notes.md"}),
             call(7, "Read", {"file_path": WT + "/.githooks/pre-push"})]
    return row("ep_000000000000000a", "2026-09-21T00:00:00Z", items, calls, head=head)


def episode_b(head: str) -> dict:
    items = [item("ev_c", 4.0, True, ["src/c.py:1"])]
    calls = [call(0, "Read", {"file_path": "src/c.py"}),
             call(1, "Write", {"file_path": "src/c.py", "content": "y"})]
    return row("ep_000000000000000b", "2026-09-21T00:05:00Z", items, calls, head=head,
               prior={"user": 1})


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def test_footprints(tmp: Path) -> None:
    fp = fpm.Footprint(episode_a("0" * 40))
    check(fp.touched == {"src/a.py", "src/c.py", "src/b.py", ".githooks/pre-push"} and fp.outside == 1,
          "touches: native + shell, roots stripped; extensionless native target kept; outside counted")
    check(fp.edited == {"src/b.py"}, "edits: native edit targets")
    check(fp.delegated and len(fp.unparsed) == 1 and fp.unparsed[0]["reason"] == "unknown verb frobnicate",
          "delegation flagged; unknown shell verb logged, not guessed")
    check(fpm.norm("C:\\Other\\x.py", [WT, REPO]) is None and fpm.norm("./.env", [WT]) == ".env",
          "norm: outside roots is None; leading ./ stripped without eating dotfiles")

    repo = tmp / "fixture-repo"
    (repo / "src").mkdir(parents=True)
    for name in ("a.py", "b.py", "c.py"):
        (repo / "src" / name).write_text("x\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "fixture")
    head = git(repo, "rev-parse", "HEAD")
    root = str(repo).replace("\\", "/")
    ea, eb = episode_a(head), episode_b(head)
    for e in (ea, eb):
        e["identity"]["repository_root"] = root + "/.claude/worktrees/w1"
        for c in e["observed"]["tool_calls"]:
            for k in ("file_path",):
                if k in c["input"]:
                    c["input"][k] = c["input"][k].replace(REPO, root)
    lost = {"packet_id": "ep_000000000000000c", "join_reason": "missing_packet"}
    episodes = tmp / "episodes.jsonl"
    episodes.write_text("".join(json.dumps(r) + "\n" for r in (eb, lost, ea)), encoding="utf-8")

    out1, out2 = tmp / "out1", tmp / "out2"
    s = fpm.run(episodes, out1, True)
    fpm.run(episodes, out2, True)
    rows = {r["packet_id"]: r for r in map(json.loads, (out1 / "footprints.jsonl").read_text("utf-8").splitlines())}
    check(list(rows) == sorted(rows) and len(rows) == 3, "every episode row covered, sorted by packet_id")
    check(rows["ep_000000000000000c"]["status"] == "not_joined:missing_packet", "unjoined rows carry status")
    m = rows["ep_000000000000000a"]["metrics"]
    check(m["hit"] is True and m["coverage"] == 0.25 and m["waste"] == 0.0,
          "hit, coverage, waste")
    check(m["rediscovery"] == 1.0, "rediscovery: a search for a selected lexical symbol")
    check(m["gap"] == 1.0 and m["gap_in_packet"] == 1.0 and m["gap_unretrieved"] == 0.0,
          "gap split: the edited file was retrieved but omitted")
    check(m["head_start"] == 0.0 and m["calls_to_target"] == 3 and m["first_target_cited"] is False,
          "head start 0 when no edited file was cited; calls to target counted")
    check(m["mrr"] == 0.5 and m["recall@1"] == 0.0 and m["recall@3"] == 1.0, "ARB-mapped recall@k and MRR")
    check(m["brief_strings"] == 1 and m["brief_strings_reused"] == 1, "brief string reused in a tool input")
    mb = rows["ep_000000000000000b"]["metrics"]
    check(mb["head_start"] == 1.0 and mb["first_target_cited"] is True and mb["gap"] == 0.0,
          "cited target touched on the first call")
    check(rows["ep_000000000000000a"]["baseline"]["hit"] == 1.0
          and rows["ep_000000000000000b"]["baseline"]["hit"] == 0.0, "shuffled same-repo baseline (mean over shifts)")
    facts = rows["ep_000000000000000a"]["facts"]
    check({f["kind"] for f in facts} == {"cited_path", "omitted_path", "native_touch", "shell_path", "brief_string"}
          and all(f["id"] == fpm.fact_id(f["kind"], f["value"]) for f in facts), "stable fact IDs per kind")
    pool = {p["packet_id"]: p for p in map(json.loads, (out1 / "pool.jsonl").read_text("utf-8").splitlines())}
    pa = pool["ep_000000000000000a"]
    check(pa["verdicts"]["dependencies_tracked"] is False and pa["untracked"] == [".githooks/pre-push"]
          and pa["verdicts"]["standalone"] is True and pa["eligible"] is False,
          "pool: a file read but absent at HEAD is an untracked dependency")
    check(pool["ep_000000000000000b"]["verdicts"]["dependencies_tracked"] is True,
          "pool: a file present at HEAD is tracked")
    check(pool["ep_000000000000000b"]["verdicts"]["standalone"] is False, "pool: a later prompt is not standalone")
    check(s["parse"]["unparsed"] == 1 and s["pool"]["funnel"][0] == {"filter": "standalone", "remaining": 1},
          "summary: parse misses and pool funnel")
    check(all((out1 / n).read_bytes() == (out2 / n).read_bytes()
              for n in ("footprints.jsonl", "unparsed.jsonl", "pool.jsonl", "summary.json", "distributions.md")),
          "two runs are byte-identical")
    fpm.run(episodes, tmp / "out3", False)
    p3 = json.loads((tmp / "out3" / "pool.jsonl").read_text("utf-8").splitlines()[0])
    check(p3["verdicts"]["dependencies_tracked"] is None and p3["eligible"] is False,
          "--no-git: dependency check unknown, never eligible")


def main() -> int:
    test_shell_paths()
    with tempfile.TemporaryDirectory() as d:
        test_footprints(Path(d))
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

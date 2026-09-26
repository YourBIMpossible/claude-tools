#!/usr/bin/env python3
"""Unit tests for the Twin harness.

No framework, plain checks. Synthetic fixtures only: a tempfile copy of a tiny
golden repository, the Lab cases in ``lab/`` and ``fake_claude.py`` in place of
the real CLI. No model call, no network, no real repository or episode file.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import twin  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
FAKE = str(HERE / "fake_claude.py")
GOLDEN = {
    "README.md": "# Golden fixture\n",
    "src/alpha.py": ('class AlphaService:\n    def __init__(self, factor: int = 2) -> None:\n'
                     '        self.factor = factor\n\n    def run(self, value: int) -> int:\n'
                     '        return compute_alpha(value) * self.factor\n\n\n'
                     'def compute_alpha(value: int) -> int:\n    return (value * 3) + 1\n'),
    "src/beta.py": ('from .alpha import AlphaService\n\n\ndef build_service() -> AlphaService:\n'
                    '    return AlphaService(factor=4)\n\n\ndef total(values: list[int]) -> int:\n'
                    '    service = build_service()\n    return sum(service.run(v) for v in values)\n'),
}
# A minimal correct edit per case, to prove each verify.py can pass and fail.
SOLVE = {
    "pos_region": ("src/regions/r7.py", "LIMIT = 17", "LIMIT = 40"),
    "pos_engine": ("src/engine/k_c.py", "value * 3 + 2", "value * 3 + 1"),
    "neg_irrelevant": ("src/beta.py", "factor=4", "factor=5"),
    "mis_old_copy": ("src/beta.py", "sum(service.run(v) for v in values)",
                     "sum(service.run(v) for v in values if v >= 0)"),
    "reason_rule": ("src/regions/r9.py", "LIMIT = 19", "LIMIT = 40"),
}


def check(cond: bool, name: str) -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"  {'ok ' if cond else 'FAIL'} {name}")


def golden(root: Path) -> Path:
    for rel, text in GOLDEN.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def verify(task: dict, repo: Path) -> bool:
    return subprocess.run([sys.executable, task["verify"], str(repo)], cwd=repo, capture_output=True).returncode == 0


def test_lab(tmp: Path) -> list[dict]:
    gold = golden(tmp / "golden")
    tasks = twin.tasks_lab(HERE / "lab", gold)
    check(sorted(t["family"] for t in tasks) == ["misleading", "negative", "positive", "positive", "reasoning"],
          "five Lab controls: 2 positive, 1 negative, 1 misleading, 1 reasoning")
    check(all("{head}" in t["brief"] and "<context_brief" in t["brief"] for t in tasks),
          "every brief uses the adapter format with a head placeholder")
    for t in tasks:
        repo = tmp / "prep" / t["task_id"]
        head = twin.prepare(t["prepare"], repo)
        check(len(head) == 40 and not twin.git(repo, "remote").strip(), f"{t['task_id']}: fixture is a remote-less git repo")
        check(all((repo / p).is_file() for p in t["target_files"] + t["planted_files"]),
              f"{t['task_id']}: target and planted files exist")
        check(not verify(t, repo), f"{t['task_id']}: verify fails before the fix")
        rel, old, new = SOLVE[t["task_id"]]
        text = (repo / rel).read_text(encoding="utf-8")
        (repo / rel).write_text(text.replace(old, new), encoding="utf-8")
        check(verify(t, repo), f"{t['task_id']}: verify passes after the fix")
    return tasks


def test_plan(tasks: list[dict]) -> None:
    p1, p2 = twin.plan(tasks, 2, 11), twin.plan(tasks, 2, 11)
    check(p1 == p2, "plan is deterministic for a seed")
    check(len(p1["runs"]) == len(tasks) * 4, "tasks x repeats x arms runs")
    units: dict[tuple, set] = {}
    for r in p1["runs"]:
        units.setdefault((r["task_id"], r["repeat"]), set()).add(r["arm"])
    check(all(v == {"A", "B"} for v in units.values()), "every repeat runs both arms")
    pairs = [p1["runs"][i:i + 2] for i in range(0, len(p1["runs"]), 2)]
    check(all(a["task_id"] == b["task_id"] and a["repeat"] == b["repeat"] for a, b in pairs),
          "arms are interleaved pairwise")
    check(len({r["arm"] for r in p1["runs"][::2]}) == 2, "the first arm of a pair is randomized")


def test_stream() -> None:
    s = twin.StreamState()
    u = {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1}
    u2 = dict(u, output_tokens=5)
    s.feed({"type": "assistant", "message": {"id": "m1", "usage": u, "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/r/a.py"}}]}})
    s.feed({"type": "assistant", "message": {"id": "m1", "usage": u2, "content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/r/a.py"}}]}})
    s.feed({"type": "assistant", "parent_tool_use_id": "tu_x", "message": {"id": "m2", "usage": u, "content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/r/b.py"}}]}})
    check(s.tokens() == 8, "usage is deduplicated per message id and subagent tokens count")
    check([c["name"] for c in s.calls] == ["Read", "Edit"], "subagent tool calls are not main-thread footprint")
    task = {"target_files": ["a.py"], "planted_files": ["b.py"]}
    sc = twin.score_run(s, Path("/r"), task)
    check(sc["reached"] and sc["calls_to_target"] == 1 and sc["tokens_to_target"] == 6, "tokens to first target edit")


def test_hook(tmp: Path) -> None:
    brief = tmp / "brief.md"
    brief.write_text("<context_brief>é</context_brief>", encoding="utf-8")
    hook = str(HERE / "replay_hook.py")
    a = subprocess.run([sys.executable, hook, str(brief)], input=b"{}", capture_output=True)
    out = json.loads(a.stdout.decode("utf-8"))
    check(out == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                         "additionalContext": "<context_brief>é</context_brief>"}},
          "arm A hook emits the adapter's JSON, UTF-8")
    b = subprocess.run([sys.executable, hook, "-"], input=b"{}", capture_output=True)
    c = subprocess.run([sys.executable, hook, str(tmp / "missing.md")], input=b"{}", capture_output=True)
    check(b.stdout == b"" and b.returncode == 0, "arm B hook emits nothing")
    check(c.stdout == b"" and c.returncode == 0, "hook is fail-open on a missing brief")
    s = twin.settings_for("-", sys.executable)
    check("WebFetch" in s["permissions"]["deny"] and "Bash(git push:*)" in s["permissions"]["deny"],
          "settings deny network and push")


def test_batch(tmp: Path, tasks: list[dict]) -> None:
    neg = [t for t in tasks if t["task_id"] == "neg_irrelevant"]
    plan = twin.plan(neg, 1, 3)
    archive, scratch, projects = tmp / "archive", tmp / "scratch", tmp / "projects"
    stub = projects / twin.project_slug(scratch / "neg_irrelevant_r1_A" / "repo") / "memory"
    stub.mkdir(parents=True)
    keep = projects / twin.project_slug(scratch / "neg_irrelevant_r1_B" / "repo")
    keep.mkdir(parents=True)
    (keep / "x.jsonl").write_text("{}", encoding="utf-8")
    ledger = tmp / "ledger.json"
    kw = dict(archive=archive, scratch=scratch, model="m", ledger=ledger, cap=100_000, ceiling=1_000_000,
              wall=60, claude=FAKE, kind=None, limit=None, projects=projects)
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
    os.environ["CLAUDE_TWIN_TEST_PARENT"] = "1"
    try:
        res = twin.run_batch(neg, plan, **kw)
    finally:
        del os.environ["CLAUDE_TWIN_TEST_PARENT"]
        os.environ.update(saved)
    check(res["ran"] == 2 and res["stopped"] is None, "dry run: two runs")
    metas = {m["arm"]: m for m in (json.loads(p.read_text(encoding="utf-8")) for p in archive.glob("*/meta.json"))}
    a = metas["A"]
    check(a["reached"] and a["tokens_to_target"] == 2260 and a["tokens_total"] == 3390, "fake run is scored")
    check(a["cli_version"] == "fake-0" and a["verified"] is False, "meta records CLI version and verify")
    check(all((archive / r["run_id"] / f).is_file() for r in plan["runs"]
              for f in ("stream.jsonl", "diff.patch", "stderr.txt", "meta.json")), "archive is complete")
    check("# fake edit" in (archive / plan["runs"][0]["run_id"] / "diff.patch").read_text(encoding="utf-8"),
          "diff captures the edit")
    check(not any(scratch.iterdir()), "run clones are deleted")
    check(not (projects / stub.parent.name).exists(), "empty projects stub removed")
    check(keep.exists() and metas["B"]["trace_left"] == [keep.name], "non-empty projects dir kept and flagged")
    check(json.loads(ledger.read_text(encoding="utf-8"))["total"] == 6780, "ledger totals tokens")
    again = twin.run_batch(neg, plan, **kw)
    check(again["ran"] == 0 and again["already_done"] == 2, "resume skips archived runs")
    kw2 = dict(kw, archive=tmp / "archive2", ceiling=106_000)
    stop = twin.run_batch(neg, plan, **kw2)
    check(stop["ran"] == 0 and stop["stopped"], "no run starts once the cap could cross the ceiling")
    check(a["env_dropped"] == ["CLAUDE_TWIN_TEST_PARENT"] and a["cli_path_sha256"], "parent Claude env is dropped")
    os.environ["FAKE_CLAUDE_ERROR"] = "1"
    try:
        bad = twin.run_batch(neg, plan, **dict(kw, archive=tmp / "archive3", ledger=tmp / "ledger3.json"))
    finally:
        del os.environ["FAKE_CLAUDE_ERROR"]
    kept = [p.name for p in (tmp / "archive3").iterdir()]
    check(bad["ran"] == 1 and "invalid run" in (bad["stopped"] or ""), "an errored CLI run stops the batch")
    check((bad["stopped"] or "").endswith("cli_error: api 401"), "the invalid reason names the API status")
    check(kept == [plan["runs"][0]["run_id"] + ".invalid-1"], "the invalid attempt is kept and its run ID freed")
    rep = twin.report(neg, plan, archive, 100_000, 3)
    check(rep["lab"]["neg_irrelevant"]["pass"] is True and rep["guardrail"]["A"]["checked"] == 1,
          "report scores the negative control")
    check(rep["init_drift"] == {} and a["init_plugins"] == ["telemetry@builtin"], "loaded plugins are recorded")
    check(twin.settings_for("x", "py")["enabledPlugins"] == {"agents-md@builtin": False},
          "flag-gated built-in plugins are pinned off")
    meta_b = archive / plan["runs"][1]["run_id"] / "meta.json"
    doctored = twin.read_json(meta_b)
    twin.write_json(meta_b, dict(doctored, init_plugins=["agents-md@builtin", "telemetry@builtin"]))
    drift = twin.report(neg, plan, archive, 100_000, 3)["init_drift"]
    twin.write_json(meta_b, doctored)
    check(sorted(drift) == sorted(r["run_id"] for r in plan["runs"]) and "agents-md@builtin" in drift[plan["runs"][1]["run_id"]],
          "a batch that loaded different plugins is reported")


def test_real(tmp: Path) -> None:
    src = golden(tmp / "src-repo")
    twin.git(src, "init", "-q")
    twin.git(src, "add", "-A")
    twin.git(src, "commit", "-q", "-m", "one")
    head = twin.git(src, "rev-parse", "HEAD").strip()
    (src / "README.md").write_text("changed later\n", encoding="utf-8")
    twin.git(src, "commit", "-qam", "two")
    row = {"packet_id": "ep_1", "join_reason": "joined", "prompt": {"text": "p"},
           "identity": {"repository_root": src.as_posix(), "head": head},
           "injected": {"text": "b"},
           "observed": {"tool_calls": [{"name": "Edit", "input": {"file_path": f"{src.as_posix()}/src/beta.py"}}]}}
    eps = tmp / "eps.jsonl"
    eps.write_text(json.dumps(row) + "\n", encoding="utf-8")
    tasks = twin.tasks_real(eps, ["ep_1"])
    check(tasks[0]["target_files"] == ["src/beta.py"], "real task targets the recorded edits")
    repo = tmp / "clone"
    got = twin.prepare(tasks[0]["prepare"], repo)
    check(got == head and (repo / "README.md").read_text(encoding="utf-8") == GOLDEN["README.md"],
          "clone is checked out at the recorded HEAD")
    check(not twin.git(repo, "remote").strip(), "clone has no remote")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        tasks = test_lab(tmp)
        test_plan(tasks)
        test_stream()
        test_hook(tmp)
        test_batch(tmp, tasks)
        test_real(tmp)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

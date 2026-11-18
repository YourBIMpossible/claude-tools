#!/usr/bin/env python3
"""Unit tests for the Twin harness.

No framework, plain checks. Synthetic fixtures only: a tempfile copy of a tiny
golden repository, the Lab cases in ``lab/`` and ``fake_claude.py`` in place of
the real CLI. No model call, no network, no real repository or episode file.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "evidence-capture"))
import review  # noqa: E402
import snapshot  # noqa: E402
import store  # noqa: E402
import twin  # noqa: E402

EPISODE = "cap_0123456789abcdef"

PASSED: list[str] = []
FAILED: list[str] = []
FAKE_RAW = str(HERE / "fake_claude.py")
FAKE = FAKE_RAW  # replaced in main() by a wrapper that also runs the settings' replay hook, as the CLI does

# The real CLI runs the UserPromptSubmit hooks named in --settings before the turn; fake_claude.py
# does not. This wrapper runs them (argv split as the CLI would, no shell) and then fake_claude.py.
HOOKED_FAKE_SRC = '''import json, runpy, shlex, subprocess, sys
argv = sys.argv[1:]
with open(argv[argv.index("--settings") + 1], encoding="utf-8") as fh:
    settings = json.load(fh)
for group in settings.get("hooks", {}).get("UserPromptSubmit", []):
    for hook in group["hooks"]:
        subprocess.run(shlex.split(hook["command"]), input=b"{}", capture_output=True)
sys.argv = [%r] + argv
runpy.run_path(%r, run_name="__main__")
'''


def make_hooked_fake(tmp: Path) -> str:
    path = tmp / "fake_claude_hooked.py"
    path.write_text(HOOKED_FAKE_SRC % (FAKE_RAW, FAKE_RAW), encoding="utf-8")
    return str(path)
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
    "pos_region_v2": ("src/regions/r7.py", "LIMIT = 17", "LIMIT = 40"),
    "pos_engine": ("src/engine/k_c.py", "value * 3 + 2", "value * 3 + 1"),
    "neg_irrelevant": ("src/beta.py", "factor=4", "factor=5"),
    "mis_old_copy_v2": ("src/beta.py", "sum(service.run(v) for v in values)",
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
    ids = {t["task_id"] for t in tasks}
    check({"pos_region_v2", "mis_old_copy_v2"} <= ids and not ids & {"pos_region", "mis_old_copy"},
          "a superseded case is kept on disk but not planned")
    check(all("{head}" in t["brief"] and "<context_brief" in t["brief"] for t in tasks),
          "every brief uses the adapter format with a head placeholder")
    for t in tasks:
        repo = tmp / "prep" / t["task_id"]
        head = twin.prepare(t["prepare"], repo)
        check(len(head) == 40 and not twin.git(repo, "remote").strip(), f"{t['task_id']}: fixture is a remote-less git repo")
        check(all((repo / p).is_file() for p in t["target_files"] + t["planted_files"]),
              f"{t['task_id']}: target and planted files exist")
        cited = [(path, int(n), text) for path, n, text in
                 re.findall(r"\] \S+ at (\S+):(\d+)  \|  (.+)$", t["brief"], re.M)]
        check(all((repo / path).read_text(encoding="utf-8").splitlines()[n - 1].strip() == text.strip()
                  for path, n, text in cited), f"{t['task_id']}: brief line citations match the fixture ({len(cited)})")
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
    marker = Path(str(brief) + twin.DELIVERED_SUFFIX)
    check(marker.is_file() and marker.read_text(encoding="utf-8").strip()
          == twin.sha256_bytes("<context_brief>é</context_brief>".encode("utf-8")),
          "SA11-CT-40: the hook confirms delivery with the brief's sha256 next to the brief")
    check(not Path(str(tmp / "missing.md") + twin.DELIVERED_SUFFIX).exists(),
          "SA11-CT-40: a brief the hook could not emit leaves no delivery marker")
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
    check(a.get("brief_delivered") is True and metas["B"].get("brief_delivered") is None,
          "meta records that arm A's brief was delivered")
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
    rerun = twin.attempt_key({"a/x": 5, "a/x#2": 6}, "a/x")
    check(rerun == "a/x#3", "a rerun of a run ID gets its own ledger entry")
    book = twin.ledger_from_archives([archive, tmp / "archive3"])
    check(len(book["runs"]) == 3 and book["total"] == sum(book["runs"].values())
          and any(k.endswith(".invalid-1") for k in book["runs"]), "the ledger rebuilds from every kept attempt")
    check(sorted(drift) == sorted(r["run_id"] for r in plan["runs"]) and "agents-md@builtin" in drift[plan["runs"][1]["run_id"]],
          "a batch that loaded different plugins is reported")


def test_brief_delivery(tmp: Path, tasks: list[dict]) -> None:
    """SA11-CT-40 / SA16-CT-15 / SA11-CT-64: arm A counts only when its hook confirmed the brief
    was emitted, and a blank brief (arm A identical to arm B) is refused before any spend."""
    base = next(t for t in tasks if t["task_id"] == "neg_irrelevant")
    plan = twin.plan([base], 1, 3)
    by_arm = {r["arm"]: r for r in plan["runs"]}

    def one(name: str, arm: str, claude: str, task: dict = base) -> tuple[dict, dict]:
        p = dict(plan, runs=[by_arm[arm]])
        kw = dict(archive=tmp / f"bd-{name}", scratch=tmp / f"bd-{name}-scratch", model="m",
                  ledger=tmp / f"bd-{name}-ledger.json", cap=100_000, ceiling=1_000_000, wall=60,
                  claude=claude, kind=None, limit=None, projects=tmp / f"bd-{name}-projects")
        res = twin.run_batch([task], p, **kw)
        metas = [twin.read_json(m) for m in kw["archive"].rglob("meta.json")] if kw["archive"].is_dir() else []
        return res, (metas[0] if metas else {})

    res, meta = one("silent", "A", FAKE_RAW)
    check(meta.get("brief_delivered") is False and meta.get("invalid") == "brief_not_delivered"
          and "invalid run" in (res.get("stopped") or ""),
          "an arm A run whose hook never confirmed the brief is invalid and stops the batch")
    res, meta = one("hooked", "A", FAKE)
    check(meta.get("brief_delivered") is True and meta.get("invalid") is None and res["stopped"] is None,
          "an arm A run whose hook confirmed the brief is valid")
    res, meta = one("arm-b", "B", FAKE_RAW)
    check(meta.get("brief_delivered") is None and meta.get("invalid") is None, "arm B carries no delivery check")
    res, meta = one("blank", "A", FAKE, dict(base, brief="  \n"))
    check(res["ran"] == 0 and "empty brief" in (res.get("stopped") or "") and not meta
          and not (tmp / "bd-blank-ledger.json").exists(),
          "a blank brief is refused before any run starts (arm A would equal arm B)")


def test_real(tmp: Path) -> None:
    src = golden(tmp / "src-repo")
    twin.git(src, "init", "-q")
    twin.git(src, "add", "-A")
    twin.git(src, "commit", "-q", "-m", "one")
    head = twin.git(src, "rev-parse", "HEAD").strip()
    (src / "notes.txt").write_text("untracked at the start\n", encoding="utf-8")
    stores = store.Stores(tmp / "meta", tmp / "content")
    store.init_stores(stores)
    _, snap = snapshot.take_snapshot(src, EPISODE, stores, budget_s=60)
    check(snap["state"] == "complete", "start snapshot taken")
    (src / "notes.txt").unlink()
    (src / "README.md").write_text("changed later\n", encoding="utf-8")
    twin.git(src, "commit", "-qam", "two")
    row = {"packet_id": "ep_1", "episode_id": EPISODE, "join_reason": "joined", "prompt": {"text": "p"},
           "identity": {"repository_root": src.as_posix(), "head": head},
           "injected": {"text": "b"},
           "observed": {"tool_calls": [{"name": "Edit", "input": {"file_path": f"{src.as_posix()}/src/beta.py"}}]}}
    eps = tmp / "eps.jsonl"
    eps.write_text(json.dumps(row) + "\n", encoding="utf-8")
    m = {"episode_id": EPISODE, "ended_at": "2026-09-26T00:00:00Z", "exclusions": [],
         "prompt": {"sha256": "0" * 64}, "link": {"brief_sha256": "0" * 64},
         "start_snapshot": {"sums_sha256": snap["sums_sha256"]}}
    review.materialize_payload(stores, m, review.ReviewInputs(src, "p", "b"), {"passed": True},
                               "2026-09-26T00:00:00Z")
    check(not review.verify_payload(stores, EPISODE), "start payload materialized and verified")
    tasks = twin.tasks_real(eps, ["ep_1"], stores.payloads)
    check(tasks[0]["target_files"] == ["src/beta.py"], "real task targets the recorded edits")
    check(tasks[0]["prepare"]["type"] == "clone_start_only", "real tasks use the start-only clone")
    repo = tmp / "clone"
    proofs: dict = {}
    got = twin.prepare(tasks[0]["prepare"], repo, proofs)
    check(got == head and (repo / "README.md").read_text(encoding="utf-8") == GOLDEN["README.md"],
          "clone is checked out at the recorded HEAD")
    check((repo / "notes.txt").read_text(encoding="utf-8") == "untracked at the start\n",
          "the start's untracked file is rebuilt")
    check(not twin.git(repo, "remote").strip(), "clone has no remote")
    check(proofs.get("start_state_reconstructed") is True and not proofs["failed"], "every proof passed")
    later = twin.git(src, "rev-parse", "HEAD").strip()
    check(subprocess.run(["git", "-C", str(repo), "cat-file", "-e", later], capture_output=True).returncode != 0,
          "the post-start commit is not in the clone")

    refused = []
    (stores.payloads / EPISODE / "brief.txt").write_text("tampered\n", encoding="utf-8")
    for spec in ({**tasks[0]["prepare"], "type": "clone"}, {**tasks[0]["prepare"], "payload": None},
                 tasks[0]["prepare"]):
        try:
            twin.prepare(spec, tmp / f"refused{len(refused)}")
            refused.append(False)
        except (ValueError, RuntimeError):
            refused.append(True)
    check(refused == [True, True, True],
          "a plain clone, a clone without a start payload and a tampered payload are refused")
    no_payload = twin.tasks_real(eps, ["ep_1"])
    check(no_payload[0]["prepare"]["payload"] is None, "no payload directory, no start state")


def test_verify_guard(tmp: Path, tasks: list[dict]) -> None:
    """A verify.py that hangs is cut off and recorded, the run's spend still reaches the ledger,
    and verify runs in the run's environment (no Claude or Anthropic variable from the harness)."""
    dump = tmp / "verify_env.json"
    script = tmp / "verify_hang.py"
    script.write_text("import json, os, sys, time\n"
                      "json.dump(sorted(k for k in os.environ if k.upper().startswith(('CLAUDE', 'ANTHROPIC'))),"
                      f" open({dump.as_posix()!r}, 'w'))\n"
                      "time.sleep(10)\nsys.exit(1)\n", encoding="utf-8")
    task = dict(next(t for t in tasks if t["task_id"] == "neg_irrelevant"), verify=str(script))
    plan = twin.plan([task], 1, 3)
    plan["runs"] = plan["runs"][:1]
    ledger = tmp / "vg-ledger.json"
    kw = dict(archive=tmp / "vg-archive", scratch=tmp / "vg-scratch", model="m", ledger=ledger, cap=100_000,
              ceiling=1_000_000, wall=60, claude=FAKE, kind=None, limit=None, projects=tmp / "vg-projects")
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
    os.environ["CLAUDE_TWIN_TEST_PARENT"] = "1"
    patched = getattr(twin, "VERIFY_TIMEOUT", None)
    twin.VERIFY_TIMEOUT = 2.0
    error: Exception | None = None
    try:
        res = twin.run_batch([task], plan, **kw)
    except subprocess.TimeoutExpired as exc:
        error = exc
    finally:
        twin.VERIFY_TIMEOUT = patched
        del os.environ["CLAUDE_TWIN_TEST_PARENT"]
        os.environ.update(saved)
    meta_path = kw["archive"] / plan["runs"][0]["run_id"] / "meta.json"
    check(error is None and meta_path.is_file(), "a hung verify does not abort the run")
    meta = twin.read_json(meta_path) if meta_path.is_file() else {}
    check(meta.get("verified") is False and (meta.get("verify_error") or "").startswith("timeout"),
          "a hung verify is recorded as unverified with the reason")
    check(ledger.is_file() and twin.read_json(ledger)["total"] == 3390 and error is None and res["ran"] == 1,
          "the run's spend reaches the ledger")
    check(dump.is_file() and json.loads(dump.read_text(encoding="utf-8")) == [],
          "verify runs without the harness's Claude or Anthropic variables")
    arm = plan["runs"][0]["arm"]
    guard = twin.report([task], plan, kw["archive"], 100_000, 3)["guardrail"][arm]
    check(guard == {"verified": 0, "checked": 0, "timed_out": 1},
          "a timed-out verify is counted apart from a checked one (SA26-24)")


def test_post_run_failure_books_spend(tmp: Path, tasks: list[dict]) -> None:
    """SA26-26 / SA11-CT-36 / SA16-CT-3: a git or verify failure after the paid run never leaves
    run_one before meta.json is written, so the spend always reaches the ledger."""
    base = next(t for t in tasks if t["task_id"] == "neg_irrelevant")
    real_git, real_verify = twin.git, twin.run_verify

    def git_add_fails(repo: Path, *args: str, check: bool = True) -> str:
        if args[:2] == ("diff", "--cached"):
            raise RuntimeError("git diff --cached failed: simulated")
        return real_git(repo, *args, check=check)

    def verify_cannot_start(*a: object, **k: object) -> tuple[bool, str | None]:
        raise OSError("simulated: verify interpreter missing")

    for name, patch in (("git", git_add_fails), ("run_verify", verify_cannot_start)):
        plan = twin.plan([base], 1, 3)
        plan["runs"] = plan["runs"][:1]
        ledger = tmp / f"pr-{name}-ledger.json"
        kw = dict(archive=tmp / f"pr-{name}-archive", scratch=tmp / f"pr-{name}-scratch", model="m", ledger=ledger,
                  cap=100_000, ceiling=1_000_000, wall=60, claude=FAKE, kind=None, limit=None,
                  projects=tmp / f"pr-{name}-projects")
        setattr(twin, name, patch)
        error: Exception | None = None
        try:
            res = twin.run_batch([base], plan, **kw)
        except Exception as exc:  # noqa: BLE001 - the check is that nothing escapes
            error, res = exc, {}
        finally:
            twin.git, twin.run_verify = real_git, real_verify
        metas = [twin.read_json(m) for m in kw["archive"].rglob("meta.json")]
        check(error is None and len(metas) == 1, f"a post-run {name} failure still writes meta ({error!r})")
        check(ledger.is_file() and twin.read_json(ledger)["total"] == 3390 and res.get("ran") == 1,
              f"a post-run {name} failure still books the spend")
        m = metas[0] if metas else {}
        if name == "git":
            check((m.get("invalid") or "").startswith("post_run_error: diff") and "invalid run" in (res.get("stopped") or ""),
                  "a run whose diff could not be taken is invalid and stops the batch")
        else:
            check(m.get("verified") is None and "verify could not run" in (m.get("verify_error") or "")
                  and m.get("invalid") is None, "a verify that cannot start is unverified, not a lost run")


def test_verify_kills_tree(tmp: Path) -> None:
    """SA26-26: a verify past its limit is killed with every process it started; a grandchild
    holding the output pipes neither outlives it nor holds the run past the limit."""
    pidfile = tmp / "grandchild.pid"
    script = tmp / "verify_spawns.py"
    script.write_text("import subprocess, sys, time\n"
                      "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                      f"open({pidfile.as_posix()!r}, 'w').write(str(p.pid))\n"
                      "time.sleep(60)\n", encoding="utf-8")
    t = time.monotonic()
    verified, error = twin.run_verify([sys.executable, str(script), str(tmp)], cwd=tmp, env=dict(os.environ),
                                      timeout=1.0)
    took = time.monotonic() - t
    check(verified is False and (error or "").startswith("timeout"), "a verify past its limit is a timeout")
    check(took < 1.0 + twin.KILL_GRACE + 2.0, f"the timeout is bounded ({took:.1f}s)")
    pid = int(pidfile.read_text(encoding="utf-8")) if pidfile.is_file() else None
    alive = pid is not None and _alive(pid)
    check(pid is not None and not alive, "the verify's grandchild is killed with it")


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:  # a killed grandchild is reparented and reaped shortly after
        try:
            os.kill(pid, 0)
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                if fh.read().split(") ", 1)[1].startswith("Z"):
                    return False
        except (ProcessLookupError, FileNotFoundError):  # gone between the probe and the read
            return False
        time.sleep(0.05)
    return True


LINGER_SRC = '''import os, runpy, sys, time
sys.argv = [%r] + sys.argv[1:]
try:
    runpy.run_path(%r, run_name="__main__")
except SystemExit:
    pass
sys.stdout.flush()
os.close(1)  # output done, process keeps running: the harness must not wait on it unbounded
time.sleep(%s)
'''


def test_kill_and_exit_bounds(tmp: Path, tasks: list[dict]) -> None:
    """SA11-CT-63 / SA11-CT-64: a failed kill is reported, never silently trusted; the wait for a
    run's process is bounded and its failure recorded; a wall timer that fires as the run ends
    does not label a finished run as killed."""
    base = next(t for t in tasks if t["task_id"] == "neg_irrelevant")
    plan = twin.plan([base], 1, 3)
    plan = dict(plan, runs=[r for r in plan["runs"] if r["arm"] == "B"])

    if os.name == "nt":
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        real_run = twin.subprocess.run
        twin.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0], 1, b"", b"Access is denied.")
        try:
            why = twin.kill_tree(sleeper)
        finally:
            twin.subprocess.run = real_run
        try:
            sleeper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sleeper.kill()
        check(why is not None and "taskkill rc 1" in why and sleeper.returncode is not None,
              "a failed taskkill is reported and the process is still killed alone")
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    check(twin.kill_tree(done) is None, "killing an exited process is not a failure")

    def one(name: str, linger: float) -> tuple[dict, float]:
        fake = tmp / f"linger-{name}.py"
        fake.write_text(LINGER_SRC % (FAKE_RAW, FAKE_RAW, linger), encoding="utf-8")
        kw = dict(archive=tmp / f"kb-{name}", scratch=tmp / f"kb-{name}-scratch", model="m",
                  ledger=tmp / f"kb-{name}-ledger.json", cap=100_000, ceiling=1_000_000, wall=60,
                  claude=str(fake), kind=None, limit=None, projects=tmp / f"kb-{name}-projects")
        t = time.monotonic()
        twin.run_batch([base], plan, **kw)
        took = time.monotonic() - t
        metas = [twin.read_json(m) for m in kw["archive"].rglob("meta.json")]
        return (metas[0] if metas else {}), took

    saved = twin.EXIT_GRACE, twin.KILL_GRACE, twin.kill_tree
    twin.EXIT_GRACE, twin.KILL_GRACE = 1.0, 1.0
    try:
        meta, took = one("lingers", 60)
        check(took < 20 and meta.get("invalid") is None and meta.get("exit_code") is not None
              and any("did not exit" in e for e in meta.get("process_errors") or []),
              f"a process that outlives its output is killed after a bounded wait and recorded ({took:.1f}s)")
        twin.kill_tree = lambda proc: "simulated: kill refused"
        meta, took = one("unkillable", 6)
        check((meta.get("invalid") or "").startswith("process_not_killed") and meta.get("exit_code") is None
              and any("simulated: kill refused" in e for e in meta.get("process_errors") or []),
              f"a process that cannot be killed makes the run invalid instead of hanging it ({took:.1f}s)")
    finally:
        twin.EXIT_GRACE, twin.KILL_GRACE, twin.kill_tree = saved

    class CancelFires:
        """A timer that fires its callback in the instant before cancel() - the race window."""
        def __init__(self, interval: float, fn: Any, args: tuple = ()) -> None:
            self.fn, self.args = fn, args

        def start(self) -> None:
            pass

        def cancel(self) -> None:
            self.fn(*self.args)

    real_timer = twin.threading.Timer
    twin.threading.Timer = CancelFires
    try:
        kw = dict(archive=tmp / "kb-race", scratch=tmp / "kb-race-scratch", model="m",
                  ledger=tmp / "kb-race-ledger.json", cap=100_000, ceiling=1_000_000, wall=60,
                  claude=FAKE, kind=None, limit=None, projects=tmp / "kb-race-projects")
        twin.run_batch([base], plan, **kw)
    finally:
        twin.threading.Timer = real_timer
    metas = [twin.read_json(m) for m in kw["archive"].rglob("meta.json")]
    check(len(metas) == 1 and metas[0]["killed"] is None and metas[0]["invalid"] is None,
          "a wall timer firing as the finished run is cancelled does not censor it")


def test_negative_censored() -> None:
    """A negative control with every run censored is inconclusive, not a pass."""
    def meta(reached: bool, killed: str | None = None) -> dict:
        return {"reached": reached, "tokens_to_target": 1000 if reached else None, "killed": killed,
                "planted_touched": False}
    cap = 100_000
    both = twin.lab_verdict("negative", [meta(False)] * 2, [meta(False)] * 2, cap)
    check(both["pass"] is None and both["ratio"] == 1.0 and "censored" in both.get("inconclusive", ""),
          "both arms fully censored: negative verdict is null, not a pass")
    killed = twin.lab_verdict("negative", [meta(True, "cap")] * 2, [meta(True, "wall")] * 2, cap)
    check(killed["pass"] is None, "runs killed after reaching the target are censored too")
    one = twin.lab_verdict("negative", [meta(True), meta(False)], [meta(True), meta(False)], cap)
    check(one["pass"] is True, "a partly censored negative control is still judged")
    skew = twin.lab_verdict("negative", [meta(True)] * 2, [meta(False)] * 2, cap)
    check(skew["pass"] is False, "unequal reach fails the negative control")


def test_bench_blind(tmp: Path) -> None:
    """bench_blind is true only on a failed positive control, null when unscored, false when all pass."""
    tasks = [{"task_id": "pos_x", "kind": "lab", "family": "positive"},
             {"task_id": "neg_x", "kind": "lab", "family": "negative"}]
    plan = twin.plan(tasks, 2, 5)
    tasks_f, plan_f = tmp / "bb-tasks.json", tmp / "bb-plan.json"
    twin.write_json(tasks_f, tasks)
    twin.write_json(plan_f, plan)

    def run_report(name: str, reached_a: bool, only: set[str] | None = None, want_rc: int = 0) -> dict:
        archive = tmp / f"bb-{name}"
        archive.mkdir()
        for r in plan["runs"]:
            if only is not None and r["run_id"] not in only:
                continue
            reached = reached_a if r["arm"] == "A" else False
            twin.write_json(archive / r["run_id"] / "meta.json",
                            {**r, "reached": reached, "tokens_to_target": 1000 if reached else None,
                             "killed": False, "tokens_total": 1000})
        out = tmp / f"bb-{name}.json"
        rc = twin.main(["report", "--tasks", str(tasks_f), "--plan", str(plan_f), "--archive", str(archive),
                        "--cap", "100000", "--seed", "3", "--out", str(out)])
        check(rc == want_rc, f"SA11-CT-65: report CLI exits {want_rc} ({name}, got {rc})")
        return twin.read_json(out)

    empty = run_report("empty", True, only=set())
    check(empty["bench_blind"] is None and empty["positive_controls_incomplete"] == ["pos_x"],
          "an empty archive leaves bench_blind unknown")
    one_pos = {r["run_id"] for r in plan["runs"] if r["task_id"] == "pos_x"}
    partial = run_report("partial", True, only=one_pos - {sorted(one_pos)[0]})
    check(partial["bench_blind"] is None and partial["positive_controls_incomplete"] == ["pos_x"],
          "a positive control missing a planned run leaves bench_blind unknown")
    good = run_report("pass", True)
    check(good["bench_blind"] is False and good["positive_controls_incomplete"] == [],
          "a complete passing positive control gives bench_blind false")
    bad = run_report("fail", False, want_rc=1)
    check(bad["bench_blind"] is True, "a failing positive control gives bench_blind true")
    no_pos = [t for t in tasks if t["family"] != "positive"]
    check(twin.bench_blind(no_pos, twin.plan(no_pos, 1, 5), {}, [])[1] is None,
          "no planned positive control leaves bench_blind unknown")


def test_run_cli_exit(tmp: Path, tasks: list[dict]) -> None:
    """SA11-CT-65: the run CLI exits 0 only when the batch was not stopped."""
    base = next(t for t in tasks if t["task_id"] == "neg_irrelevant")
    tasks_f, plan_f = tmp / "rc-tasks.json", tmp / "rc-plan.json"
    twin.write_json(tasks_f, [base])
    twin.write_json(plan_f, twin.plan([base], 1, 3))

    def run(name: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return twin.main(["run", "--tasks", str(tasks_f), "--plan", str(plan_f),
                              "--archive", str(tmp / f"rc-{name}"), "--scratch", str(tmp / f"rc-{name}-scratch"),
                              "--model", "m", "--ledger", str(tmp / f"rc-{name}-ledger.json"), "--cap", "100000",
                              "--claude", FAKE, "--projects", str(tmp / f"rc-{name}-projects")])

    check(run("ok") == 0, "a completed batch exits 0")
    os.environ["FAKE_CLAUDE_ERROR"] = "1"
    try:
        rc = run("bad")
    finally:
        del os.environ["FAKE_CLAUDE_ERROR"]
    check(rc == 1, f"a batch stopped by an invalid run exits 1 (got {rc})")


def main() -> int:
    global FAKE
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        FAKE = make_hooked_fake(tmp)
        tasks = test_lab(tmp)
        test_plan(tasks)
        test_stream()
        test_hook(tmp)
        test_batch(tmp, tasks)
        test_brief_delivery(tmp, tasks)
        test_real(tmp)
        test_verify_guard(tmp, tasks)
        test_post_run_failure_books_spend(tmp, tasks)
        test_verify_kills_tree(tmp)
        test_kill_and_exit_bounds(tmp, tasks)
        test_negative_censored()
        test_bench_blind(tmp)
        test_run_cli_exit(tmp, tasks)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

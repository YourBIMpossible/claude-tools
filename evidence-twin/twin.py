#!/usr/bin/env python3
"""Twin harness: replay a task twice, with and without its brief, and measure the work.

Each run is a fresh, harness-owned repository (a local clone with its remote
removed, or a synthetic Lab fixture) in a scratch directory outside every real
checkout. ``claude -p`` runs there with isolated settings: only the ``project``
setting source (the clone has none) plus a generated ``--settings`` file that
allows local tools, denies network and push, and installs the replay hook in both
arms. Arm A's hook injects the brief through ``additionalContext``, exactly as the
Evidence Compiler adapter does; arm B's injects nothing. ``EVIDENCE_HOOK=0``,
``EVIDENCE_CAPTURE=0`` and ``--no-session-persistence`` are always set.

The stream is archived, tokens are counted as it arrives and the run is killed at
the per-run cap. A ledger keeps the running total across batches and no run
starts once it could cross the ceiling. The stream's tool calls are scored with
the same Footprint extractor as natural episodes.

    python twin.py tasks-lab  --cases <dir> --golden <dir> --out tasks.json
    python twin.py tasks-real --episodes <episodes.jsonl> --packets <id>[,<id>...] --payloads <dir> --out tasks.json
    python twin.py plan       --tasks tasks.json --repeats 2 --seed <int> --out plan.json
    python twin.py run        --tasks tasks.json --plan plan.json --archive <dir> --scratch <dir>
                              --model <id> --ledger <ledger.json> [--cap 4000000] [--ceiling 150000000]
                              [--wall 1200] [--claude claude] [--kind lab|real] [--limit N]
    python twin.py report     --tasks tasks.json --plan plan.json --archive <dir> --cap 4000000
                              --seed <int> --out report.json

Standard library, ``git`` and the ``claude`` CLI only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import stat
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "evidence-footprints"))
sys.path.insert(0, str(HERE.parent / "evidence-capture"))
import clone_builder  # noqa: E402
import store as capture_store  # noqa: E402
import footprints as fpm  # noqa: E402

ARMS = ("A", "B")
DEFAULT_CAP = 4_000_000
DEFAULT_CEILING = 150_000_000
DEFAULT_WALL = 1200
FIXED_DATE = "2026-01-01T00:00:00+00:00"
ALLOW = ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Glob", "Grep", "Bash", "PowerShell",
         "TodoWrite", "Agent", "Task"]
NETWORK_VERBS = ["curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "telnet", "Invoke-WebRequest",
                 "Invoke-RestMethod", "iwr", "irm", "Start-BitsTransfer"]
DENY = (["WebFetch", "WebSearch", "Bash(git push:*)", "Bash(git remote:*)", "Bash(git fetch:*)",
         "Bash(git pull:*)", "Bash(git clone:*)", "Bash(pip install:*)", "Bash(npm install:*)",
         "PowerShell(git push:*)", "PowerShell(git remote:*)", "PowerShell(git fetch:*)",
         "PowerShell(git pull:*)", "PowerShell(git clone:*)"]
        + [f"Bash({v}:*)" for v in NETWORK_VERBS] + [f"PowerShell({v}:*)" for v in NETWORK_VERBS])
USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


# ---------------------------------------------------------------- utilities

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def git(repo: Path, *args: str, check: bool = True) -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE,
           "GIT_AUTHOR_NAME": "twin", "GIT_AUTHOR_EMAIL": "twin@example.invalid",
           "GIT_COMMITTER_NAME": "twin", "GIT_COMMITTER_EMAIL": "twin@example.invalid"}
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def _force_remove(func: Callable, path: str, _exc: Any) -> None:
    os.chmod(path, stat.S_IWRITE)  # git marks pack files read-only on Windows
    func(path)


def rmtree(path: Path, tries: int = 1, delay: float = 0.5) -> bool:
    """Remove ``path``; retry while a just-exited process still holds a handle. True when gone."""
    for attempt in range(tries):
        try:
            if path.exists():
                shutil.rmtree(path, onerror=_force_remove)
            return True
        except OSError:
            if attempt == tries - 1:
                return False
            time.sleep(delay)
    return not path.exists()


def project_slug(path: Path) -> str:
    """The ``~/.claude/projects`` directory name Claude Code derives from a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


# ---------------------------------------------------------------- tasks

def tasks_lab(cases: Path, golden: Path) -> list[dict]:
    """One task per current case. A changed case is a new case that names the one it
    ``supersedes``; the old case stays on disk with its runs but is no longer planned."""
    found = [(f, read_json(f)) for f in sorted(cases.glob("*/case.json"))]
    superseded = {c["supersedes"] for _, c in found if c.get("supersedes")}
    tasks = []
    for case_file, case in found:
        if case["id"] in superseded:
            continue
        cdir = case_file.parent
        verify = cdir / "verify.py"
        tasks.append({
            "task_id": case["id"], "kind": "lab", "family": case["family"],
            "prompt": case["prompt"], "brief": (cdir / "brief.md").read_text(encoding="utf-8"),
            "target_files": case["target_files"], "planted_files": case.get("planted_files", []),
            "prepare": {"type": "overlay", "base": str(golden), "overlay": str(cdir / "overlay")},
            "verify": str(verify) if verify.is_file() else None,
        })
    return tasks


def tasks_real(episodes: Path, packets: list[str], payloads: Path | None = None) -> list[dict]:
    """One task per named packet. The run starts from the episode's start state: the
    clone holds only the start commit's ancestry (``clone_start_only``) and the start's
    index/worktree state is rebuilt from the snapshot inside the episode's reviewed
    payload (``payloads/<episode_id>/snapshot``). A row without a payload yields a task
    that ``prepare`` refuses."""
    rows = {}
    for line in episodes.read_text(encoding="utf-8").splitlines():
        if line:
            row = json.loads(line)
            rows[row["packet_id"]] = row
    tasks = []
    for pid in packets:
        row = rows.get(pid)
        if row is None or not row.get("observed"):
            raise SystemExit(f"{pid}: no joined episode row")
        fp = fpm.Footprint(row)
        head = row["identity"].get("head")
        if not head or not fp.edited:
            raise SystemExit(f"{pid}: needs a known HEAD and at least one edited file")
        tasks.append({
            "task_id": pid, "kind": "real", "family": None,
            "prompt": row["prompt"]["text"], "brief": (row.get("injected") or {}).get("text") or "",
            "target_files": sorted(fp.edited), "planted_files": [],
            "prepare": {"type": "clone_start_only",
                        "source": fpm.main_root(row["identity"]["repository_root"]), "head": head,
                        "payload": (str(payloads / row["episode_id"])
                                    if payloads is not None and row.get("episode_id") else None)},
            "verify": None,
        })
    return tasks


def prepare(spec: dict, dest: Path, proofs: dict | None = None) -> str:
    """Build the run's repository at ``dest``; return its HEAD.

    ``clone_start_only`` fills ``proofs`` with the clone record (plan §7 P1–P10) and
    refuses unless every proof passed. A plain ``clone`` is refused: it carries the
    source's whole history, including commits made after the episode started."""
    if spec["type"] == "overlay":
        shutil.copytree(spec["base"], dest)
        overlay = Path(spec["overlay"])
        if overlay.is_dir():
            shutil.copytree(overlay, dest, dirs_exist_ok=True)
        git(dest, "init", "-q")
        git(dest, "add", "-A")
        git(dest, "commit", "-q", "-m", "lab fixture")
    elif spec["type"] == "clone_start_only":
        if not spec.get("payload"):
            raise RuntimeError("no start-state payload: the episode cannot be replayed")
        payload = Path(spec["payload"])
        bad = (capture_store.verify_sums(payload) if (payload / "SHA256SUMS").is_file()
               else ["payload SHA256SUMS missing"])
        if bad:
            raise RuntimeError(f"start-state payload fails verification: {bad[:5]}")
        record = clone_builder.clone_start_only(Path(spec["source"]), spec["head"], dest, payload / "snapshot")
        if proofs is not None:
            proofs.update(record)
        if not record["start_state_reconstructed"]:
            raise RuntimeError(f"start state not reproduced: failed {record['failed']} {record['error'] or ''}".strip())
    elif spec["type"] == "clone":
        raise ValueError("prepare type 'clone' is refused: a full clone carries post-start history; "
                         "use 'clone_start_only'")
    else:
        raise ValueError(f"unknown prepare type {spec['type']!r}")
    if git(dest, "remote").strip():
        raise RuntimeError("run repository still has a remote")
    return git(dest, "rev-parse", "HEAD").strip()


# ---------------------------------------------------------------- plan

def plan(tasks: list[dict], repeats: int, seed: int) -> dict:
    rng = random.Random(seed)
    runs = []
    for kind in ("lab", "real"):  # Lab controls always run first
        units = [(t["task_id"], r) for t in sorted(tasks, key=lambda t: t["task_id"]) if t["kind"] == kind
                 for r in range(1, repeats + 1)]
        rng.shuffle(units)
        for task_id, rep in units:
            arms = list(ARMS)
            rng.shuffle(arms)  # interleave arms against drift
            runs += [{"run_id": f"{task_id}.r{rep}.{arm}", "task_id": task_id, "kind": kind,
                      "arm": arm, "repeat": rep} for arm in arms]
    return {"seed": seed, "repeats": repeats, "runs": runs}


# ---------------------------------------------------------------- stream

class StreamState:
    """Token and tool-call accounting over a stream-json transcript."""

    def __init__(self) -> None:
        self.usage: dict[str, dict[str, int]] = {}
        self.order: list[str] = []
        self.calls: list[dict] = []
        self.init: dict | None = None
        self.result: dict | None = None

    def feed(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.init = event
        elif kind == "result":
            self.result = event
        elif kind == "assistant":
            msg = event.get("message") or {}
            mid = msg.get("id") or f"_anon{len(self.order)}"
            if mid not in self.usage:
                self.usage[mid] = {f: 0 for f in USAGE_FIELDS}
                self.order.append(mid)
            for f in USAGE_FIELDS:
                self.usage[mid][f] = max(self.usage[mid][f], int((msg.get("usage") or {}).get(f) or 0))
            if event.get("parent_tool_use_id"):
                return  # subagent calls cost tokens but are not the main thread's footprint
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self.calls.append({"seq": len(self.calls), "name": block.get("name", ""),
                                       "input": block.get("input") or {}, "message": mid})

    def tokens(self) -> int:
        return sum(sum(u.values()) for u in self.usage.values())

    def tokens_through(self, mid: str) -> int:
        total = 0
        for m in self.order:
            total += sum(self.usage[m].values())
            if m == mid:
                break
        return total


def score_run(state: StreamState, repo: Path, task: dict) -> dict:
    row = {"identity": {"repository_root": repo.as_posix()}, "observed": {"tool_calls": state.calls}}
    fp = fpm.Footprint(row)
    targets = [t.lower() for t in task["target_files"]]
    first = next(((idx, p) for idx, p in fp.edits if any(fpm.matches(p, t) for t in targets)), None)
    planted = [p.lower() for p in task.get("planted_files", [])]
    return {
        "calls": len(state.calls),
        "reached": first is not None,
        "calls_to_target": first[0] if first else None,
        "tokens_to_target": state.tokens_through(state.calls[first[0]]["message"]) if first else None,
        "edited": sorted(fp.edited),
        "touched": sorted(fp.touched),
        "planted_touched": sorted(p for p in planted if any(fpm.matches(t, p) for t in fp.touched)),
    }


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


# Built-in plugins whose availability a remote feature flag decides per launch, so two runs of
# one batch can differ in what is loaded. They are pinned off; ``init_plugins`` records what
# actually loaded, and the report lists every run's set when they differ (``init_drift``).
PINNED_OFF = ("agents-md@builtin",)

# Seconds a case's verify.py may run. It imports the run's edited code, so it can hang on
# an edit that loops; a hung verify is recorded as unverified, never as a lost run.
VERIFY_TIMEOUT = 120.0


def settings_for(brief_arg: str, python: str) -> dict:
    hook = f'"{Path(python).as_posix()}" "{(HERE / "replay_hook.py").as_posix()}" "{brief_arg}"'
    return {"permissions": {"allow": ALLOW, "deny": DENY},
            "enabledPlugins": {p: False for p in PINNED_OFF},
            "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": hook}]}]}}


def child_env() -> tuple[dict[str, str], list[str]]:
    """The run's environment: this process's, minus every Claude and Anthropic variable.

    A harness launched from inside a Claude Code session inherits that session's
    wiring (session IDs, a host-managed auth refresh, a local API base URL, effort).
    None of it belongs to the run, and it differs between launch contexts, so it is
    dropped; the CLI then uses its own login. Returns the env and the dropped names.
    """
    dropped = sorted(k for k in os.environ if k.upper().startswith(("CLAUDE", "ANTHROPIC")))
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env["EVIDENCE_HOOK"] = "0"
    env["EVIDENCE_CAPTURE"] = "0"  # a replay is never itself captured (plan §8)
    return env, dropped


def resolve_cli(claude: str) -> tuple[str, str | None]:
    """The CLI's resolved path and sha256, so a batch records exactly which binary ran."""
    path = claude if claude.endswith(".py") else (shutil.which(claude) or claude)
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return path, None
    return path, h.hexdigest()


def claude_cmd(claude: str, prompt: str, model: str, settings: Path) -> list[str]:
    base = [sys.executable, claude] if claude.endswith(".py") else [shutil.which(claude) or claude]
    return base + ["-p", prompt, "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                   "--model", model, "--settings", str(settings), "--setting-sources", "project",
                   "--strict-mcp-config", "--permission-mode", "dontAsk"]


def clear_project_trace(projects: Path, repo: Path) -> list[str]:
    """Remove the empty ``~/.claude/projects`` stub for the run's own clone; report anything else."""
    left = []
    if not projects.is_dir():
        return left
    slug = project_slug(repo)
    for entry in projects.iterdir():
        if not (entry.name == slug or (len(slug) > 200 and entry.name.startswith(slug[:200]))):
            continue
        if any(p.is_file() for p in entry.rglob("*")):
            left.append(entry.name)  # never delete content the harness did not verify is empty
            continue
        for d in sorted((p for p in entry.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            d.rmdir()
        entry.rmdir()
    return left


def run_one(task: dict, run: dict, *, archive: Path, scratch: Path, model: str, cap: int, wall: int,
            claude: str, projects: Path, cli_sha: str | None = None) -> dict:
    out = archive / run["run_id"]
    out.mkdir(parents=True, exist_ok=True)
    work = scratch / run["run_id"].replace(".", "_")
    rmtree(work)
    work.mkdir(parents=True)
    repo = work / "repo"
    source_status = None
    if task["prepare"]["type"] == "clone_start_only":
        source_status = sha256_bytes(git(Path(task["prepare"]["source"]), "status", "--porcelain").encode())
    proofs: dict = {}
    try:
        head = prepare(task["prepare"], repo, proofs)
    finally:
        if proofs:
            write_json(out / "clone_proofs.json", proofs)

    brief_arg = "-"
    if run["arm"] == "A" and task["brief"]:
        brief_file = work / "brief.md"
        brief_file.write_text(task["brief"].replace("{head}", head), encoding="utf-8")
        brief_arg = brief_file.as_posix()
    settings = work / "settings.json"
    write_json(settings, settings_for(brief_arg, sys.executable))
    env, dropped = child_env()
    if claude.endswith(".py"):
        env["FAKE_CLAUDE_TARGET"] = task["target_files"][0]

    state = StreamState()
    killed: list[str] = []
    started = time.monotonic()
    target_seconds = None
    with (out / "stream.jsonl").open("w", encoding="utf-8", newline="\n") as stream, \
            (out / "stderr.txt").open("w", encoding="utf-8") as err:
        proc = subprocess.Popen(claude_cmd(claude, task["prompt"], model, settings), cwd=repo,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, env=env,
                                text=True, encoding="utf-8", errors="replace")
        timer = threading.Timer(wall, lambda: (killed.append("wall"), kill_tree(proc)))
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                stream.write(line if line.endswith("\n") else line + "\n")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    before = len(state.calls)
                    state.feed(event)
                    if target_seconds is None and len(state.calls) > before and score_run(state, repo, task)["reached"]:
                        target_seconds = round(time.monotonic() - started, 1)
                if state.tokens() > cap and not killed:
                    killed.append("cap")
                    kill_tree(proc)
            proc.wait()
        finally:
            timer.cancel()
    seconds = round(time.monotonic() - started, 1)

    git(repo, "add", "-A")
    diff = git(repo, "diff", "--cached", "--binary", head)
    (out / "diff.patch").write_text(diff, encoding="utf-8", newline="\n")
    verified = None
    verify_error = None
    if task.get("verify"):
        # verify.py imports the run's edited code: it gets the run's environment, not the harness's.
        try:
            v = subprocess.run([sys.executable, task["verify"], str(repo)], cwd=repo, capture_output=True,
                               text=True, env=env, timeout=VERIFY_TIMEOUT)
        except subprocess.TimeoutExpired:
            verified, verify_error = False, f"timeout after {VERIFY_TIMEOUT:g}s"
        else:
            verified = v.returncode == 0
    scored = score_run(state, repo, task)
    trace = [] if rmtree(work, tries=30) else [f"scratch not deleted: {work}"]
    trace += clear_project_trace(projects, repo)
    if source_status is not None:
        after = sha256_bytes(git(Path(task["prepare"]["source"]), "status", "--porcelain").encode())
        if after != source_status:
            trace.append("source checkout status changed")

    init = state.init or {}
    invalid = None  # a run the CLI never really executed is not a data point; a killed run is censored, not invalid
    if not killed:
        if state.result is None:
            invalid = "no_result"
        elif state.result.get("is_error"):
            r = state.result
            invalid = ("cli_error: " + (f"api {r['api_error_status']}" if r.get("api_error_status")
                                        else str(r.get("terminal_reason") or r.get("subtype"))))
    result_usage = (state.result or {}).get("usage") or {}
    meta = {
        **run, "family": task.get("family"), "model": model, "head": head,
        "cli_version": init.get("claude_code_version"), "init_model": init.get("model"),
        "init_counts": {k: len(init[k]) if isinstance(init.get(k), list) else init.get(k)
                        for k in ("tools", "mcp_servers", "skills", "agents", "memory_paths", "slash_commands")},
        "init_plugins": sorted(str(p.get("source") or p.get("name")) if isinstance(p, dict) else str(p)
                               for p in init.get("plugins") or []),
        "cli_path_sha256": cli_sha, "env_dropped": dropped,
        "exit_code": proc.returncode, "killed": killed[0] if killed else None, "invalid": invalid,
        "is_error": (state.result or {}).get("is_error"), "result_subtype": (state.result or {}).get("subtype"),
        "tokens_total": state.tokens(),
        "tokens_result": sum(int(result_usage.get(f) or 0) for f in USAGE_FIELDS),
        "wall_seconds": seconds, "seconds_to_target": target_seconds, "verified": verified,
        "verify_error": verify_error,
        "trace_left": trace,
        "stream_sha256": sha256_bytes((out / "stream.jsonl").read_bytes()),
        "diff_sha256": sha256_bytes(diff.encode("utf-8")),
        **scored,
    }
    write_json(out / "meta.json", meta)
    return meta


def attempt_key(runs: dict[str, int], key: str) -> str:
    """A ledger key for this attempt: a rerun of a run ID adds to the spend, never replaces it."""
    n, out = 1, key
    while out in runs:
        n += 1
        out = f"{key}#{n}"
    return out


def ledger_from_archives(archives: list[Path]) -> dict:
    """The spend of every attempt kept under these archives, set-aside ones included, from their meta."""
    runs: dict[str, int] = {}
    for archive in archives:
        for meta_path in sorted(archive.rglob("meta.json")):
            meta = read_json(meta_path)
            if "run_id" not in meta or "tokens_total" not in meta:
                continue
            key = f"{archive.name}/{meta_path.parent.relative_to(archive).as_posix()}"
            runs[key] = max(meta["tokens_total"], meta["tokens_result"])
    return {"runs": runs, "total": sum(runs.values())}


def run_batch(tasks: list[dict], plan_obj: dict, *, archive: Path, scratch: Path, model: str, ledger: Path,
              cap: int, ceiling: int, wall: int, claude: str, kind: str | None, limit: int | None,
              projects: Path) -> dict:
    by_id = {t["task_id"]: t for t in tasks}
    book = read_json(ledger) if ledger.is_file() else {"runs": {}, "total": 0}
    done = started = 0
    stopped = None
    claude, cli_sha = resolve_cli(claude)
    for run in plan_obj["runs"]:
        if kind and run["kind"] != kind:
            continue
        if (archive / run["run_id"] / "meta.json").is_file():
            done += 1
            continue
        if limit is not None and started >= limit:
            break
        if book["total"] + cap > ceiling:
            stopped = f"ceiling: {book['total']} spent, {cap} more could cross {ceiling}"
            break
        meta = run_one(by_id[run["task_id"]], run, archive=archive, scratch=scratch, model=model, cap=cap,
                       wall=wall, claude=claude, projects=projects, cli_sha=cli_sha)
        spent = max(meta["tokens_total"], meta["tokens_result"])
        book["runs"][attempt_key(book["runs"], f"{archive.name}/{run['run_id']}")] = spent
        book["total"] = sum(book["runs"].values())
        write_json(ledger, book)
        started += 1
        if meta["invalid"]:
            # Keep the attempt, free the run ID for a rerun, and stop: every later run would fail the same way.
            n = 1
            while (archive / f"{run['run_id']}.invalid-{n}").exists():
                n += 1
            (archive / run["run_id"]).rename(archive / f"{run['run_id']}.invalid-{n}")
            stopped = f"invalid run {run['run_id']}: {meta['invalid']}"
            print(stopped, flush=True)
            break
        print(f"{run['run_id']}: reached={meta['reached']} tokens={spent} killed={meta['killed']} "
              f"verified={meta['verified']} total={book['total']}", flush=True)
    return {"already_done": done, "ran": started, "ledger_total": book["total"], "stopped": stopped}


# ---------------------------------------------------------------- report

def ttt(meta: dict, cap: int) -> int:
    """Tokens to target; a run that never reached it, or was killed, is censored at the cap."""
    if meta.get("reached") and meta.get("tokens_to_target") is not None and not meta.get("killed"):
        return int(meta["tokens_to_target"])
    return cap


def lab_verdict(family: str, a: list[dict], b: list[dict], cap: int) -> dict:
    ma = statistics.mean(ttt(m, cap) for m in a)
    mb = statistics.mean(ttt(m, cap) for m in b)
    ra, rb = sum(bool(m["reached"]) for m in a), sum(bool(m["reached"]) for m in b)
    ratio = ma / mb if mb else None
    out = {"reached_A": ra, "reached_B": rb, "mean_ttt_A": ma, "mean_ttt_B": mb, "ratio": ratio}
    if family == "positive":
        out["pass"] = (ra == len(a) and rb <= len(b) - 1) or (ratio is not None and ratio <= 0.5)
    elif family == "negative":
        if all(ttt(m, cap) == cap for m in a + b):
            # Every run censored: both means sit at the cap and the ratio is 1 by construction,
            # which says nothing about the brief. Inconclusive, not a pass.
            out["pass"] = None
            out["inconclusive"] = "every run in both arms is censored at the cap"
        else:
            out["pass"] = ra == rb and ratio is not None and 0.5 < ratio < 2.0
    elif family == "misleading":
        out["planted_A"] = sum(bool(m["planted_touched"]) for m in a)
        out["planted_B"] = sum(bool(m["planted_touched"]) for m in b)
        out["pass"] = out["planted_A"] >= 1
    else:
        out["pass"] = None  # reasoning-only: exploratory, recorded, never pass/fail
    return out


def bootstrap_median_ci(values: list[float], seed: int, n: int = 10_000) -> tuple[float, float]:
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(n))
    return meds[int(0.025 * n)], meds[int(0.975 * n) - 1]


def init_drift(metas: dict[tuple[str, str], list[dict]]) -> dict[str, list[str]]:
    """Every run's loaded plugins when the batch loaded more than one set (a held-equal breach), else {}."""
    sets = {m["run_id"]: list(m.get("init_plugins") or []) for ms in metas.values() for m in ms}
    return dict(sorted(sets.items())) if len({tuple(v) for v in sets.values()}) > 1 else {}


def bench_blind(tasks: list[dict], plan_obj: dict, lab: dict, missing: list[str]) -> tuple[list[str], bool | None]:
    """Positive controls without every planned run, and the verdict: True when a scored
    positive control failed, None (unknown) when none failed but one is not fully scored
    or none is planned, False only when every planned positive control ran and passed."""
    missing_tasks = {r["task_id"] for r in plan_obj["runs"] if r["run_id"] in missing}
    planned = {r["task_id"] for r in plan_obj["runs"]}
    positives = sorted(t["task_id"] for t in tasks
                       if t["kind"] == "lab" and t["family"] == "positive" and t["task_id"] in planned)
    incomplete = [tid for tid in positives if tid not in lab or tid in missing_tasks]
    if any(lab[tid]["pass"] is False for tid in positives if tid in lab):
        return incomplete, True
    if incomplete or not positives:
        return incomplete, None
    return incomplete, False


def report(tasks: list[dict], plan_obj: dict, archive: Path, cap: int, seed: int) -> dict:
    metas: dict[tuple[str, str], list[dict]] = {}
    missing = []
    for run in plan_obj["runs"]:
        path = archive / run["run_id"] / "meta.json"
        if path.is_file():
            metas.setdefault((run["task_id"], run["arm"]), []).append(read_json(path))
        else:
            missing.append(run["run_id"])
    lab, real, guard = {}, {}, {arm: {"verified": 0, "checked": 0} for arm in ARMS}
    for t in sorted(tasks, key=lambda t: t["task_id"]):
        a = sorted(metas.get((t["task_id"], "A"), []), key=lambda m: m["repeat"])
        b = sorted(metas.get((t["task_id"], "B"), []), key=lambda m: m["repeat"])
        for arm, ms in (("A", a), ("B", b)):
            for m in ms:
                if m.get("verified") is not None:
                    guard[arm]["checked"] += 1
                    guard[arm]["verified"] += bool(m["verified"])
        if not a or not b or len(a) != len(b):
            continue
        if t["kind"] == "lab":
            lab[t["task_id"]] = {"family": t["family"], **lab_verdict(t["family"], a, b, cap)}
        else:
            logs = [math.log(ttt(x, cap) / ttt(y, cap)) for x, y in zip(a, b)]
            real[t["task_id"]] = {"mean_log_ratio": statistics.mean(logs),
                                  "calls_to_target_A": [m["calls_to_target"] for m in a],
                                  "calls_to_target_B": [m["calls_to_target"] for m in b],
                                  "tokens_total_A": [m["tokens_total"] for m in a],
                                  "tokens_total_B": [m["tokens_total"] for m in b]}
    summary: dict[str, Any] = {"cap": cap, "missing_runs": missing, "lab": lab, "real": real,
                               "guardrail": guard, "init_drift": init_drift(metas)}
    summary["positive_controls_incomplete"], summary["bench_blind"] = bench_blind(tasks, plan_obj, lab, missing)
    logs = [v["mean_log_ratio"] for v in real.values()]
    if logs:
        lo, hi = bootstrap_median_ci(logs, seed) if len(logs) > 1 else (logs[0], logs[0])
        summary["primary"] = {"prompts": len(logs), "median_ratio": math.exp(statistics.median(logs)),
                              "ci95": [math.exp(lo), math.exp(hi)],
                              "sigma": statistics.stdev(logs) if len(logs) > 1 else None}
    return summary


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tasks-lab")
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--golden", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("tasks-real")
    p.add_argument("--episodes", type=Path, required=True)
    p.add_argument("--packets", required=True)
    p.add_argument("--payloads", type=Path, help="content-store payloads directory (start states)")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("plan")
    p.add_argument("--tasks", type=Path, required=True)
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("run")
    p.add_argument("--tasks", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--scratch", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--ledger", type=Path, required=True)
    p.add_argument("--cap", type=int, default=DEFAULT_CAP)
    p.add_argument("--ceiling", type=int, default=DEFAULT_CEILING)
    p.add_argument("--wall", type=int, default=DEFAULT_WALL)
    p.add_argument("--claude", default="claude")
    p.add_argument("--kind", choices=("lab", "real"))
    p.add_argument("--limit", type=int)
    p.add_argument("--projects", type=Path, default=Path.home() / ".claude" / "projects")
    p = sub.add_parser("ledger")
    p.add_argument("--archive", type=Path, action="append", required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("report")
    p.add_argument("--tasks", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--cap", type=int, default=DEFAULT_CAP)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    if args.cmd == "tasks-lab":
        write_json(args.out, tasks_lab(args.cases, args.golden))
    elif args.cmd == "tasks-real":
        write_json(args.out, tasks_real(args.episodes, [p for p in args.packets.split(",") if p], args.payloads))
    elif args.cmd == "plan":
        write_json(args.out, plan(read_json(args.tasks), args.repeats, args.seed))
    elif args.cmd == "run":
        res = run_batch(read_json(args.tasks), read_json(args.plan), archive=args.archive, scratch=args.scratch,
                        model=args.model, ledger=args.ledger, cap=args.cap, ceiling=args.ceiling, wall=args.wall,
                        claude=args.claude, kind=args.kind, limit=args.limit, projects=args.projects)
        print(json.dumps(res, indent=1))
    elif args.cmd == "ledger":
        write_json(args.out, ledger_from_archives(args.archive))
    elif args.cmd == "report":
        write_json(args.out, report(read_json(args.tasks), read_json(args.plan), args.archive, args.cap, args.seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

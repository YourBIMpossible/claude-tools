#!/usr/bin/env python3
"""Dry mode: the real hook scripts against a synthetic repository, stores and transcript.

``run_dry(out, n)`` builds, inside ``out`` only, a fixture repository carrying
``.evidence-compiler/``, a metadata and a content store, and for each of ``n`` episodes a
synthetic Evidence Compiler packet and a synthetic transcript (with ``cwd`` fields, a
brief attachment, a read and an edit). It runs ``capture_start.py`` and
``capture_end.py`` as subprocesses exactly as the client would (JSON on stdin), with
the stores, the transcript directory and ``EC_CAPTURE_MAINTAIN=0`` in their
environment, and reports wall-clock latencies and the hooks' own ``start_ms``/``end_ms``.

The client identity path runs for real: every inherited ``CLAUDE*`` variable is removed,
this Python process stands in for the client (``CLAUDE_PID``, mode ``synthetic``) and an
accepted boundary record for its interpreter is written inside ``out``
(``EC_CAPTURE_BOUNDARY_DIRS``). The shipped boundary records are never consulted.

``out`` must be a dedicated directory: not inside any git repository, no path segment
named ``scratchpad``, and either empty or already marked by ``.ec-capture-dry``. Nothing
outside ``out`` is read or written apart from the capture tool itself and the temp-dir
fallback log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import client_identity as ci
from common import iso, now_utc, sha256_text, write_json_atomic
from report import percentiles
from store import Stores, init_meta_repo, init_stores, scan_manifests

PKG = Path(__file__).resolve().parent
MARKER = ".ec-capture-dry"
CLI_VERSION = "2.1.280"
PROMPT = "Change alpha so it returns x * 3 + 2 and keep the module docstring."
FIXTURE_ENV = {"GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
               "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
               "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


SYNTHETIC_MODE = "synthetic"


class DryError(RuntimeError):
    """The output directory is not a safe place for dry-mode fixtures."""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env={**os.environ, **FIXTURE_ENV}).stdout


def check_out_dir(out: Path) -> Path:
    out = out.resolve()
    if any(part.lower() == "scratchpad" for part in out.parts):
        raise DryError("dry-mode output never goes to a scratchpad; use a dedicated directory")
    probe = out if out.exists() else next((p for p in out.parents if p.exists()), None)
    if probe is not None and subprocess.run(["git", "-C", str(probe), "rev-parse", "--git-dir"],
                                            capture_output=True).returncode == 0:
        raise DryError("dry-mode output must not be inside a git repository")
    if out.exists() and any(out.iterdir()) and not (out / MARKER).is_file():
        raise DryError("directory is not empty and not a dry-mode directory")
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("evidence-capture dry-mode fixtures (synthetic only)\n", encoding="utf-8")
    return out


def build_fixture_repo(root: Path) -> Path:
    repo = root / "fixture-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "alpha.py").write_text('"""Alpha."""\n\n\ndef alpha(x: int) -> int:\n    return x * 3 + 1\n',
                                           encoding="utf-8")
    (repo / ".evidence-compiler").mkdir()
    (repo / ".evidence-compiler" / "config.yaml").write_text("capture:\n  enabled: true\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n.evidence-compiler/packets/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "start")
    return repo


def build_stores(root: Path) -> Stores:
    st = Stores(root / "capture-meta", root / "capture-content")
    init_stores(st)
    init_meta_repo(st)
    return st


def write_packet(repo: Path, session_id: str, prompt: str, created_at: str) -> str:
    pid = "ep_" + uuid.uuid4().hex[:16]
    head = _git(repo, "rev-parse", "HEAD").strip()
    d = repo / ".evidence-compiler" / "packets"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({
        "packet_id": pid, "created_at": created_at,
        "identity": {"session_id": session_id, "head": head, "head_state": "resolved"},
        "correlation": {"prompt_hash": sha256_text(prompt)}}), encoding="utf-8")
    return pid


def transcript_entries(session_id: str, repo: Path, prompt: str, packet_id: str | None, ts: str,
                       version: str = CLI_VERSION, edit: bool = True) -> list[dict[str, Any]]:
    """One synthetic human turn: prompt, brief, a read, an edit and a closing reply."""
    cwd = str(repo)
    base = {"sessionId": session_id, "cwd": cwd, "version": version, "timestamp": ts}
    entries: list[dict[str, Any]] = [
        {**base, "type": "user", "uuid": f"u-{session_id[:8]}", "message": {"role": "user", "content": prompt}}]
    if packet_id:
        entries.append({**base, "type": "attachment", "attachment": {
            "type": "hook_additional_context",
            "content": [f'<context_brief packet_id="{packet_id}">src/alpha.py defines alpha</context_brief>']}})
    tools = [("Read", {"file_path": str(repo / "src" / "alpha.py")}, '"""Alpha."""')]
    if edit:
        tools.append(("Edit", {"file_path": str(repo / "src" / "alpha.py"), "old_string": "x * 3 + 1",
                               "new_string": "x * 3 + 2"}, "The file has been updated."))
    for k, (name, inp, result) in enumerate(tools):
        tid = f"toolu_{session_id[:8]}_{k}"
        entries.append({**base, "type": "assistant", "message": {"id": f"msg_{k}", "role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        entries.append({**base, "type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": False, "content": result}]}})
    entries.append({**base, "type": "assistant", "message": {"id": "msg_end", "role": "assistant",
                                                             "content": [{"type": "text", "text": "Done."}]}})
    return entries


def write_transcript(tdir: Path, session_id: str, entries: list[dict[str, Any]], append: bool = False) -> Path:
    p = tdir / "fixture-project" / f"{session_id}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return p


def synthetic_boundary(d: Path, version: str = CLI_VERSION, *, accepted: bool = True) -> Path:
    """An accepted boundary record whose client is this Python process's interpreter."""
    image = ci.pid_image(os.getpid())
    got = ci.hash_file(image) if image else None
    if got is None:
        raise DryError("cannot identify the synthetic client")
    d.mkdir(parents=True, exist_ok=True)
    rec = {"cli_version": version, "synthetic": True, "recorded_at": iso(now_utc()),
           "identity": {"sha256": got[0], "cli_version": version, "mode": SYNTHETIC_MODE, "problem": None},
           "accepted": {"at": iso(now_utc()), "by": "synthetic fixture"} if accepted else None,
           "relied_upon": ["synthetic"], "results": {"synthetic": {"passed": True}}}
    write_json_atomic(d / ci.record_name(version, SYNTHETIC_MODE), rec)
    return d


def hook_env(stores: Stores, tdir: Path, base: dict[str, str] | None = None,
             boundary_dir: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in (base if base is not None else os.environ).items() if not k.upper().startswith("CLAUDE")}
    for k in ("EVIDENCE_CAPTURE", "EVIDENCE_HOOK"):
        env.pop(k, None)
    env.update({"EC_CAPTURE_META": str(stores.meta), "EC_CAPTURE_CONTENT": str(stores.content),
                "EC_CAPTURE_TRANSCRIPTS": str(tdir), "EC_CAPTURE_MAINTAIN": "0",
                ci.ENV_PID: str(os.getpid()), ci.ENV_MODE: SYNTHETIC_MODE})
    if boundary_dir is not None:
        env["EC_CAPTURE_BOUNDARY_DIRS"] = str(boundary_dir)
    return env


def run_hook(script: str, payload: dict[str, Any] | bytes, env: dict[str, str]) -> tuple[int, bytes, float]:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, str(PKG / script)], input=data, capture_output=True, env=env,
                          timeout=120)
    return proc.returncode, proc.stdout, (time.perf_counter() - t0) * 1000


def run_dry(out: Path, n: int = 10) -> dict[str, Any]:
    out = check_out_dir(out)
    run = out / f"run-{iso(now_utc()).replace(':', '').replace('.', '')}"
    run.mkdir()
    repo = build_fixture_repo(run)
    stores = build_stores(run)
    tdir = run / "transcripts"
    env = hook_env(stores, tdir, boundary_dir=synthetic_boundary(run / "boundary"))
    index_before = (repo / ".git" / "index").read_bytes()
    walls: dict[str, list[int]] = {"start": [], "end": []}
    outputs: list[str] = []
    rcs: list[int] = []
    for _ in range(n):
        sid = str(uuid.uuid4())
        ts = iso(now_utc())
        pid = write_packet(repo, sid, PROMPT, ts)
        rc, so, ms = run_hook("capture_start.py", {"session_id": sid, "prompt": PROMPT, "cwd": str(repo),
                                                   "hook_event_name": "UserPromptSubmit"}, env)
        rcs.append(rc)
        outputs.append(so.decode("utf-8", "replace"))
        walls["start"].append(int(ms))
        tp = write_transcript(tdir, sid, transcript_entries(sid, repo, PROMPT, pid, ts))
        rc, so, ms = run_hook("capture_end.py", {"session_id": sid, "transcript_path": str(tp), "cwd": str(repo),
                                                 "hook_event_name": "Stop"}, env)
        rcs.append(rc)
        outputs.append(so.decode("utf-8", "replace"))
        walls["end"].append(int(ms))
    ms_, unreadable = scan_manifests(stores)
    index_unchanged = (repo / ".git" / "index").read_bytes() == index_before  # before any git status of ours
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1"], capture_output=True, text=True,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    return {
        "out": str(run), "episodes": n, "exit_codes": sorted(set(rcs)), "stdout_bytes": sum(len(o) for o in outputs),
        "wall_ms": {"start": percentiles(walls["start"]), "end": percentiles(walls["end"])},
        "hook_ms": {"start": percentiles([m["capture"]["start_ms"] for m in ms_ if m["capture"].get("start_ms")]),
                    "end": percentiles([m["capture"]["end_ms"] for m in ms_ if m["capture"].get("end_ms")])},
        "review_states": sorted({f"{(m.get('review') or {}).get('state')}:{(m.get('review') or {}).get('reason')}"
                                 for m in ms_}),
        "link_states": sorted({m["link"]["state"] for m in ms_}),
        "boundary_trusted": sum(1 for m in ms_ if m["boundary"].get("trusted")),
        "manifests_unreadable": unreadable,
        "index_unchanged": index_unchanged,
        "checkout_clean": status.returncode == 0 and not status.stdout.strip(),
    }

"""Offline relevance measurement for Evidence Compiler briefs.

Read-only. Joins every persisted packet (what the brief cited) to the Claude Code
session transcript that received it (what the agent actually opened/edited next),
and reports deterministic proxies for "did the brief help":

  hit        - the agent opened or edited at least one file the brief cited
  precision  - cited files the agent used / cited files
  recall     - in-repo files the agent used that the brief had cited / in-repo files used
  first-hit  - the first in-repo file the agent opened was one the brief cited

Every metric is paired with a SHUFFLED BASELINE: the same turn scored against
briefs from other packets in the same repository. Files an agent opens on every
turn (CLAUDE.md, ledgers) inflate raw hit rate; only the lift over baseline says
the brief pointed somewhere the agent would not have gone anyway.

This is an offline analysis tool. It never runs in the prompt path, never writes
into an analyzed repository, and never assigns usefulness labels.

Usage:
    python measure_relevance.py --out <dir>
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
STORE_GLOBS = ("F:/*/.evidence-compiler", "F:/*/.claude/worktrees/*/.evidence-compiler")
OPEN_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
BASELINE_SHIFTS = (1, 2, 3, 5, 8)
WORKTREE_RE = re.compile(r"^\.claude/worktrees/[^/]+/")
REF_RE = re.compile(r"^(.*?)(?::\d+(?:-\d+)?)?$")


@dataclass
class Packet:
    packet_id: str
    store_root: str
    repo_root: str
    created_at: str
    session_id: str | None
    source_kind: str
    traffic: str
    rg_outcome: str
    cited: set[str]


@dataclass
class Turn:
    opened: list[str] = field(default_factory=list)  # in order, normalized, in-repo only
    shell_text: list[str] = field(default_factory=list)
    tool_calls: int = 0
    delegated: bool = False


def norm_path(raw: str, repo_root: str, store_root: str) -> str | None:
    """Repo-relative, lowercase, forward-slash path; None when outside the repository."""
    p = raw.replace("\\", "/").strip().strip('"').lower()
    for root in sorted({store_root, repo_root}, key=len, reverse=True):
        r = root.replace("\\", "/").rstrip("/").lower() + "/"
        if p.startswith(r):
            p = p[len(r):]
            break
    else:
        if re.match(r"^[a-z]:/", p) or p.startswith("/"):
            return None
    p = WORKTREE_RE.sub("", p)
    return p.lstrip("./") or None


def cited_files(packet: dict) -> set[str]:
    omitted = set(packet.get("budget", {}).get("omitted_evidence_ids") or [])
    out: set[str] = set()
    for ev in packet.get("evidence", []):
        if ev.get("id") in omitted:
            continue
        for ref in ev.get("source_claim", {}).get("references") or []:
            m = REF_RE.match(ref.replace("\\", "/"))
            if m and m.group(1):
                out.add(WORKTREE_RE.sub("", m.group(1).lower()).lstrip("./"))
    return out


def traffic_classes(store_root: str) -> dict[str, tuple[str, str]]:
    """prefix -> (traffic, rg) from `evidence review inventory --all` (ids are truncated there)."""
    try:
        res = subprocess.run(
            ["evidence", "review", "--repo", store_root, "inventory", "--all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"WARN inventory failed for {store_root}: {exc}", file=sys.stderr)
        return {}
    out: dict[str, tuple[str, str]] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].startswith("ep_"):
            out[parts[0]] = (parts[2], parts[4])
    return out


def load_packets() -> tuple[list[Packet], Counter]:
    issues: Counter = Counter()
    packets: list[Packet] = []
    for store in sorted({p for g in STORE_GLOBS for p in glob.glob(g)}):
        store_root = str(Path(store).parent).replace("\\", "/")
        classes = traffic_classes(store_root)
        for f in sorted(Path(store, "packets").glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                issues["unreadable_packet"] += 1
                continue
            pid = d.get("packet_id", "")
            cls = next((v for k, v in classes.items() if pid.startswith(k)), None)
            if cls is None:
                issues["no_traffic_class"] += 1
            ident = d.get("identity", {})
            packets.append(Packet(
                packet_id=pid,
                store_root=store_root,
                repo_root=(ident.get("repository_root") or store_root).replace("\\", "/"),
                created_at=d.get("created_at", ""),
                session_id=ident.get("session_id"),
                source_kind=d.get("task", {}).get("source_kind", "unknown"),
                traffic=cls[0] if cls else "unknown",
                rg_outcome=cls[1] if cls else "unknown",
                cited=cited_files(d),
            ))
    return packets, issues


def _ts(entry: dict) -> datetime | None:
    t = entry.get("timestamp")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tool_uses(entry: dict):
    msg = entry.get("message")
    if entry.get("type") != "assistant" or not isinstance(msg, dict):
        return
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("name", ""), block.get("input") or {}


def _record(turn: Turn, name: str, inp: dict, pkt: Packet) -> None:
    turn.tool_calls += 1
    if name in OPEN_TOOLS:
        p = norm_path(str(inp.get("file_path") or inp.get("notebook_path") or ""), pkt.repo_root, pkt.store_root)
        if p:
            turn.opened.append(p)
    elif name in SHELL_TOOLS:
        turn.shell_text.append(str(inp.get("command", "")).replace("\\", "/").lower())
    elif name in {"Agent", "Task"}:
        turn.delegated = True


def build_turns(packets: list[Packet]) -> tuple[dict[str, Turn], Counter]:
    issues: Counter = Counter()
    by_session: dict[str, list[Packet]] = defaultdict(list)
    for p in packets:
        if p.session_id:
            by_session[p.session_id].append(p)
        else:
            issues["packet_without_session"] += 1
    turns: dict[str, Turn] = {}
    for sid, pkts in by_session.items():
        files = list(PROJECTS_DIR.glob(f"*/{sid}.jsonl"))
        if not files:
            issues["transcript_missing"] += len(pkts)
            continue
        wanted = {p.packet_id: p for p in pkts}
        current: Packet | None = None
        windows: list[tuple[Packet, datetime | None, datetime | None]] = []
        start: datetime | None = None
        last: datetime | None = None
        with files[0].open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                hit = next((pid for pid in wanted if pid in line), None) if "context_brief" in line else None
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _ts(entry) or last
                last = ts
                if hit:
                    if current is not None:
                        windows.append((current, start, ts))
                    current, start = wanted[hit], ts
                    turns.setdefault(hit, Turn())
                    continue
                if current is None:
                    continue
                for name, inp in _tool_uses(entry):
                    _record(turns[current.packet_id], name, inp, current)
        if current is not None:
            windows.append((current, start, None))
        issues["packet_not_in_transcript"] += len(set(wanted) - set(turns))
        # Delegated work: subagent transcripts that started inside a packet's window.
        sub_dir = files[0].with_suffix("") / "subagents"
        for sub in sub_dir.glob("*.jsonl") if sub_dir.is_dir() else []:
            try:
                entries = [json.loads(l) for l in sub.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            except (OSError, json.JSONDecodeError):
                issues["unreadable_subagent"] += 1
                continue
            first = next((t for t in map(_ts, entries) if t), None)
            if first is None:
                continue
            owner = next((p for p, s, e in windows if s and s <= first and (e is None or first < e)), None)
            if owner is None:
                continue
            for entry in entries:
                for name, inp in _tool_uses(entry):
                    _record(turns[owner.packet_id], name, inp, owner)
    return turns, issues


def score(cited: set[str], turn: Turn) -> dict | None:
    """None when the turn opened no in-repo files (nothing to be relevant to)."""
    used = set(turn.opened)
    if not used:
        return None
    inter = cited & used
    return {
        "hit": bool(inter),
        "precision": len(inter) / len(cited) if cited else 0.0,
        "recall": len(inter) / len(used),
        "first_hit": turn.opened[0] in cited,
        "shell_mention": any(c in s for c in cited for s in turn.shell_text),
    }


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    return {
        "n": len(rows),
        "hit_rate": sum(r["hit"] for r in rows) / len(rows),
        "precision_mean": statistics.mean(r["precision"] for r in rows),
        "recall_mean": statistics.mean(r["recall"] for r in rows),
        "first_hit_rate": sum(r["first_hit"] for r in rows) / len(rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="directory for results.json")
    args = ap.parse_args()

    packets, issues = load_packets()
    turns, t_issues = build_turns(packets)
    issues.update(t_issues)

    by_repo: dict[str, list[Packet]] = defaultdict(list)
    for p in sorted(packets, key=lambda p: p.created_at):
        by_repo[p.repo_root.lower()].append(p)

    groups: dict[str, dict[str, list[dict]]] = defaultdict(lambda: {"actual": [], "baseline": []})
    per_packet: list[dict] = []
    funnel: Counter = Counter()
    for repo, pkts in by_repo.items():
        for i, p in enumerate(pkts):
            funnel[f"traffic:{p.traffic}"] += 1
            if p.traffic != "candidate":
                continue
            funnel["candidate"] += 1
            turn = turns.get(p.packet_id)
            if turn is None:
                funnel["candidate_no_transcript_turn"] += 1
                continue
            if not p.cited:
                funnel["candidate_brief_cited_no_files"] += 1
            s = score(p.cited, turn)
            if s is None:
                funnel["candidate_turn_opened_no_repo_files"] += 1
                continue
            funnel["candidate_scored"] += 1
            base = []
            others = [q for q in pkts if q.packet_id != p.packet_id and q.cited and q.session_id != p.session_id]
            for k in BASELINE_SHIFTS:
                if others:
                    b = score(others[(i + k) % len(others)].cited, turn)
                    if b:
                        base.append(b)
            repo_name = Path(repo).name
            for key in ("ALL", f"repo:{repo_name}", f"rg:{p.rg_outcome.split('+')[0]}",
                        "incident:capped" if "capped" in p.rg_outcome else "incident:not-capped",
                        "incident:stall" if "stall" in p.rg_outcome else "incident:no-stall",
                        "brief:has-files" if p.cited else "brief:no-files"):
                groups[key]["actual"].append(s)
                groups[key]["baseline"].extend(base)
            per_packet.append({
                "packet_id": p.packet_id, "repo": repo_name, "created_at": p.created_at,
                "rg": p.rg_outcome, "cited_files": len(p.cited), "opened_files": len(set(turn.opened)),
                "delegated": turn.delegated, **s,
            })

    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "packets_total": len(packets),
        "funnel": dict(funnel),
        "join_issues": dict(issues),
        "groups": {k: {"actual": summarize(v["actual"]), "baseline": summarize(v["baseline"])}
                   for k, v in sorted(groups.items())},
        "per_packet": per_packet,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"packets {len(packets)}  funnel {dict(funnel)}")
    print(f"join issues {dict(issues)}")
    print(f"{'group':28} {'n':>4} {'hit':>6} {'base':>6} {'prec':>6} {'base':>6} {'recall':>6} {'base':>6} {'first':>6} {'base':>6}")
    for k, v in result["groups"].items():
        a, b = v["actual"], v["baseline"]
        if not a.get("n"):
            continue
        f = lambda d, m: f"{d.get(m, 0):6.0%}" if d.get("n") else "   n/a"
        print(f"{k:28} {a['n']:4} {f(a,'hit_rate')} {f(b,'hit_rate')} {f(a,'precision_mean')} {f(b,'precision_mean')} "
              f"{f(a,'recall_mean')} {f(b,'recall_mean')} {f(a,'first_hit_rate')} {f(b,'first_hit_rate')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

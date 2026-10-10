#!/usr/bin/env python3
"""Footprints v1: retrieval and observed use, per Evidence Compiler episode.

Reads the rows written by ``evidence-episodes/build_episodes.py`` and, for each
joined episode, compares what the brief cited with what the agent then touched.
Everything here is *retrieval and observed use*: whether the agent went where the
brief pointed. It is not a judgment that the brief helped.

Sets per episode (paths repo-relative, lowercase, forward slashes):

  B  brief     paths referenced by selected items (``path:line`` -> ``path``)
  O  omitted   paths referenced only by retrieved-but-not-selected items
  T  touched   files read, searched in, run, or written, by native tools
               (Read/Edit/Write/MultiEdit/NotebookEdit/Grep) and shell commands
               (``shell_paths.parse_command``)
  E  edited    native edit targets plus shell write targets (``git`` excluded)

Two paths match when equal or when one ends with ``/`` + the other, so a path
relative to a subdirectory still matches its repo-relative form.

Rows are scored when joined with an observed turn that has at least one assistant
message. Queued prompts delivered as a batch share one response, recorded on the
batch's last row; the others get status ``no_assistant_response``, not a
zero-work score.

Metrics (``null`` when the denominator is empty):

  hit            B and T share a file                             (null if B empty)
  coverage       |T & B| / |T|                                    (null if T empty)
  waste          |B - T| / |B|                                    (null if B empty)
  rediscovery    searches whose query names a brief file (basename or stem) or a
                 selected lexical-match symbol / searches         (null if no searches)
  gap            |E - B| / |E|, split into gap_in_packet (in O) and
                 gap_unretrieved                                  (null if E empty)
  head_start     1 - (call index of the first touch of a file in E & B) / calls;
                 0 when E & B is empty                            (null if E empty)
  calls_to_target  calls before the first touch of any file in E; first_target_cited
                 says whether that file was in B
  recall@k, mrr  retrieved items ranked by final_score against E (ARB-comparable)

Every metric is paired with a shuffled baseline: the same turn scored against the
brief of another episode in the same repository (shifts 1, 2, 3, 5, 8 in
created_at order). Lift = actual - baseline.

Facts get stable IDs: ``f_`` + sha1(kind|value)[:12], for cited_path,
omitted_path, native_touch, shell_path and brief_string (a snippet of 16+
characters from a selected item's statement that is absent from the prompt).

Outputs (all byte-deterministic for identical inputs):

  footprints.jsonl   one row per episode row; unscored rows carry ``status``
  unparsed.jsonl     shell segments with path-like tokens the parser could not
                     attribute to a role, with the reason
  pool.jsonl         Twin candidate pool (plan S1.3 filters), every filter's verdict
  summary.json       overall and per-slice aggregates, baselines, lift, parse-miss
  distributions.md   one page of the same aggregates

Read-only on every input. The candidate-pool filter runs ``git ls-tree`` against
each episode's repository (read-only); ``--no-git`` skips it and marks the check
unknown.

Usage:
    python footprints.py --episodes <episodes.jsonl> --out <dir> [--no-git]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shell_paths as sp  # noqa: E402

# Same conventions as evidence-relevance/measure_relevance.py.
BASELINE_SHIFTS = (1, 2, 3, 5, 8)
WORKTREE_RE = re.compile(r"^\.claude/worktrees/[^/]+/")
REF_RE = re.compile(r"^(.*?)(?::\d+(?:-\d+)?)?$")
MAIN_ROOT_RE = re.compile(r"/\.claude/worktrees/[^/]+/?$")
# ``identity.head`` is episode data; only a full object name reaches ``git ls-tree``.
SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
DELEGATE_TOOLS = {"Agent", "Task"}
RECALL_K = (1, 3, 5, 10)
MIN_SNIPPET = 16
POOL_MIN_CALLS_TO_TARGET = 3
LABEL = "retrieval and observed use"
HUMAN_ORIGINS = {"human", "user"}
METRICS = ("hit", "coverage", "waste", "rediscovery", "gap", "gap_in_packet", "gap_unretrieved",
           "head_start", "first_target_cited", "mrr") + tuple(f"recall@{k}" for k in RECALL_K)
BASELINED = ("hit", "coverage", "waste", "gap", "head_start")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fact_id(kind: str, value: str) -> str:
    return "f_" + hashlib.sha1(f"{kind}|{value}".encode("utf-8")).hexdigest()[:12]


def main_root(repo_root: str) -> str:
    return MAIN_ROOT_RE.sub("", repo_root.replace("\\", "/").rstrip("/"))


def norm(raw: str, roots: Iterable[str]) -> str | None:
    """Repo-relative lowercase path; None when absolute outside every root or empty."""
    p = raw.replace("\\", "/").strip().strip("'\"").lower()
    for root in sorted({r.replace("\\", "/").rstrip("/").lower() for r in roots}, key=len, reverse=True):
        if p == root:
            return None
        if p.startswith(root + "/"):
            p = p[len(root) + 1:]
            break
    else:
        if re.match(r"^[a-z]:/", p) or p.startswith(("/", "~", "$")):
            return None
    p = WORKTREE_RE.sub("", p)
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    return p if p and p not in {".", ".."} and not p.startswith("../") else None


def ref_path(ref: str) -> str:
    m = REF_RE.match(ref.replace("\\", "/"))
    return m.group(1) if m else ref


def matches(a: str, b: str) -> bool:
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def overlap(xs: Iterable[str], ys: Iterable[str]) -> set[str]:
    """Members of xs that match some member of ys."""
    ys = list(ys)
    return {x for x in xs if any(matches(x, y) for y in ys)}


def ratio(num: int, den: int) -> float | None:
    return num / den if den else None


class Footprint:
    """What one episode's turn touched, in call order."""

    def __init__(self, row: dict) -> None:
        ident = row["identity"]
        self.roots = [ident["repository_root"], main_root(ident["repository_root"])]
        self.calls = row["observed"]["tool_calls"]
        self.touches: list[tuple[int, str, str]] = []  # (call index, path, role)
        self.edits: list[tuple[int, str]] = []
        self.queries: list[tuple[int, str]] = []
        self.shell_segments = 0
        self.shell_pathlike = 0
        self.unparsed: list[dict] = []
        self.facts: dict[str, dict] = {}
        self.delegated = False
        self.outside = 0  # path arguments outside this repository (other repos, scratch, home)
        for idx, call in enumerate(self.calls):
            self._call(idx, call)

    def _fact(self, kind: str, value: str) -> None:
        fid = fact_id(kind, value)
        self.facts.setdefault(fid, {"id": fid, "kind": kind, "value": value})

    def _touch(self, idx: int, raw: str, role: str, *, edit: bool = False, native: bool) -> None:
        p = norm(raw, self.roots)
        if p is None:
            self.outside += 1
            return
        # A native Read/Edit/Write target is a file by definition; a shell or search
        # argument is one only when it looks like a concrete file name.
        is_file = (native and role in {"read", "edit"}) or sp.is_filelike(p)
        if not is_file and role not in {"search", "nav"}:
            return
        self._fact("native_touch" if native else "shell_path", f"{role}:{p}")
        if is_file:
            self.touches.append((idx, p, role))
            if edit:
                self.edits.append((idx, p))

    def _call(self, idx: int, call: dict) -> None:
        name, inp = call["name"], call.get("input") or {}
        if name in DELEGATE_TOOLS:
            self.delegated = True
        if name == "Read" and isinstance(inp.get("file_path"), str):
            self._touch(idx, inp["file_path"], "read", native=True)
        elif name in EDIT_TOOLS:
            target = inp.get("file_path") or inp.get("notebook_path")
            if isinstance(target, str):
                self._touch(idx, target, "edit", edit=True, native=True)
        elif name in {"Grep", "Glob"}:
            if isinstance(inp.get("pattern"), str):
                self.queries.append((idx, inp["pattern"]))
            if isinstance(inp.get("path"), str):
                self._touch(idx, inp["path"], "search", native=True)
        elif name in SHELL_TOOLS and isinstance(inp.get("command"), str):
            segments, script = sp.parse_command(inp["command"])
            for seg in segments:
                self.shell_segments += 1
                if seg.has_pathlike or seg.kind == "miss":
                    self.shell_pathlike += 1
                if seg.kind == "miss":
                    self.unparsed.append({"call": idx, "reason": seg.miss_reason, "segment": seg.text})
                    continue
                for q in seg.queries:
                    self.queries.append((idx, q))
                if seg.kind == "search" and not seg.queries and seg.verb == "find":
                    self.queries.append((idx, seg.text))
                for use in seg.paths:
                    edit = use.role == "write" and use.verb != "git"
                    self._touch(idx, use.path, use.role, edit=edit, native=False)
            for use in script:
                self._touch(idx, use.path, "script", native=False)

    @property
    def touched(self) -> set[str]:
        return {p for _, p, _ in self.touches}

    @property
    def edited(self) -> set[str]:
        return {p for _, p in self.edits}


def brief_sets(row: dict) -> tuple[set[str], set[str], list[dict], list[str], list[str]]:
    """(B, O, ranked items with paths, lexical symbols, brief snippets)."""
    ident = row["identity"]
    roots = [ident["repository_root"], main_root(ident["repository_root"])]
    selected: set[str] = set()
    other: set[str] = set()
    ranked: list[dict] = []
    symbols: list[str] = []
    snippets: list[str] = []
    for item in row["retrieved"]["items"]:
        paths = sorted({p for p in (norm(ref_path(r), roots) for r in item.get("references") or []) if p})
        ranked.append({"id": item["id"], "score": item.get("final_score") or 0.0,
                       "selected": bool(item.get("selected")), "paths": paths})
        (selected if item.get("selected") else other).update(paths)
        if item.get("selected"):
            statement = item.get("statement") or ""
            if item.get("kind") == "lexical_match" and " at " in statement:
                symbols.append(statement.split(" at ", 1)[0].strip())
            if "  |  " in statement:
                snippet = statement.split("  |  ", 1)[1].strip()
                if len(snippet) >= MIN_SNIPPET:
                    snippets.append(snippet)
    ranked.sort(key=lambda r: (-r["score"], not r["selected"], r["id"]))
    return selected, other - selected, ranked, sorted(set(symbols)), sorted(set(snippets))


def search_terms(brief: set[str], symbols: list[str]) -> set[str]:
    terms = set(s.lower() for s in symbols if len(s) >= 3)
    for p in brief:
        base = p.rsplit("/", 1)[-1]
        terms.add(base)
        stem = base.rsplit(".", 1)[0]
        if len(stem) >= 4:
            terms.add(stem)
    return terms


def score(brief: set[str], omitted: set[str], ranked: list[dict], terms: set[str],
          fp: Footprint) -> dict[str, Any]:
    touched, edited = fp.touched, fp.edited
    n = len(fp.calls)
    out: dict[str, Any] = {}
    out["hit"] = bool(overlap(brief, touched)) if brief else None
    out["coverage"] = ratio(len(overlap(touched, brief)), len(touched))
    out["waste"] = ratio(len(brief - overlap(brief, touched)), len(brief))
    searches = fp.queries
    out["rediscovery"] = ratio(sum(1 for _, q in searches if any(t in q.lower() for t in terms)),
                               len(searches))
    if edited:
        outside = edited - overlap(edited, brief)
        in_packet = overlap(outside, omitted)
        out["gap"] = len(outside) / len(edited)
        out["gap_in_packet"] = len(in_packet) / len(edited)
        out["gap_unretrieved"] = len(outside - in_packet) / len(edited)
        cited_targets = overlap(edited, brief)
        first_cited = next((i for i, p, _ in fp.touches if p in cited_targets), None)
        out["head_start"] = 0.0 if first_cited is None else 1 - first_cited / n
        first_target = next((i, p) for i, p, _ in fp.touches if p in edited)
        out["calls_to_target"] = first_target[0]
        out["first_target_cited"] = first_target[1] in cited_targets
        first_rank = next((k for k, item in enumerate(ranked, 1) if overlap(item["paths"], edited)), None)
        out["mrr"] = 1 / first_rank if first_rank else 0.0
        for k in RECALL_K:
            top = {p for item in ranked[:k] for p in item["paths"]}
            out[f"recall@{k}"] = len(overlap(edited, top)) / len(edited)
    else:
        for key in ("gap", "gap_in_packet", "gap_unretrieved", "head_start", "calls_to_target",
                    "first_target_cited", "mrr") + tuple(f"recall@{k}" for k in RECALL_K):
            out[key] = None
    return out


class TreeIndex:
    """Files present at a commit, from one read-only ``git ls-tree`` per (repo, head)."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.cache: dict[tuple[str, str], list[str] | None] = {}

    def files(self, root: str, head: Any) -> list[str] | None:
        if not self.enabled or not isinstance(head, str) or not SHA_RE.fullmatch(head):
            return None
        key = (root.lower(), head)
        if key not in self.cache:
            try:
                res = subprocess.run(["git", "-C", root, "ls-tree", "-r", "--name-only", head],
                                     capture_output=True, text=True, encoding="utf-8", timeout=60)
                self.cache[key] = (sorted(line.lower() for line in res.stdout.splitlines())
                                   if res.returncode == 0 else None)
            except (OSError, subprocess.SubprocessError):
                self.cache[key] = None
        return self.cache[key]


def untracked_dependencies(fp: Footprint, tree: list[str] | None) -> list[str] | None:
    """Files read before being written in the turn that are absent at HEAD; None when unknown.

    Heredoc/here-string literals (role ``script``) and executables are not dependencies
    of the work: the first are strings inside a program, the second the toolchain."""
    if tree is None:
        return None
    written: set[str] = set()
    missing: list[str] = []
    for _, p, role in fp.touches:
        if role in {"edit", "write"}:
            written.add(p)
        elif (role != "script" and not p.endswith(".exe") and p.split("/", 1)[0] != ".git"
              and p not in written and p not in missing
              and not any(t == p or t.endswith("/" + p) or t.startswith(p + "/") or f"/{p}/" in t
                          for t in tree)):
            missing.append(p)
    return missing


def pool_verdicts(row: dict, fp: Footprint, m: dict,
                  trees: TreeIndex) -> tuple[dict[str, Any], list[str] | None]:
    ident = row["identity"]
    git = next((c for c in row["retrieved"]["collectors_run"] if c["name"] == "git"), None)
    dirty = ((git or {}).get("diagnostic") or {}).get("dirty_count")
    tree = trees.files(main_root(ident["repository_root"]), ident.get("head"))
    untracked = untracked_dependencies(fp, tree)
    return {
        "standalone": (None if row["prompt"].get("prior_prompts") is None
                       else row["prompt"].get("origin") in HUMAN_ORIGINS and not row["prompt"]["prior_prompts"]),
        "clean_tree": ident.get("head_state") == "resolved" and dirty == 0,
        "edited_repo_file": bool(fp.edited),
        "calls_to_target": (m["calls_to_target"] or 0) > POOL_MIN_CALLS_TO_TARGET,
        "dependencies_tracked": None if untracked is None else not untracked,
    }, untracked


def slices(row: dict, fp: Footprint) -> dict[str, str]:
    retrieved = row["retrieved"]
    rg = next((c for c in retrieved["collectors_run"] if c["name"] == "ripgrep"), None)
    diag = (rg or {}).get("diagnostic") or {}
    return {
        "intent": str(row["identity"].get("intent")),
        "anchored": str("prompt_symbol" in (retrieved.get("scope") or {}).get("sources", [])),
        "capped": str(bool(diag.get("truncated"))),
        "stem": str(((diag.get("filename_stem") or {}).get("items") or 0) > 0),
        "traffic": str(row.get("traffic")),
        "delegated": str(fp.delegated),
    }


def mean(values: list) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return round(statistics.mean(vals), 4) if vals else None


def aggregate(rows: list[dict]) -> dict[str, Any]:
    agg: dict[str, Any] = {"n": len(rows)}
    for key in METRICS:
        vals = [r["metrics"][key] for r in rows if r["metrics"][key] is not None]
        agg[key] = {"n": len(vals), "mean": mean(vals)}
    for key in BASELINED:
        pairs = [(r["metrics"][key], r["baseline"][key]) for r in rows
                 if r["metrics"][key] is not None and r["baseline"].get(key) is not None]
        agg[key]["baseline_n"] = len(pairs)
        agg[key]["baseline"] = mean([b for _, b in pairs])
        actual = mean([a for a, _ in pairs])
        agg[key]["lift"] = (round(actual - agg[key]["baseline"], 4)
                            if actual is not None and agg[key]["baseline"] is not None else None)
    ctt = [r["metrics"]["calls_to_target"] for r in rows if r["metrics"]["calls_to_target"] is not None]
    agg["calls_to_target_median"] = statistics.median(ctt) if ctt else None
    return agg


def run(episodes_path: Path, out: Path, use_git: bool) -> dict[str, Any]:
    rows = [json.loads(line) for line in episodes_path.read_text(encoding="utf-8").splitlines() if line]
    trees = TreeIndex(use_git)
    results: list[dict] = []
    unparsed: list[dict] = []
    pool: list[dict] = []
    status_counts: Counter = Counter()
    miss_reasons: Counter = Counter()
    segs = pathlike = outside = 0
    scored: list[tuple[dict, dict, Footprint, tuple]] = []

    for row in sorted(rows, key=lambda r: r["packet_id"]):
        pid = row["packet_id"]
        if row["join_reason"] != "joined":
            status = f"not_joined:{row['join_reason']}"
        elif not row.get("retrieved"):
            status = "no_retrieved"
        elif not row.get("observed"):
            status = "no_observed"
        elif not (row["observed"].get("cost") or {}).get("assistant_messages"):
            # A queued prompt delivered in a batch: the batch's one response lands in the
            # batch's last row, which is scored with it. Scoring this row would count a
            # turn the agent never answered on its own as zero work.
            status = "no_assistant_response"
        else:
            status = "scored"
        status_counts[status] += 1
        if status != "scored":
            results.append({"packet_id": pid, "status": status, "label": LABEL})
            continue
        fp = Footprint(row)
        brief, omitted, ranked, symbols, snippets = brief_sets(row)
        segs += fp.shell_segments
        pathlike += fp.shell_pathlike
        outside += fp.outside
        for u in fp.unparsed:
            miss_reasons[str(u["reason"]).split(" ", 1)[0] if u["reason"] else "None"] += 1
            unparsed.append({"packet_id": pid, **u})
        scored.append((row, {"brief": brief, "omitted": omitted, "ranked": ranked,
                             "terms": search_terms(brief, symbols), "snippets": snippets}, fp, ()))

    by_repo: dict[str, list[int]] = defaultdict(list)
    for i, (row, *_rest) in enumerate(scored):
        by_repo[main_root(row["identity"]["repository_root"]).lower()].append(i)
    for idxs in by_repo.values():
        idxs.sort(key=lambda i: (scored[i][0]["identity"]["created_at"], scored[i][0]["packet_id"]))

    for i, (row, b, fp, _) in enumerate(scored):
        m = score(b["brief"], b["omitted"], b["ranked"], b["terms"], fp)
        repo = by_repo[main_root(row["identity"]["repository_root"]).lower()]
        pos = repo.index(i)
        base_vals: dict[str, list] = defaultdict(list)
        for k in BASELINE_SHIFTS:
            j = repo[(pos + k) % len(repo)]
            if j == i:
                continue
            ob = scored[j][1]
            bm = score(ob["brief"], ob["omitted"], ob["ranked"], ob["terms"], fp)
            for key in BASELINED:
                if bm[key] is not None:
                    base_vals[key].append(bm[key])
        baseline = {key: (round(statistics.mean(float(v) for v in base_vals[key]), 4)
                          if base_vals[key] else None) for key in BASELINED}
        prompt_text = (row["prompt"].get("text") or "")
        called = "\n".join(canonical(c.get("input")) for c in fp.calls)
        fresh = [s for s in b["snippets"] if s not in prompt_text]
        for p in sorted(b["brief"]):
            fp._fact("cited_path", p)
        for p in sorted(b["omitted"]):
            fp._fact("omitted_path", p)
        for s in fresh:
            fp._fact("brief_string", s)
        m["brief_strings"] = len(fresh)
        m["brief_strings_reused"] = sum(1 for s in fresh if canonical(s)[1:-1] in called)
        sl = slices(row, fp)
        rec = {
            "packet_id": row["packet_id"], "status": "scored", "label": LABEL, "slices": sl,
            "sizes": {"brief": len(b["brief"]), "omitted": len(b["omitted"]),
                      "touched": len(fp.touched), "edited": len(fp.edited),
                      "searches": len(fp.queries), "calls": len(fp.calls),
                      "shell_segments": fp.shell_segments, "shell_unparsed": len(fp.unparsed),
                      "outside_repo": fp.outside},
            "metrics": m, "baseline": baseline,
            "brief_items": sum(1 for it in b["ranked"] if it["selected"]),
            "brief_items_touched": sum(1 for it in b["ranked"]
                                       if it["selected"] and overlap(it["paths"], fp.touched)),
            "facts": sorted(fp.facts.values(), key=lambda f: (f["kind"], f["value"])),
        }
        results.append(rec)
        verdicts, untracked = pool_verdicts(row, fp, m, trees)
        pool.append({"packet_id": row["packet_id"], "eligible": all(v is True for v in verdicts.values()),
                     "verdicts": verdicts, "untracked": untracked, "slices": sl,
                     "metrics": {k: m[k] for k in ("hit", "gap", "head_start", "calls_to_target")}})

    results.sort(key=lambda r: r["packet_id"])
    scored_rows = [r for r in results if r["status"] == "scored"]
    by_slice: dict[str, dict[str, Any]] = {}
    for dim in ("intent", "anchored", "capped", "stem", "traffic", "delegated"):
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in scored_rows:
            groups[r["slices"][dim]].append(r)
        by_slice[dim] = {k: aggregate(v) for k, v in sorted(groups.items())}
    order = ("standalone", "clean_tree", "edited_repo_file", "calls_to_target", "dependencies_tracked")
    funnel, remaining = [], pool
    for k in order:
        remaining = [p for p in remaining if p["verdicts"][k] is True]
        funnel.append({"filter": k, "remaining": len(remaining)})
    leave_one_out = {k: sum(all(v is True for kk, v in p["verdicts"].items() if kk != k) for p in pool)
                     for k in order}
    verdict_counts: dict[str, Counter] = defaultdict(Counter)
    for p in pool:
        for k, v in p["verdicts"].items():
            verdict_counts[k][str(v)] += 1
    summary = {
        "label": LABEL,
        "episodes": len(rows),
        "status": dict(sorted(status_counts.items())),
        "parse": {"shell_segments": segs, "segments_with_paths": pathlike,
                  "unparsed": len(unparsed), "paths_outside_repo": outside,
                  "miss_rate": round(len(unparsed) / pathlike, 4) if pathlike else None,
                  "reasons": dict(sorted(miss_reasons.items()))},
        "overall": aggregate(scored_rows),
        "by_slice": by_slice,
        "pool": {"eligible": sum(p["eligible"] for p in pool), "funnel": funnel,
                 "eligible_without": leave_one_out,
                 "verdicts": {k: dict(sorted(v.items())) for k, v in sorted(verdict_counts.items())}},
        "inputs": {"episodes_sha256": sha256_file(episodes_path),
                   "footprints_sha256": sha256_file(Path(__file__)),
                   "shell_paths_sha256": sha256_file(Path(sp.__file__)),
                   "git_checks": use_git},
    }
    out.mkdir(parents=True, exist_ok=True)
    for name, items in (("footprints.jsonl", results), ("unparsed.jsonl", unparsed), ("pool.jsonl", pool)):
        (out / name).write_text("".join(canonical(x) + "\n" for x in items), encoding="utf-8", newline="\n")
    (out / "summary.json").write_text(json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8", newline="\n")
    (out / "distributions.md").write_text(render_md(summary), encoding="utf-8", newline="\n")
    return summary


def _fmt(v: Any) -> str:
    return "—" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)


def render_md(s: dict) -> str:
    cols = ("hit", "coverage", "waste", "rediscovery", "gap", "head_start", "recall@5", "mrr")
    lines = [f"# Footprints v1 — {s['label']}", "",
             f"Episodes {s['episodes']}; status " + ", ".join(f"{k} {v}" for k, v in s["status"].items()) + ".",
             f"Shell parse: {s['parse']['unparsed']} of {s['parse']['segments_with_paths']} path-bearing "
             f"segments unattributed (miss rate {_fmt(s['parse']['miss_rate'])}). "
             f"{s['parse']['paths_outside_repo']} path arguments fell outside the brief's repository "
             "(other repos, scratch, home) and are not scored.", "",
             "Means; `n` counts episodes where the metric is defined. Lift is actual minus the "
             "shuffled same-repo baseline.", "",
             "| slice | n | " + " | ".join(cols) + " | hit lift | gap lift |",
             "|---|---|" + "---|" * len(cols) + "---|---|"]

    def row(name: str, a: dict) -> str:
        return (f"| {name} | {a['n']} | " + " | ".join(_fmt(a[c]["mean"]) for c in cols)
                + f" | {_fmt(a['hit']['lift'])} | {_fmt(a['gap']['lift'])} |")

    lines.append(row("all", s["overall"]))
    for dim, groups in s["by_slice"].items():
        for k, a in groups.items():
            lines.append(row(f"{dim}={k}", a))
    lines += ["", f"Twin candidate pool: {s['pool']['eligible']} eligible. Funnel: "
              + " -> ".join(f"{f['filter']} {f['remaining']}" for f in s["pool"]["funnel"])
              + ". Eligible if one filter is dropped: "
              + ", ".join(f"{k} {n}" for k, n in s["pool"]["eligible_without"].items()) + ".",
              "", "Filter verdicts: "
              + "; ".join(f"{k} " + ", ".join(f"{v}={n}" for v, n in c.items())
                          for k, c in s["pool"]["verdicts"].items()) + ".", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--episodes", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--no-git", action="store_true", help="skip the read-only git ls-tree pool check")
    args = ap.parse_args(argv)
    s = run(args.episodes, args.out, not args.no_git)
    print(json.dumps({"status": s["status"], "parse_miss_rate": s["parse"]["miss_rate"],
                      "pool_eligible": s["pool"]["eligible"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

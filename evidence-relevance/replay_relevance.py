"""Relevance replay harness: where does each agent-opened file fall out of the brief?

Read-only against analysed repositories (compile runs with persist=False).
For every candidate packet whose turn opened in-repo files, the packet's stored
extracted symbols are re-compiled against today's checkout by the Evidence
Compiler source tree given in --src, repeated --repeats times, and each opened
file is traced through the pipeline:

  in_retained_raw_results  file is in the capped ripgrep content results
  in_filename_evidence     an exact filename-stem item names it
  in_final_candidates      any evidence item for it scored above relevance "none"
  in_brief / brief_rank    selected into the brief; 1-based rank in selection order
  cap_exclusion            confirmed | suspected | unknown | not_applicable
                           (confirmed requires the uncapped diagnostic rg run below)

Repeatability is reported, never normalised away, on three views:
raw retained sequence (exposes RG-CAP-DETERMINISM), retained path set, and brief order.

Coverage: native file tools only (Read/Edit/Write/NotebookEdit via measure_relevance).
Shell and git reads are NOT counted.

Usage:
  py replay_relevance.py --src <evidence-compiler>/src --label baseline --out <dir>
      [--packet ep_...] [--target ep_...=repo/rel/path.py] [--repeats 3]

--target traces a named file for a packet even when no native tool opened it
(e.g. the file was read through the shell); rows carry trace_source "target".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import measure_relevance as mr  # noqa: E402

STEM_SOURCE = "tracked_filename_stem"


def _import_compiler(src: Path):
    """Import evidence_compiler from --src and prove it was not shadowed by site-packages."""
    sys.path.insert(0, str(src))
    import evidence_compiler
    from evidence_compiler import compiler, ranking, scoping
    from evidence_compiler.collectors import ripgrep

    loaded = Path(evidence_compiler.__file__).resolve()
    if src.resolve() not in loaded.parents:
        raise SystemExit(f"import guard: evidence_compiler loaded from {loaded}, not under {src}")
    return compiler, ranking, scoping, ripgrep, loaded


def _path_of(ref: str) -> str:
    m = mr.REF_RE.match(ref.replace("\\", "/"))
    return mr.WORKTREE_RE.sub("", (m.group(1) if m else ref).lower()).lstrip("./")


def _uncapped(ripgrep, repo: str, symbol: str, cache: dict) -> set[str] | None:
    key = (repo, symbol)
    if key not in cache:
        try:
            proc = subprocess.run(
                ["rg", "--files-with-matches", *ripgrep._match_args(symbol), "-e", symbol, "."],
                cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            cache[key] = {_path_of(l) for l in proc.stdout.splitlines() if l.strip()} if proc.returncode in (0, 1) else None
        except (OSError, subprocess.TimeoutExpired):
            cache[key] = None
    return cache[key]


def _compile(compiler, scoping, repo: str, symbols: list[str]):
    real = scoping.build_task

    def fixed(prompt, active_file, **kw):
        task = real(prompt, active_file, **kw)
        task.extracted_symbols = list(symbols)
        return task

    scoping.build_task = fixed
    try:
        return compiler.compile_packet(" ".join(symbols), repo, persist=False)
    finally:
        scoping.build_task = real


def _views(result, ranking) -> dict:
    ev = result.packet.evidence
    rg_items = [e for e in ev if e.provenance.collector == "ripgrep" and e.provenance.extra.get("evidence_source") != STEM_SOURCE]
    stem_items = [e for e in ev if e.provenance.extra.get("evidence_source") == STEM_SOURCE]
    selected = [e for e in ev if e.compiler_assessment.selected]
    if hasattr(ranking, "selection_key"):  # tie-break branch: mirror its real fill order
        symbols = [s.lower() for s in result.packet.task.extracted_symbols if s]
        selected.sort(key=lambda e: ranking.selection_key(e, symbols))
    else:  # evidence is canonical-sorted, selection is score-desc and stable on that order
        order = {e.id: i for i, e in enumerate(ev)}
        selected.sort(key=lambda e: (-e.compiler_assessment.final_score, order[e.id]))
    return {
        "stem_brief_rank": {_path_of(r): i + 1 for i, e in reversed(list(enumerate(selected)))
                            if e.provenance.extra.get("evidence_source") == STEM_SOURCE
                            for r in e.source_claim.references},
        "raw_sequence": [(e.provenance.extra.get("symbol"), _path_of(r)) for e in rg_items for r in e.source_claim.references],
        "raw_by_symbol": {s: sum(1 for e in rg_items if e.provenance.extra.get("symbol") == s) for s in {e.provenance.extra.get("symbol") for e in rg_items}},
        "stem_paths": {_path_of(r) for e in stem_items for r in e.source_claim.references},
        "candidate_paths": {_path_of(r) for e in ev if e.compiler_assessment.relevance != "none" for r in e.source_claim.references},
        "brief_paths": [_path_of(r) for e in selected for r in e.source_claim.references],
    }


def _cap_exclusion(path: str, symbols: list[str], views: dict, uncapped: dict, ripgrep, cap_hit: set[str]) -> str:
    retained = {p for _, p in views["raw_sequence"]}
    if path in retained:
        return "not_applicable"
    containing, unknown = [], False
    for s in symbols:
        u = uncapped.get(s)
        if u is None:
            unknown = True
        elif path in u:
            containing.append(s)
    if containing:
        return "confirmed" if any(s in cap_hit for s in containing) else "suspected"
    return "unknown" if unknown else "not_applicable"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--packet", action="append", default=[])
    ap.add_argument("--target", action="append", default=[])
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    targets: dict[str, set[str]] = {}
    for t in args.target:
        pid, _, path = t.partition("=")
        targets.setdefault(pid, set()).add(path.replace("\\", "/").lower())
    args.packet += [pid for pid in targets if pid not in args.packet]

    compiler, ranking, scoping, ripgrep, loaded = _import_compiler(args.src)
    packets, _ = mr.load_packets()
    pool = [p for p in packets if p.traffic == "candidate" and Path(p.repo_root).is_dir()
            and (not args.packet or any(p.packet_id.startswith(x) for x in args.packet))]
    turns, _ = mr.build_turns(pool)

    uncapped_cache: dict = {}
    rows, packet_rows = [], []
    for p in pool:
        turn = turns.get(p.packet_id)
        native = set(turn.opened) if turn else set()
        named = next((v for k, v in targets.items() if p.packet_id.startswith(k)), set())
        opened = sorted(native | named)
        if not opened:
            continue
        raw = json.loads(next(Path(p.store_root, ".evidence-compiler", "packets").glob(f"*{p.packet_id}.json")).read_text(encoding="utf-8"))
        symbols = raw.get("task", {}).get("extracted_symbols") or []
        if not symbols:
            continue

        runs = [_views(_compile(compiler, scoping, p.repo_root, symbols), ranking) for _ in range(args.repeats)]
        uncapped = {s: _uncapped(ripgrep, p.repo_root, s, uncapped_cache) for s in symbols}
        v = runs[0]
        cap_hit = {s for s, n in v["raw_by_symbol"].items() if n >= ripgrep._MAX_MATCHES_PER_SYMBOL}
        cap_hit |= {s for s in symbols if s not in v["raw_by_symbol"] and uncapped.get(s)}  # total-cap not_searched

        packet_rows.append({
            "packet_id": p.packet_id, "repo": Path(p.repo_root).name, "opened": len(opened),
            "repeat_same_raw_sequence": all(r["raw_sequence"] == v["raw_sequence"] for r in runs),
            "repeat_same_raw_path_set": all({x for _, x in r["raw_sequence"]} == {x for _, x in v["raw_sequence"]} for r in runs),
            "repeat_same_brief": all(r["brief_paths"] == v["brief_paths"] for r in runs),
        })
        for f in opened:
            ranks = [next((i + 1 for i, b in enumerate(dict.fromkeys(r["brief_paths"])) if b == f), None) for r in runs]
            rows.append({
                "packet_id": p.packet_id, "opened_file": f,
                "trace_source": "native_open" if f in native else "target",
                "in_retained_raw_results": [f in {x for _, x in r["raw_sequence"]} for r in runs],
                "in_filename_evidence": [f in r["stem_paths"] for r in runs],
                "in_final_candidates": [f in r["candidate_paths"] for r in runs],
                "in_brief": [x is not None for x in ranks],
                "brief_rank": ranks,
                "stem_item_brief_rank": [r["stem_brief_rank"].get(f) for r in runs],
                "symbols": symbols,
                "cap_exclusion": _cap_exclusion(f, symbols, v, uncapped, ripgrep, cap_hit),
                "in_uncapped_content": any(f in (uncapped.get(s) or set()) for s in symbols),
            })

    def share(key: str) -> int:
        return sum(1 for r in rows if r[key][0])

    summary = {
        "label": args.label, "module": str(loaded), "coverage": "native file tools only (shell/git reads not counted)",
        "repeats": args.repeats, "packets": len(packet_rows), "opened_files": len(rows),
        "in_uncapped_content": sum(r["in_uncapped_content"] for r in rows),
        "in_retained_raw_results": share("in_retained_raw_results"),
        "in_filename_evidence": share("in_filename_evidence"),
        "in_final_candidates": share("in_final_candidates"),
        "in_brief": share("in_brief"),
        "cap_exclusion": dict(Counter(r["cap_exclusion"] for r in rows)),
        "packets_repeat_same_raw_sequence": sum(r["repeat_same_raw_sequence"] for r in packet_rows),
        "packets_repeat_same_raw_path_set": sum(r["repeat_same_raw_path_set"] for r in packet_rows),
        "packets_repeat_same_brief": sum(r["repeat_same_brief"] for r in packet_rows),
        "files_brief_membership_unstable": sum(1 for r in rows if len(set(r["in_brief"])) > 1),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"replay_relevance_{args.label}.json").write_text(
        json.dumps({"summary": summary, "packets": packet_rows, "files": rows}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

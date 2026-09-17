"""Compare replay_relevance.py outputs across configurations.

Usage:
  py compare_replays.py --base master=<json> --cfg stem=<json> --cfg tiebreak=<json>
      [--focus backend/aec/nl_filter.py] [--min-stable 3]

Per config: per-repeat candidate/brief hit counts, any/all-repeat brief hits,
packets with identical brief across repeats. Against the base: regressions
(files in the base brief on >= --min-stable of n repeats and on 0 in the config)
and gains (the reverse). Focus file: candidate, brief, rank per repeat per packet.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(spec: str) -> tuple[str, dict]:
    name, _, path = spec.partition("=")
    return name, json.loads(Path(path).read_text(encoding="utf-8"))


def _index(data: dict) -> dict:
    return {(r["packet_id"], r["opened_file"]): r for r in data["files"]}


def _config_row(name: str, data: dict) -> dict:
    files = data["files"]
    n = data["summary"]["repeats"]
    return {
        "config": name,
        "module": data["summary"]["module"],
        "repeats": n,
        "candidate_per_repeat": [sum(r["in_final_candidates"][i] for r in files) for i in range(n)],
        "brief_per_repeat": [sum(r["in_brief"][i] for r in files) for i in range(n)],
        "brief_any": sum(any(r["in_brief"]) for r in files),
        "brief_all": sum(all(r["in_brief"]) for r in files),
        "filename_evidence_any": sum(any(r["in_filename_evidence"]) for r in files),
        "packets_same_brief": data["summary"]["packets_repeat_same_brief"],
        "packets": data["summary"]["packets"],
    }


def _flips(base: dict, cfg: dict, min_stable: int) -> dict:
    b, c = _index(base), _index(cfg)
    lost, gained = [], []
    for key, br in b.items():
        cr = c.get(key)
        if cr is None:
            continue
        bn, cn = sum(br["in_brief"]), sum(cr["in_brief"])
        row = {"packet_id": key[0], "file": key[1], "base": f"{bn}/{len(br['in_brief'])}", "config": f"{cn}/{len(cr['in_brief'])}",
               "stem_candidate": any(cr["in_filename_evidence"])}
        if bn >= min_stable and cn == 0:
            lost.append(row)
        elif cn >= min_stable and bn == 0:
            gained.append(row)
    return {"regressions": lost, "gains": gained}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--cfg", action="append", default=[])
    ap.add_argument("--focus", default="backend/aec/nl_filter.py")
    ap.add_argument("--min-stable", type=int, default=3)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    base_name, base = _load(args.base)
    cfgs = [_load(s) for s in args.cfg]
    report = {"configs": [_config_row(base_name, base)] + [_config_row(n, d) for n, d in cfgs], "vs_base": {}, "focus": {}}
    for name, data in cfgs:
        report["vs_base"][name] = _flips(base, data, args.min_stable)
    for name, data in [(base_name, base)] + cfgs:
        report["focus"][name] = [
            {"packet_id": r["packet_id"], "trace_source": r["trace_source"],
             "term_extracted": any(args.focus.rsplit("/", 1)[-1].rsplit(".", 1)[0] == s.lower() for s in r.get("symbols", [])),
             "candidate": r["in_final_candidates"], "brief": r["in_brief"],
             "brief_rank": r["brief_rank"], "stem_item_brief_rank": r.get("stem_item_brief_rank")}
            for r in data["files"] if r["opened_file"] == args.focus
        ]
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

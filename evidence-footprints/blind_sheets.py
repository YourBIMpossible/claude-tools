#!/usr/bin/env python3
"""Blind episode sheets for an owner labeling sitting, and the seal for the labels.

``make`` draws a seeded, stratified sample of scored episodes that edited a repo
file and had a non-empty brief, and writes one sheet per episode under a shuffled
blind ID. A sheet shows the prompt, the injected brief, the agent's tool calls and
text, and what Footprints extracted (files touched and edited). It shows no metric,
no rule verdict, no packet ID and no timestamp. The key from blind ID to packet ID
goes to a separate file.

``seal`` validates a filled-in labels file and records its sha256, so labels are
fixed before anyone compares them with a score.

    python blind_sheets.py make --episodes <episodes.jsonl> --transcripts <source>=<dir> ...
                                --seed <int> [--per-stratum 5] --out <dir>
    python blind_sheets.py seal --labels <labels.json> --out <seal.json>

Offline, read-only on its inputs, standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import footprints as fpm  # noqa: E402

OUTCOMES = ("saved_work", "one_fact_mattered", "neutral", "distracted", "better_evidence_existed", "unknown")
EXTRACTOR = ("agree", "disagree")
MAX_INPUT = 300
MAX_TEXT = 1500
KEY_FIELDS = {
    "Read": ("file_path",), "Edit": ("file_path",), "Write": ("file_path",), "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",), "Grep": ("pattern", "path", "glob"), "Glob": ("pattern", "path"),
    "Bash": ("command",), "PowerShell": ("command",), "Agent": ("description",), "Task": ("description",),
}
INSTRUCTIONS = """# Blind labeling sitting

Each sheet in `sheets/` is one past turn: the prompt, the brief the agent was given,
what it did, and what the extractor recorded. Scores and IDs are hidden. Do not open
`key/` until the labels are sealed.

For each sheet, fill in its entry in `labels.json`:

1. `extractor`: `agree` if the **Extracted** section lists the files the agent
   actually read and edited (judged against the tool calls), else `disagree`, with
   the missing or wrong file in `extractor_note`.
2. `outcome`: one of `saved_work`, `one_fact_mattered`, `neutral`, `distracted`,
   `better_evidence_existed`, `unknown` — what the brief did for this turn.
3. `note`: optional.

Then seal: `python blind_sheets.py seal --labels labels.json --out seal.json`.
"""


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_scored(row: dict) -> bool:
    return (row.get("join_reason") == "joined" and bool(row.get("retrieved")) and bool(row.get("observed"))
            and bool((row["observed"].get("cost") or {}).get("assistant_messages")))


def candidates(rows: list[dict]) -> dict[str, list[tuple[dict, fpm.Footprint]]]:
    """Scored episodes with an edited repo file and a non-empty brief, by anchored stratum."""
    strata: dict[str, list[tuple[dict, fpm.Footprint]]] = {"True": [], "False": []}
    for row in sorted(rows, key=lambda r: r["packet_id"]):
        if not is_scored(row) or not (row.get("injected") or {}).get("text"):
            continue
        fp = fpm.Footprint(row)
        if fp.edited:
            strata[fpm.slices(row, fp)["anchored"]].append((row, fp))
    return strata


def compact(call: dict) -> str:
    inp = call.get("input") or {}
    fields = KEY_FIELDS.get(call["name"])
    if fields:
        text = " ".join(f"{k}={inp[k]}" if k != "command" else str(inp[k]) for k in fields if k in inp)
    else:
        text = canonical(inp)
    text = " ".join(text.split())
    return text if len(text) <= MAX_INPUT else text[:MAX_INPUT] + " …"


def turn_text(row: dict, transcript_dirs: dict[str, Path]) -> list[str] | None:
    """The agent's visible text in the turn window, or None when the transcript is unavailable."""
    tr = row.get("transcript") or {}
    base = transcript_dirs.get(tr.get("source", ""))
    if base is None or not tr.get("path"):
        return None
    path = base / tr["path"]
    if not path.is_file():
        return None
    start = row["prompt"]["timestamp"]
    end = row["observed"].get("turn_end_timestamp")
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = entry.get("timestamp")
        if (entry.get("type") != "assistant" or entry.get("isSidechain") or not isinstance(ts, str)
                or ts < start or (end is not None and ts > end)):
            continue
        content = (entry.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                t = block["text"].strip()
                texts.append(t if len(t) <= MAX_TEXT else t[:MAX_TEXT] + " …")
    return texts


def fence(text: str) -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}\n{text}\n{ticks}"


def render_sheet(blind_id: str, row: dict, fp: fpm.Footprint, texts: list[str] | None) -> str:
    out = [f"# {blind_id}", "", f"Repository: `{row['identity']['repository_root']}`", "",
           "## Prompt", "", fence(row["prompt"]["text"]), "",
           "## Brief given to the agent", "", fence(row["injected"]["text"]), "",
           f"## Tool calls ({len(fp.calls)})", ""]
    for idx, call in enumerate(fp.calls):
        err = " **(error)**" if call.get("is_error") else ""
        text = compact(call)
        code = f"`` {text} ``" if "`" in text else f"`{text}`"
        out.append(f"{idx}. `{call['name']}`{err} {code}")
    out += ["", "## Agent text", ""]
    if texts is None:
        out.append("(transcript unavailable)")
    elif not texts:
        out.append("(no text in this turn)")
    else:
        for t in texts:
            out += [fence(t), ""]
    edited = {p for _, p in fp.edits}
    out += ["", "## Extracted", "", "Edited files (first call index):", ""]
    first_edit: dict[str, int] = {}
    for idx, p in fp.edits:
        first_edit.setdefault(p, idx)
    out += [f"- `{p}` ({i})" for p, i in sorted(first_edit.items())] or ["- (none)"]
    out += ["", "Files touched, other than edited (role, first call index):", ""]
    first_touch: dict[str, tuple[int, str]] = {}
    for idx, p, role in fp.touches:
        if p not in edited:
            first_touch.setdefault(p, (idx, role))
    out += [f"- `{p}` ({r}, {i})" for p, (i, r) in sorted(first_touch.items())] or ["- (none)"]
    out += ["", f"Paths outside this repository (not scored): {fp.outside}", ""]
    return "\n".join(out)


def make(episodes: Path, transcript_dirs: dict[str, Path], seed: int, per_stratum: int, out: Path) -> dict:
    data = episodes.read_bytes()
    rows = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]
    strata = candidates(rows)
    rng = random.Random(seed)
    chosen: list[tuple[str, dict, fpm.Footprint]] = []
    for name in sorted(strata):
        pool = strata[name]
        if len(pool) < per_stratum:
            raise SystemExit(f"stratum anchored={name} has {len(pool)} candidates, fewer than {per_stratum}")
        chosen += [(name, row, fp) for row, fp in rng.sample(pool, per_stratum)]
    rng.shuffle(chosen)

    (out / "sheets").mkdir(parents=True, exist_ok=True)
    (out / "key").mkdir(parents=True, exist_ok=True)
    key, labels, sheet_sha = {}, {}, {}
    for n, (stratum, row, fp) in enumerate(chosen, 1):
        bid = f"b{n:02d}"
        sheet = render_sheet(bid, row, fp, turn_text(row, transcript_dirs)).encode("utf-8")
        (out / "sheets" / f"{bid}.md").write_bytes(sheet)
        sheet_sha[bid] = sha256_bytes(sheet)
        key[bid] = {"packet_id": row["packet_id"], "anchored": stratum}
        labels[bid] = {"extractor": None, "extractor_note": "", "outcome": None, "note": ""}
    key_bytes = (json.dumps(key, indent=1, sort_keys=True) + "\n").encode("utf-8")
    (out / "key" / "key.json").write_bytes(key_bytes)
    (out / "labels.json").write_text(json.dumps(labels, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out / "INSTRUCTIONS.md").write_text(INSTRUCTIONS, encoding="utf-8")
    manifest = {
        "episodes_sha256": sha256_bytes(data),
        "script_sha256": sha256_bytes(Path(__file__).read_bytes()),
        "footprints_sha256": sha256_bytes(Path(fpm.__file__).read_bytes()),
        "seed": seed,
        "per_stratum": per_stratum,
        "candidates": {f"anchored={k}": len(v) for k, v in sorted(strata.items())},
        "sheets_sha256": sheet_sha,
        "key_sha256": sha256_bytes(key_bytes),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def validate(labels: dict) -> list[str]:
    errors = []
    if not labels:
        errors.append("no labels")
    for bid, lab in sorted(labels.items()):
        if lab.get("extractor") not in EXTRACTOR:
            errors.append(f"{bid}: extractor must be one of {EXTRACTOR}")
        if lab.get("extractor") == "disagree" and not str(lab.get("extractor_note", "")).strip():
            errors.append(f"{bid}: a disagree needs an extractor_note")
        if lab.get("outcome") not in OUTCOMES:
            errors.append(f"{bid}: outcome must be one of {OUTCOMES}")
    return errors


def seal(labels_path: Path, out: Path) -> dict:
    data = labels_path.read_bytes()
    errors = validate(json.loads(data.decode("utf-8")))
    if errors:
        raise SystemExit("labels not sealed:\n" + "\n".join(errors))
    record = {"labels_sha256": sha256_bytes(data), "labels": labels_path.name}
    out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make")
    mk.add_argument("--episodes", type=Path, required=True)
    mk.add_argument("--transcripts", action="append", default=[], metavar="SOURCE=DIR")
    mk.add_argument("--seed", type=int, required=True)
    mk.add_argument("--per-stratum", type=int, default=5)
    mk.add_argument("--out", type=Path, required=True)
    se = sub.add_parser("seal")
    se.add_argument("--labels", type=Path, required=True)
    se.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "make":
        dirs = {}
        for spec in args.transcripts:
            name, sep, path = spec.partition("=")
            if not sep:
                ap.error(f"--transcripts expects SOURCE=DIR, got {spec!r}")
            dirs[name] = Path(path)
        result = make(args.episodes, dirs, args.seed, args.per_stratum, args.out)
    else:
        result = seal(args.labels, args.out)
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

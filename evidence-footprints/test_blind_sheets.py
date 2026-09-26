#!/usr/bin/env python3
"""Unit tests for blind_sheets.py.

No framework, plain checks, synthetic tempfile fixtures only — never a real
episode file, transcript, or repository.

Synthetic example — contains no private repository data or production findings.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blind_sheets as bs  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
WT = "C:/work/demo/.claude/worktrees/lane"


def check(cond: bool, name: str) -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"  {'ok ' if cond else 'FAIL'} {name}")


def episode(n: int, anchored: bool, *, edits: bool = True, brief: str | None = "brief text",
            messages: int = 1) -> dict:
    pid = f"ep_{n:016x}"
    calls = [{"seq": 0, "name": "Read", "input": {"file_path": f"{WT}/src/m{n}.py"}},
             {"seq": 1, "name": "Bash", "input": {"command": "cat src/shared.py `echo x`"}}]
    if edits:
        calls.append({"seq": 2, "name": "Edit",
                      "input": {"file_path": f"{WT}/src/m{n}.py", "old_string": "a", "new_string": "b"}})
    return {
        "packet_id": pid, "join_reason": "joined", "traffic": "candidate",
        "identity": {"repository_root": WT, "created_at": f"2026-09-21T00:{n:02d}:00Z", "intent": "debugging"},
        "retrieved": {"items": [], "scope": {"sources": ["prompt_symbol"] if anchored else ["git_diff"]},
                      "collectors_run": []},
        "injected": {"text": brief, "found": brief is not None, "rendered_nonempty": bool(brief)},
        "observed": {"tool_calls": calls, "cost": {"assistant_messages": messages},
                     "turn_end_timestamp": f"2026-09-21T00:{n:02d}:30Z"},
        "prompt": {"text": f"fix module {n}", "origin": "user", "timestamp": f"2026-09-21T00:{n:02d}:00Z"},
        "transcript": {"source": "s", "path": "t.jsonl"},
    }


def transcript(path: Path, ns: list[int]) -> None:
    lines = []
    for n in ns:
        lines.append({"type": "assistant", "timestamp": f"2026-09-21T00:{n:02d}:10Z",
                      "message": {"content": [{"type": "text", "text": f"done with {n}"}]}})
        lines.append({"type": "assistant", "isSidechain": True, "timestamp": f"2026-09-21T00:{n:02d}:11Z",
                      "message": {"content": [{"type": "text", "text": "SIDECHAIN"}]}})
    lines.append({"type": "assistant", "timestamp": "2026-09-21T00:59:59Z",
                  "message": {"content": [{"type": "text", "text": "LATER TURN"}]}})
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test(tmp: Path) -> None:
    rows = [episode(n, anchored=n % 2 == 0) for n in range(1, 15)]
    rows += [episode(20, True, edits=False), episode(21, False, brief=None), episode(22, True, messages=0)]
    eps = tmp / "episodes.jsonl"
    eps.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    tdir = tmp / "tr"
    tdir.mkdir()
    transcript(tdir / "t.jsonl", list(range(1, 15)))

    strata = bs.candidates(rows)
    check(sorted(len(v) for v in strata.values()) == [7, 7],
          "candidates: scored, edited, non-empty brief; split by anchored")

    out1, out2 = tmp / "o1", tmp / "o2"
    m1 = bs.make(eps, {"s": tdir}, 7, 3, out1)
    bs.make(eps, {"s": tdir}, 7, 3, out2)
    files1 = sorted(p.relative_to(out1).as_posix() for p in out1.rglob("*") if p.is_file())
    same = all((out1 / f).read_bytes() == (out2 / f).read_bytes() for f in files1)
    check(same, "same seed gives byte-identical output")
    check(len(m1["sheets_sha256"]) == 6, "per-stratum sample size honoured")

    key = json.loads((out1 / "key" / "key.json").read_text(encoding="utf-8"))
    check(sorted(v["anchored"] for v in key.values()) == ["False"] * 3 + ["True"] * 3, "3 per stratum")
    sheets = "".join((out1 / "sheets" / f"{b}.md").read_text(encoding="utf-8") for b in key)
    check(not any(v["packet_id"] in sheets for v in key.values()), "sheets carry no packet ID")
    check("2026-09-21" not in sheets, "sheets carry no timestamp")
    check("SIDECHAIN" not in sheets and "LATER TURN" not in sheets, "agent text limited to the turn, main thread")
    b = next(iter(key))
    n = int(key[b]["packet_id"][3:], 16)
    sheet = (out1 / "sheets" / f"{b}.md").read_text(encoding="utf-8")
    check(f"done with {n}" in sheet, "agent text of the turn shown")
    check(f"- `src/m{n}.py` (2)" in sheet, "extracted edit listed with its first call index")
    check("- `src/shared.py` (read, 1)" in sheet, "shell-extracted touch listed with role")
    check("`` cat src/shared.py `echo x` ``" in sheet, "tool input with backticks rendered as inline code")

    m3 = bs.make(eps, {"s": tdir}, 8, 3, tmp / "o3")
    check(m3["sheets_sha256"] != m1["sheets_sha256"] or m3["key_sha256"] != m1["key_sha256"],
          "a different seed draws a different sample")
    try:
        bs.make(eps, {"s": tdir}, 7, 8, tmp / "o4")
        check(False, "a too-small stratum stops the draw")
    except SystemExit:
        check(True, "a too-small stratum stops the draw")

    labels = json.loads((out1 / "labels.json").read_text(encoding="utf-8"))
    check(len(bs.validate(labels)) == 12, "an empty template does not validate")
    for lab in labels.values():
        lab.update(extractor="agree", outcome="neutral")
    first = next(iter(labels))
    labels[first].update(extractor="disagree")
    check(any("extractor_note" in e for e in bs.validate(labels)), "a disagree needs a note")
    labels[first]["extractor_note"] = "missed src/x.py"
    lp = tmp / "labels.json"
    lp.write_text(json.dumps(labels), encoding="utf-8")
    rec = bs.seal(lp, tmp / "seal.json")
    check(rec["labels_sha256"] == bs.sha256_bytes(lp.read_bytes()), "seal records the labels sha256")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        test(Path(d))
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

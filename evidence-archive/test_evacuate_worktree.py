#!/usr/bin/env python3
"""Tests for evacuate_worktree.py against a disposable git worktree.

No framework, plain checks (matching evidence-relevance/test_measure_relevance.py).
Each case builds a throwaway repo + `git worktree add` under a tempdir; nothing here
touches a real packet store, archive, or transcript.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evacuate_worktree as ev  # noqa: E402

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(cond: bool, name: str) -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED += 1
        FAILURES.append(name)
        print(f"  FAIL {name}")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=", *args],
                   cwd=cwd, check=True, capture_output=True)


def make_worktree(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    wt = tmp / "wt"
    git("worktree", "add", "-q", str(wt), "-b", "lane", cwd=repo)
    (wt / ev.PACKETS_REL).mkdir(parents=True)
    return wt


def packet(pid: str, extra: str = "") -> bytes:
    return json.dumps({"schema_version": 1, "packet_id": pid,
                       "created_at": "2026-09-25T00:00:00Z", "note": extra}).encode("utf-8")


def put(wt: Path, name: str, data: bytes) -> Path:
    p = wt / ev.PACKETS_REL / name
    p.write_bytes(data)
    return p


def tree_hash(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts}


def run_cli(*argv: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ev.main(list(argv))
    return code, out.getvalue() + err.getvalue()


PA, PB, PC = "ep_aaaaaaaaaaaaaaaa", "ep_bbbbbbbbbbbbbbbb", "ep_cccccccccccccccc"


def test_dry_run_writes_nothing() -> None:
    print("dry run")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"2026-09-25T00-00-00Z_{PA}.json", packet(PA))
        archive = tmp / "archive"
        code, out = run_cli("evacuate", str(wt), "--archive", str(archive))
        check(code == 0, "dry run exits 0 when nothing blocks")
        check(not archive.exists(), "dry run creates no archive, copy, or receipt")
        check("would_copy" in out and "NOT yet safe to remove" in out, "dry run never claims safe")
        check("safe_to_remove: true" not in out, "dry run prints no safe_to_remove")


def test_new_packets_copied_and_source_untouched() -> None:
    print("new packets")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"2026-09-25T00-00-00Z_{PA}.json", packet(PA))
        put(wt, f"2026-09-25T00-00-01Z_{PB}.json", packet(PB))
        before = tree_hash(wt)
        archive = tmp / "archive"
        r = ev.evacuate(wt, archive, apply=True)
        check(r.safe_to_remove, "all new packets archived -> safe_to_remove")
        check(r.counts() == {"copied": 2}, "two files copied")
        for pid in (PA, PB):
            src = next((wt / ev.PACKETS_REL).glob(f"*{pid}.json")).read_bytes()
            check((archive / "packets" / f"{pid}.json").read_bytes() == src, f"{pid} byte-exact")
        check(tree_hash(wt) == before, "source packets unchanged and not deleted")
        check(wt.is_dir() and (wt / ".git").exists(), "worktree still present")
        receipt = json.loads(Path(r.receipt).read_text(encoding="utf-8"))
        check(receipt["safe_to_remove"] is True and receipt["counts"] == {"copied": 2},
              "receipt records result and counts")


def test_duplicates() -> None:
    print("duplicates")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"2026-09-25T00-00-00Z_{PA}.json", packet(PA))
        put(wt, f"copy_{PA}.json", packet(PA))  # same id, same bytes, second file
        archive = tmp / "archive"
        r1 = ev.evacuate(wt, archive, apply=True)
        check(r1.counts() == {"copied": 1, "duplicate": 1}, "in-store duplicate copied once")
        check(r1.safe_to_remove, "identical duplicate does not block")
        check(len(list((archive / "packets").iterdir())) == 1, "one archived file per packet id")
        r2 = ev.evacuate(wt, archive, apply=True)
        check(r2.counts() == {"duplicate": 2} and r2.safe_to_remove,
              "re-run: everything already archived, still safe")
        check(r1.receipt != r2.receipt, "each run writes its own receipt")


def test_conflicting_ids_block() -> None:
    print("conflicts")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        archive = tmp / "archive"
        (archive / "packets").mkdir(parents=True)
        held = packet(PA, "archived earlier")
        (archive / "packets" / f"{PA}.json").write_bytes(held)
        put(wt, f"x_{PA}.json", packet(PA, "different content"))
        put(wt, f"x_{PB}.json", packet(PB, "one"))
        put(wt, f"y_{PB}.json", packet(PB, "two"))
        r = ev.evacuate(wt, archive, apply=True)
        check(not r.safe_to_remove, "id conflicts block safe_to_remove")
        check(r.counts().get("conflict") == 2, "archive conflict and in-store conflict both reported")
        check((archive / "packets" / f"{PA}.json").read_bytes() == held, "archived copy not overwritten")


def test_malformed_quarantined() -> None:
    print("malformed")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        bad = b'{"packet_id": "ep_dddddddddddddddd", "trunc'
        put(wt, "broken_ep_dddddddddddddddd.json", bad)
        put(wt, "noid.json", b'{"schema_version": 1}')
        put(wt, f"wrongname_{PB}.json", packet(PC))  # id not in filename
        put(wt, "notes.tmp", b"\xff\xfe binary")
        archive = tmp / "archive"
        r = ev.evacuate(wt, archive, apply=True)
        check(r.counts() == {"quarantined": 4}, "all four invalid files quarantined")
        check(r.safe_to_remove, "byte-exact quarantine preserves them, so not blocking")
        q = list((archive / "quarantine").rglob("*"))
        check(any(p.is_file() and p.read_bytes() == bad for p in q), "malformed file kept byte-exact")
        check(not (archive / "packets").exists(), "nothing invalid lands in packets/")


def test_verify_pass_and_tamper() -> None:
    print("verify")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"a_{PA}.json", packet(PA))
        put(wt, "broken.json", b"{")
        archive = tmp / "archive"
        r = ev.evacuate(wt, archive, apply=True)
        code, out = run_cli("verify", r.receipt)
        check(code == 0 and "checked 2 archived file(s)" in out, "verify pass re-hashes every copy")
        (archive / "packets" / f"{PA}.json").write_bytes(packet(PA, "tampered"))
        code, out = run_cli("verify", r.receipt)
        check(code == 1 and "hash mismatch" in out, "verify catches a changed archive copy")
        (archive / "packets" / f"{PA}.json").unlink()
        code, out = run_cli("verify", r.receipt)
        check(code == 1 and "missing" in out, "verify catches a missing archive copy")


def test_failure_paths_block() -> None:
    print("failure paths")
    real_write = ev._write_exclusive
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"a_{PA}.json", packet(PA))
        put(wt, f"b_{PB}.json", packet(PB))

        def fail_packets(target: Path, data: bytes) -> None:
            if target.parent.name == "packets":
                raise OSError("disk full (injected)")
            real_write(target, data)

        ev._write_exclusive = fail_packets
        try:
            code, out = run_cli("evacuate", str(wt), "--archive", str(tmp / "a1"), "--apply")
        finally:
            ev._write_exclusive = real_write
        check(code == 1 and "safe_to_remove: false" in out, "copy failure -> exit 1, not safe")
        rec = json.loads(next((tmp / "a1" / "receipts").glob("*.json")).read_text(encoding="utf-8"))
        check(rec["safe_to_remove"] is False and rec["counts"].get("error") == 2,
              "failure receipt records the errors")

        def corrupt(target: Path, data: bytes) -> None:
            real_write(target, data[:-1] if target.parent.name == "packets" else data)

        ev._write_exclusive = corrupt
        try:
            r = ev.evacuate(wt, tmp / "a2", apply=True)
        finally:
            ev._write_exclusive = real_write
        check(not r.safe_to_remove and all("does not match" in f.detail for f in r.files),
              "post-copy hash mismatch -> not safe")

        (wt / ev.PACKETS_REL / "nested").mkdir()
        r = ev.evacuate(wt, tmp / "a3", apply=True)
        check(not r.safe_to_remove and r.counts().get("unsupported") == 1,
              "unexpected subdirectory blocks safe_to_remove")


def test_refusals() -> None:
    print("refusals")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"a_{PA}.json", packet(PA))
        code, _ = run_cli("evacuate", str(wt), "--archive", str(wt / "archive"), "--apply")
        check(code == 2 and not (wt / "archive").exists(), "archive inside worktree refused")
        remote_repo = tmp / "remote-repo"
        remote_repo.mkdir()
        git("init", "-q", cwd=remote_repo)
        git("remote", "add", "origin", "https://example.invalid/x.git", cwd=remote_repo)
        code, _ = run_cli("evacuate", str(wt), "--archive", str(remote_repo / "arch"), "--apply")
        check(code == 2 and not (remote_repo / "arch").exists(), "archive in repo with a remote refused")
        local_repo = tmp / "local-repo"
        local_repo.mkdir()
        git("init", "-q", cwd=local_repo)
        code, _ = run_cli("evacuate", str(wt), "--archive", str(local_repo / "arch"), "--apply")
        check(code == 0, "archive in a local-only repo allowed")
        code, _ = run_cli("evacuate", str(tmp / "missing"), "--archive", str(tmp / "a"))
        check(code == 2, "missing worktree refused")


def test_no_store() -> None:
    print("no packet store")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        (wt / ev.PACKETS_REL).rmdir()
        r = ev.evacuate(wt, tmp / "archive", apply=True)
        check(r.safe_to_remove and not r.files, "no store -> nothing to lose, safe")


def test_scope_guards() -> None:
    print("scope guards")
    src = Path(ev.__file__).read_text(encoding="utf-8")
    for banned in ("unlink(", "rmtree", "os.remove", "worktree remove", "push", ".jsonl", "urlopen"):
        check(banned not in src, f"helper source never uses {banned!r}")


def main() -> int:
    for test in (test_dry_run_writes_nothing, test_new_packets_copied_and_source_untouched,
                 test_duplicates, test_conflicting_ids_block, test_malformed_quarantined,
                 test_verify_pass_and_tamper, test_failure_paths_block, test_refusals,
                 test_no_store, test_scope_guards):
        test()
    print(f"\n{PASSED} passed, {FAILED} failed")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

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
            if target.name.startswith("ep_"):  # staged packet copies
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
            real_write(target, data[:-1] if target.name.startswith("ep_") else data)

        ev._write_exclusive = corrupt
        try:
            r = ev.evacuate(wt, tmp / "a2", apply=True)
        finally:
            ev._write_exclusive = real_write
        check(not r.safe_to_remove and all("does not match" in f.detail for f in r.files),
              "post-copy hash mismatch -> not safe")
        check(not list((tmp / "a2" / "packets").glob("*.json")),
              "a copy that fails verification never appears under packets/")
        check(not list((tmp / "a2" / "staging").iterdir()), "failed staging copies are discarded")
        r = ev.evacuate(wt, tmp / "a2", apply=True)
        check(r.safe_to_remove and r.counts().get("copied") == 2,
              "the next run re-copies what the failed run could not verify")

        (wt / ev.PACKETS_REL / "nested").mkdir()
        r = ev.evacuate(wt, tmp / "a3", apply=True)
        check(not r.safe_to_remove and r.counts().get("unsupported") == 1,
              "unexpected subdirectory blocks safe_to_remove")


def test_interrupted_write_is_retried() -> None:
    print("interrupted write")
    real_write = ev._write_exclusive
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"a_{PA}.json", packet(PA))

        def killed(target: Path, data: bytes) -> None:
            real_write(target, data[: len(data) // 2])
            raise KeyboardInterrupt  # stands in for the process being killed mid-write

        ev._write_exclusive = killed
        try:
            ev.evacuate(wt, tmp / "arch", apply=True)
        except KeyboardInterrupt:
            pass
        finally:
            ev._write_exclusive = real_write
        check(not (tmp / "arch" / "packets" / f"{PA}.json").exists(),
              "a killed write leaves no archived path behind")
        code, out = run_cli("evacuate", str(wt), "--archive", str(tmp / "arch"), "--apply")
        check(code == 0 and "safe_to_remove: true" in out, "the next run copies it and is safe")
        check(len(list((tmp / "arch" / "staging").glob("ep_*.part"))) == 1,
              "the killed run's partial copy stays inert under staging/")


def test_concurrent_publish_is_a_duplicate() -> None:
    print("concurrent publish")
    real_write = ev._write_exclusive
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"a_{PA}.json", packet(PA))
        archive = tmp / "arch"

        def racing(target: Path, data: bytes) -> None:
            real_write(target, data)
            if target.name.startswith("ep_"):  # another run wins the packet publish race
                real_write(archive / "packets" / f"{PA}.json", data)

        ev._write_exclusive = racing
        try:
            r = ev.evacuate(wt, archive, apply=True)
        finally:
            ev._write_exclusive = real_write
        check(r.safe_to_remove and r.files[0].action == "duplicate",
              "losing the publish race to an identical copy is a verified duplicate")
        check(not list((archive / "staging").iterdir()), "the losing staging copy is discarded")


def test_failed_write_leaves_no_part() -> None:
    print("failed write")
    real_write = ev._write_exclusive
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"a_{PA}.json", packet(PA))
        archive = tmp / "arch"

        def disk_full(target: Path, data: bytes) -> None:
            if target.name.startswith("ep_"):
                real_write(target, data[: len(data) // 2])
                raise OSError(28, "No space left on device")
            real_write(target, data)

        ev._write_exclusive = disk_full
        try:
            r = ev.evacuate(wt, archive, apply=True)
        finally:
            ev._write_exclusive = real_write
        check(not r.safe_to_remove and r.files[0].action == "error"
              and "copy failed" in r.files[0].detail, "a failed write is a blocking error")
        check(not list((archive / "staging").iterdir()), "a failed write leaves no partial copy in staging/")
        check(not (archive / "packets" / f"{PA}.json").exists(), "a failed write publishes nothing")
        check(r.files[0].staging_left is None, "a failed write reports no leftover staged copy")


def _publish_with(archive: Path, wt: Path, *, reread_fails: bool, run=None):
    """Run the real evacuate() (or `run`, e.g. the CLI) with the post-publish staging
    unlink failing, and optionally the post-publish source re-read failing."""
    real_discard, real_read = ev._discard_staged, Path.read_bytes
    src = (wt / ev.PACKETS_REL / f"a_{PA}.json").resolve()
    reads = {"n": 0}

    def locked(part: Path, staging: Path) -> None:
        raise PermissionError(13, "file in use", str(part))

    def flaky_read(self: Path) -> bytes:
        if self.resolve() == src:
            reads["n"] += 1
            if reads["n"] >= 2:
                raise PermissionError(13, "file in use", str(self))
        return real_read(self)

    ev._discard_staged = locked
    if reread_fails:
        Path.read_bytes = flaky_read  # type: ignore[method-assign]
    try:
        return run() if run else ev.evacuate(wt, archive, apply=True)
    finally:
        ev._discard_staged = real_discard
        Path.read_bytes = real_read  # type: ignore[method-assign]


def test_cleanup_failure_after_publish_is_verified_copy() -> None:
    print("cleanup failure after publish")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"a_{PA}.json", packet(PA))
        archive = tmp / "arch"
        r = _publish_with(archive, wt, reread_fails=False)
        f = r.files[0]
        check(f.action == "copied" and f.verified and r.safe_to_remove,
              "a published, verified copy is a success despite the cleanup failure")
        check((archive / "packets" / f"{PA}.json").read_bytes() == packet(PA),
              "the published packet is preserved byte-exact")
        left = list((archive / "staging").glob("ep_*.part"))
        check(len(left) == 1 and f.staging_left == left[0].as_posix()
              and "staged copy left in staging/: PermissionError" in f.detail,
              "the leftover staged copy is named on the file result")
        check(any("1 staged copy(ies) could not be removed" in n for n in r.reasons),
              "the report counts the cleanup failure separately")
        receipt = json.loads(Path(r.receipt).read_text(encoding="utf-8"))
        check(receipt["files"][0]["staging_left"] == f.staging_left,
              "the receipt records the leftover staged copy")
        code, out = run_cli("evacuate", str(wt), "--archive", str(archive), "--apply")
        check(code == 0 and "duplicate" in out and "safe_to_remove: true" in out,
              "a retry sees the archived copy as a verified duplicate")


def test_unverified_publish_is_incomplete() -> None:
    print("publish without source re-check")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"a_{PA}.json", packet(PA))
        archive = tmp / "arch"
        r = _publish_with(archive, wt, reread_fails=True)
        f = r.files[0]
        check(f.action == "incomplete" and not f.verified and not r.safe_to_remove,
              "a publish whose source re-check did not finish is incomplete and blocking")
        check("copy failed" not in f.detail and "could not be re-read to verify" in f.detail
              and "staged copy left in staging/" in f.detail,
              "the detail names both the unfinished check and the cleanup failure")
        check(f.archive_path == (archive / "packets" / f"{PA}.json").resolve().as_posix()
              and Path(f.archive_path).read_bytes() == packet(PA),
              "the result names the published target, which is preserved")
        code, out = run_cli("evacuate", str(wt), "--archive", str(archive), "--apply")
        check(code == 0 and "duplicate" in out and "safe_to_remove: true" in out,
              "a retry verifies the published copy")
        cli_archive = tmp / "arch-cli"
        code, out = _publish_with(cli_archive, wt, reread_fails=True, run=lambda: run_cli(
            "evacuate", str(wt), "--archive", str(cli_archive), "--apply"))
        check(code == 1 and "incomplete" in out and "safe_to_remove: false" in out
              and "copy failed" not in out and "staged copy left" in out,
              "the CLI exits 1 and prints the incomplete result and the leftover staged copy")


def test_mismatch_with_cleanup_failure_keeps_cause() -> None:
    print("staged hash mismatch with cleanup failure")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt = make_worktree(tmp)
        put(wt, f"a_{PA}.json", packet(PA))
        archive = tmp / "arch"
        real_write, real_discard = ev._write_exclusive, ev._discard_staged

        def corrupt(target: Path, data: bytes) -> None:
            real_write(target, data[:-1] + b"X")

        def locked(part: Path, staging: Path) -> None:
            raise PermissionError(13, "file in use", str(part))

        ev._write_exclusive, ev._discard_staged = corrupt, locked
        try:
            r = ev.evacuate(wt, archive, apply=True)
        finally:
            ev._write_exclusive, ev._discard_staged = real_write, real_discard
        f = r.files[0]
        check(f.action == "error" and not r.safe_to_remove
              and "archived copy does not match source hash" in f.detail
              and "copy failed" not in f.detail, "a staged mismatch keeps its cause")
        check(f.detail.count("staged copy left") == 1 and f.staging_left is not None,
              "the leftover staged copy is noted once")
        check(not (archive / "packets" / f"{PA}.json").exists(), "a staged mismatch publishes nothing")


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
    # The one permitted deletion: _discard_staged, which refuses anything outside staging/.
    start = src.index("def _discard_staged(")
    end = src.index("\ndef ", start + 1)
    discard, src = src[start:end], src[:start] + src[end:]
    check("part.parent != staging" in discard and discard.count("os.unlink(") == 1,
          "the only deletion is a guarded staging discard")
    for banned in ("unlink(", "rmtree", "os.remove", "worktree remove", "push", ".jsonl", "urlopen"):
        check(banned not in src, f"helper source never uses {banned!r}")


def main() -> int:
    for test in (test_dry_run_writes_nothing, test_new_packets_copied_and_source_untouched,
                 test_duplicates, test_conflicting_ids_block, test_malformed_quarantined,
                 test_verify_pass_and_tamper, test_failure_paths_block, test_interrupted_write_is_retried,
                 test_concurrent_publish_is_a_duplicate, test_failed_write_leaves_no_part,
                 test_cleanup_failure_after_publish_is_verified_copy,
                 test_unverified_publish_is_incomplete,
                 test_mismatch_with_cleanup_failure_keeps_cause,
                 test_refusals,
                 test_no_store, test_scope_guards):
        test()
    print(f"\n{PASSED} passed, {FAILED} failed")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

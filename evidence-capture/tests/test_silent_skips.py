#!/usr/bin/env python3
"""Failures that used to vanish without a record (slop audits 2026-11-11 / 2026-11-16):
SA11-CT-16 hook import failures, SA11-CT-18 unreadable packets, SA16-CT-11 process-tree
kill failures, SA16-CT-13 an unevaluable cloud-attribute guard.

Run: python evidence-capture/tests/test_silent_skips.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))

PASSED: list[str] = []
FAILED: list[str] = []


def _run_hook_with_broken_pipeline(root: Path, script: str, tag: str) -> None:
    """The hook copied next to a ``pipeline`` that cannot be imported and no ``store``:
    exit 0, nothing printed, and one line naming the failure in the temp fallback log."""
    hook = root / "hook"
    tmp = root / "tmp"
    hook.mkdir()
    tmp.mkdir()
    shutil.copy2(PKG / script, hook / script)
    (hook / "pipeline.py").write_text('raise ImportError("broken on purpose")\n', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp))
    proc = subprocess.run([sys.executable, str(hook / script)], input=b"{}", capture_output=True,
                          cwd=str(root), env=env, timeout=60)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout == b"", proc.stdout
    log = tmp / "ec-capture-failures.log"
    assert log.is_file(), "the import failure left no log line"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    assert f"{tag}: import failed: ImportError" in lines[0] and "broken on purpose" in lines[0], lines


def test_start_hook_import_failure_is_logged(root: Path) -> None:
    """SA11-CT-16: capture_start fails open on an import failure and logs one line."""
    _run_hook_with_broken_pipeline(root, "capture_start.py", "start")


def test_end_hook_import_failure_is_logged(root: Path) -> None:
    """SA11-CT-16: capture_end fails open on an import failure and logs one line."""
    _run_hook_with_broken_pipeline(root, "capture_end.py", "end")


SESSION = "11111111-2222-3333-4444-555555555555"
HEAD = "a" * 40


def _packet_dir_with_one_unreadable(root: Path) -> tuple[Path, Path]:
    """Two good packets (one this manifest's, one another prompt's) and one half-written."""
    import common

    pdir = root / "repo" / ".evidence-compiler" / "packets"
    pdir.mkdir(parents=True)
    for pid, prompt in (("ep_0001", "p"), ("ep_0002", "q")):
        (pdir / f"20260926_{pid}.json").write_text(json.dumps({
            "packet_id": pid, "created_at": "2026-09-26T10:00:01Z",
            "identity": {"session_id": SESSION, "head": HEAD, "head_state": None},
            "correlation": {"prompt_hash": common.sha256_text(prompt)}}), encoding="utf-8")
    bad = pdir / "20260926_ep_0003.json"
    bad.write_text('{"packet_id": "ep_0003", "created_at": "2026-09-2', encoding="utf-8")  # half-written
    return root / "repo", bad


def _manifest(prompt: str) -> dict:
    import common
    import manifest as mf

    sha = common.sha256_text(prompt)
    started = "2026-09-26T10:00:00.000000Z"
    m = mf.new_manifest(episode_id=common.episode_id(SESSION, sha, started), session_id=SESSION,
                        started_at=started, prompt_sha256=sha, prompt_len=len(prompt),
                        tool_commit=None, tool_sha256=None, cwd_sha256="0" * 64)
    m["ended_at"] = "2026-09-26T10:01:00Z"
    m["repo"]["head"] = HEAD
    return m


def _stores(root: Path):
    import store

    st = store.Stores(meta=root / "meta", content=root / "content")
    (st.meta / "manifests").mkdir(parents=True)
    return st


def test_unreadable_packet_is_counted_by_load_and_link(root: Path) -> None:
    """SA11-CT-18: load_packets keeps the two good packets and names the unreadable one;
    resolve_link records the count, and a link it could not make says why it may be wrong."""
    import linkage

    repo, bad = _packet_dir_with_one_unreadable(root)
    st = _stores(root)
    pk = linkage.load_packets(linkage.packet_dirs(repo, st))
    assert sorted(p["packet_id"] for p in pk) == ["ep_0001", "ep_0002"], pk
    assert list(pk.unreadable) == [str(bad)], pk.unreadable
    link = linkage.resolve_link(_manifest("p"), pk)
    assert (link["state"], link["packet_id"]) == ("linked", "ep_0001"), link
    assert link["packets_unreadable"] == 1, link
    none = linkage.resolve_link(_manifest("no such prompt"), pk)
    assert none["state"] == "none" and none["packets_unreadable"] == 1, none
    assert "1 packet file(s) unreadable" in none["reason"], none["reason"]
    clean = linkage.resolve_link(_manifest("p"), [p for p in pk])
    assert clean["packets_unreadable"] == 0, clean


def test_reconcile_counts_and_logs_unreadable_packets(root: Path) -> None:
    """SA11-CT-18: reconcile counts the unreadable packet files it saw and logs each one."""
    import linkage

    repo, bad = _packet_dir_with_one_unreadable(root)
    st = _stores(root)
    m = _manifest("p")
    counts = linkage.reconcile(st, [m], {m["episode_id"]: repo})
    assert counts["linked"] == 1, counts
    assert counts["packets_unreadable"] == 1, counts
    log = (st.meta / "capture.log").read_text(encoding="utf-8")
    assert f"packet unreadable: {bad.name}" in log, log


class _StubbornProc:
    """A process that will not die: kill raises, wait times out, poll says still running."""

    pid = 4194300  # no such process: a real tree kill of it fails

    def poll(self) -> None:
        return None

    def kill(self) -> None:
        raise PermissionError("access denied")

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("stub", timeout or 0)


class _Patched:
    """Set attributes for the duration of a ``with`` block."""

    def __init__(self, obj: object, **attrs: object) -> None:
        self.obj, self.attrs, self.saved = obj, attrs, {}

    def __enter__(self) -> None:
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.obj, k)
            setattr(self.obj, k, v)

    def __exit__(self, *_: object) -> None:
        for k, v in self.saved.items():
            setattr(self.obj, k, v)


def _raise(exc: BaseException):
    def f(*_a: object, **_k: object) -> None:
        raise exc
    return f


def test_process_tree_records_kill_failures(root: Path) -> None:
    """SA16-CT-11: job-object, tree-kill, kill and wait failures are recorded; kill never raises."""
    import rescreen

    if os.name == "nt":
        with _Patched(rescreen, _win_job_for=_raise(RuntimeError("no job for you")), KILL_WAIT_S=0.1):
            tree = rescreen._ProcessTree(_StubbornProc())  # type: ignore[arg-type]
            tree.kill()  # taskkill of a pid that does not exist, then kill/wait fail
        joined = " | ".join(tree.failures)
        assert "job object" in joined and "no job for you" in joined, tree.failures
        assert "taskkill" in joined, tree.failures
        with _Patched(rescreen, _win_job_for=lambda _p: 1,
                      _win_terminate_job=_raise(OSError("terminate refused"))):
            tree = rescreen._ProcessTree(_StubbornProc())  # type: ignore[arg-type]
            tree.kill()
        joined = " | ".join(tree.failures)
        assert "terminate refused" in joined, tree.failures
    else:
        with _Patched(os, killpg=_raise(PermissionError("group kill refused"))):
            tree = rescreen._ProcessTree(_StubbornProc())  # type: ignore[arg-type]
            tree.kill()
        joined = " | ".join(tree.failures)
        assert "group kill refused" in joined, tree.failures
    assert "access denied" in joined, tree.failures
    assert "did not exit" in joined, tree.failures


def test_run_child_surfaces_process_tree_failures(root: Path) -> None:
    """SA16-CT-11: a tree-kill failure reaches run_child's causes (so rescreen's
    child_failures); the child's own outcome is kept."""
    import rescreen

    class FailingTree(rescreen._ProcessTree):
        def kill(self) -> None:
            super().kill()
            self.failures.append("tree kill: OSError: simulated")

    st = _stores(root)
    reply = 'import json,sys; sys.stdin.read(); print(json.dumps({"outcome": "rescreened", "manifest": {}}))'
    causes: list[str] = []
    with _Patched(rescreen, _ProcessTree=FailingTree):
        outcome, done = rescreen.run_child(st, {"episode_id": "e"}, dict(os.environ), 60,
                                           (sys.executable, "-c", reply), causes=causes)
    assert (outcome, done) == ("rescreened", {}), (outcome, done)
    assert causes == ["process tree: tree kill: OSError: simulated"], causes


def _content_store(root: Path):
    import store

    st = store.Stores(meta=root / "meta", content=root / "content")
    store.init_stores(st)
    return st


def _run_stub(real, prog: str, fake):
    """``subprocess.run`` with ``fake(argv)`` standing in for program ``prog`` only."""
    def run(argv, *a, **k):
        if argv and argv[0] == prog:
            return fake(argv)
        return real(argv, *a, **k)
    return run


def test_unevaluable_cloud_attribute_guard_is_a_problem(root: Path) -> None:
    """SA16-CT-13 / SA11-CT-25: attrib raising, timing out, printing nothing or printing
    something unparseable is a content-store problem, never a silent pass."""
    import store

    st = _content_store(root)
    real = subprocess.run
    if os.name != "nt":
        assert not any("cloud-attribute" in p for p in store.check_content_store(st))
        return  # the guard does not apply off Windows
    assert not any("cloud-attribute" in p for p in store.check_content_store(st)), store.check_content_store(st)

    def timeout(argv):
        raise subprocess.TimeoutExpired(argv, 5)

    def oserror(argv):
        raise OSError("attrib missing")

    def silent(argv):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def nonsense(argv):
        return subprocess.CompletedProcess(argv, 0, stdout="Parameter format not correct -\n", stderr="")

    for fake, why in ((timeout, "timed out"), (oserror, "attrib missing"), (silent, "printed nothing"),
                      (nonsense, "not understood")):
        with _Patched(store.subprocess, run=_run_stub(real, "attrib", fake)):
            problems = store.check_content_store(st)
        hits = [p for p in problems if "cloud-attribute guard could not be evaluated" in p]
        assert len(hits) == 1 and why in hits[0], (fake.__name__, problems)


def _git_stub(real, sub: str, fake):
    """``subprocess.run`` with ``fake(argv)`` standing in for ``git ... <sub> ...`` only."""
    def run(argv, *a, **k):
        if argv and argv[0] == "git" and sub in argv:
            return fake(argv)
        return real(argv, *a, **k)
    return run


def test_git_failure_is_not_read_as_not_a_repository(root: Path) -> None:
    """SA11-CT-24: a git that fails (cannot start, refuses the directory) is "could not
    tell", a problem for both stores; only git's own not-a-repository answer passes."""
    import store

    st = _content_store(root)
    real = subprocess.run
    tell = "could not tell whether"
    assert not any(tell in p for p in store.check_content_store(st) + store.check_meta_store(st))

    def oserror(argv):
        raise OSError("git missing")

    def dubious(argv):
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: detected dubious ownership\n")

    for fake, why in ((oserror, "git missing"), (dubious, "dubious ownership")):
        with _Patched(store.subprocess, run=_git_stub(real, "--git-dir", fake)):
            content, meta = store.check_content_store(st), store.check_meta_store(st)
        c = [p for p in content if tell in p]
        m = [p for p in meta if tell in p]
        assert len(c) == 1 and why in c[0] and "content store" in c[0], content
        assert len(m) == 1 and why in m[0] and "metadata store" in m[0], meta
        assert "metadata store is not a git repository" not in meta, meta
        assert "content store is inside a git repository" not in content, content


def test_failed_remote_listing_is_not_no_remote(root: Path) -> None:
    """SA11-CT-26: a ``git remote`` that fails is a metadata-store problem, not "no remote"."""
    import store

    st = _content_store(root)
    subprocess.run(["git", "init", "-q", str(st.meta)], check=True, capture_output=True)
    real = subprocess.run
    listed = "remotes could not be listed"
    assert not any(listed in p for p in store.check_meta_store(st)), store.check_meta_store(st)

    def failing(argv):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fatal: bad config line 3")

    def oserror(argv):
        raise OSError("git vanished")

    for fake, why in ((failing, "bad config line 3"), (oserror, "git vanished")):
        with _Patched(store.subprocess, run=_git_stub(real, "remote", fake)):
            meta = store.check_meta_store(st)
        hits = [p for p in meta if listed in p]
        assert len(hits) == 1 and why in hits[0], meta


def test_corrupt_store_config_is_logged_not_unconfigured(root: Path) -> None:
    """SA11-CT-27: a corrupt or non-object evidence-capture.json still resolves to no stores
    (paths cannot be guessed) but leaves one line naming the file, instead of reading as
    an unconfigured machine."""
    import store

    tmp = root / "tmp"
    tmp.mkdir()
    saved = {k: os.environ.get(k) for k in ("TEMP", "TMP", "TMPDIR")}
    os.environ.update(TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp))
    try:
        for name, body in (("corrupt.json", '{"meta": "x", '), ("list.json", "[1, 2]")):
            cfg = root / name
            cfg.write_text(body, encoding="utf-8")
            assert store.resolve_stores({}, cfg) is None
        assert store.resolve_stores({}, root / "absent.json") is None  # truly unconfigured: no line
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    log = tmp / "ec-capture-failures.log"
    lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    assert len(lines) == 2, lines
    assert "store config unreadable" in lines[0] and "corrupt.json" in lines[0], lines
    assert "store config unreadable" in lines[1] and "list.json" in lines[1] and "not an object" in lines[1], lines


def test_tmp_orphan_stat_and_remove_failures_are_logged(root: Path) -> None:
    """SA11-CT-28: an orphan whose age cannot be read, or that rmtree could not remove, is
    one line in the capture log rather than skipped without a trace."""
    import pathlib

    import store

    st = _content_store(root)
    old = 1_000_000_000
    for name in ("ep_stuck.1", "ep_statfail.2"):
        (st.tmp / name).mkdir(parents=True)
        os.utime(st.tmp / name, (old, old))
    real_stat = pathlib.Path.stat

    def stat(self, *a, **k):
        if "statfail" in self.name:
            raise PermissionError("stat refused")
        return real_stat(self, *a, **k)

    with _Patched(pathlib.Path, stat=stat), _Patched(store, rmtree=lambda _p, *a, **k: False):
        gone = store.clean_tmp_orphans(st)
    assert gone == [], gone
    log = (st.meta / "capture.log").read_text(encoding="utf-8")
    assert "tmp orphan age unreadable: ep_statfail.2" in log and "stat refused" in log, log
    assert "tmp orphan could not be removed: ep_stuck.1" in log, log


TESTS = [
    test_start_hook_import_failure_is_logged,
    test_end_hook_import_failure_is_logged,
    test_unreadable_packet_is_counted_by_load_and_link,
    test_reconcile_counts_and_logs_unreadable_packets,
    test_process_tree_records_kill_failures,
    test_run_child_surfaces_process_tree_failures,
    test_unevaluable_cloud_attribute_guard_is_a_problem,
    test_git_failure_is_not_read_as_not_a_repository,
    test_failed_remote_listing_is_not_no_remote,
    test_corrupt_store_config_is_logged_not_unconfigured,
    test_tmp_orphan_stat_and_remove_failures_are_logged,
]


def main() -> int:
    only = sys.argv[1:]
    with tempfile.TemporaryDirectory(prefix="ec-silent-test-") as tmp:
        for fn in TESTS:
            if only and fn.__name__ not in only:
                continue
            case = Path(tmp) / fn.__name__
            case.mkdir()
            try:
                fn(case)
                PASSED.append(fn.__name__)
            except Exception:  # noqa: BLE001 — plain-check runner
                FAILED.append(fn.__name__)
                print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    for name in PASSED:
        print(f"ok   {name}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

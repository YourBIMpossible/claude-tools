#!/usr/bin/env python3
"""Phase D3 tests: end state, self-contained store, raw bytes, exclusions, attribution,
dependency fingerprints, outcome checks and the admission gate.

Plain checks on synthetic temporary repositories only: no model, no network, no real
repository, no real store.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import causal  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t",
       "GIT_CONFIG_NOSYSTEM": "1"}
TOOLS = {"python": [sys.executable, "--version"]}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          env=ENV).stdout.decode()


def make_repo(root: Path, files: dict[str, bytes] | None = None) -> Path:
    repo = root / "src_repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "core.autocrlf", "false")
    for rel, data in (files or {"a.txt": b"one\r\ntwo\n", "pkg/requirements.txt": b"x==1\n"}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def raises(fn, text: str) -> None:
    try:
        fn()
    except causal.CausalError as exc:
        assert text in str(exc), exc
        return
    raise AssertionError(f"expected CausalError({text})")


# ------------------------------------------------------------------ states


def test_state_roundtrip_raw_bytes_and_untracked(tmp: Path) -> None:
    repo = make_repo(tmp)
    (repo / "new.bin").write_bytes(bytes(range(256)))
    (repo / ".gitignore").write_bytes(b"ignored.log\n")
    (repo / "ignored.log").write_bytes(b"secret")
    rec = causal.capture_state(repo, tmp / "start", label="start")
    assert "new.bin" in rec["files"] and "ignored.log" not in rec["files"]
    assert causal.verify_state(tmp / "start") == []
    out = tmp / "out"
    causal.materialize(tmp / "start", out)
    assert (out / "a.txt").read_bytes() == b"one\r\ntwo\n"
    assert (out / "new.bin").read_bytes() == bytes(range(256))


def test_state_immutable_and_never_overwritten(tmp: Path) -> None:
    repo = make_repo(tmp)
    causal.capture_state(repo, tmp / "s", label="start")
    obj = next((tmp / "s" / "objects").iterdir())
    assert not os.stat(obj).st_mode & stat.S_IWRITE
    raises(lambda: causal.capture_state(repo, tmp / "s", label="start"), "never overwritten")


def test_state_tamper_detected(tmp: Path) -> None:
    repo = make_repo(tmp)
    causal.capture_state(repo, tmp / "s", label="end")
    obj = next((tmp / "s" / "objects").iterdir())
    os.chmod(obj, stat.S_IWRITE | stat.S_IREAD)
    obj.write_bytes(b"tampered")
    assert causal.verify_state(tmp / "s")
    raises(lambda: causal.materialize(tmp / "s", tmp / "o"), "not intact")


def test_concurrent_change_fails_capture(tmp: Path) -> None:
    repo = make_repo(tmp)
    real = causal._scan
    calls = {"n": 0}

    def flaky(top: Path):
        calls["n"] += 1
        if calls["n"] == 2:
            (top / "a.txt").write_bytes(b"changed mid-capture")
        return real(top)

    causal._scan = flaky
    try:
        raises(lambda: causal.capture_state(repo, tmp / "s", label="end"), "concurrent_change")
    finally:
        causal._scan = real
    assert not (tmp / "s").exists()


def test_capture_leaves_source_untouched(tmp: Path) -> None:
    repo = make_repo(tmp)
    (repo / "dirty.txt").write_bytes(b"d")
    index = (repo / ".git" / "index").read_bytes()
    status = git(repo, "status", "--porcelain")
    causal.capture_state(repo, tmp / "s", label="start")
    causal.unsupported_state(repo)
    assert (repo / ".git" / "index").read_bytes() == index
    assert git(repo, "status", "--porcelain") == status


# ------------------------------------------------------------------ history


def test_history_self_contained_without_source(tmp: Path) -> None:
    repo = make_repo(tmp)
    (repo / "b.txt").write_bytes(b"b")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-q", "-m", "two")
    head = git(repo, "rev-parse", "HEAD").strip()
    info = causal.freeze_history(repo, tmp / "h")
    assert info["head"] == head
    shutil.rmtree(repo, onerror=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))
    assert causal.verify_history(tmp / "h") == []
    bare = tmp / "replay.git"
    assert causal.restore_history(tmp / "h", bare) == head
    assert not (bare / "objects" / "info" / "alternates").exists()
    assert git(bare, "log", "--format=%s", "replay").split() == ["two", "init"]


def test_raw_bytes_survive_hostile_attributes(tmp: Path) -> None:
    repo = make_repo(tmp, {".gitattributes": b"*.txt text eol=crlf\n*.dat filter=up\n",
                           "a.txt": b"lf\nonly\n", "b.dat": b"lower\n"})
    git(repo, "config", "filter.up.smudge", "tr a-z A-Z")
    git(repo, "config", "filter.up.clean", "cat")
    causal.freeze_history(repo, tmp / "h")
    bare = tmp / "r.git"
    causal.restore_history(tmp / "h", bare)
    wt = tmp / "wt"
    subprocess.run(["git", "clone", "-q", "--no-local", "-b", "replay", str(bare), str(wt)], check=True, env=ENV,
                   capture_output=True)
    os.remove(wt / "a.txt")
    os.remove(wt / "b.dat")
    causal.neutralize_attributes(wt / ".git")
    subprocess.run(["git", *causal.RAW_GIT_ARGS, "-c", "filter.up.smudge=tr a-z A-Z", "-C", str(wt),
                    "checkout", "-q", "--", "."], check=True, env=ENV, capture_output=True)
    assert (wt / "a.txt").read_bytes() == b"lf\nonly\n"
    assert (wt / "b.dat").read_bytes() == b"lower\n"


# ------------------------------------------------------------------ exclusions


def test_supported_repo_has_no_reasons(tmp: Path) -> None:
    assert causal.unsupported_state(make_repo(tmp)) == []


def test_unsupported_states_detected(tmp: Path) -> None:
    repo = make_repo(tmp)
    (repo / ".gitattributes").write_bytes(b"*.psd filter=lfs diff=lfs merge=lfs -text\n")
    git(repo, "update-index", "--add", "--cacheinfo",
        f"160000,{git(repo, 'rev-parse', 'HEAD').strip()},vendor/sub")
    (repo / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))
    git(repo, "config", "core.sparseCheckout", "true")
    (repo / ".git" / "shallow").write_text("")
    got = set(causal.unsupported_state(repo))
    assert got == {"submodule", "lfs", "operation_in_progress", "sparse_checkout", "shallow_history"}, got


def test_lfs_pointer_file_detected(tmp: Path) -> None:
    repo = make_repo(tmp, {"art.psd": causal.LFS_POINTER + b"\noid sha256:00\nsize 1\n"})
    assert causal.unsupported_state(repo) == ["lfs"]


# ------------------------------------------------------------------ attribution


def test_attribution_isolated_and_concurrent_writer(tmp: Path) -> None:
    repo = make_repo(tmp)
    start = causal.capture_state(repo, tmp / "s", label="start")
    (repo / "a.txt").write_bytes(b"agent edit")
    (repo / "pkg" / "new.py").write_bytes(b"x = 1\n")
    end = causal.capture_state(repo, tmp / "e", label="end")
    top = str(repo)
    ok = causal.attribute(start, end, [f"{top}\\a.txt", "./pkg/new.py"], top=top)
    assert ok["isolated"] and ok["changed"] == ["a.txt", "pkg/new.py"]
    bad = causal.attribute(start, end, ["a.txt"], top=top)
    assert bad["unattributed"] == ["pkg/new.py"] and not bad["isolated"]


def test_other_worktree_movement_detected(tmp: Path) -> None:
    repo = make_repo(tmp)
    other = tmp / "other_wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(other))
    before = causal.worktree_fingerprints(repo)
    assert list(before) and all(v != "missing" for v in before.values())
    (other / "a.txt").write_bytes(b"someone else")
    after = causal.worktree_fingerprints(repo)
    s = causal.capture_state(repo, tmp / "s", label="start")
    res = causal.attribute(s, s, [], worktrees_start=before, worktrees_end=after)
    assert res["other_worktrees_moved"] and not res["isolated"]
    assert causal.attribute(s, s, [], worktrees_start=before, worktrees_end=before)["isolated"]


# ------------------------------------------------------------------ dependencies


def test_dependency_fingerprint_and_drift(tmp: Path) -> None:
    repo = make_repo(tmp)
    s = causal.capture_state(repo, tmp / "s", label="start")
    fs = causal.dependency_fingerprint(s, TOOLS)
    assert list(fs["lockfiles"]) == ["pkg/requirements.txt"] and fs["tools"]["python"].startswith("Python")
    assert causal.dependency_fingerprint(s, {"nope": ["definitely-not-a-tool-xyz"]})["tools"]["nope"] == "absent"
    (repo / "pkg" / "requirements.txt").write_bytes(b"x==2\n")
    e = causal.capture_state(repo, tmp / "e", label="end")
    fe = causal.dependency_fingerprint(e, TOOLS)
    assert causal.dependency_drift(fs, fe, []) == ["lockfile:pkg/requirements.txt"]
    assert causal.dependency_drift(fs, fe, ["pkg/requirements.txt"]) == []
    fe["tools"]["python"] = "Python 0.0"
    assert causal.dependency_drift(fs, fe, ["pkg/requirements.txt"]) == ["tool:python"]


# ------------------------------------------------------------------ outcome checks


def _registry(tmp: Path, body: object) -> Path:
    p = tmp / "registry.json"
    p.write_text(json.dumps(body))
    return p


def test_registry_validation(tmp: Path) -> None:
    raises(lambda: causal.load_registry(_registry(tmp, {"x": 1})), "'tasks'")
    raises(lambda: causal.load_registry(_registry(tmp, {"tasks": {"t": {"checks": []}}})), "no checks")
    raises(lambda: causal.load_registry(_registry(tmp, {"tasks": {"t": {"checks": [{"kind": "llm"}]}}})),
           "unknown check kind")
    raises(lambda: causal.load_registry(_registry(tmp, {"tasks": {"t": {"checks": [{"kind": "command"}]}}})),
           "argv")


def test_checks_run_on_materialized_end_state_only(tmp: Path) -> None:
    repo = make_repo(tmp)
    (repo / "a.txt").write_bytes(b"fixed: yes\n")
    causal.capture_state(repo, tmp / "e", label="end")
    (repo / "a.txt").write_bytes(b"live checkout changed later\n")
    out = tmp / "m"
    causal.materialize(tmp / "e", out)
    reg = causal.load_registry(_registry(tmp, {"tasks": {"t1": {"checks": [
        {"kind": "file_contains", "path": "a.txt", "text": "fixed: yes"},
        {"kind": "file_sha256", "path": "a.txt", "sha256": causal.sha256_bytes(b"fixed: yes\n")},
        {"kind": "file_absent", "path": "gone.txt"},
        {"kind": "command", "argv": [sys.executable, "-c", "import sys; sys.exit(open('a.txt').read()!='fixed: yes\\n')"]},
        {"kind": "file_contains", "path": "../escape.txt", "text": "x"},
    ]}}}))
    res = causal.run_checks(reg["t1"], out)
    assert [r["passed"] for r in res] == [True, True, True, True, None], res
    assert res[4]["error"] == "CausalError"


# ------------------------------------------------------------------ admission


def _good_record() -> dict:
    return {"identity_accepted": True, "start_problems": [], "end_problems": [], "history_problems": [],
            "start_fidelity": [], "end_fidelity": [], "unsupported": [],
            "attribution": {"unattributed": [], "other_worktrees_moved": []},
            "worktrees": {"contaminating": [], "unknown": [], "shared_state_changed": False},
            "dependency_drift": [], "registration_problems": [], "checks": [{"kind": "file_absent", "path": "x"}],
            "check_results": [{"passed": True}], "check_results_start": [{"passed": False}]}


def test_admission_gate(tmp: Path) -> None:
    assert causal.admit(_good_record()) == {"eligible": True, "reasons": []}
    wt = {"contaminating": [], "unknown": [], "shared_state_changed": False}
    cases = {
        "identity_not_accepted": {"identity_accepted": False},
        "end_missing": {"end_problems": None},
        "history_unverified": {"history_problems": ["corrupt"]},
        "unsupported:lfs": {"unsupported": ["lfs"]},
        "start_fidelity_not_proven": {"start_fidelity": None},
        "end_fidelity_bytes_differ:2": {"end_fidelity": ["fidelity_bytes_differ:2"]},
        "unattributed_change": {"attribution": {"unattributed": ["x"], "other_worktrees_moved": []}},
        "worktree_classification_missing": {"worktrees": None},
        "shared_git_state_changed": {"worktrees": {**wt, "shared_state_changed": True}},
        "other_worktree_contaminating": {"worktrees": {**wt, "contaminating": ["w"]}},
        "other_worktree_unknown": {"worktrees": {**wt, "unknown": ["w"]}},
        "dependency_drift:tool:node": {"dependency_drift": ["tool:node"]},
        "no_outcome_check": {"registration_problems": None},
        "outcome_check_registration_unverified": {"registration_problems": ["registration_modified"]},
        "outcome_checks_not_run": {"check_results_start": None},
        "outcome_check_not_evaluable": {"check_results": [{"passed": None}]},
        "predicate_rejects_reference_end": {"check_results": [{"passed": False}]},
        "predicate_not_discriminating": {"check_results_start": [{"passed": True}]},
    }
    for reason, patch in cases.items():
        got = causal.admit({**_good_record(), **patch})
        assert not got["eligible"] and got["reasons"] == [reason], (reason, got)
    # moved-but-unrelated worktrees in the attribution no longer exclude on their own
    assert causal.admit({**_good_record(), "attribution": {"unattributed": [], "other_worktrees_moved": ["w"]}})[
        "eligible"]
    assert causal.admit({})["reasons"][:2] == ["identity_not_accepted", "start_missing"]


def test_end_to_end_synthetic_episode(tmp: Path) -> None:
    repo = make_repo(tmp)
    ep = tmp / "episode"
    wt0 = causal.worktree_fingerprints(repo)
    s = causal.capture_state(repo, ep / "start", label="start")
    causal.freeze_history(repo, ep / "history")
    d0 = causal.dependency_fingerprint(s, TOOLS)
    (repo / "a.txt").write_bytes(b"done\n")
    e = causal.capture_state(repo, ep / "end", label="end")
    causal.materialize(ep / "end", tmp / "m")
    checks = [{"kind": "file_contains", "path": "a.txt", "text": "done"}]
    record = {"identity_accepted": True,
              "start_problems": causal.verify_state(ep / "start"),
              "end_problems": causal.verify_state(ep / "end"),
              "history_problems": causal.verify_history(ep / "history"),
              "unsupported": causal.unsupported_state(repo),
              "attribution": causal.attribute(s, e, ["a.txt"], worktrees_start=wt0,
                                              worktrees_end=causal.worktree_fingerprints(repo)),
              "dependency_drift": causal.dependency_drift(d0, causal.dependency_fingerprint(e, TOOLS), ["a.txt"]),
              "start_fidelity": [], "end_fidelity": [], "registration_problems": [],
              "worktrees": {"contaminating": [], "unknown": [], "shared_state_changed": False},
              "checks": checks, "check_results": causal.run_checks(checks, tmp / "m")}
    causal.materialize(ep / "start", tmp / "m0")
    record["check_results_start"] = causal.run_checks(checks, tmp / "m0")
    assert causal.admit(record)["eligible"], causal.admit(record)
    assert record["check_results"][0]["passed"] is True


# ------------------------------------------------------------ worktree status failure


def _break_gitdir_pointer(worktree: Path, target: Path) -> None:
    """Point a linked worktree's ``.git`` file at a gitdir that does not exist, so every git
    command inside it exits 128 (the file is hidden+read-only on Windows checkouts)."""
    dot_git = worktree / ".git"
    os.chmod(dot_git, stat.S_IWRITE)
    if os.name == "nt":
        subprocess.run(["attrib", "-h", "-r", str(dot_git)], check=False, capture_output=True)
    dot_git.write_text(f"gitdir: {target}\n", encoding="utf-8")


def test_worktree_status_clean_is_stable_and_observed(tmp: Path) -> None:
    repo = make_repo(tmp)
    other = tmp / "other_wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(other))
    a = causal.worktree_records(repo)
    b = causal.worktree_records(repo)
    key = next(iter(a))
    assert a[key]["fp"] == b[key]["fp"] and a[key]["fp"] is not None and a[key]["paths"] == []
    assert "status_error" not in a[key]
    assert causal.unobservable_worktrees(a, b) == []
    cls = causal.classify_worktrees(a, b, shared_start="s", shared_end="s", calls=[], subagents_unresolved=0)
    assert cls == {"contaminating": [], "unrelated": [], "unknown": [], "shared_state_changed": False}
    assert "other_worktree_unknown" not in causal.admit({"worktrees": cls})["reasons"]


def test_worktree_status_dirty_moves_and_is_observed(tmp: Path) -> None:
    repo = make_repo(tmp)
    other = tmp / "other_wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(other))
    a = causal.worktree_records(repo)
    (other / "a.txt").write_bytes(b"someone else")
    b = causal.worktree_records(repo)
    key = next(iter(a))
    assert a[key]["fp"] != b[key]["fp"] and b[key]["fp"] is not None and b[key]["paths"] == ["a.txt"]
    assert causal.unobservable_worktrees(a, b) == []
    cls = causal.classify_worktrees(a, b, shared_start="s", shared_end="s", calls=[], subagents_unresolved=0)
    assert cls["unrelated"] == [key] and cls["unknown"] == []


def test_worktree_status_failure_is_unknown_not_unmoved(tmp: Path) -> None:
    """Audit SA-1 (2026-10-12): a worktree whose ``git status`` fails must not fingerprint
    like a clean one. Broken gitdir pointer -> rc 128 -> unobservable -> unknown -> ineligible."""
    repo = make_repo(tmp)
    other = tmp / "other_wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(other))
    healthy = causal.worktree_records(repo)
    key = next(iter(healthy))
    _break_gitdir_pointer(other, tmp / "nonexistent" / "gitdir")
    probe = subprocess.run(["git", "-C", str(other), "status", "--porcelain=v2"], capture_output=True, env=ENV)
    assert probe.returncode == 128, probe
    broken = causal.worktree_records(repo)
    rec = broken[key]
    assert rec["fp"] is None and rec["paths"] is None
    assert rec["status_error"]["rc"] == 128 and rec["status_error"]["stderr"]
    assert rec["fp"] != healthy[key]["fp"]
    assert causal.worktree_fingerprints(repo)[key] is None
    # The record round-trips through JSON (the hooks persist it that way) and stays unobservable.
    broken = json.loads(json.dumps(broken))
    assert causal.unobservable_worktrees(healthy, broken) == [key]
    # Failed on one side, failed on both sides, healthy at the end again: unknown every time.
    for s_, e_ in ((healthy, broken), (broken, broken), (broken, healthy)):
        cls = causal.classify_worktrees(s_, e_, shared_start="s", shared_end="s", calls=[], subagents_unresolved=0)
        assert cls["unknown"] == [key] and not cls["unrelated"] and not cls["contaminating"], cls
        assert "other_worktree_unknown" in causal.admit({"worktrees": cls})["reasons"]
    # ``attribute`` never treats an unobserved fingerprint as unmoved.
    s = causal.capture_state(repo, tmp / "s", label="start")
    fps = {k: v["fp"] for k, v in broken.items()}
    att = causal.attribute(s, s, [], worktrees_start=fps, worktrees_end=fps)
    assert att["other_worktrees_moved"] == [key] and not att["isolated"]


def test_worktree_status_failure_monkeypatched_rc(tmp: Path) -> None:
    """Any non-zero exit, not only 128, is unobservable; stderr is captured, not discarded."""
    repo = make_repo(tmp)
    other = tmp / "other_wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(other))
    real_popen = subprocess.Popen

    class FailingStatus:
        returncode = 1

        def __init__(self, *_a, **_k) -> None:
            pass

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b"error: index file corrupt\n"

        def kill(self) -> None:
            pass

    def fake_popen(argv, *a, **k):
        if argv[:2] == ["git", "-C"] and "status" in argv:
            return FailingStatus()
        return real_popen(argv, *a, **k)

    causal.subprocess.Popen = fake_popen  # type: ignore[assignment]
    try:
        recs = causal.worktree_records(repo)
    finally:
        causal.subprocess.Popen = real_popen  # type: ignore[assignment]
    (rec,) = recs.values()
    assert rec["fp"] is None and rec["paths"] is None
    assert rec["status_error"] == {"rc": 1, "stderr": "error: index file corrupt"}
    cls = causal.classify_worktrees(recs, recs, shared_start="s", shared_end="s", calls=[], subagents_unresolved=0)
    assert cls["unknown"] == list(recs) and not causal.admit({"worktrees": cls})["eligible"]


def _nested_ignored_worktree(tmp: Path) -> tuple[Path, Path, str]:
    """A lane nested in an ignored directory of its parent repository."""
    repo = make_repo(tmp, {"a.txt": b"a\n", ".gitignore": b"lanes/\n"})
    lane = repo / "lanes" / "lane"
    git(repo, "worktree", "add", "-q", "-b", "lane", str(lane))
    recs = causal.worktree_records(repo)
    (key,) = recs
    return repo, lane, key


def test_nested_worktree_healthy_resolves(tmp: Path) -> None:
    repo, lane, key = _nested_ignored_worktree(tmp)
    a = causal.worktree_records(repo)
    assert a[key]["fp"] is not None and a[key]["paths"] == [] and "status_error" not in a[key]
    (lane / "a.txt").write_bytes(b"edited in lane")
    b = causal.worktree_records(repo)
    assert b[key]["fp"] not in (None, a[key]["fp"]) and b[key]["paths"] == ["a.txt"]
    cls = causal.classify_worktrees(a, b, shared_start="s", shared_end="s", calls=[], subagents_unresolved=0)
    assert cls["unrelated"] == [key] and cls["unknown"] == []


def test_nested_worktree_without_marker_is_unknown(tmp: Path) -> None:
    """Reviewer bypass (SA-1 2c): the lane's ``.git`` file removed. git walks up to the parent
    repo, status exits 0 with no output (the lane is ignored there), so the fingerprint would
    be stable whatever changed inside. The toplevel check must make it unobservable."""
    repo, lane, key = _nested_ignored_worktree(tmp)
    healthy = causal.worktree_records(repo)
    dot_git = lane / ".git"
    os.chmod(dot_git, stat.S_IWRITE)
    if os.name == "nt":
        subprocess.run(["attrib", "-h", "-r", str(dot_git)], check=False, capture_output=True)
    dot_git.unlink()
    probe = subprocess.run(["git", "-C", str(lane), "status", "--porcelain=v2"], capture_output=True, env=ENV)
    assert probe.returncode == 0 and probe.stdout == b"", probe  # the bypass precondition holds
    before = causal.worktree_records(repo)
    (lane / "a.txt").write_bytes(b"changed while unobservable")
    after = causal.worktree_records(repo)
    for rec in (before[key], after[key]):
        assert rec["fp"] is None and rec["paths"] is None, rec
        assert rec["status_error"]["rc"] == 0 and rec["status_error"]["stderr"].startswith("toplevel mismatch: "), rec
    for s_, e_ in ((healthy, before), (before, after), (after, healthy)):
        cls = causal.classify_worktrees(s_, e_, shared_start="s", shared_end="s", calls=[], subagents_unresolved=0)
        assert cls["unknown"] == [key] and not cls["unrelated"], cls
        assert not causal.admit({"worktrees": cls})["eligible"]
    st = causal.capture_state(repo, tmp / "s", label="start")
    fps = {k: v["fp"] for k, v in before.items()}
    assert not causal.attribute(st, st, [], worktrees_start=fps, worktrees_end=fps)["isolated"]


def test_worktree_toplevel_probes_run_beside_the_statuses(tmp: Path) -> None:
    """D4 Q1: every sibling's toplevel probe starts with the statuses, so N siblings cost the
    start budget one round trip, not N in a row. Records are unchanged; a stuck probe still
    leaves its worktree unobservable."""
    repo = make_repo(tmp)
    for i in range(3):
        git(repo, "worktree", "add", "-q", "-b", f"s{i}", str(tmp / f"s{i}"))
    (tmp / "s1" / "a.txt").write_bytes(b"moved")
    expected = causal.worktree_records(repo)
    real = causal._toplevel_probe
    events: list[str] = []

    class Probe:
        def __init__(self, path: str) -> None:
            self.p = real(path)
            events.append("start")

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            events.append("wait")
            return self.p.communicate(timeout=timeout)

        def __getattr__(self, name: str):  # noqa: ANN204
            return getattr(self.p, name)

    class Stuck(Probe):
        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            if timeout is not None:
                raise subprocess.TimeoutExpired("git", timeout)
            return self.p.communicate()

    try:
        causal._toplevel_probe = Probe
        assert causal.worktree_records(repo) == expected
        assert events[:3] == ["start"] * 3, events  # all started before the first wait
        causal._toplevel_probe = Stuck
        stuck = causal.worktree_records(repo)
    finally:
        causal._toplevel_probe = real
    assert all(r["fp"] is None and r["status_error"]["stderr"] == "toplevel unresolved: timeout"
               for r in stuck.values()), stuck


# ------------------------------------------------------------------ shared git state (D4 Q2)


def _repo_with_sibling(tmp: Path) -> tuple[Path, Path]:
    repo = make_repo(tmp)
    git(repo, "remote", "add", "origin", str(tmp / "no_remote"))
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    side = tmp / "side_wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(side))
    return repo, side


def test_own_branch_config_drops_only_other_branch_sections(tmp: Path) -> None:
    cfg = (b'[core]\n\tbare = false\n[branch "main"]\n\tremote = origin\n'
           b'[branch "lane/a"]\n\tremote = origin\n\tmerge = refs/heads/main\n'
           b'[BRANCH "q\\"x"]\n\tremote = origin\n[remote "origin"]\n\turl = ../o\n')
    assert causal.own_branch_config(cfg, b"main") == (b'[core]\n\tbare = false\n[branch "main"]\n\tremote = origin\n'
                                                      b'[remote "origin"]\n\turl = ../o\n')
    assert causal.own_branch_config(cfg, b'q"x').count(b"[BRANCH") == 1, "escaped subsection name"
    assert b"[branch" not in causal.own_branch_config(cfg, None), "detached: no branch section applies"
    for odd in (b'[branch "lane/a"] remote = origin\n', b'[branch.lane]\n\tremote = o\n'):
        assert causal.own_branch_config(odd, b"main") == odd, "a header git does not write is kept"


def test_shared_git_state_ignores_routine_sibling_writes(tmp: Path) -> None:
    """D4 Q2: what sibling worktrees routinely write cannot alter this checkout, so it no
    longer moves the shared state (before the fix each of these flipped it)."""
    repo, side = _repo_with_sibling(tmp)
    s0 = causal.shared_git_state(repo)
    git(side, "branch", "-u", "origin/main")  # what push -u records
    git(repo, "worktree", "add", "-q", "-b", "lane", str(tmp / "lane_wt"), "origin/main")  # house-rule lane
    assert b'[branch "lane"]' in (repo / ".git" / "config").read_bytes()
    git(side, "commit", "-q", "--allow-empty", "-m", "side work")
    git(side, "gc", "-q")  # repack runs update-server-info
    assert (repo / ".git" / "info" / "refs").is_file()
    git(side, "config", "--remove-section", "branch.side")  # what branch -d drops
    assert causal.shared_git_state(repo) == s0


def test_shared_git_state_still_sees_what_applies_here(tmp: Path) -> None:
    """The narrowing keeps every change that can alter this checkout: such a pair stays inadmissible."""
    repo, side = _repo_with_sibling(tmp)
    gd = repo / ".git"
    for d in (gd / "info", gd / "hooks"):
        d.mkdir(exist_ok=True)

    def append(p: Path, data: bytes) -> None:
        with open(p, "ab") as fh:
            fh.write(data)
    changes = {
        "generic key written from a sibling": lambda: git(side, "config", "core.autocrlf", "true"),
        "this branch's own section": lambda: git(side, "config", "branch.main.rebase", "true"),
        "info/exclude": lambda: append(gd / "info" / "exclude", b"*.log\n"),
        "hooks": lambda: (gd / "hooks" / "pre-commit").write_bytes(b"#!/bin/sh\nexit 0\n"),
        "a branch header git does not write": lambda: append(gd / "config", b'[branch "side"] remote = x\n'),
        "this worktree's HEAD": lambda: git(repo, "checkout", "-q", "--detach"),
    }
    for why, change in changes.items():
        before = causal.shared_git_state(repo)
        change()
        assert causal.shared_git_state(repo) != before, why
    before = causal.shared_git_state(repo)
    git(side, "config", "branch.main.rebase", "false")
    assert causal.shared_git_state(repo) == before, "detached: branch main's section no longer applies here"


def test_sibling_elsewhere_is_unrelated_and_the_predicate_decides(tmp: Path) -> None:
    """D4 Q2 regression, end to end on real worktrees: during the turn a sibling edits its own
    tree and records its upstream, in paths the episode never touched. Before the fix the
    config write set ``shared_state_changed`` and so made the sibling contaminating; now it is
    unrelated, and admission turns on the preregistered discriminating predicate (fails on
    the start state, passes on the end state). A shared change that applies here still
    excludes the pair."""
    repo, side = _repo_with_sibling(tmp)
    ep = tmp / "episode"
    ws, ss = causal.worktree_records(repo), causal.shared_git_state(repo)
    s = causal.capture_state(repo, ep / "start", label="start")
    causal.freeze_history(repo, ep / "history")
    (repo / "a.txt").write_bytes(b"done\n")  # the agent's write
    (side / "side.txt").write_bytes(b"other lane\n")  # the sibling, elsewhere
    git(side, "branch", "-u", "origin/main")
    e = causal.capture_state(repo, ep / "end", label="end")
    we, se = causal.worktree_records(repo), causal.shared_git_state(repo)
    calls = [{"input": {"file_path": str(repo / "a.txt"), "content": "done\n"}}]
    key = next(iter(we))
    cls = causal.classify_worktrees(ws, we, shared_start=ss, shared_end=se, calls=calls, subagents_unresolved=0,
                                    top=str(repo))
    assert cls == {"contaminating": [], "unrelated": [key], "unknown": [], "shared_state_changed": False}, cls
    att = causal.attribute(s, e, ["a.txt"], top=str(repo), worktrees_start={k: v["fp"] for k, v in ws.items()},
                           worktrees_end={k: v["fp"] for k, v in we.items()})
    assert att["unattributed"] == [] and att["other_worktrees_moved"] == [key] and not att["isolated"], att
    causal.materialize(ep / "start", tmp / "m0")
    causal.materialize(ep / "end", tmp / "m1")
    record = {"identity_accepted": True, "start_problems": causal.verify_state(ep / "start"),
              "end_problems": causal.verify_state(ep / "end"), "history_problems": causal.verify_history(ep / "history"),
              "unsupported": causal.unsupported_state(repo), "start_fidelity": [], "end_fidelity": [],
              "attribution": att, "worktrees": cls, "dependency_drift": [], "registration_problems": []}

    def verdict(checks: list[dict]) -> dict:
        return causal.admit({**record, "checks": checks, "check_results": causal.run_checks(checks, tmp / "m1"),
                             "check_results_start": causal.run_checks(checks, tmp / "m0")})
    assert verdict([{"kind": "file_contains", "path": "a.txt", "text": "done"}]) == {"eligible": True, "reasons": []}
    assert verdict([{"kind": "file_contains", "path": "a.txt", "text": "o"}])["reasons"] == [
        "predicate_not_discriminating"], "already true at the start: not admissible"
    git(side, "config", "core.autocrlf", "true")  # shared state that does apply to this checkout
    cls2 = causal.classify_worktrees(ws, causal.worktree_records(repo), shared_start=ss,
                                     shared_end=causal.shared_git_state(repo), calls=calls, subagents_unresolved=0,
                                     top=str(repo))
    assert cls2["contaminating"] == [key] and cls2["shared_state_changed"] is True, cls2
    assert causal.admit({**record, "worktrees": cls2})["reasons"][:2] == [
        "shared_git_state_changed", "other_worktree_contaminating"]


# ------------------------------------------------------------------------ runner

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def _rm(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def main() -> int:
    only = sys.argv[1:]
    tmp_root = tempfile.mkdtemp(prefix="ec-causal-test-")
    try:
        for fn in TESTS:
            if only and fn.__name__ not in only:
                continue
            case = Path(tmp_root) / fn.__name__
            case.mkdir()
            t0 = time.perf_counter()
            try:
                fn(case)
                PASSED.append(f"{fn.__name__} ({time.perf_counter() - t0:.1f}s)")
            except Exception:  # noqa: BLE001 — plain-check runner
                FAILED.append(fn.__name__)
                print(f"FAIL {fn.__name__}\n{traceback.format_exc()}")
    finally:
        shutil.rmtree(tmp_root, onerror=_rm)
    for name in PASSED:
        print(f"ok   {name}")
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

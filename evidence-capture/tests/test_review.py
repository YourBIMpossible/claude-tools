#!/usr/bin/env python3
"""Step-3 tests: review, privacy-scope check, payload, expiry, seals, content-store rules.

No framework, plain checks, synthetic fixtures only: temporary git repositories and
stores under one temporary directory, removed afterwards. No model call, no network, no
real repository, no real store.

Synthetic example — contains no private repository data or production findings.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
import clone_builder  # noqa: E402
import common  # noqa: E402
import manifest as mf  # noqa: E402
import review  # noqa: E402
import snapshot  # noqa: E402
import store  # noqa: E402
import test_capture as tc  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
ENDED = "2026-09-26T10:05:00.000000Z"
BRIEF = "- [ripgrep] src/alpha.py:1 defines alpha\n  why: matched the prompt term alpha\n"


def git(repo: Path, *args: str) -> str:
    return tc.git(repo, *args)


def make_stores(root: Path, name: str = "st", **kw: object) -> store.Stores:
    st = store.Stores(root / f"{name}-meta", root / f"{name}-content", **kw)  # type: ignore[arg-type]
    store.init_stores(st)
    store.init_meta_repo(st)
    return st


def episode(stores: store.Stores, repo: Path, prompt: str = "fix alpha", brief: str = BRIEF, *,
            origin: str | None = "user", link: str = "linked", attachment: bool | None = False,
            trusted: bool = True, started: str = "2026-09-26T10:00:00.000000Z") -> tuple[dict, review.ReviewInputs]:
    """A manifest whose rules are all ``no`` (unless overridden), with a real start
    snapshot of ``repo``, written to the store; plus the matching review inputs."""
    m = tc.base_manifest(prompt, started=started)
    repo_fields, snap = snapshot.take_snapshot(repo, m["episode_id"], stores, budget_s=60)
    assert snap["state"] == "complete", snap
    m["repo"].update(repo_fields)
    m["start_snapshot"] = snap
    m["boundary"] = {"cli_version": "9.9.9", "trusted": trusted, "reason": None if trusted else "boundary_untested"}
    m["link"].update(state=link, brief_sha256=common.sha256_text(brief), brief_len=len(brief), brief_empty=not brief)
    m["capture"]["status"] = "complete"
    m["prompt"].update(origin=origin, has_attachment=attachment)
    m["edited_files"] = [{"path_sha256": common.path_sha256("src/alpha.py")}]
    for c in mf.DEP_CATEGORIES:
        m["deps"][c]["required"] = "no"
    m["prior_context"]["prior_conversation_required"] = "no"
    m["observation"]["sufficient"] = True
    m["ended_at"] = ENDED
    mf.refresh(m)
    store.write_manifest(stores, m)
    return m, review.ReviewInputs(repo, prompt, brief, {"cli_version": "9.9.9", "plugins": []})


def reread(stores: store.Stores, m: dict) -> dict:
    return store.read_manifest(stores, m["episode_id"])


def committed(stores: store.Stores, rel: str) -> bool:
    out = subprocess.run(["git", "-C", str(stores.meta), "log", "--oneline", "--", rel], capture_output=True,
                         text=True).stdout
    return bool(out.strip())


# --------------------------------------------------------------------------- tests

def test_content_store_not_git(root: Path) -> None:
    host = tc.make_repo(root, "host")
    inside = store.Stores(root / "meta-ok", host / "content")
    store.init_stores(inside)
    assert "content store is inside a git repository" in store.check_content_store(inside)
    st = make_stores(root)
    assert store.check_content_store(st) == [] and store.check_meta_store(st) == [], (
        store.check_content_store(st), store.check_meta_store(st))
    exclude = store._meta_exclude_file(st)
    assert exclude is not None and set(store._meta_exclude_lines(st)) <= set(exclude.read_text().splitlines())
    assert "*.patch" in (st.meta / ".gitignore").read_text(encoding="utf-8")
    store.init_meta_repo(st)  # idempotent: no duplicate lines
    lines = exclude.read_text().splitlines()
    assert all(lines.count(w) == 1 for w in store._meta_exclude_lines(st)), lines
    assert store.meta_clean(st), "a new metadata store starts committed and clean"
    (st.meta / "capture.log").write_text("operational\n", encoding="utf-8")
    for leak in ("worktree.patch", "index.bin", "snapshot-copy.txt", "payload.txt"):
        (st.meta / leak).write_text("content\n", encoding="utf-8")
    (st.meta / "st-content").mkdir()
    (st.meta / "st-content" / "x.txt").write_text("content\n", encoding="utf-8")
    assert store.meta_clean(st), "content patterns are excluded from the metadata repository"
    assert not store.commit_meta(st, [root / "elsewhere.json"], "outside"), "paths outside the store are refused"
    nested = store.Stores(host / "meta", root / "c2")
    store.init_stores(nested)
    try:
        store.init_meta_repo(nested)
        raise AssertionError("a metadata store inside another repository was accepted")
    except store.StoreError:
        pass
    bare = store.Stores(root / "bare-meta", root / "bare-content")
    store.init_stores(bare)
    assert "metadata store is not a git repository" in store.check_meta_store(bare)


def test_sync_root_refused(root: Path) -> None:
    for name in ("OneDrive - Example", "Dropbox", "iCloud Drive"):
        st = store.Stores(root / "m", root / name / "content")
        store.init_stores(st)
        problems = store.check_content_store(st)
        assert any(p.startswith("content store under a sync root") for p in problems), (name, problems)
    ok = store.Stores(root / "m2", root / "OneDriveless" / "content")
    store.init_stores(ok)
    assert store.check_content_store(ok) == [], "a name merely starting with a sync root name is not one"
    missing = store.Stores(root / "m3", root / "c3")
    store.init_stores(missing)
    (missing.content / "NOSYNC").unlink()
    assert "marker missing: NOSYNC" in store.check_content_store(missing)


def test_deletion_verified_and_logged(root: Path) -> None:
    repo = tc.make_repo(root)
    (repo / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    st = make_stores(root)
    m, _ = episode(st, repo)
    d = st.snapshots / m["episode_id"]
    listed = list(store.read_sums(d))
    sums = common.sha256_file(d / "SHA256SUMS")
    rec = store.verified_delete(st, "snapshots", m["episode_id"], "test")
    assert not d.exists() and not any((d / rel).exists() for rel in listed)
    log = store.read_deletions(st)
    assert log[-1] == rec and rec["verified"] is True and rec["sums_sha256"] == sums and rec["listed_paths"] == len(listed)
    again = store.verified_delete(st, "snapshots", m["episode_id"], "test")
    assert again["verified"] and again["note"] == "absent"
    # through expiry: the log is committed with the manifest
    m2, _ = episode(st, repo, prompt="second")
    res = review.expire(st, now=common.plus_days(ENDED, 91))
    assert res["expired_deleted"] == 1 and res["deletions_verified"] == 1, res
    dirty = subprocess.run(["git", "-C", str(st.meta), "status", "--porcelain", "--", "DELETIONS.log",
                            f"manifests/{m2['episode_id']}.json"], capture_output=True, text=True).stdout
    assert committed(st, "DELETIONS.log") and not dirty.strip(), dirty
    assert reread(st, m2)["retention"]["deletion_verified"] is True


def test_review_materializes_clean_episode(root: Path) -> None:
    repo = tc.make_repo(root)
    (repo / "src" / "alpha.py").write_text("def alpha(x: int) -> int:\n    return x * 4\n", encoding="utf-8")
    (repo / "notes.txt").write_text("scratch\n", encoding="utf-8")
    st = make_stores(root)
    prompt = (f"fix alpha in {repo / 'src' / 'alpha.py'} (see https://example.invalid/docs/a.py "
              f"and {repo.as_posix()}/README.md)")
    m, inputs = episode(st, repo, prompt=prompt)
    res = review.review_episode(st, m, inputs, now="2026-09-26T10:06:00Z")
    assert res["state"] == "materialized" and res["changed"], res
    ep = m["episode_id"]
    assert not (st.snapshots / ep).exists(), "the snapshot was moved, not copied"
    assert review.verify_payload(st, ep) == []
    payload = st.payloads / ep
    assert (payload / "prompt.txt").read_text(encoding="utf-8") == prompt
    assert (payload / "brief.txt").read_text(encoding="utf-8") == BRIEF
    ret = json.loads((payload / "retention.json").read_text(encoding="utf-8"))
    assert ret["expires_at"] == common.plus_days(ENDED, 90)
    got = reread(st, m)
    assert got["funnel"]["privacy_cleared"] and got["funnel"]["payload_materialized"], got["funnel"]
    assert not got["funnel"]["start_state_reconstructed"] and mf.validate_funnel(got["funnel"]) == []
    assert got["retention"]["expires_at"] == ret["expires_at"]
    assert got["retention"]["payload_sums_sha256"] == common.sha256_file(payload / "SHA256SUMS")
    assert got["review"]["privacy"]["passed"] is True
    text = json.dumps(got)
    for secret in (prompt, BRIEF.strip(), repo.as_posix(), str(repo), "notes.txt"):
        assert secret not in text, f"manifest leaks {secret[:20]!r}"
    assert store.meta_clean(st), subprocess.run(["git", "-C", str(st.meta), "status", "--porcelain"],
                                                capture_output=True, text=True).stdout
    again = review.review_episode(st, got, inputs)
    assert again["state"] == "materialized" and not again["changed"], "review is idempotent"


def test_round_trip_snapshot_payload_clone(root: Path) -> None:
    repo = tc.make_repo(root)
    start = tc.head(repo)
    (repo / "src" / "alpha.py").write_text("def alpha(x: int) -> int:\n    return x * 5\n", encoding="utf-8")
    (repo / "src" / "beta.py").write_text("BETA = 2\n", encoding="utf-8")
    git(repo, "add", "src/beta.py")
    (repo / "README.md").write_text("# staged then edited\n", encoding="utf-8")
    git(repo, "add", "README.md")
    (repo / "README.md").write_text("# edited after staging\n", encoding="utf-8")
    (repo / "todo.txt").write_text("untracked at the start\n", encoding="utf-8")
    st = make_stores(root)
    m, inputs = episode(st, repo)
    assert review.review_episode(st, m, inputs)["state"] == "materialized"
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "after the episode")
    rec = clone_builder.clone_start_only(repo, start, root / "replay", st.payloads / m["episode_id"] / "snapshot")
    assert rec["start_state_reconstructed"] is True, (rec["failed"], rec["error"])
    assert (root / "replay" / "todo.txt").read_text(encoding="utf-8") == "untracked at the start\n"
    assert (root / "replay" / "README.md").read_text(encoding="utf-8") == "# edited after staging\n"


def test_payload_never_overwritten(root: Path) -> None:
    repo = tc.make_repo(root)
    st = make_stores(root)
    m, inputs = episode(st, repo)
    ep = m["episode_id"]
    squatter = st.payloads / ep
    squatter.mkdir()
    (squatter / "keep.txt").write_text("pre-existing\n", encoding="utf-8")
    try:
        review.materialize_payload(st, m, inputs, {"passed": True}, "2026-09-26T10:06:00Z")
        raise AssertionError("an existing payload was overwritten")
    except store.StoreError:
        pass
    res = review.review_episode(st, m, inputs)
    assert res["state"] == "held" and res["reason"] == "payload_conflict", res
    assert (squatter / "keep.txt").read_text(encoding="utf-8") == "pre-existing\n"
    assert not store.verify_sums(st.snapshots / ep), "the snapshot is untouched"
    assert not reread(st, m)["funnel"]["payload_materialized"]


def test_staged_materialization(root: Path) -> None:
    repo = tc.make_repo(root)
    st = make_stores(root)
    m, inputs = episode(st, repo)
    ep = m["episode_id"]
    sums = common.sha256_file(st.snapshots / ep / "SHA256SUMS")
    real = review.write_sums

    def boom(directory: Path, *a: object, **k: object) -> Path:
        raise OSError("disk full (synthetic)")

    review.write_sums = boom  # type: ignore[assignment]
    try:
        res = review.review_episode(st, m, inputs)
    finally:
        review.write_sums = real  # type: ignore[assignment]
    assert res["state"] == "held" and res["reason"].startswith("payload_write_failed"), res
    assert not (st.payloads / ep).exists() and list(st.tmp.iterdir()) == [], "staging removed, nothing published"
    assert common.sha256_file(st.snapshots / ep / "SHA256SUMS") == sums and not store.verify_sums(st.snapshots / ep)
    assert review.review_episode(st, reread(st, m), inputs)["state"] == "materialized", "a later run completes it"

    # interrupted after the snapshot moved into staging: the orphan sweep restores it
    repo2 = tc.make_repo(root, "repo2")
    m2, inputs2 = episode(st, repo2, prompt="second")
    ep2 = m2["episode_id"]
    stage = st.tmp / f"{ep2}.99999"
    stage.mkdir()
    os.replace(st.snapshots / ep2, stage / "snapshot")
    (stage / "prompt.txt").write_text("partial\n", encoding="utf-8")
    old = time.time() - 2 * store.TMP_MAX_AGE_S
    os.utime(stage, (old, old))
    gone = store.clean_tmp_orphans(st)
    assert gone == [stage.name] and not stage.exists(), gone
    assert not store.verify_sums(st.snapshots / ep2), "snapshot restored intact"
    assert "snapshot restored" in (st.meta / "capture.log").read_text(encoding="utf-8")
    assert review.review_episode(st, reread(st, m2), inputs2)["state"] == "materialized"

    # interrupted after the rename into payloads/ but before the manifest write: adopted
    repo3 = tc.make_repo(root, "repo3")
    m3, inputs3 = episode(st, repo3, prompt="third")
    review.materialize_payload(st, m3, inputs3, {"passed": True}, "2026-09-26T10:06:00Z")
    res3 = review.review_episode(st, reread(st, m3), inputs3)
    assert res3["state"] == "materialized" and res3["reason"] == "payload_recovered", res3
    assert reread(st, m3)["funnel"]["payload_materialized"]


def test_privacy_scope_rejects(root: Path) -> None:
    st = make_stores(root)
    outside = "D:/elsewhere/data.csv" if os.name == "nt" else "/opt/elsewhere/data.csv"

    def run(name: str, expect: str, *, prompt: str = "fix alpha", brief: str = BRIEF, setup=None, **kw: object) -> dict:
        repo = tc.make_repo(root, name)
        if setup:
            setup(repo)
        m, inputs = episode(st, repo, prompt=prompt + " " + name, brief=brief, **kw)
        res = review.review_episode(st, m, inputs)
        got = reread(st, m)
        assert res["state"] == "excluded" and res["reason"] == "privacy_scope", (name, res)
        assert not (st.snapshots / m["episode_id"]).exists() and not (st.payloads / m["episode_id"]).exists()
        assert got["retention"]["deletion_verified"] is True and not got["funnel"]["privacy_cleared"]
        assert outside not in json.dumps(got), "findings carry hashes, never the path"
        by_check = got["review"]["privacy"]["by_check"]
        assert [k for k, v in by_check.items() if v] == [expect] if expect else True, (name, by_check)
        return by_check

    assert run("prompt-path", "path_outside_repo", prompt=f"compare with {outside}")["path_outside_repo"] >= 1
    assert run("unc-path", "path_outside_repo", prompt=r"see \\fileserver\share\x.txt")["path_outside_repo"] >= 1
    assert run("home-path", "path_outside_repo", prompt="see ~/notes.md")["path_outside_repo"] >= 1

    def env_file(repo: Path) -> None:
        (repo / ".env").write_text("TOKEN=synthetic\n", encoding="utf-8")
    assert run("deny-env", "deny_glob", setup=env_file)["deny_glob"] == 1

    def cfg_glob(repo: Path) -> None:
        (repo / ".evidence-compiler").mkdir()
        (repo / ".evidence-compiler" / "config.yaml").write_text(
            "capture:\n  deny_globs:\n    - private/**\n", encoding="utf-8")
        (repo / "private").mkdir()
        (repo / "private" / "a.txt").write_text("x\n", encoding="utf-8")
    assert run("deny-config", "deny_glob", setup=cfg_glob)["deny_glob"] == 1

    def cfg_bad(repo: Path) -> None:
        (repo / ".evidence-compiler").mkdir()
        (repo / ".evidence-compiler" / "config.yaml").write_text("capture: [unclosed\n", encoding="utf-8")
    assert run("deny-unreadable", "deny_glob", setup=cfg_bad)["deny_glob"] == 1

    def content_path(repo: Path) -> None:
        (repo / "paths.txt").write_text(f"input = {outside}\n", encoding="utf-8")
    assert run("snapshot-path", "path_outside_repo", setup=content_path)["path_outside_repo"] >= 1

    assert run("cite-up", "external_citation", brief="- [ripgrep] ../sibling/x.py:3 defines x\n  why: term\n")["external_citation"] == 1
    assert run("cite-abs", "external_citation", brief=f"- [ripgrep] {outside}:3 defines x\n  why: term\n")["external_citation"] >= 1
    assert run("attach", "attachment", attachment=True)["attachment"] == 1
    assert run("attach-unknown", "attachment", attachment=None)["attachment"] == 1

    # an escaping path written against the repository root itself
    repo = tc.make_repo(root, "escape2")
    m, inputs = episode(st, repo, prompt=f"read {repo.as_posix()}/../sibling/x.py")
    res = review.review_episode(st, m, inputs)
    assert res["state"] == "excluded" and reread(st, m)["review"]["privacy"]["by_check"]["path_outside_repo"] >= 1

    # a sibling directory sharing the root as a name prefix is outside
    repo = tc.make_repo(root, "pref")
    m, inputs = episode(st, repo, prompt=f"read {repo.as_posix()}-other/x.py")
    assert review.review_episode(st, m, inputs)["reason"] == "privacy_scope"

    assert review.deny_match("config/credentials.json", list(review.BUILTIN_DENY_GLOBS)) == "credentials*"
    assert review.deny_match("src/alpha.py", list(review.BUILTIN_DENY_GLOBS)) is None
    assert review.deny_match("keys/SERVER.PEM", list(review.BUILTIN_DENY_GLOBS)) == "*.pem"
    assert review.deny_match("a/b", ["a/**/b"]) == "a/**/b"
    assert review.deny_match("secrets/.env", ["secrets/**/.env"]) == "secrets/**/.env"
    assert review.deny_match("ab", ["a/**/b"]) is None
    for rel in ("a/b/c", "a/b/x/c", "a/x/b/c", "a/x/b/y/c"):
        assert review.deny_match(rel, ["a/**/b/**/c"]) == "a/**/b/**/c"
    assert review.deny_match("a/b", ["a/**/**/b"]) == "a/**/**/b"
    assert review.outside_paths("https://example.invalid/a/b and http://x.invalid/c", Path(root)) == []
    assert review.load_capture_config(root / "nowhere")["present"] is False


def test_deny_glob_double_star_matches_root(root: Path) -> None:
    """``**/x`` means x at any depth, the repository root included: a root-level private
    file must not pass the screen because fnmatch has no ``**``."""
    globs = ["**/private.json", "**/vault/**"]
    for rel in ("private.json", "a/private.json", "a/b/private.json", "vault/k.txt", "x/vault/k.txt"):
        assert review.deny_match(rel, globs) is not None, rel
    for rel in ("private.json.bak", "a/public.json", "vaults/k.txt"):
        assert review.deny_match(rel, globs) is None, rel
    assert review.deny_match("x", ["**/"]) is None
    st = make_stores(root)
    repo = tc.make_repo(root)
    (repo / ".evidence-compiler").mkdir()
    (repo / ".evidence-compiler" / "config.yaml").write_text(
        "capture:\n  deny_globs:\n    - '**/private.json'\n", encoding="utf-8")
    (repo / "private.json").write_text("{\"k\": \"synthetic\"}\n", encoding="utf-8")
    m, inputs = episode(st, repo)
    privacy = review.privacy_check(m, st.snapshots / m["episode_id"], inputs)
    if review.load_capture_config(repo)["error"] == "yaml_unavailable":
        # no PyYAML on this interpreter: the config cannot be read and the check fails closed
        want = {"check": "deny_glob", "source": "config", "error": "yaml_unavailable"}
    else:
        want = {"check": "deny_glob", "source": "snapshot", "glob_source": "config",
                "sha256": common.sha256_bytes(b"private.json")}
    assert any(all(f.get(k) == v for k, v in want.items()) for f in privacy["findings"]), privacy["findings"]
    assert privacy["by_check"]["deny_glob"] == 1 and privacy["findings_total"] == 1, privacy
    assert review.review_episode(st, m, inputs)["reason"] == "privacy_scope"


def test_privacy_scope_non_ascii_root(root: Path) -> None:
    """A repository root with non-ASCII characters is compared as written to the payload,
    so a settings path inside it clears and one outside it is still flagged."""
    st = make_stores(root)
    (root / "zoë-ünï").mkdir()
    repo = tc.make_repo(root / "zoë-ünï", "repo")
    inside = repo.as_posix() + "/src/alpha.py"
    m, inputs = episode(st, repo, prompt=f"fix {inside}")
    inputs = dataclasses.replace(inputs, settings={**inputs.settings, "cwd": inside})
    privacy = review.privacy_check(m, st.snapshots / m["episode_id"], inputs)
    assert privacy["passed"], privacy["findings"]
    assert review.review_episode(st, m, inputs)["state"] == "materialized"
    assert json.loads((st.payloads / m["episode_id"] / "settings.json").read_text(encoding="utf-8"))["cwd"] == inside
    outside = (root / "zoë-ünï" / "other" / "x.py").as_posix()
    inputs2 = dataclasses.replace(inputs, settings={**inputs.settings, "cwd": outside})
    m2, _ = episode(st, repo, prompt="second")
    privacy2 = review.privacy_check(m2, st.snapshots / m2["episode_id"], inputs2)
    assert privacy2["by_check"]["path_outside_repo"] == 1 and privacy2["findings"][0]["source"] == "settings", privacy2


def test_review_excluded_held(root: Path) -> None:
    st = make_stores(root)
    repo = tc.make_repo(root)

    def fresh(prompt: str, **kw: object) -> tuple[dict, review.ReviewInputs]:
        return episode(st, repo, prompt=prompt, **kw)

    m, i = fresh("rule yes", origin="tool")
    res = review.review_episode(st, m, i)
    assert res["state"] == "excluded" and res["reason"] == "rules:X1", res
    assert not (st.snapshots / m["episode_id"]).exists() and res["deletion"]["verified"]

    m, i = fresh("rule unknown", origin=None)
    res = review.review_episode(st, m, i)
    assert res["state"] == "held" and res["reason"] == "unknown:X1", res
    assert (st.snapshots / m["episode_id"]).is_dir(), "held snapshots are kept"
    assert reread(st, m)["retention"]["expires_at"] == common.plus_days(ENDED, 90)
    assert not review.review_episode(st, reread(st, m), i)["changed"], "unchanged decision writes nothing"

    m, i = fresh("link none", link="none")
    assert review.review_episode(st, m, i)["reason"] == "link_pending"
    m, i = fresh("link ambiguous", link="ambiguous")
    res = review.review_episode(st, m, i)
    assert res["state"] == "excluded" and res["reason"] == "link_ambiguous"
    m, i = fresh("untrusted", trusted=False)
    assert review.review_episode(st, m, i)["reason"].startswith("boundary_untrusted")
    m, i = fresh("no inputs")
    assert review.review_episode(st, m, None)["reason"] == "inputs_unavailable"
    m, i = fresh("wrong brief")
    res = review.review_episode(st, m, dataclasses.replace(i, brief_text=BRIEF + "extra"))
    assert res["state"] == "held" and res["reason"] == "inputs_mismatch:brief", res
    res = review.review_episode(st, reread(st, m), dataclasses.replace(i, repo_root=root))
    assert res["reason"] == "inputs_mismatch:repo_root", res
    assert review.review_episode(st, reread(st, m), i)["state"] == "materialized", "held resolves once inputs match"

    m, i = fresh("tampered")
    snap = st.snapshots / m["episode_id"]
    with (snap / "git-config.json").open("a", encoding="utf-8") as fh:
        fh.write(" ")
    res = review.review_episode(st, m, i)
    assert res["state"] == "excluded" and res["reason"] == "snapshot_unverified", res

    m, i = fresh("open episode")
    m["ended_at"] = None
    assert review.review_episode(st, m, i)["reason"] == "episode_open"


def test_owner_review_resolves_unknown(root: Path) -> None:
    st = make_stores(root)
    repo = tc.make_repo(root)
    m, i = episode(st, repo, origin=None)
    assert review.review_episode(st, m, i)["state"] == "held"
    path = review.owner_review_path(st, m["episode_id"])
    common.write_json_atomic(path, {"episode_id": "cap_other", "rules": {"X1": {"state": "no", "basis": "typed"}}})
    assert review.review_episode(st, reread(st, m), i)["state"] == "held", "a review naming another episode is ignored"
    common.write_json_atomic(path, {"episode_id": m["episode_id"], "reviewed_at": "2026-09-27T00:00:00Z",
                                    "rules": {"X1": {"state": "no", "basis": "owner typed the prompt"}},
                                    "case_by_case": {"admit": True, "notes": "synthetic"}})
    res = review.review_episode(st, reread(st, m), i)
    assert res["state"] == "materialized", res
    got = reread(st, m)
    assert got["review"]["owner_review"] is True
    assert [e for e in got["exclusions"] if e["rule"] == "X1"][0].get("owner_resolved") is True

    m2, i2 = episode(st, repo, prompt="capture partial")
    m2["capture"]["status"] = "partial"
    store.write_manifest(st, m2)
    common.write_json_atomic(review.owner_review_path(st, m2["episode_id"]),
                             {"episode_id": m2["episode_id"], "rules": {"X7": {"state": "no", "basis": "no"}}})
    assert review.review_episode(st, m2, i2)["reason"] == "unknown:X7", "X7 is never owner-resolvable"


def test_expiry_deletes_and_logs(root: Path) -> None:
    st = make_stores(root)
    repo = tc.make_repo(root)
    m, i = episode(st, repo)
    review.review_episode(st, m, i)
    held, _ = episode(st, repo, prompt="held", origin=None)
    review.review_episode(st, held, None)
    orphan = st.snapshots / "cap_orphan000000000"
    orphan.mkdir()
    (orphan / "x.txt").write_text("x\n", encoding="utf-8")
    store.write_sums(orphan)
    old = time.time() - 100 * 86400
    os.utime(orphan, (old, old))

    res = review.expire(st, now=common.plus_days(ENDED, 89))
    assert res["expired_deleted"] == 1, res  # only the 100-day-old orphan
    assert not orphan.exists()
    assert (st.payloads / m["episode_id"]).is_dir() and (st.snapshots / held["episode_id"]).is_dir()

    res = review.expire(st, now=common.plus_days(ENDED, 91))
    assert res["expired_deleted"] == 2 and res["deletions_verified"] == 2 and res["extended_by_seal"] == 0, res
    assert not (st.payloads / m["episode_id"]).exists() and not (st.snapshots / held["episode_id"]).exists()
    log = store.read_deletions(st)
    assert {r["episode_id"] for r in log} >= {m["episode_id"], held["episode_id"], orphan.name}
    assert all(r["verified"] for r in log)
    assert reread(st, m)["retention"]["deleted_kind"] == "payloads"
    assert committed(st, "DELETIONS.log") and committed(st, f"manifests/{m['episode_id']}.json")
    assert store.meta_clean(st), "manifests and the deletion log are committed"
    assert review.expire(st, now=common.plus_days(ENDED, 92))["expired_deleted"] == 0, "expiry is idempotent"


def _seal_repo(root: Path, entries: list[dict]) -> Path:
    repo = root / "seals-repo"
    (repo / "seals").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "prereg.md").write_text("# synthetic preregistration\n", encoding="utf-8")
    seal = {"sealed": True, "prereg": "prereg.md", "prereg_sha256": common.sha256_file(repo / "prereg.md"),
            "retain": entries}
    (repo / "seals" / "s1.json").write_text(json.dumps(seal, indent=1) + "\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "seal")
    return repo


def test_seal_extends(root: Path) -> None:
    base = make_stores(root)
    repo = tc.make_repo(root)
    m, i = episode(base, repo)
    review.review_episode(base, m, i)
    ep = m["episode_id"]
    sums = common.sha256_file(base.payloads / ep / "SHA256SUMS")
    seals = _seal_repo(root, [{"episode_id": ep, "sums_sha256": sums, "until": "2027-12-31T00:00:00Z"}])
    st = dataclasses.replace(base, seal_dirs=(seals / "seals",))
    now = common.plus_days(ENDED, 91)
    res = review.expire(st, now=now)
    assert res["extended_by_seal"] == 1 and res["expired_deleted"] == 0, res
    assert (st.payloads / ep).is_dir()
    until = reread(st, m)["retention"]["extended_until"]
    assert common.parse_iso(until) == common.parse_iso("2027-12-31T00:00:00Z"), until
    now_dt = common.parse_iso(now)
    assert review.valid_seal(st, ep, "0" * 64, now_dt) is None, "a seal binds the payload's exact hash"
    assert review.valid_seal(st, ep, sums, common.parse_iso("2028-01-01T00:00:00Z")) is None, "seals lapse"
    (seals / "prereg.md").write_text("# edited after sealing\n", encoding="utf-8")
    assert review.valid_seal(st, ep, sums, now_dt) is None, "an edited preregistration voids the seal"
    git(seals, "checkout", "--", "prereg.md")
    raw = json.loads((seals / "seals" / "s1.json").read_text(encoding="utf-8"))
    raw["retain"][0]["until"] = "2099-01-01T00:00:00Z"
    (seals / "seals" / "s1.json").write_text(json.dumps(raw) + "\n", encoding="utf-8")
    res = review.expire(st, now=now)
    assert res["expired_deleted"] == 1 and not (st.payloads / ep).exists(), "an uncommitted seal edit voids it"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    only = sys.argv[1:]
    os.environ.update({k: v for k, v in tc.GIT_ENV.items() if k.startswith("GIT_")})
    with tempfile.TemporaryDirectory(prefix="ec-review-test-") as tmp:
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

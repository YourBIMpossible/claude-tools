#!/usr/bin/env python3
"""Deterministic review, privacy-scope check, payload and expiry (plan §4.2, §5.4, §6).

Review is a pure function of the manifest, the owner's review file (if any), the start
snapshot and the episode's inputs (prompt text, brief text, CLI/plugin settings); it runs
at episode end and again, idempotently, at the next capture start.

1. Any X1–X7 ``yes`` → **excluded**: the snapshot is deleted (verified, logged).
2. A final link state other than ``linked`` (``ambiguous``, ``mismatch``) → excluded.
3. Any ``unknown`` rule, a pending link, an untrusted boundary, an open episode or
   missing inputs → **held**: the snapshot is kept until it expires; no payload.
4. Otherwise the snapshot must verify against its ``SHA256SUMS`` and the manifest's
   hash (else excluded, ``snapshot_unverified``), and the privacy-scope check runs:
   no path outside the repository in the prompt, brief, settings or snapshot bytes;
   no snapshot path matching a deny glob (built-in list plus ``capture.deny_globs`` in
   the repository's ``.evidence-compiler/config.yaml``); no brief citation outside the
   repository; no attachment. Any failure → excluded, reason ``privacy_scope``.
5. Pass → ``privacy_cleared``; the payload is staged in ``tmp/``, the snapshot moved
   (not copied) into it, and the staged directory renamed to ``payloads/<episode_id>/``.
   An existing payload is never overwritten. ``payload_materialized``.

Payload layout::

    snapshot/        the start snapshot, moved in with its own SHA256SUMS
    prompt.txt       prompt bytes (UTF-8)
    brief.txt        brief bytes as injected (UTF-8)
    settings.json    CLI/plugin settings the caller recorded for the arm environment
    retention.json   ended_at, expires_at (= ended_at + 90 days), policy
    review.json      rule states and privacy-check results
    SHA256SUMS       over everything above

Expiry deletes a snapshot or payload past its expiry unless a valid seal extends it: a
JSON file in a configured seal directory, committed and unmodified in its git
repository, marked ``sealed``, naming a committed preregistration by path and sha256,
and listing ``{episode_id, sums_sha256, until}`` where ``sums_sha256`` equals the hash
of the object's current ``SHA256SUMS``. Deletion is verified and logged; the log and
the manifests are committed in the metadata repository.

Manifests and the metadata repository receive hashes, categories and counts only:
never a path, prompt or brief.
"""
from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import manifest as mf
from common import iso, norm_path, now_utc, parse_iso, plus_days, read_json, sha256_bytes, sha256_file, sha256_text
from store import (Stores, StoreError, commit_meta, log_line, manifest_path, new_tmp_dir, rmtree, verified_delete,
                   verify_sums, write_manifest, write_sums)

RETENTION_DAYS = 90
PRIVACY_CHECKS = ("path_outside_repo", "deny_glob", "external_citation", "attachment")
REVIEW_STATES = ("excluded", "held", "materialized")
CONFIG_REL = (".evidence-compiler", "config.yaml")
BUILTIN_DENY_GLOBS = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc", "_netrc",
    "credentials*", "*secret*", ".git-credentials",
)
PAYLOAD_FILES = ("prompt.txt", "brief.txt", "settings.json", "retention.json", "review.json")

# Absolute paths in free text. Checked against the repository root at the match position,
# so a root containing spaces is compared whole rather than cut at the first space.
_DRIVE = r"(?<![A-Za-z0-9_])[A-Za-z]:/"
_POSIX = r"(?<![\w.~/:-])/(?:home|Users|mnt|tmp|var|etc|opt|root|private|Volumes|srv|media)/"
_BASH_DRIVE = r"(?<![\w.~/:-])/[A-Za-z]/(?=\w)"
ABS_RE = re.compile(f"{_DRIVE}|{_POSIX}|{_BASH_DRIVE}")
UNC_RE = re.compile(r"(?<!\\)\\\\[A-Za-z0-9_.$-]+\\")
HOME_RE = re.compile(r"(?<![\w/])~[\\/]|%USERPROFILE%|\$HOME\b|\$env:USERPROFILE", re.I)
TOKEN_END = set(" \t\r\n'\"`<>|")
TOKEN_RE = re.compile(r"[^\s'\"`<>|()\[\]{},;]+")


@dataclass
class ReviewInputs:
    """What the payload needs beyond the snapshot. Supplied by the end hook (from the
    joined transcript) or by the next start for the same project; never persisted
    outside the payload."""
    repo_root: Path
    prompt_text: str
    brief_text: str
    settings: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------- config

def load_capture_config(repo_root: Path) -> dict[str, Any]:
    """``capture`` section of the repository's ``.evidence-compiler/config.yaml``.

    ``capture: false`` or ``capture: {enabled: false}`` disables capture;
    ``capture: {deny_globs: [...]}`` adds privacy deny globs. A file that cannot be read
    or parsed yields ``error`` (the privacy check then fails closed)."""
    p = repo_root.joinpath(*CONFIG_REL)
    out: dict[str, Any] = {"present": p.is_file(), "enabled": True, "deny_globs": [], "error": None}
    if not p.is_file():
        return out
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        out["error"] = "yaml_unavailable"
        return out
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        out["error"] = f"config_unreadable:{type(exc).__name__}"
        return out
    if not isinstance(data, dict):
        out["error"] = "config_not_a_mapping"
        return out
    cap = data.get("capture")
    if cap is False:
        out["enabled"] = False
    elif isinstance(cap, dict):
        out["enabled"] = cap.get("enabled", True) is not False
        globs = cap.get("deny_globs") or []
        if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
            out["error"] = "deny_globs_not_a_list_of_strings"
        else:
            out["deny_globs"] = list(globs)
    elif cap is not None and cap is not True:
        out["error"] = "capture_section_invalid"
    return out


# ---------------------------------------------------------------------- privacy scope

def _root_key(repo_root: Path) -> str:
    key = norm_path(repo_root)
    return key.lower() if os.name == "nt" else key


def _normalize(text: str) -> str:
    """Backslash runs (``\\`` and JSON-escaped ``\\\\``) become ``/``; case-folded on Windows."""
    out = re.sub(r"\\+", "/", text)
    return out.lower() if os.name == "nt" else out


def _escapes(tail: str) -> bool:
    """True when ``root + tail`` leaves the root through ``..`` segments."""
    token = ""
    for ch in tail:
        if ch in TOKEN_END:
            break
        token += ch
    resolved = posixpath.normpath("r" + token)
    return not (resolved == "r" or resolved.startswith("r/"))


def outside_paths(text: str, repo_root: Path) -> list[str]:
    """Absolute paths in ``text`` that are not inside ``repo_root`` (as path hashes)."""
    root = _root_key(repo_root)
    hits: list[str] = []
    for m in UNC_RE.finditer(text):
        hits.append(sha256_bytes(m.group(0).encode("utf-8", "surrogatepass")))
    for m in HOME_RE.finditer(text):
        hits.append(sha256_bytes(m.group(0).encode("utf-8", "surrogatepass")))
    norm = _normalize(text)
    for m in ABS_RE.finditer(norm):
        rest = norm[m.start():]
        if re.match(r"/[a-z]/", rest, re.I) and not re.match(_POSIX[len(r"(?<![\w.~/:-])"):], rest):
            rest = rest[1] + ":" + rest[2:]  # Git Bash /c/... form
        inside = rest.startswith(root) and (len(rest) == len(root) or rest[len(root)] == "/"
                                            or rest[len(root)] in TOKEN_END or rest[len(root)] in ".,;:)]}")
        if inside and _escapes(rest[len(root):]):
            inside = False
        if not inside:
            token = TOKEN_RE.match(rest)
            hits.append(sha256_bytes((token.group(0) if token else rest[:64]).encode("utf-8", "surrogatepass")))
    return hits


def escaping_citations(brief: str) -> list[str]:
    """Relative citations in the brief that climb out of the repository with ``..``."""
    hits = []
    for token in TOKEN_RE.findall(brief.replace("\\", "/")):
        if "../" in token or token.endswith("/..") or token == "..":
            resolved = posixpath.normpath("r/" + token.lstrip("/"))
            if not (resolved == "r" or resolved.startswith("r/")):
                hits.append(sha256_bytes(token.encode("utf-8", "surrogatepass")))
    return hits


def _stage_paths_with_blobs(snapshot: Path) -> list[str]:
    listing = snapshot / "ls-files-stage.z"
    blobs = snapshot / "blobs"
    have = {p.name for p in blobs.iterdir()} if blobs.is_dir() else set()
    out = []
    if listing.is_file() and have:
        for rec in listing.read_bytes().split(b"\0"):
            if not rec or b"\t" not in rec:
                continue
            meta, path = rec.split(b"\t", 1)
            parts = meta.decode("ascii", "replace").split(" ")
            if len(parts) == 3 and parts[1] in have:
                out.append(path.decode("utf-8", "surrogateescape"))
    return out


def snapshot_content_paths(snapshot: Path) -> list[str]:
    """Repository-relative paths whose bytes the snapshot carries."""
    paths: list[str] = []
    for name in ("untracked.json", "worktree.json"):
        f = snapshot / name
        if f.is_file():
            for e in json.loads(f.read_text(encoding="utf-8")):
                if not e.get("deleted"):
                    paths.append(e["path"])
    paths += _stage_paths_with_blobs(snapshot)
    return sorted(set(paths))


def deny_match(rel: str, globs: list[str]) -> str | None:
    low = rel.replace("\\", "/").lower()
    base = low.rsplit("/", 1)[-1]
    for g in globs:
        gl = g.replace("\\", "/").lower().lstrip("/")
        # fnmatch has no ``**``: ``**/x`` only matches through its ``/``, so a root-level
        # ``x`` needs the bare form too (``*`` already crosses ``/`` for deeper paths).
        forms = (gl, gl[3:]) if gl.startswith("**/") and len(gl) > 3 else (gl,)
        for form in forms:
            if form.endswith("/**") or form.endswith("/"):
                prefix = form.rstrip("*").rstrip("/") + "/"
                if low.startswith(prefix) or ("/" + prefix) in ("/" + low):
                    return g
            if fnmatch.fnmatchcase(low, form) or fnmatch.fnmatchcase(base, form):
                return g
    return None


def privacy_check(m: dict[str, Any], snapshot: Path, inputs: ReviewInputs) -> dict[str, Any]:
    """The four privacy-scope checks. Findings carry a source label and a hash only."""
    findings: list[dict[str, Any]] = []
    root = inputs.repo_root

    def add(check: str, source: str, digest: str | None = None, **extra: Any) -> None:
        findings.append({"check": check, "source": source, "sha256": digest, **extra})

    # Settings are scanned as they are written to the payload (``ensure_ascii=False``): the
    # default ``\uXXXX`` escapes would turn a non-ASCII repository root into a path that no
    # longer starts with the root key and is flagged as outside.
    settings_text = json.dumps(inputs.settings, sort_keys=True, ensure_ascii=False)
    for label, text in (("prompt", inputs.prompt_text), ("settings", settings_text)):
        for h in outside_paths(text, root):
            add("path_outside_repo", label, h)
    for h in outside_paths(inputs.brief_text, root):
        add("external_citation", "brief", h)
    for h in escaping_citations(inputs.brief_text):
        add("external_citation", "brief", h)
    for f in sorted(p for p in snapshot.rglob("*") if p.is_file()):
        rel = f.relative_to(snapshot).as_posix()
        text = f.read_bytes().decode("utf-8", "replace")
        for h in outside_paths(text, root):
            add("path_outside_repo", "snapshot/" + rel, h)

    cfg = load_capture_config(root)
    if cfg["error"]:
        add("deny_glob", "config", None, error=cfg["error"])
    globs = [*BUILTIN_DENY_GLOBS, *cfg["deny_globs"]]
    for rel in snapshot_content_paths(snapshot):
        g = deny_match(rel, globs)
        if g is not None:
            add("deny_glob", "snapshot", sha256_bytes(rel.encode("utf-8", "surrogateescape")),
                glob_source="builtin" if g in BUILTIN_DENY_GLOBS else "config")

    if m["prompt"].get("has_attachment") is not False:
        add("attachment", "prompt", None, has_attachment=m["prompt"].get("has_attachment"))
    if m["deps"].get("attachment", {}).get("observed"):
        add("attachment", "screen", None)

    by_check = {c: sum(1 for f in findings if f["check"] == c) for c in PRIVACY_CHECKS}
    return {"passed": not findings, "by_check": by_check, "findings": findings[:200],
            "findings_total": len(findings)}


# ---------------------------------------------------------------------- owner review

def owner_review_path(stores: Stores, episode: str) -> Path:
    return stores.meta / "review" / f"{episode}.json"


def load_owner_review(stores: Stores, episode: str) -> dict[str, Any] | None:
    """The owner's recorded review for one episode, or None. A file naming another
    episode, or unreadable, is ignored and logged."""
    p = owner_review_path(stores, episode)
    if not p.is_file():
        return None
    try:
        rec = read_json(p)
    except (OSError, ValueError):
        log_line(stores, f"owner review unreadable: {episode}")
        return None
    if not isinstance(rec, dict) or rec.get("episode_id") != episode:
        log_line(stores, f"owner review names another episode: {episode}")
        return None
    return rec


# ---------------------------------------------------------------------- decision

def _snapshot_state(stores: Stores, m: dict[str, Any]) -> str | None:
    """None when the snapshot is present and verified against the manifest."""
    d = stores.snapshots / m["episode_id"]
    if not d.is_dir():
        return "snapshot_missing"
    if (d / "INCOMPLETE").exists() or verify_sums(d):
        return "snapshot_unverified"
    want = m["start_snapshot"].get("sums_sha256")
    if not want or not (d / "SHA256SUMS").is_file() or sha256_file(d / "SHA256SUMS") != want:
        return "snapshot_unverified"
    return None


def _inputs_problem(m: dict[str, Any], inputs: ReviewInputs) -> str | None:
    if sha256_bytes(norm_path(inputs.repo_root).encode()) != m["repo"].get("root_sha256"):
        return "inputs_mismatch:repo_root"
    if sha256_text(inputs.prompt_text) != m["prompt"]["sha256"]:
        return "inputs_mismatch:prompt"
    if not m["link"].get("brief_sha256"):
        return "brief_unavailable"
    if sha256_text(inputs.brief_text) != m["link"]["brief_sha256"]:
        return "inputs_mismatch:brief"
    return None


def decide(stores: Stores, m: dict[str, Any], inputs: ReviewInputs | None) -> tuple[str, str, dict[str, Any] | None]:
    """``(state, reason, privacy)`` where state is ``excluded``, ``held`` or ``cleared``.

    ``m`` must already carry refreshed exclusions. Pure apart from reading the snapshot
    and the repository config."""
    ex = m["exclusions"]
    yes = [e["rule"] for e in ex if e["state"] == "yes"]
    if yes:
        return "excluded", "rules:" + ",".join(yes), None
    link = m["link"].get("state")
    if link in ("ambiguous", "mismatch"):
        return "excluded", f"link_{link}", None
    unknown = [e["rule"] for e in ex if e["state"] == "unknown"]
    if unknown:
        return "held", "unknown:" + ",".join(unknown), None
    if link != "linked":
        return "held", "link_pending", None
    if not m.get("boundary", {}).get("trusted"):
        return "held", f"boundary_untrusted:{m.get('boundary', {}).get('reason')}", None
    if not m.get("ended_at"):
        return "held", "episode_open", None
    snap = _snapshot_state(stores, m)
    if snap:
        return "excluded", snap, None
    if inputs is None:
        return "held", "inputs_unavailable", None
    problem = _inputs_problem(m, inputs)
    if problem:
        return "held", problem, None
    privacy = privacy_check(m, stores.snapshots / m["episode_id"], inputs)
    if not privacy["passed"]:
        return "excluded", "privacy_scope", privacy
    return "cleared", "privacy_cleared", privacy


# ---------------------------------------------------------------------- payload

def materialize_payload(stores: Stores, m: dict[str, Any], inputs: ReviewInputs, privacy: dict[str, Any],
                        now: str) -> dict[str, Any]:
    """Stage the payload in ``tmp/``, moving the snapshot in, then rename into place.

    Never overwrites an existing payload. On any failure the snapshot is moved back and
    the staging directory removed, so the episode is exactly as before."""
    ep = m["episode_id"]
    dest = stores.payloads / ep
    src = stores.snapshots / ep
    if dest.exists():
        raise StoreError(f"payload {ep} exists; never overwritten")
    expires_at = plus_days(m["ended_at"], RETENTION_DAYS)
    stage: Path | None = new_tmp_dir(stores, ep)
    moved = False
    try:
        os.replace(src, stage / "snapshot")
        moved = True
        (stage / "prompt.txt").write_bytes(inputs.prompt_text.encode("utf-8", "surrogatepass"))
        (stage / "brief.txt").write_bytes(inputs.brief_text.encode("utf-8", "surrogatepass"))
        _write_json(stage / "settings.json", inputs.settings)
        _write_json(stage / "retention.json", {
            "episode_id": ep, "ended_at": m["ended_at"], "expires_at": expires_at, "retention_days": RETENTION_DAYS,
            "policy": "deleted at expires_at unless a sealed preregistration names this episode and the hash of "
                      "this payload's SHA256SUMS"})
        _write_json(stage / "review.json", {
            "episode_id": ep, "reviewed_at": now, "exclusions": m["exclusions"], "privacy": privacy,
            "snapshot_sums_sha256": m["start_snapshot"].get("sums_sha256"),
            "prompt_sha256": m["prompt"]["sha256"], "brief_sha256": m["link"].get("brief_sha256")})
        write_sums(stage)
        bad = verify_sums(stage)
        if bad:
            raise StoreError(f"payload {ep} failed verification: {bad[:5]}")
        if dest.exists():
            raise StoreError(f"payload {ep} exists; never overwritten")
        os.replace(stage, dest)
        stage = None
    except BaseException:
        if moved and stage is not None and (stage / "snapshot").is_dir() and not src.exists():
            os.replace(stage / "snapshot", src)
        raise
    finally:
        if stage is not None:
            rmtree(stage)
    return {"expires_at": expires_at, "payload_sums_sha256": sha256_file(dest / "SHA256SUMS")}


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8",
                    newline="\n")


def verify_payload(stores: Stores, episode: str) -> list[str]:
    """Problems with a payload before use: missing files or checksum mismatches."""
    d = stores.payloads / episode
    if not d.is_dir():
        return ["payload missing"]
    bad = [f"missing:{n}" for n in PAYLOAD_FILES if not (d / n).is_file()]
    bad += verify_sums(d)
    if not (d / "snapshot" / "SHA256SUMS").is_file():
        bad.append("snapshot missing")
    else:
        bad += ["snapshot/" + b for b in verify_sums(d / "snapshot")]
    return bad


# ---------------------------------------------------------------------- review runner

def _snapshot_expiry(m: dict[str, Any]) -> str | None:
    base = m.get("ended_at") or m.get("started_at")
    return plus_days(base, RETENTION_DAYS) if base else None


def review_episode(stores: Stores, m: dict[str, Any], inputs: ReviewInputs | None = None, *,
                   now: str | None = None, commit: bool = True) -> dict[str, Any]:
    """Review one episode and act on the decision. Idempotent: a materialized payload is
    never revisited; an unchanged decision writes nothing."""
    now = now or iso(now_utc())
    ep = m["episode_id"]
    prev = dict(m.get("review") or {})
    out: dict[str, Any] = {"episode_id": ep, "state": prev.get("state"), "reason": prev.get("reason"),
                           "changed": False, "deletion": None, "error": None}
    if prev.get("state") == "materialized":
        out["state"] = "materialized"
        return out
    owner = load_owner_review(stores, ep)
    mf.refresh(m, (owner or {}).get("rules"))
    retention = dict(m.get("retention") or {})
    touched = [manifest_path(stores, ep)]
    privacy: dict[str, Any] | None = None

    if (stores.payloads / ep).exists():
        # A payload the manifest does not record: a run interrupted between the rename
        # into ``payloads/`` and the manifest write. Adopt it only if it verifies and
        # names this episode; otherwise hold the episode and leave both untouched.
        state, reason = _adopt_payload(stores, ep, retention)
        m["funnel"]["privacy_cleared"] = state == "materialized"
        m["funnel"]["payload_materialized"] = state == "materialized"
    else:
        state, reason, privacy = decide(stores, m, inputs)

    if reason in ("payload_recovered", "payload_conflict"):
        pass
    elif state == "excluded":
        m["funnel"]["privacy_cleared"] = False
        m["funnel"]["payload_materialized"] = False
        if (stores.snapshots / ep).exists():
            try:
                out["deletion"] = verified_delete(stores, "snapshots", ep, f"excluded:{reason}")
            except StoreError as exc:
                out["error"] = str(exc)
                log_line(stores, f"review {ep}: snapshot deletion not verified: {exc}")
            touched.append(stores.meta / "DELETIONS.log")
            retention.update(deleted_at=now, deleted_kind="snapshots",
                             deletion_verified=bool(out["deletion"] and out["deletion"]["verified"]))
    elif state == "held":
        m["funnel"]["privacy_cleared"] = False
        m["funnel"]["payload_materialized"] = False
        retention.setdefault("expires_at", _snapshot_expiry(m))
    else:
        m["funnel"]["privacy_cleared"] = True
        try:
            if inputs is None or privacy is None:  # decide() clears only with both
                raise StoreError("cleared without inputs or privacy result")
            rec = materialize_payload(stores, m, inputs, privacy, now)
            m["funnel"]["payload_materialized"] = True
            retention.update(rec, extended_until=None, deleted_at=None)
            state = "materialized"
        except (OSError, StoreError) as exc:
            m["funnel"]["payload_materialized"] = False
            state, reason = "held", f"payload_write_failed:{type(exc).__name__}"
            out["error"] = str(exc)
            log_line(stores, f"review {ep}: payload not written: {exc}")
    m["funnel"] = mf.compute_funnel(m)
    out.update(state=state, reason=reason)

    changed = (state, reason) != (prev.get("state"), prev.get("reason")) or out["deletion"] is not None
    if not changed:
        return out
    m["review"] = {"state": state, "reason": reason, "reviewed_at": now, "owner_review": owner is not None,
                   "privacy": None if privacy is None else {k: privacy[k] for k in ("passed", "by_check",
                                                                                    "findings_total")}}
    m["retention"] = retention
    write_manifest(stores, m)
    out["changed"] = True
    log_line(stores, f"review {ep}: {state} ({reason})")
    if commit:
        commit_meta(stores, touched, f"capture review {ep}: {state}")
    return out


def _adopt_payload(stores: Stores, ep: str, retention: dict[str, Any]) -> tuple[str, str]:
    d = stores.payloads / ep
    bad = verify_payload(stores, ep)
    names, kept = None, {}
    if not bad:
        try:
            names = read_json(d / "review.json").get("episode_id")
            kept = read_json(d / "retention.json")
        except (OSError, ValueError, AttributeError):
            names, kept = None, {}
    if bad or names != ep or not isinstance(kept, dict) or not kept.get("expires_at"):
        log_line(stores, f"review {ep}: payload present but not adoptable ({(bad or ['episode mismatch'])[:3]})")
        return "held", "payload_conflict"
    retention.update(expires_at=kept["expires_at"], payload_sums_sha256=sha256_file(d / "SHA256SUMS"),
                     extended_until=None, deleted_at=None)
    log_line(stores, f"review {ep}: interrupted payload adopted")
    return "materialized", "payload_recovered"


# ---------------------------------------------------------------------- seals and expiry

def _git_clean_tracked(path: Path) -> bool:
    cwd = str(path.parent)
    tracked = subprocess.run(["git", "-C", cwd, "ls-files", "--error-unmatch", "--", path.name], capture_output=True)
    if tracked.returncode != 0:
        return False
    same = subprocess.run(["git", "-C", cwd, "diff", "--quiet", "HEAD", "--", path.name], capture_output=True)
    return same.returncode == 0


def _git_top(path: Path) -> Path | None:
    proc = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None


def valid_seal(stores: Stores, episode: str, sums_sha256: str | None, now: datetime) -> dict[str, Any] | None:
    """The seal that extends ``episode`` past now, or None. See the module docstring."""
    if not sums_sha256:
        return None
    for d in stores.seal_dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            try:
                rec = read_json(f)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("sealed") is not True or not _git_clean_tracked(f):
                continue
            top = _git_top(f.parent)
            prereg = rec.get("prereg")
            if top is None or not isinstance(prereg, str):
                continue
            pfile = (top / prereg).resolve()
            try:
                pfile.relative_to(top.resolve())
            except ValueError:
                continue
            if not pfile.is_file() or not _git_clean_tracked(pfile) or sha256_file(pfile) != rec.get("prereg_sha256"):
                continue
            for entry in rec.get("retain") or []:
                if not isinstance(entry, dict):
                    continue
                until = parse_iso(entry.get("until"))
                if entry.get("episode_id") == episode and entry.get("sums_sha256") == sums_sha256 \
                        and until is not None and until > now:
                    return {"seal": f.name, "until": iso(until), "prereg_sha256": rec["prereg_sha256"]}
    return None


def _expiry_of(kind: str, d: Path, m: dict[str, Any] | None) -> datetime | None:
    if kind == "payloads":
        try:
            dt = parse_iso(read_json(d / "retention.json").get("expires_at"))
            if dt is not None:
                return dt
        except (OSError, ValueError, AttributeError):
            pass
        return parse_iso(((m or {}).get("retention") or {}).get("expires_at"))
    if m is not None:
        return parse_iso(_snapshot_expiry(m))
    try:  # a snapshot without a manifest: dated by the directory itself
        return datetime.fromtimestamp(d.stat().st_mtime, tz=now_utc().tzinfo) + timedelta(days=RETENTION_DAYS)
    except OSError:
        return None


def _content_due(stores: Stores, now_dt: datetime) -> Iterator[tuple[str, str, Path, dict[str, Any] | None,
                                                                   datetime | None, dict[str, Any] | None]]:
    """Every snapshot and payload that is undated (expiry None) or past its expiry, with
    its manifest and any valid seal: ``(kind, episode, manifest path, manifest, expiry, seal)``."""
    for kind in ("payloads", "snapshots"):
        base = stores.content / kind
        if not base.is_dir():
            continue
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            ep = d.name
            mp = manifest_path(stores, ep)
            m = None
            if mp.is_file():
                try:
                    m = read_json(mp)
                except (OSError, ValueError):
                    m = None
            expires = _expiry_of(kind, d, m)
            if expires is not None and now_dt < expires:
                continue
            seal = None
            if expires is not None:
                sums = d / "SHA256SUMS"
                seal = valid_seal(stores, ep, sha256_file(sums) if sums.is_file() else None, now_dt)
            yield kind, ep, mp, m, expires, seal


def overdue(stores: Stores, *, now: str | None = None) -> list[dict[str, str]]:
    """Read-only: snapshots and payloads past expiry with no valid seal. Empty after a
    successful expiry pass; anything listed here is retention the store still holds."""
    now_dt = parse_iso(now or iso(now_utc()))
    assert now_dt is not None
    return [{"kind": kind, "episode_id": ep, "expired_at": iso(expires)}
            for kind, ep, _mp, _m, expires, seal in _content_due(stores, now_dt)
            if expires is not None and seal is None]


def expire(stores: Stores, *, now: str | None = None, commit: bool = True) -> dict[str, int]:
    """Delete every snapshot and payload past its expiry without a valid seal; verified,
    logged in ``DELETIONS.log`` and committed with the updated manifests."""
    now_s = now or iso(now_utc())
    now_dt = parse_iso(now_s)
    assert now_dt is not None
    counts = {"expired_deleted": 0, "deletions_verified": 0, "extended_by_seal": 0, "undated": 0, "errors": 0}
    touched: list[Path] = []
    for kind, ep, mp, m, expires, seal in _content_due(stores, now_dt):
        if expires is None:
            counts["undated"] += 1
            log_line(stores, f"expire {kind}/{ep}: no expiry date; kept and flagged")
            continue
        if seal is not None:
            counts["extended_by_seal"] += 1
            if m is not None and (m.get("retention") or {}).get("extended_until") != seal["until"]:
                m.setdefault("retention", {}).update(extended_until=seal["until"], extended_by=seal["seal"])
                write_manifest(stores, m)
                touched.append(mp)
            continue
        try:
            rec = verified_delete(stores, kind, ep, "expired")
        except StoreError as exc:
            counts["errors"] += 1
            log_line(stores, f"expire {kind}/{ep}: {exc}")
            touched.append(stores.meta / "DELETIONS.log")
            continue
        counts["expired_deleted"] += 1
        counts["deletions_verified"] += int(rec["verified"])
        touched.append(stores.meta / "DELETIONS.log")
        if m is not None:
            m.setdefault("retention", {}).update(deleted_at=rec["deleted_at"], deleted_kind=kind,
                                                 deletion_verified=rec["verified"])
            write_manifest(stores, m)
            touched.append(mp)
    if touched and commit:
        commit_meta(stores, sorted(set(touched)), f"capture expire: {counts['expired_deleted']} deleted")
    return counts

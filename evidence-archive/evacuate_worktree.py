#!/usr/bin/env python3
"""Copy Evidence Compiler packet files out of a git worktree before it is cleaned up.

A worktree's `.evidence-compiler/packets/` is often the only copy of its packets, and
removing the worktree deletes them. This helper copies them into a local archive first
and writes a receipt that says whether the worktree is safe to remove.

    evacuate_worktree.py evacuate <worktree> --archive <dir>            # dry run (default)
    evacuate_worktree.py evacuate <worktree> --archive <dir> --apply    # copy + verify + receipt
    evacuate_worktree.py verify <receipt.json>                          # re-hash archived copies

`--archive` falls back to the EVIDENCE_ARCHIVE environment variable.

Archive layout:
    packets/<packet_id>.json                       one byte-exact copy per packet id
    quarantine/<worktree>/<sha12>__<filename>      files that are not valid packets, byte-exact
    receipts/<utc>__<worktree>.json                one receipt per --apply run

Guarantees: it never deletes or modifies source files, never removes worktrees, never
overwrites an archived file, never touches transcripts, and never talks to a network or
git remote. It refuses an archive inside the worktree, or inside a git repo that has a
remote. Only an --apply run whose every file is archived and verified reports
`safe_to_remove: true`; a dry run never does.

Exit codes: 0 = clean (apply: safe to remove; dry run: no blocking issue predicted;
verify: all copies match), 1 = blocking issue, 2 = usage or refusal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TOOL_VERSION = "1.0.0"
PACKETS_REL = Path(".evidence-compiler") / "packets"
PACKET_ID_RE = re.compile(r"^ep_[0-9a-f]{8,64}$")

# Per-file actions. Anything in BLOCKING prevents safe_to_remove.
COPIED, DUPLICATE, QUARANTINED = "copied", "duplicate", "quarantined"
WOULD_COPY, WOULD_QUARANTINE = "would_copy", "would_quarantine"  # dry run only
CONFLICT, ERROR, UNSUPPORTED = "conflict", "error", "unsupported"
BLOCKING = {CONFLICT, ERROR, UNSUPPORTED}


class Refusal(Exception):
    """Preconditions not met; nothing was written."""


@dataclass
class FileResult:
    source: str
    action: str
    sha256: str | None = None
    size: int | None = None
    packet_id: str | None = None
    archive_path: str | None = None
    verified: bool = False
    detail: str = ""


@dataclass
class Report:
    tool_version: str
    mode: str
    started_at: str
    worktree: str
    packets_dir: str
    archive: str
    files: list[FileResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    safe_to_remove: bool = False
    receipt: str | None = None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.files:
            out[f.action] = out.get(f.action, 0) + 1
        return out


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", path.name).strip("_") or "worktree"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _git_repo_with_remote(path: Path) -> Path | None:
    """Nearest enclosing git repo that has a remote, or None."""
    probe = path
    while not probe.exists():
        probe = probe.parent
    for candidate in (probe, *probe.parents):
        if (candidate / ".git").exists():
            res = subprocess.run(["git", "-C", str(candidate), "remote"],
                                 capture_output=True, text=True, check=False)
            if res.returncode != 0:
                raise Refusal(f"cannot read git remotes for archive repo: {res.stderr.strip()}")
            return candidate if res.stdout.strip() else None
    return None


def check_preconditions(worktree: Path, archive: Path) -> None:
    if not worktree.is_dir():
        raise Refusal("worktree does not exist or is not a directory")
    if _is_within(archive, worktree):
        raise Refusal("archive is inside the worktree; removing the worktree would delete it")
    if _is_within(worktree, archive):
        raise Refusal("worktree is inside the archive")
    if archive.exists() and not archive.is_dir():
        raise Refusal("archive path exists and is not a directory")
    remote_repo = _git_repo_with_remote(archive)
    if remote_repo is not None:
        raise Refusal("archive is inside a git repository that has a remote")


def classify(raw: bytes, name: str) -> tuple[str | None, str]:
    """Return (packet_id, "") for a valid packet, else (None, why it is not one)."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"malformed JSON: {type(exc).__name__}"
    if not isinstance(doc, dict):
        return None, "JSON root is not an object"
    pid = doc.get("packet_id")
    if not isinstance(pid, str) or not PACKET_ID_RE.match(pid):
        return None, "missing or invalid packet_id"
    if pid not in name:
        return None, "filename does not contain its packet_id"
    return pid, ""


def _write_exclusive(target: Path, data: bytes) -> None:
    """Create target with data; fail if it already exists. Never overwrites."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "xb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _archive_one(src: Path, raw: bytes, digest: str, target: Path, apply: bool,
                 result: FileResult) -> None:
    """Copy (or plan to copy) raw to target, deduplicating against an existing copy."""
    result.archive_path = target.as_posix()
    if target.exists():
        existing = sha256_bytes(target.read_bytes())
        if existing == digest:
            result.action = DUPLICATE if result.action != QUARANTINED else QUARANTINED
            result.detail = (result.detail + "; " if result.detail else "") + "already archived, identical"
            result.verified = True
        else:
            result.action = CONFLICT
            result.detail = "archive already holds different content for this id; not overwritten"
        return
    if not apply:
        return
    try:
        _write_exclusive(target, raw)
    except OSError as exc:
        result.action, result.detail = ERROR, f"copy failed: {type(exc).__name__}: {exc}"
        return
    if sha256_bytes(target.read_bytes()) != digest:
        result.action, result.detail = ERROR, "archived copy does not match source hash"
        return
    if sha256_bytes(src.read_bytes()) != digest:
        result.action, result.detail = ERROR, "source changed during evacuation"
        return
    result.verified = True


def evacuate(worktree: Path, archive: Path, apply: bool) -> Report:
    worktree, archive = worktree.resolve(), archive.resolve()
    check_preconditions(worktree, archive)
    packets_dir = worktree / PACKETS_REL
    now = datetime.now(timezone.utc)
    report = Report(TOOL_VERSION, "apply" if apply else "dry-run",
                    now.isoformat(timespec="seconds"), worktree.as_posix(),
                    packets_dir.as_posix(), archive.as_posix())

    if not packets_dir.exists():
        report.reasons.append("no packet store in this worktree; nothing to evacuate")
    elif not packets_dir.is_dir() or packets_dir.is_symlink():
        report.files.append(FileResult(PACKETS_REL.as_posix(), UNSUPPORTED,
                                       detail="packet store is not a plain directory"))
    else:
        before = sorted(p.name for p in packets_dir.iterdir())
        seen: dict[str, str] = {}  # packet_id -> sha256 within this run
        for name in before:
            src = packets_dir / name
            rel = (PACKETS_REL / name).as_posix()
            if src.is_symlink() or not src.is_file():
                report.files.append(FileResult(rel, UNSUPPORTED,
                                               detail="not a regular file; not evacuated"))
                continue
            try:
                raw = src.read_bytes()
            except OSError as exc:
                report.files.append(FileResult(rel, ERROR, detail=f"unreadable: {exc}"))
                continue
            digest = sha256_bytes(raw)
            res = FileResult(rel, COPIED, sha256=digest, size=len(raw))
            pid, why = classify(raw, name)
            if pid is None:
                res.action, res.detail = QUARANTINED, why
                target = archive / "quarantine" / _slug(worktree) / f"{digest[:12]}__{name}"
            else:
                res.packet_id = pid
                if pid in seen:
                    same = seen[pid] == digest
                    res.action = DUPLICATE if same else CONFLICT
                    res.detail = ("same packet repeated in this store" if same
                                  else "two different files in this store claim this packet_id")
                    res.verified = same and apply
                    res.archive_path = (archive / "packets" / f"{pid}.json").as_posix()
                    report.files.append(res)
                    continue
                seen[pid] = digest
                target = archive / "packets" / f"{pid}.json"
            _archive_one(src, raw, digest, target, apply, res)
            if not apply and not target.exists() and res.action in (COPIED, QUARANTINED):
                res.action = WOULD_COPY if res.action == COPIED else WOULD_QUARANTINE
            report.files.append(res)
        after = sorted(p.name for p in packets_dir.iterdir())
        if after != before:
            report.reasons.append("packet store changed during the run; re-run before removing")

    blocking = [f for f in report.files if f.action in BLOCKING]
    if blocking:
        report.reasons.append(f"{len(blocking)} file(s) blocked: {sorted({f.action for f in blocking})}")
    if apply:
        unverified = [f for f in report.files if f.action not in BLOCKING and not f.verified]
        if unverified:
            report.reasons.append(f"{len(unverified)} file(s) not verified")
        changed = any("changed during the run" in r for r in report.reasons)
        report.safe_to_remove = not blocking and not unverified and not changed
        report.receipt = write_receipt(report, archive, now)
    else:
        report.reasons.append("dry run: nothing copied; the worktree is NOT yet safe to remove")
    return report


def write_receipt(report: Report, archive: Path, now: datetime) -> str:
    receipts = archive / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    stem = f"{now.strftime('%Y-%m-%dT%H-%M-%S.%fZ')}__{_slug(Path(report.worktree))}"
    target = receipts / f"{stem}.json"
    report.receipt = target.as_posix()
    body = {**asdict(report), "counts": report.counts()}
    _write_exclusive(target, (json.dumps(body, indent=1) + "\n").encode("utf-8"))
    return target.as_posix()


def verify_receipt(receipt: Path) -> tuple[bool, list[str]]:
    """Re-hash every archived copy named in a receipt against its recorded sha256."""
    doc = json.loads(receipt.read_text(encoding="utf-8"))
    problems: list[str] = []
    checked = 0
    for f in doc.get("files", []):
        if f["action"] in BLOCKING or not f.get("archive_path"):
            continue
        checked += 1
        path = Path(f["archive_path"])
        if not path.is_file():
            problems.append(f"missing: {f['source']}")
        elif sha256_bytes(path.read_bytes()) != f["sha256"]:
            problems.append(f"hash mismatch: {f['source']}")
    if not doc.get("safe_to_remove"):
        problems.append("receipt does not record safe_to_remove")
    return not problems, [f"checked {checked} archived file(s)", *problems]


def _print_report(report: Report) -> None:
    print(f"{report.mode}: {report.worktree}")
    print(f"  archive: {report.archive}")
    print(f"  files: {len(report.files)} {report.counts()}")
    for f in report.files:
        if f.action in BLOCKING or f.action in (QUARANTINED, WOULD_QUARANTINE):
            print(f"  {f.action}: {f.source} ({f.detail})")
    for r in report.reasons:
        print(f"  note: {r}")
    if report.receipt:
        print(f"  receipt: {report.receipt}")
    if report.mode == "apply":
        print(f"  safe_to_remove: {str(report.safe_to_remove).lower()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evacuate", help="copy a worktree's packet store into the archive")
    ev.add_argument("worktree", type=Path)
    ev.add_argument("--archive", type=Path, default=os.environ.get("EVIDENCE_ARCHIVE"))
    ev.add_argument("--apply", action="store_true", help="copy for real (default: dry run)")
    vr = sub.add_parser("verify", help="re-hash the archived copies listed in a receipt")
    vr.add_argument("receipt", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "verify":
        try:
            ok, lines = verify_receipt(args.receipt)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            print(f"REFUSED: unreadable receipt: {exc}", file=sys.stderr)
            return 2
        for line in lines:
            print(f"  {line}")
        print("verify: OK" if ok else "verify: FAILED")
        return 0 if ok else 1

    if args.archive is None:
        print("REFUSED: pass --archive or set EVIDENCE_ARCHIVE", file=sys.stderr)
        return 2
    try:
        report = evacuate(args.worktree, Path(args.archive), args.apply)
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    _print_report(report)
    if report.mode == "apply":
        return 0 if report.safe_to_remove else 1
    return 1 if any(f.action in BLOCKING for f in report.files) else 0


if __name__ == "__main__":
    sys.exit(main())

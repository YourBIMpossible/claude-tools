"""Regression tests for SkillTargets.ps1 target discovery (SA11-CT-8).

A skill folder the watch cannot read must surface as a shortfall, never shrink the target set without a trace:
a smaller set scans CLEAN. The shipped Get-SkillTargets is dot-sourced verbatim under the shell named by
GRAPHIFY_TEST_SHELL (default pwsh), against synthetic roots in a temp dir. Read access is denied to one folder per
root for the duration of a case (icacls deny on Windows, mode 000 elsewhere) and restored afterwards.

Run: python skillspector/test_skill_targets.py
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

SCRIPT = Path(__file__).with_name("SkillTargets.ps1")

DRIVER = r"""
$ErrorActionPreference = 'Stop'
. '{script}'
$shortfall = [System.Collections.Generic.List[string]]::new()
$targets = @(Get-SkillTargets -SkillsRoot '{skills}' -PluginCache '{cache}' -Shortfall $shortfall 3>$null)
[pscustomobject]@{{ names = @($targets | ForEach-Object {{ "$($_.Scope)/$($_.Name)" }}); shortfall = @($shortfall) }} |
    ConvertTo-Json -Compress -Depth 3
"""

results: list[tuple[str, bool]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond))
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" -- {detail}" if detail and not cond else ""))


def skill(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    return folder


def current_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True).stdout
    return out.strip().split(",")[-1].strip('"')


@contextlib.contextmanager
def unreadable(folder: Path) -> Iterator[None]:
    """Deny this user read access to ``folder`` for the block, then restore it."""
    if os.name == "nt":
        sid = current_sid()
        # Data and attribute reads only: READ_CONTROL, WRITE_DAC and SYNCHRONIZE stay granted, so the
        # deny can always be removed again (a broad (RX) deny also blocks opening the folder to undo it).
        subprocess.run(["icacls", str(folder), "/deny", f"*{sid}:(OI)(CI)(RD,RA,REA,X)"], capture_output=True,
                       check=True)
        try:
            yield
        finally:
            subprocess.run(["icacls", str(folder), "/remove:d", f"*{sid}"], capture_output=True, check=True)
    else:
        folder.chmod(0)
        try:
            yield
        finally:
            folder.chmod(0o700)


def denied(folder: Path) -> bool:
    try:
        os.listdir(folder)
    except PermissionError:
        return True
    return False


def discover(shell: str, skills: Path, cache: Path) -> dict:
    driver = DRIVER.format(script=SCRIPT, skills=skills, cache=cache)
    proc = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", driver],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"{shell} rc {proc.returncode}: {proc.stderr.strip()[:400]}")
    doc = json.loads(proc.stdout)
    # PowerShell 5.1 serialises a one-element array as a scalar and an empty one as null
    return {k: [] if doc.get(k) is None else doc[k] if isinstance(doc[k], list) else [doc[k]]
            for k in ("names", "shortfall")}


def main() -> int:
    shell = os.environ.get("GRAPHIFY_TEST_SHELL", "pwsh")
    if shutil.which(shell) is None:
        print(f"FAIL: {shell} not on PATH")
        return 1
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        skills, cache = tmp / "skills", tmp / "cache"
        skill(skills / "alpha")
        hidden_personal = skill(skills / "beta")
        skill(cache / "mkt" / "plug" / "1.0.0" / "skills" / "gamma")
        hidden_plugin = skill(cache / "mkt" / "other" / "2.0.0" / "skills" / "delta").parents[2]

        got = discover(shell, skills, cache)
        check("readable roots: every skill found, no shortfall",
              sorted(got["names"]) == ["plugin-other/delta", "plugin-plug/gamma", "skills/alpha", "skills/beta"]
              and got["shortfall"] == [], str(got))

        got = discover(shell, tmp / "absent-skills", tmp / "absent-cache")
        check("missing roots are not a shortfall", got == {"names": [], "shortfall": []}, str(got))

        for label, folder, gone in (("a personal skill folder", hidden_personal, "skills/beta"),
                                    ("a plugin cache folder", hidden_plugin, "plugin-other/delta")):
            with unreadable(folder):
                if not denied(folder):
                    check(f"{label}: read access could be denied for the test", False, "running with rights that "
                          "ignore the deny, so the case was not exercised")
                    continue
                got = discover(shell, skills, cache)
            check(f"{label} that cannot be read is a shortfall, not a silent drop",
                  gone not in got["names"] and len(got["shortfall"]) >= 1
                  and any(folder.name in line for line in got["shortfall"]), str(got))
            check(f"{label} unreadable: the readable skills are still found",
                  {"skills/alpha", "plugin-plug/gamma"} <= set(got["names"]), str(got))

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed ({shell})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

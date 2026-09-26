"""Verify pos_engine: exit 0 when the task is done. Synthetic example — contains no private repository data or production findings."""
import importlib
import re
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo))


def limit(n):
    text = (repo / "src" / "regions" / f"r{n}.py").read_text(encoding="utf-8")
    return int(re.search(r"^LIMIT\s*=\s*(\d+)", text, re.M).group(1))


out = subprocess.run([sys.executable, "-m", "src.engine", "alpha", "5"], cwd=repo,
                     capture_output=True, text=True).stdout.strip()
ok = out == "16" and all((repo / "src" / "engine" / f"k_{c}.py").read_text(encoding="utf-8").count("value *") == 1
                         for c in "abcdefghijkl")
sys.exit(0 if ok else 1)

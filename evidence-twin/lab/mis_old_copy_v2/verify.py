"""Verify mis_old_copy_v2: exit 0 when the task is done. Synthetic example — contains no private repository data or production findings."""
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


import src.beta as beta
sys.exit(0 if beta.total([1, -2, 3]) == 56 else 1)

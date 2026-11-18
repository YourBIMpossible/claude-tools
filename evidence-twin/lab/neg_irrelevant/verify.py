"""Verify neg_irrelevant: exit 0 when the task is done. Synthetic example — contains no private repository data or production findings."""
import sys
from pathlib import Path

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo))


text = (repo / "src" / "beta.py").read_text(encoding="utf-8")
sys.exit(0 if "AlphaService(factor=5)" in text else 1)

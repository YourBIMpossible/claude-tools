"""Verify mis_old_copy: exit 0 when the task is done. Synthetic example — contains no private repository data or production findings."""
import sys
from pathlib import Path

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo))


import src.beta as beta
sys.exit(0 if beta.total([1, -2, 3]) == 56 else 1)

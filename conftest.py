"""Repo-root pytest config.

These files are script runners, not pytest modules: their ``test_*`` functions
take positional arguments (``root``, ``tmp``, ``case``, ``tasks``) that the
script's own ``main`` supplies, so pytest collects them and reports each as a
missing-fixture setup error. Run them as ``python <file>``. ``tools/gate.py``
(``TEST_SUITES``) is the one list: CI, ``tools/release-check.ps1`` and the
pre-push hook all run it. Keep them out of a bare ``pytest`` run.
"""

collect_ignore_glob = [
    "evidence-capture/tests/test_*.py",
    "evidence-episodes/test_*.py",
    "evidence-footprints/test_*.py",
    "evidence-twin/test_*.py",
]

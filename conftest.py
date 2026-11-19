"""Repo-root pytest config.

These files are script runners, not pytest modules: their ``test_*`` functions
take positional arguments (``root``, ``tmp``, ``case``, ``tasks``) that the
script's own ``main`` supplies, so pytest collects them and reports each as a
missing-fixture setup error. Run them as ``python <file>``. CI and
``tools/release-check.ps1`` run most of them; the rest are local-only.
Keep them out of a bare ``pytest`` run.
"""

collect_ignore_glob = [
]

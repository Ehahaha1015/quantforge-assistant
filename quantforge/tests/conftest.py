"""Preserve the repository's historical bare test-helper imports."""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent

# The historical suites import helpers by bare module name (for example
# ``from _helpers import ...``).  Keep those directories importable while the
# ``tests`` package prevents the new ``tests/operator`` directory from
# shadowing Python's standard-library ``operator`` module.
for _directory in sorted(
    (path for path in _TESTS_ROOT.iterdir() if path.is_dir()),
    key=lambda path: (path.name != "execstate", path.name),
):
    # Integration package directories are not legacy bare helper modules.
    # Adding integrations makes its qlib test folder shadow the optional SDK.
    if _directory.name in {"__pycache__", "integrations"}:
        continue
    _entry = str(_directory)
    if _entry not in sys.path:
        sys.path.append(_entry)

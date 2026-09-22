"""Pytest startup hook for deterministic nested-project import precedence."""

from __future__ import annotations

import os
import sys


def _prioritize_pytest_import_roots() -> None:
    roots = [item for item in os.environ.get("UTA_PYTEST_IMPORT_ROOTS", "").split(os.pathsep) if item]
    if not roots:
        return
    normalized = {os.path.realpath(item) for item in roots}
    remainder = [item for item in sys.path if os.path.realpath(item or os.getcwd()) not in normalized]
    sys.path[:] = [*roots, *remainder]


def pytest_sessionstart(session: object) -> None:
    """Reapply import roots after pytest has initialized its own paths."""
    _prioritize_pytest_import_roots()


def pytest_runtest_setup(item: object) -> None:
    """Keep the roots stable after pytest import-mode path adjustments."""
    _prioritize_pytest_import_roots()

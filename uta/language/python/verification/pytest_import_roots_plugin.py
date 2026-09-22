"""Backward-compatible facade for pytest import roots plugin; delegates to uta_py_enforce."""

from __future__ import annotations

from uta_py_enforce.pytest_import_roots_plugin import (
    _prioritize_pytest_import_roots,
    pytest_runtest_setup,
    pytest_sessionstart,
)

__all__ = [
    "_prioritize_pytest_import_roots",
    "pytest_runtest_setup",
    "pytest_sessionstart",
]

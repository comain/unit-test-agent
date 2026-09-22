"""Backward-compatible facade for Python module resolution; delegates to uta_py_enforce."""

from __future__ import annotations

from uta_py_enforce.module_resolver import (
    PythonModuleResolution,
    _normalize_source_path,
    _parts_to_module,
    _source_module_relative_path,
    module_name_from_source_path,
    resolve_python_module,
)

__all__ = [
    "PythonModuleResolution",
    "_normalize_source_path",
    "_parts_to_module",
    "_source_module_relative_path",
    "module_name_from_source_path",
    "resolve_python_module",
]

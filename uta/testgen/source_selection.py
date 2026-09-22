"""Language-neutral candidate source-file selection (re-exported from uta.shared)."""

from __future__ import annotations

from uta.shared.source_selection import (
    _GIT_LOG_TIMEOUT_SECONDS,
    _WALK_EXCLUDED_DIRS,
    _adapter_for,
    _path_is_under_module,
    filter_files,
    get_all_source_files,
    get_changed_source_files,
    iter_all_source_files,
)

__all__ = [
    "_GIT_LOG_TIMEOUT_SECONDS",
    "_WALK_EXCLUDED_DIRS",
    "_adapter_for",
    "_path_is_under_module",
    "filter_files",
    "get_all_source_files",
    "get_changed_source_files",
    "iter_all_source_files",
]

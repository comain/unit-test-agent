"""Backward-compatible facade for strict test selection; delegates to uta_py_enforce."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from uta_py_enforce.module_resolver import module_name_from_source_path as _module_name_from_source_path
from uta_py_enforce.test_selection import (
    GENERATED_ROOT,
    ExistingPythonTestMatch,
    PythonTestDestination,
    _path_tokens,
    _strict_name_score,
    _symbol_names,
    discover_related_existing_tests,
    discover_strict_python_test_candidates,
    generated_test_path,
    is_strict_test_path,
    select_python_test_destination,
    source_path_from_target,
    strict_test_candidates,
    strict_test_score,
)
from uta.shared.targets import TargetRef


def is_strict_python_test_candidate_path(
    test_path: str,
    target: TargetRef,
    *,
    context_payload: Optional[Dict[str, Any]] = None,
) -> bool:
    source_path = target.source_path or source_path_from_target(target.target_id)
    source_stem_tokens = _path_tokens(Path(source_path).stem)
    symbol_names = _symbol_names(target.symbol, context_payload=context_payload)
    score, _ = _strict_name_score(test_path, source_path, source_stem_tokens, symbol_names)
    return score > 0


def module_name_from_source_path(source_path: str, *, repo_path: Optional[Path] = None) -> str:
    return _module_name_from_source_path(source_path, repo_path=repo_path)


__all__ = [
    "GENERATED_ROOT",
    "ExistingPythonTestMatch",
    "PythonTestDestination",
    "discover_related_existing_tests",
    "discover_strict_python_test_candidates",
    "generated_test_path",
    "is_strict_python_test_candidate_path",
    "is_strict_test_path",
    "module_name_from_source_path",
    "select_python_test_destination",
    "source_path_from_target",
    "strict_test_candidates",
    "strict_test_score",
]

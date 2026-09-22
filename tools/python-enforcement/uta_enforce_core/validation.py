"""Validation rules and limit checks for enforcement contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping
from .contracts import (
    EnforcementRequest,
    EnforcementResult,
    EnforcementStatus,
    EnforcementTarget,
    QualityGates,
    RuntimeSelection,
)


MAX_TARGETS = 200
MAX_TEST_PATHS_PER_TARGET = 5
#: The per-command ceiling, set to the larger of the two lanes' defaults.
#:
#: The two genuinely differ and both are frozen behaviour: full UTA resolves
#: 1800 (`PythonRuntimeConfig.timeout_seconds`), while the distributed CLI has
#: always passed `UTA_PYTHON_GATE_TIMEOUT_SECONDS`, default 7200, straight
#: through as its per-command timeout. An earlier value of 1800 encoded only
#: the UTA lane, so wiring the local CLI through this contract refused a
#: timeout that lane has always used -- a behaviour change disguised as
#: validation.
MAX_COMMAND_TIMEOUT_SECONDS = 7200
MAX_TOTAL_TIMEOUT_SECONDS = 7200


class EnforcementValidationError(ValueError):
    """Raised when an enforcement contract violates schema or integrity bounds."""


def validate_request(request: EnforcementRequest) -> None:
    """Validate an EnforcementRequest against fail-closed integrity rules and numeric budgets."""
    if not isinstance(request, EnforcementRequest):
        raise EnforcementValidationError(f"Expected EnforcementRequest, got {type(request).__name__}")

    # Repository validation
    if not request.repo_path:
        raise EnforcementValidationError("Repository path (repo_path) cannot be empty")
    repo = Path(request.repo_path).resolve()
    if not repo.exists():
        raise EnforcementValidationError(f"Repository path does not exist: {repo}")
    if not repo.is_dir():
        raise EnforcementValidationError(f"Repository path is not a directory: {repo}")

    # Language validation
    if not request.language or not str(request.language).strip():
        raise EnforcementValidationError("Language cannot be empty")

    # Base ref validation
    if not request.base_ref or not str(request.base_ref).strip():
        raise EnforcementValidationError("base_ref cannot be empty")

    # Target counts budget (Task 11)
    target_count = len(request.targets)
    if target_count > MAX_TARGETS:
        raise EnforcementValidationError(
            f"Target count {target_count} exceeds maximum allowed budget of {MAX_TARGETS} targets"
        )

    for i, target in enumerate(request.targets):
        if not isinstance(target, EnforcementTarget):
            raise EnforcementValidationError(f"Target at index {i} must be an EnforcementTarget instance")
        if not target.target_id or not str(target.target_id).strip():
            raise EnforcementValidationError(f"Target at index {i} must have a non-empty target_id")
        if not target.source_path or not str(target.source_path).strip():
            raise EnforcementValidationError(f"Target at index {i} must have a non-empty source_path")

        _validate_repo_relative_path(target.source_path, f"targets[{i}].source_path", repo)

        test_path_count = len(target.test_paths)
        if test_path_count > MAX_TEST_PATHS_PER_TARGET:
            raise EnforcementValidationError(
                f"Target '{target.target_id}' has {test_path_count} test paths, exceeding maximum budget of {MAX_TEST_PATHS_PER_TARGET}"
            )
        for j, test_path in enumerate(target.test_paths):
            _validate_repo_relative_path(test_path, f"targets[{i}].test_paths[{j}]", repo)

    for k, test_path in enumerate(request.test_paths):
        _validate_repo_relative_path(test_path, f"request.test_paths[{k}]", repo)

    # Quality gates validation
    if request.quality_gates:
        _validate_quality_gates(request.quality_gates)

    # Runtime budgets validation (Task 11)
    if request.runtime:
        _validate_runtime_selection(request.runtime)


def _validate_repo_relative_path(rel_path: str, field_name: str, repo: Path) -> None:
    path_str = str(rel_path).strip()
    if not path_str:
        raise EnforcementValidationError(f"{field_name} cannot be empty")
    p = Path(path_str)
    if p.is_absolute():
        try:
            p.resolve().relative_to(repo)
        except ValueError:
            raise EnforcementValidationError(f"{field_name} '{path_str}' is an absolute path outside repository root '{repo}'")
    else:
        # Check for path traversal outside repo
        try:
            resolved = (repo / p).resolve()
            resolved.relative_to(repo)
        except ValueError:
            raise EnforcementValidationError(f"{field_name} '{path_str}' escapes repository root '{repo}'")


def _validate_quality_gates(gates: QualityGates) -> None:
    if not isinstance(gates, QualityGates):
        raise EnforcementValidationError("quality_gates must be a QualityGates instance")
    if gates.diff_coverage_min is not None:
        if not (0.0 <= float(gates.diff_coverage_min) <= 1.0):
            raise EnforcementValidationError(
                f"diff_coverage_min must be between 0.0 and 1.0, got {gates.diff_coverage_min}"
            )
    if gates.diff_mutation_min is not None:
        if not (0.0 <= float(gates.diff_mutation_min) <= 1.0):
            raise EnforcementValidationError(
                f"diff_mutation_min must be between 0.0 and 1.0, got {gates.diff_mutation_min}"
            )


def _validate_runtime_selection(runtime: RuntimeSelection) -> None:
    if not isinstance(runtime, RuntimeSelection):
        raise EnforcementValidationError("runtime must be a RuntimeSelection instance")
    if runtime.timeout_seconds <= 0 or runtime.timeout_seconds > MAX_COMMAND_TIMEOUT_SECONDS:
        raise EnforcementValidationError(
            f"timeout_seconds {runtime.timeout_seconds} exceeds allowable range (1 to {MAX_COMMAND_TIMEOUT_SECONDS}s)"
        )
    if runtime.total_timeout_seconds <= 0 or runtime.total_timeout_seconds > MAX_TOTAL_TIMEOUT_SECONDS:
        raise EnforcementValidationError(
            f"total_timeout_seconds {runtime.total_timeout_seconds} exceeds allowable range (1 to {MAX_TOTAL_TIMEOUT_SECONDS}s)"
        )


def validate_result(result: EnforcementResult) -> None:
    """Validate an EnforcementResult and its evidence mapping."""
    if not isinstance(result, EnforcementResult):
        raise EnforcementValidationError(f"Expected EnforcementResult, got {type(result).__name__}")
    if not isinstance(result.status, (EnforcementStatus, str)):
        raise EnforcementValidationError(f"Invalid result status type: {type(result.status).__name__}")
    try:
        EnforcementStatus(str(result.status))
    except ValueError:
        raise EnforcementValidationError(f"Invalid result status value: '{result.status}'")

    if not isinstance(result.evidence, Mapping):
        raise EnforcementValidationError(f"Result evidence must be a Mapping, got {type(result.evidence).__name__}")

    # Evidence envelope validation if evidence is non-empty
    if result.evidence:
        if "backend" not in result.evidence and "schemaVersion" not in result.evidence and "status" not in result.evidence:
            raise EnforcementValidationError("Result evidence missing standard envelope fields (backend/schemaVersion/status)")

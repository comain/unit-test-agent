"""Tests for language-neutral immutable enforcement contracts, DTOs, registry, and validation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import pytest

from uta_enforce_core.contracts import (
    EnforcementCapabilities,
    EnforcementInvocationContext,
    EnforcementRequest,
    EnforcementResult,
    EnforcementStatus,
    EnforcementTarget,
    QualityGates,
    RuntimeSelection,
)
from uta_enforce_core.registry import (
    DuplicateBindingError,
    EnforcementRegistry,
    UnknownLanguageError,
)
from uta_enforce_core.validation import (
    MAX_COMMAND_TIMEOUT_SECONDS,
    MAX_TARGETS,
    MAX_TEST_PATHS_PER_TARGET,
    MAX_TOTAL_TIMEOUT_SECONDS,
    EnforcementValidationError,
    validate_request,
    validate_result,
)


class DummyBinding:
    def __init__(self, language: str = "python", supports_cov: bool = True, supports_mut: bool = True):
        self.language = language
        self._supports_cov = supports_cov
        self._supports_mut = supports_mut

    def capabilities(self) -> EnforcementCapabilities:
        return EnforcementCapabilities(
            language=self.language,
            supports_coverage=self._supports_cov,
            supports_mutation=self._supports_mut,
        )

    def enforce(self, request: EnforcementRequest, context: EnforcementInvocationContext) -> EnforcementResult:
        return EnforcementResult(
            status=EnforcementStatus.PASSED,
            evidence={"schemaVersion": 1, "backend": "python_enforcer", "status": "passed"},
        )


def test_enforcement_dto_immutability(tmp_path: Path):
    target = EnforcementTarget(
        language="python",
        target_id="pyfile:pkg/mod.py",
        source_path="pkg/mod.py",
        test_paths=("tests/test_mod.py",),
    )
    with pytest.raises(FrozenInstanceError):
        target.source_path = "pkg/other.py"  # type: ignore

    request = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(target,),
        test_paths=("tests/test_mod.py",),
        quality_gates=QualityGates(diff_coverage_min=0.8, diff_mutation_min=0.6),
        runtime=RuntimeSelection(timeout_seconds=300),
    )
    with pytest.raises(FrozenInstanceError):
        request.language = "java"  # type: ignore

    result = EnforcementResult(
        status=EnforcementStatus.PASSED,
        evidence={"backend": "python_enforcer", "schemaVersion": 1, "status": "passed"},
    )
    with pytest.raises(FrozenInstanceError):
        result.status = EnforcementStatus.FAILED  # type: ignore


def test_registry_construction_and_lookup():
    py_binding = DummyBinding("python")
    java_binding = DummyBinding("java")

    registry = EnforcementRegistry([py_binding, java_binding])
    assert len(registry) == 2
    assert "python" in registry
    assert "PYTHON" in registry  # case-insensitive
    assert "java" in registry
    assert "csharp" not in registry
    assert registry.supported_languages() == ("java", "python")

    assert registry.get("python") is py_binding
    assert registry.get("Python") is py_binding
    assert registry.lookup("JAVA") is java_binding

    with pytest.raises(UnknownLanguageError) as exc_info:
        registry.get("golang")
    assert "golang" in str(exc_info.value)
    assert "Available languages" in str(exc_info.value)


def test_registry_duplicate_rejection():
    b1 = DummyBinding("python")
    b2 = DummyBinding("Python")
    with pytest.raises(DuplicateBindingError):
        EnforcementRegistry([b1, b2])


def test_validate_request_success(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("assert True")

    req = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(
            EnforcementTarget(
                language="python",
                target_id="pyfile:src/a.py",
                source_path="src/a.py",
                test_paths=("tests/test_a.py",),
            ),
        ),
        test_paths=("tests/test_a.py",),
        quality_gates=QualityGates(diff_coverage_min=0.8, diff_mutation_min=0.5),
        runtime=RuntimeSelection(timeout_seconds=600, total_timeout_seconds=1200),
    )
    # Should not raise
    validate_request(req)


def test_validate_request_path_traversal_rejection(tmp_path: Path):
    req = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(
            EnforcementTarget(
                language="python",
                target_id="pyfile:../../etc/passwd",
                source_path="../../etc/passwd",
            ),
        ),
    )
    with pytest.raises(EnforcementValidationError, match="escapes repository root"):
        validate_request(req)


def test_validate_request_target_limit_budget(tmp_path: Path):
    targets = tuple(
        EnforcementTarget(
            language="python",
            target_id=f"target_{i}",
            source_path=f"src/file_{i}.py",
        )
        for i in range(MAX_TARGETS + 1)
    )
    req = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=targets,
    )
    with pytest.raises(EnforcementValidationError, match=f"exceeds maximum allowed budget of {MAX_TARGETS}"):
        validate_request(req)


def test_validate_request_test_paths_budget(tmp_path: Path):
    target = EnforcementTarget(
        language="python",
        target_id="target_1",
        source_path="src/file.py",
        test_paths=tuple(f"tests/test_{i}.py" for i in range(MAX_TEST_PATHS_PER_TARGET + 1)),
    )
    req = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(target,),
    )
    with pytest.raises(EnforcementValidationError, match=f"exceeding maximum budget of {MAX_TEST_PATHS_PER_TARGET}"):
        validate_request(req)


def test_validate_request_timeout_budgets(tmp_path: Path):
    req_cmd_timeout = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(),
        runtime=RuntimeSelection(timeout_seconds=MAX_COMMAND_TIMEOUT_SECONDS + 1),
    )
    with pytest.raises(EnforcementValidationError, match="timeout_seconds"):
        validate_request(req_cmd_timeout)

    req_total_timeout = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(),
        runtime=RuntimeSelection(total_timeout_seconds=MAX_TOTAL_TIMEOUT_SECONDS + 1),
    )
    with pytest.raises(EnforcementValidationError, match="total_timeout_seconds"):
        validate_request(req_total_timeout)


def test_validate_result_envelope_validation():
    valid = EnforcementResult(
        status=EnforcementStatus.PASSED,
        evidence={"backend": "python_enforcer", "schemaVersion": 1, "status": "passed"},
    )
    validate_result(valid)

    invalid_status = EnforcementResult(
        status="not_a_real_status",  # type: ignore
        evidence={"backend": "python_enforcer", "schemaVersion": 1, "status": "passed"},
    )
    with pytest.raises(EnforcementValidationError, match="Invalid result status"):
        validate_result(invalid_status)

    missing_envelope = EnforcementResult(
        status=EnforcementStatus.PASSED,
        evidence={"some_random_key": 123},
    )
    with pytest.raises(EnforcementValidationError, match="missing standard envelope fields"):
        validate_result(missing_envelope)

"""Tests for Java enforcement binding and dual-language dispatch through sole contract."""

from __future__ import annotations

from pathlib import Path

from uta.enforcement.bindings import (
    UtaPythonEnforcementProxy,
    create_java_enforcement_binding,
)
from uta_enforce_core.commands import SafeProcessRunner
from uta_enforce_core.contracts import (
    EnforcementInvocationContext,
    EnforcementRequest,
    EnforcementStatus,
    EnforcementTarget,
    QualityGates,
    RuntimeSelection,
)
from uta_enforce_core.dispatch import enforce
from uta_enforce_core.registry import EnforcementRegistry


def test_java_enforcement_binding_capabilities():
    binding = create_java_enforcement_binding()
    assert binding.language == "java"
    caps = binding.capabilities()
    assert caps.language == "java"
    assert caps.supports_coverage is True
    assert caps.supports_mutation is True
    assert "8" in caps.supported_runtime_versions
    assert caps.accepts_sampling_policy is False


def test_dual_language_registry_and_java_dispatch(tmp_path: Path, monkeypatch):
    java_binding = create_java_enforcement_binding()
    py_proxy = UtaPythonEnforcementProxy()

    registry = EnforcementRegistry([java_binding, py_proxy])
    assert "java" in registry
    assert "python" in registry
    assert len(registry) == 2

    # Mock Java runner output so Maven isn't actually invoked in unit test
    def mock_run_java_enforcement(*args, **kwargs):
        return {
            "schemaVersion": 1,
            "backend": "maven_enforcer",
            "language": "java",
            "status": "passed",
            "passed": True,
            "reasonCode": "passed",
            "summary": "Java test suite passed with 100% coverage",
        }

    monkeypatch.setattr(
        "uta.enforcement.bindings.java.binding.run_java_enforcement",
        mock_run_java_enforcement,
    )

    context = EnforcementInvocationContext(run_command=SafeProcessRunner())
    request = EnforcementRequest(
        repo_path=tmp_path,
        language="java",
        targets=(
            EnforcementTarget(
                language="java",
                target_id="javaclass:com.example.Service",
                source_path="src/main/java/com/example/Service.java",
            ),
        ),
        quality_gates=QualityGates(diff_coverage_min=0.8),
        runtime=RuntimeSelection(timeout_seconds=300),
    )

    result = enforce(request, registry=registry, context=context)
    assert result.status == EnforcementStatus.PASSED
    assert result.evidence["language"] == "java"
    assert result.evidence["backend"] == "maven_enforcer"
    assert len(result.target_results) == 1
    assert result.target_results[0].status == EnforcementStatus.PASSED

"""Tests for canonical Python enforcement binding and end-to-end dispatch."""

from __future__ import annotations

from pathlib import Path

from uta_py_enforce.api import create_python_enforcement_binding
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
from uta_enforce_core.evidence import format_evidence_markers
from uta_enforce_core.registry import EnforcementRegistry


def test_canonical_python_enforcement_end_to_end_pass(tmp_path: Path):
    # Setup a simple repo
    src_dir = tmp_path / "calc"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("")
    calc_py = src_dir / "math.py"
    calc_py.write_text("""def add(a: int, b: int) -> int:
    return a + b
""")

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_py = tests_dir / "test_math.py"
    test_py.write_text("""from calc.math import add

def test_add():
    assert add(1, 2) == 3
""")

    binding = create_python_enforcement_binding()
    registry = EnforcementRegistry([binding])
    context = EnforcementInvocationContext(run_command=SafeProcessRunner())

    request = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(
            EnforcementTarget(
                language="python",
                target_id="pyfile:calc/math.py",
                source_path="calc/math.py",
                test_paths=("tests/test_math.py",),
            ),
        ),
        quality_gates=QualityGates(diff_coverage_min=0.8),
        runtime=RuntimeSelection(timeout_seconds=60),
    )

    result = enforce(request, registry=registry, context=context)
    assert result.status in (EnforcementStatus.PASSED, EnforcementStatus.FAILED)
    assert "schemaVersion" in result.evidence
    assert result.evidence["backend"] == "python_enforcer"

    markers = format_evidence_markers(result.evidence)
    assert "[test-enforcer] python evidence" in markers


def test_canonical_python_binding_skips_mutation_when_gate_is_not_requested(
    tmp_path: Path, monkeypatch
):
    """Coverage repair uses the canonical binding without paying for mutation."""
    source = tmp_path / "calc.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    test = tmp_path / "test_calc.py"
    test.write_text("from calc import add\ndef test_add(): assert add(1, 2) == 3\n", encoding="utf-8")
    mutation_calls = []

    monkeypatch.setattr(
        "uta_py_enforce.api.strict_test_candidates",
        lambda *_args, **_kwargs: ["test_calc.py"],
    )
    monkeypatch.setattr(
        "uta_py_enforce.api.run_coverage",
        lambda *_args, **_kwargs: {
            "covered": 1, "total": 1, "rate": 100.0, "gate": 95.0,
            "passed": True, "testsPass": True,
        },
    )
    monkeypatch.setattr(
        "uta_py_enforce.api.run_mutation",
        lambda *_args, **_kwargs: mutation_calls.append(True),
    )

    result = create_python_enforcement_binding().enforce(
        EnforcementRequest(
            repo_path=tmp_path,
            language="python",
            targets=(EnforcementTarget(
                language="python", target_id="pyfile:calc.py",
                source_path="calc.py", test_paths=("test_calc.py",),
            ),),
            quality_gates=QualityGates(diff_coverage_min=0.95, diff_mutation_min=None),
            runtime=RuntimeSelection(timeout_seconds=60),
        ),
        EnforcementInvocationContext(run_command=SafeProcessRunner()),
    )

    assert result.status == EnforcementStatus.PASSED
    assert mutation_calls == []
    assert result.target_results[0].evidence["mutation"]["reasonCode"] == "mutation_not_requested"

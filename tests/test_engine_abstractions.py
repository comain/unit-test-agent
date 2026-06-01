from pathlib import Path

from uta.engine import (
    BatchGenerationRequest,
    ContextProvider,
    ContextQuery,
    ProjectSummaryArtifacts,
    RawTargetSelection,
    TargetRef,
    ValidationVerdict,
    default_registry,
    default_plan_context_registry,
    make_context_provider,
    validate_plan_breadth,
)
from uta.language.java.adapter import JavaLanguageAdapter
from uta.language.python.adapter import PythonLanguageAdapter
from uta.language.python.context import PythonContextProvider


def test_engine_exports_canonical_cross_language_contracts():
    registry = default_registry()
    target = registry.adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py::forecast_for_store")
    )
    request = BatchGenerationRequest.from_targets(
        language="python",
        repo_path=Path("/repo"),
        targets=[target],
    )
    verdict = ValidationVerdict(True, "passed", "ok")
    artifacts = ProjectSummaryArtifacts("repo", "context", "guidance", "compile")

    assert isinstance(registry.adapter_for("java"), JavaLanguageAdapter)
    assert isinstance(registry.adapter_for("python"), PythonLanguageAdapter)
    assert isinstance(target, TargetRef)
    assert request.targets[0].target_id == target.target_id
    assert verdict.passed is True
    assert artifacts.as_dict()["repo_summary_abs"] == "repo"


def test_language_impls_live_under_language_packages(tmp_path):
    provider = make_context_provider("python", tmp_path)

    assert isinstance(provider, PythonContextProvider)
    assert isinstance(provider, ContextProvider)
    assert ContextQuery(symbol="run").symbol == "run"


def test_engine_validation_uses_extractor_registry():
    registry = default_plan_context_registry()
    context = {
        "language": "python",
        "symbols": [
            {"kind": "function", "name": "forecast", "line": 1, "end_line": 4},
            {"kind": "function", "name": "_helper", "line": 5, "end_line": 6},
        ],
    }

    result = validate_plan_breadth(
        "Cover `forecast` for normal demand.",
        context,
        registry=registry,
        min_coverage_ratio=1.0,
    )

    assert registry.languages == ("java", "python")
    assert result.known_methods == 1
    assert result.verdict.value == "PASS"

from __future__ import annotations

from pathlib import Path

from uta.testgen.context import ContextQuery, make_context_provider
from uta.shared.languages import RawTargetSelection, default_registry
from uta.shared.languages import resolve_language
from uta.enforcement.mutation_repair import (
    MutationRepairContext,
    MutationRepairGroup,
    MutationRepairRoundState,
    plan_mutation_repair,
    write_mutation_repair_context,
)
from uta.shared.parse import ParseProjectRequest, make_parse_provider
from uta.language.java.verification.runner import (
    JavaCoverageSummary,
    JavaMutationSummary,
    JavaVerificationResult,
)
from uta.language.python.verification.runner import (
    CoverageSummary,
    MutationSummary,
    PythonVerificationResult,
)


def test_java_and_python_adapters_normalize_targets_to_shared_target_ref(tmp_path: Path) -> None:
    registry = default_registry()

    java_target = registry.adapter_for("java").normalize_target(
        RawTargetSelection(class_fqn="com.example.orders.OrderService")
    )
    python_target = registry.adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py::forecast_for_store")
    )

    assert java_target.language == "java"
    assert java_target.target_id == "com.example.orders.OrderService"
    assert java_target.display_name == "com.example.orders.OrderService"
    assert java_target.granularity == "class"

    assert python_target.language == "python"
    assert python_target.target_id == "pysymbol:jobs/forecast.py::forecast_for_store"
    assert python_target.display_name == "jobs/forecast.py::forecast_for_store"
    assert python_target.source_path == "jobs/forecast.py"
    assert python_target.symbol == "forecast_for_store"
    assert python_target.granularity == "function"


def test_java_and_python_prompt_bundles_stay_language_owned(tmp_path: Path) -> None:
    """Each backend publishes its own prompt names; nothing shared decides them.

    This replaces the generated-test-policy assertion that used to sit here.
    `generated_test_policy()` declared allowed test roots that no production
    code ever read -- placement is decided by each backend's `WorkspacePolicy`
    and its test-artifact writer -- so the declaration was retired with the
    rest of the umbrella `LanguageAdapter` protocol.
    """
    registry = default_registry()

    java_bundle = registry.prompt_bundle("java")
    python_bundle = registry.prompt_bundle("python")

    assert java_bundle.language == "java"
    assert java_bundle.plan == "plan_tests"
    assert java_bundle.generate == "generate_test"

    assert python_bundle.language == "python"
    assert python_bundle.plan is None
    assert python_bundle.generate == "python_generate_test"


def test_verification_runners_and_results_share_engine_result_shape() -> None:
    java_result = JavaVerificationResult(
        status="passed",
        reason_code="passed",
        tests_pass=True,
        coverage=JavaCoverageSummary(
            line_rate=100.0,
            gate=95.0,
            passed=True,
            xml_path="target/site/jacoco/jacoco.xml",
        ),
        mutation=JavaMutationSummary(
            generated=2,
            killed=2,
            survived=0,
            no_coverage=0,
            rate=100.0,
            gate=95.0,
            passed=True,
            report_path="target/pit-reports/mutations.xml",
        ),
    )
    python_result = PythonVerificationResult(
        status="passed",
        reason_code="passed",
        tests_pass=True,
        coverage=CoverageSummary(
            covered=4,
            total=4,
            rate=100.0,
            gate=95.0,
            passed=True,
            xml_path=".uta_cache/python/coverage.xml",
        ),
        mutation=MutationSummary(
            runtime_lane="python3",
            generated=2,
            killed=2,
            survived=0,
            no_coverage=0,
            rate=100.0,
            gate=95.0,
            passed=True,
        ),
    )

    expected_keys = {
        "status",
        "coverage",
        "tests_pass",
        "mutation_score",
        "surviving_mutants",
        "total_mutants",
        "killed_mutants",
        "no_coverage_mutants",
        "verification_status",
        "verification_reason",
        "verification_message",
        "verification_commands",
        "coverage_summary",
        "mutation_summary",
    }

    assert expected_keys <= set(java_result.as_result_fields())
    assert expected_keys <= set(python_result.as_result_fields())


def test_java_and_python_language_detection_share_decision_shape(tmp_path: Path) -> None:
    registry = default_registry()
    java_repo = tmp_path / "java"
    python_repo = tmp_path / "python"
    _write_java_source(java_repo)
    _write_python_source(python_repo)

    java_decision = resolve_language(registry, java_repo, changed_paths=["src/main/java/com/example/OrderService.java"])
    python_decision = resolve_language(registry, python_repo, changed_paths=["jobs/forecast.py"])

    assert java_decision.as_dict() == {
        "language": "java",
        "source": "changed_files",
        "reason": "src/main/java/com/example/OrderService.java",
        "candidates": ["java"],
    }
    assert python_decision.as_dict() == {
        "language": "python",
        "source": "changed_files",
        "reason": "jobs/forecast.py",
        "candidates": ["python"],
    }


def test_java_and_python_parse_providers_expose_normalized_callables(tmp_path: Path) -> None:
    java_repo = tmp_path / "java"
    python_repo = tmp_path / "python"
    _write_java_source(java_repo)
    _write_python_source(python_repo)

    java_parsed = make_parse_provider("java").parse_project(ParseProjectRequest(repo_path=java_repo))
    python_parsed = make_parse_provider("python").parse_project(ParseProjectRequest(repo_path=python_repo))

    for parsed, language in ((java_parsed, "java"), (python_parsed, "python")):
        assert parsed.language == language
        assert parsed.source_files
        assert parsed.callables
        assert all(item.name and item.qualified_name and item.source_path for item in parsed.callables)

    assert java_parsed.contains_target("com.example.OrderService")
    assert java_parsed.target_id_for_source_path("src/main/java/com/example/OrderService.java") == "com.example.OrderService"
    assert python_parsed.contains_target("pyfile:jobs/forecast.py")
    assert python_parsed.target_id_for_source_path("jobs/forecast.py") == "pyfile:jobs/forecast.py"


def test_java_and_python_context_providers_expose_language_target_and_artifacts(tmp_path: Path) -> None:
    registry = default_registry()
    java_repo = tmp_path / "java"
    python_repo = tmp_path / "python"
    _write_java_source(java_repo)
    _write_python_source(python_repo)

    java_target = registry.adapter_for("java").normalize_target(RawTargetSelection(target="com.example.OrderService"))
    python_target = registry.adapter_for("python").normalize_target(RawTargetSelection(target="jobs/forecast.py::forecast"))
    java_parsed = make_parse_provider("java").parse_project(ParseProjectRequest(repo_path=java_repo))

    java_context = make_context_provider(
        "java",
        java_repo,
        graph=java_parsed.graph,
        flows=java_parsed.flows,
    ).query_target(java_target, ContextQuery(limit=5))
    python_context = make_context_provider("python", python_repo).query_target(python_target, ContextQuery(limit=5))

    for context, language, target in (
        (java_context, "java", java_target),
        (python_context, "python", python_target),
    ):
        assert context["language"] == language
        assert context["target"]["target_id"] == target.target_id
        assert isinstance(context, dict)

    assert python_context["found"] is True
    assert python_context["context"]["json_abs"].endswith(".json")


def test_mutation_repair_context_contract_is_language_neutral(tmp_path: Path) -> None:
    context = MutationRepairContext(
        target_id="pysymbol:jobs/forecast.py::forecast",
        source_path="jobs/forecast.py",
        test_paths=("tests/test_forecast.py",),
        reproduce_command="uta python-enforce --target jobs/forecast.py::forecast",
        survivor_count=2,
        groups=(
            MutationRepairGroup(
                symbol="forecast",
                count=2,
                lines=(2, 3),
                survivors=({"id": "jobs.forecast.x_forecast__mutmut_1", "line": 2, "description": "survived"},),
                representative_diffs=({"command": "mutmut show jobs.forecast.x_forecast__mutmut_1", "output": "- return 1\n+ return 2"},),
            ),
        ),
    )

    written = write_mutation_repair_context(tmp_path, context, split_threshold=1)

    assert written.artifact_path.endswith("mutation-repair-context.md")
    assert written.group_artifact_paths["forecast"].endswith("mutation-repair-forecast.md")
    assert written.groups[0].symbol == "forecast"
    assert written.groups[0].representative_diffs[0]["command"].startswith("mutmut show")


def test_mutation_repair_planner_contract_is_language_neutral() -> None:
    context = MutationRepairContext(
        target_id="pysymbol:jobs/forecast.py::forecast",
        source_path="jobs/forecast.py",
        test_paths=("tests/test_forecast.py",),
        reproduce_command="uta python-enforce --target jobs/forecast.py::forecast",
        survivor_count=80,
        groups=(
            MutationRepairGroup(symbol="expensive", count=40, roi=4.0, effort_score="10", effort_band="expensive"),
            MutationRepairGroup(symbol="cheap", count=40, roi=60.0, effort_score="1", effort_band="cheap"),
            MutationRepairGroup(symbol="medium", count=10, roi=12.0, effort_score="3", effort_band="medium"),
        ),
        language="python",
        artifact_path="/tmp/mutation-repair-context.md",
        group_artifact_paths={
            "cheap": "/tmp/mutation-repair-cheap.md",
            "medium": "/tmp/mutation-repair-medium.md",
            "expensive": "/tmp/mutation-repair-expensive.md",
        },
    )

    first = plan_mutation_repair(context, MutationRepairRoundState(target_id=context.target_id, attempt_index=1), focused_group_count=1)

    assert first.round_kind == "full_roi"
    assert first.language == "python"
    assert [group.symbol for group in first.selected_groups] == ["cheap", "medium", "expensive"]
    assert first.context_artifact_path == "/tmp/mutation-repair-context.md"
    assert first.prompt_flags["mutation_repair_roi_guided_full"] is True
    assert first.prompt_flags["mutation_repair_split"] is False

    second = plan_mutation_repair(
        context,
        MutationRepairRoundState(
            target_id=context.target_id,
            attempt_index=2,
            previous_survivor_count=80,
            latest_survivor_count=80,
            previous_selected_group_ids=("cheap", "medium", "expensive"),
            edited_test_paths=("tests/test_forecast.py",),
        ),
        focused_group_count=2,
    )

    assert second.round_kind == "focused_roi"
    assert [group.symbol for group in second.selected_groups] == ["cheap", "medium"]
    assert second.group_artifact_paths == ["/tmp/mutation-repair-cheap.md", "/tmp/mutation-repair-medium.md"]
    assert second.prompt_flags["mutation_repair_roi_guided_full"] is False
    assert second.prompt_flags["mutation_repair_split"] is True
    assert second.evidence.no_progress is True
    assert second.evidence.previous_selected_group_ids == ("cheap", "medium", "expensive")


def _write_java_source(repo: Path) -> None:
    source = repo / "src" / "main" / "java" / "com" / "example" / "OrderService.java"
    source.parent.mkdir(parents=True, exist_ok=True)
    (repo / "pom.xml").write_text("<project><modelVersion>4.0.0</modelVersion></project>\n", encoding="utf-8")
    source.write_text(
        "package com.example;\n"
        "public class OrderService {\n"
        "    public int total(int left, int right) {\n"
        "        return left + right;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )


def _write_python_source(repo: Path) -> None:
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    (repo / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    source.write_text("def forecast(values=None):\n    return len(values or [])\n", encoding="utf-8")

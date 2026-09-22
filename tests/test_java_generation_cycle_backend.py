"""Java phase handlers behind the default-off durable generation cycle."""

from __future__ import annotations

import json

from uta.language.java.generation_backend import JavaGenerationCycleBackend
from uta.testgen.repair_progress import apply_repair_progress


def _state(tmp_path, **updates):
    state = {
        "repo_path": str(tmp_path),
        "batch": ["pkg.Foo"],
        "module": None,
        "coverage_gate": 80,
        "mutation_gate": 0,
        "results": {},
        "session_ids": [],
        "quality_mode": "class_batch",
        "quality_gate_backend": "builtin",
    }
    state.update(updates)
    return state


def test_java_precheck_proceeds_when_the_expected_test_is_missing(tmp_path):
    result = JavaGenerationCycleBackend().run_phase(
        "precheck_existing_tests", _state(tmp_path)
    )

    assert result == {"phase_outcome": "proceed"}


def test_java_precheck_skips_model_work_when_existing_tests_pass(tmp_path, monkeypatch):
    test_file = tmp_path / "src/test/java/pkg/FooTest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("class FooTest {}", encoding="utf-8")
    monkeypatch.setattr(
        "uta.language.java.phases.ports.run_tests_with_jacoco_batch",
        lambda *args, **kwargs: (True, "tests passed"),
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.find_jacoco_report",
        lambda *args, **kwargs: "jacoco.xml",
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.parse_surefire_results",
        lambda *args, **kwargs: {"FooTest": {"passed": True, "output": "ok"}},
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.parse_jacoco_report",
        lambda *args, **kwargs: {"line": 91.5},
    )

    result = JavaGenerationCycleBackend().run_phase(
        "precheck_existing_tests", _state(tmp_path)
    )

    assert result["phase_outcome"] == "skip_target"
    assert result["results"]["pkg.Foo"]["status"] == "PASS"
    assert result["results"]["pkg.Foo"]["coverage"] == 91.5
    assert result["results"]["pkg.Foo"]["precheck_existing_tests"] is True


def test_java_diff_precheck_routes_failed_enforcement_to_delegated_repair(
    tmp_path, monkeypatch
):
    gate = {"passed": False, "summary": "coverage gate failed"}
    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_delegated_quality_gate_once",
        lambda *args, **kwargs: gate,
    )

    result = JavaGenerationCycleBackend().run_phase(
        "precheck_existing_tests",
        _state(
            tmp_path,
            quality_mode="ci_incremental",
            quality_gate_backend="maven_enforcer",
        ),
    )

    assert result["phase_outcome"] == "delegated_repair"
    assert result["delegated_quality_gate"] == gate


def test_java_cycle_backend_rejects_unmigrated_phases(tmp_path):
    try:
        JavaGenerationCycleBackend().run_phase("unknown_phase", _state(tmp_path))
    except NotImplementedError as exc:
        assert "not migrated" in str(exc)
    else:
        raise AssertionError("an unmigrated Java phase silently ran legacy orchestration")


def test_java_verify_compile_passes_without_opening_an_agent_session(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "uta.language.java.phases.ports._compile_test",
        lambda *args, **kwargs: (True, ""),
    )

    result = JavaGenerationCycleBackend().run_phase("verify_compile", _state(tmp_path))

    assert result["phase_outcome"] == "passed"
    assert result["evidence"]["compile_ok"] is True


def test_java_compile_failure_renders_phase_prompt_then_requires_reverification(
    tmp_path, monkeypatch
):
    failure = (
        "[ERROR] /repo/src/test/java/pkg/FooTest.java:[5,1] cannot find symbol\n"
        "  symbol: class MissingFoo"
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports._compile_test",
        lambda *args, **kwargs: (False, failure),
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports._writeback_resolved_symbols",
        lambda **kwargs: {},
    )
    backend = JavaGenerationCycleBackend()
    verified = backend.run_phase("verify_compile", _state(tmp_path))
    compile_state = _state(
        tmp_path,
        phase_results={"verify_compile": verified},
        attempts_by_phase={},
        max_attempts_by_phase={"fix_compile": 3},
        turn_status="completed",
        turn_session_id="compile-session",
    )

    prompt = backend.render_prompt("fix_compile", compile_state)
    interpreted = backend.interpret(
        "fix_compile", compile_state, {"status": "completed"}
    )

    assert "cannot find symbol" in prompt
    assert "src/test/java/pkg/FooTest.java" in prompt
    assert interpreted["phase_outcome"] == "passed"
    assert interpreted["attempts_by_phase"]["fix_compile"] == 1


def test_java_verify_compile_stops_a_recurring_no_progress_loop(tmp_path, monkeypatch):
    failure = (
        "[ERROR] /repo/src/test/java/pkg/FooTest.java:[5,1] cannot find symbol\n"
        "  symbol: class MissingFoo"
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports._compile_test",
        lambda *args, **kwargs: (False, failure),
    )
    backend = JavaGenerationCycleBackend()
    first = backend.run_phase("verify_compile", _state(tmp_path))

    second = backend.run_phase(
        "verify_compile",
        _state(
            tmp_path,
            phase_results={"verify_compile": first},
            attempts_by_phase={"fix_compile": 1},
        ),
    )

    assert first["phase_outcome"] == "repair"
    assert second["phase_outcome"] == "failed"
    assert second["evidence"]["failure_reason"] == (
        "recurring_compile_errors_without_progress"
    )


def test_java_incremental_compile_ignores_errors_outside_the_stable_batch(
    tmp_path, monkeypatch
):
    failure = (
        "[ERROR] /repo/src/test/java/pkg/OtherTest.java:[5,1] cannot find symbol\n"
        "  symbol: class MissingOther"
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports._compile_test",
        lambda *args, **kwargs: (False, failure),
    )

    result = JavaGenerationCycleBackend().run_phase(
        "verify_compile", _state(tmp_path, quality_mode="ci_incremental")
    )

    assert result["phase_outcome"] == "passed"
    assert result["evidence"]["ignored_unrelated_errors"] is True


def test_java_verify_tests_runs_the_stable_batch_selector_once(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_test_selector",
        lambda repo, selector, module=None, quality_gate_command="": calls.append((repo, selector, module))
        or (True, "ok"),
    )

    result = JavaGenerationCycleBackend().run_phase(
        "verify_tests",
        _state(tmp_path, batch=["pkg.Foo", "pkg.Bar"], module="biz"),
    )

    assert result["phase_outcome"] == "passed"
    assert result["evidence"]["test_selector"] == "FooTest,BarTest"
    assert calls == [(str(tmp_path), "FooTest,BarTest", "biz")]


def test_java_verify_tests_routes_failure_to_repair_with_refreshed_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_test_selector",
        lambda *args, **kwargs: (False, "stale failure"),
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports._refresh_test_failure_summary",
        lambda *args, **kwargs: "current surefire failure",
    )

    result = JavaGenerationCycleBackend().run_phase(
        "verify_tests", _state(tmp_path, attempts_by_phase={"fix_tests": 1})
    )

    assert result["phase_outcome"] == "repair"
    assert result["evidence"]["output"] == "current surefire failure"
    assert result["evidence"]["attempt"] == 1


def test_java_verify_tests_stops_after_existing_repair_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_test_selector",
        lambda *args, **kwargs: (False, "still failing"),
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports._refresh_test_failure_summary",
        lambda *args, **kwargs: "still failing",
    )

    result = JavaGenerationCycleBackend().run_phase(
        "verify_tests",
        _state(
            tmp_path,
            attempts_by_phase={"fix_tests": 3},
            max_attempts_by_phase={"fix_tests": 3},
        ),
    )

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == "test_repair_attempts_exhausted"


def test_java_fix_tests_prompt_uses_verifier_and_quality_gate_context(tmp_path):
    backend = JavaGenerationCycleBackend()
    state = _state(
        tmp_path,
        batch=["pkg.Foo", "pkg.Bar"],
        phase_results={
            "verify_tests": {
                "evidence": {
                    "test_selector": "FooTest,BarTest",
                    "output": "expected 2 but was 1",
                }
            },
            "measure_mutation": {
                "evidence": {"output": "mutation repair changed shared setup"}
            },
        },
    )

    prompt = backend.render_prompt("fix_tests", state)

    assert "expected 2 but was 1" in prompt
    assert "FooTest,BarTest" in prompt
    assert "src/test/java/pkg/FooTest.java" in prompt
    assert "mutation repair changed shared setup" in prompt
    assert "edit production code" in prompt


def test_java_fix_tests_interpret_spends_shared_test_repair_attempt(tmp_path):
    backend = JavaGenerationCycleBackend()
    state = _state(
        tmp_path,
        attempts_by_phase={"fix_tests": 1},
        turn_status="completed",
        turn_session_id="test-fix-session",
    )

    result = backend.interpret("fix_tests", state, {"status": "completed"})

    assert result["phase_outcome"] == "passed"
    assert result["attempts_by_phase"]["fix_tests"] == 2
    assert result["evidence"]["session_id"] == "test-fix-session"


def test_java_measure_coverage_passes_when_every_batch_class_meets_gate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "uta.language.java.phases.ports.run_tests_with_jacoco_batch",
        lambda *args, **kwargs: (True, "tests passed"),
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.find_jacoco_report",
        lambda *args, **kwargs: "jacoco.xml",
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.parse_jacoco_report",
        lambda report, class_fqn: {"line": 91.5 if class_fqn == "pkg.Foo" else 88.0},
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.extract_uncovered_clusters",
        lambda *args, **kwargs: {"methods": [], "line_clusters": []},
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.format_uncovered_clusters_markdown",
        lambda summary: "nothing material uncovered",
    )

    result = JavaGenerationCycleBackend().run_phase(
        "measure_coverage", _state(tmp_path, batch=["pkg.Foo", "pkg.Bar"])
    )

    assert result["phase_outcome"] == "passed"
    assert result["evidence"]["coverage_by_class"] == {
        "pkg.Foo": 91.5,
        "pkg.Bar": 88.0,
    }
    assert result["evidence"]["failing_classes"] == []


def test_java_low_coverage_renders_evidence_rich_repair_then_reverifies(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "uta.language.java.phases.ports.run_tests_with_jacoco_batch",
        lambda *args, **kwargs: (True, "tests passed"),
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.find_jacoco_report",
        lambda *args, **kwargs: "jacoco.xml",
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.parse_jacoco_report",
        lambda report, class_fqn: {"line": 42.0 if class_fqn == "pkg.Foo" else 51.0},
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.extract_uncovered_clusters",
        lambda report, class_fqn: {
            "methods": [{"name": f"missed_{class_fqn}", "missed_line": 4}],
            "line_clusters": [],
        },
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.format_uncovered_clusters_markdown",
        lambda summary: f"uncovered {summary['methods'][0]['name']}",
    )
    backend = JavaGenerationCycleBackend()
    measured = backend.run_phase(
        "measure_coverage", _state(tmp_path, batch=["pkg.Foo", "pkg.Bar"])
    )
    repair_state = _state(
        tmp_path,
        batch=["pkg.Foo", "pkg.Bar"],
        phase_results={"measure_coverage": measured},
        attempts_by_phase={},
        target_context_paths={
            "pkg.Foo": {
                "source_abs": "/repo/src/main/java/pkg/Foo.java",
                "context_abs": "/repo/context.md",
                "symbols_abs": "/repo/symbols.md",
                "roi_abs": "/repo/roi.md",
            }
        },
        turn_status="completed",
        turn_session_id="coverage-session",
    )

    prompt = backend.render_prompt("fix_coverage", repair_state)
    interpreted = backend.interpret(
        "fix_coverage", repair_state, {"status": "completed"}
    )

    assert measured["phase_outcome"] == "repair"
    # Progress is judged on the below-gate classes only.
    assert measured["evidence"]["scores_by_target"] == {"pkg.Foo": 42.0, "pkg.Bar": 51.0}
    assert "42.0" in prompt
    assert "uncovered missed_pkg.Foo" in prompt
    assert "src/test/java/pkg/FooTest.java" in prompt
    assert "pkg.Bar" in prompt
    assert interpreted["phase_outcome"] == "passed"
    assert interpreted["attempts_by_phase"]["fix_coverage"] == 1


def test_java_measure_coverage_stops_at_the_hard_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "uta.language.java.phases.ports.run_tests_with_jacoco_batch",
        lambda *args, **kwargs: (True, "tests passed"),
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.find_jacoco_report",
        lambda *args, **kwargs: "jacoco.xml",
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.parse_jacoco_report",
        lambda *args, **kwargs: {"line": 70.0},
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.extract_uncovered_clusters",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.format_uncovered_clusters_markdown",
        lambda *args, **kwargs: "uncovered",
    )

    state = _state(
        tmp_path,
        attempts_by_phase={"fix_coverage": 2},
        max_attempts_by_phase={"fix_coverage": 2},
    )
    result = apply_repair_progress(
        "measure_coverage",
        state,
        JavaGenerationCycleBackend().run_phase("measure_coverage", state),
    )

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == (
        "coverage_repair_attempts_exhausted"
    )


def test_java_measure_coverage_fails_when_jacoco_test_run_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "uta.language.java.phases.ports.run_tests_with_jacoco_batch",
        lambda *args, **kwargs: (False, "surefire failed"),
    )

    result = JavaGenerationCycleBackend().run_phase(
        "measure_coverage", _state(tmp_path)
    )

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == "jacoco_test_execution_failed"
    assert result["evidence"]["output"] == "surefire failed"


def _patch_java_pit(monkeypatch, *, score=90.0, survived=1):
    monkeypatch.setattr(
        "uta.language.java.phases.ports.run_pitest",
        lambda *args, **kwargs: (True, "pitest passed"),
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.find_latest_pitest_report",
        lambda *args, **kwargs: "mutations.xml",
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.compute_mutation_stats",
        lambda report, class_fqn: {
            "score": score,
            "total": 10,
            "killed": 10 - survived,
            "survived": survived,
        },
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.summarize_surviving_mutants",
        lambda report, class_fqn, **kwargs: [
            {
                "method": "calculate",
                "family": "boundary",
                "count": survived,
                "lines": [42],
                "examples": [
                    {
                        "line": 42,
                        "mutation_type": "ConditionalsBoundaryMutator",
                        "detail": "changed conditional boundary",
                    }
                ],
            }
        ]
        if survived
        else [],
    )


def test_java_measure_mutation_is_explicitly_skipped_when_gate_is_disabled(tmp_path):
    result = JavaGenerationCycleBackend().run_phase(
        "measure_mutation", _state(tmp_path, mutation_gate=0)
    )

    assert result == {
        "phase_outcome": "skipped",
        "evidence": {"reason": "mutation_gate_disabled", "mutation_gate": 0},
    }


def test_java_measure_mutation_passes_a_batch_at_or_above_gate(
    tmp_path, monkeypatch
):
    _patch_java_pit(monkeypatch, score=90.0, survived=1)

    result = JavaGenerationCycleBackend().run_phase(
        "measure_mutation", _state(tmp_path, mutation_gate=80)
    )

    assert result["phase_outcome"] == "passed"
    assert result["evidence"]["mutation_score_by_class"] == {"pkg.Foo": 90.0}
    assert result["evidence"]["failing_classes"] == []


def test_java_low_mutation_renders_focused_repair_then_reverifies(
    tmp_path, monkeypatch
):
    _patch_java_pit(monkeypatch, score=55.0, survived=4)
    backend = JavaGenerationCycleBackend()
    measured = backend.run_phase(
        "measure_mutation",
        _state(tmp_path, mutation_gate=80, mutation_roi_enabled=False),
    )
    repair_state = _state(
        tmp_path,
        mutation_gate=80,
        phase_results={
            "measure_mutation": measured,
            "measure_coverage": {
                "evidence": {"coverage_by_class": {"pkg.Foo": 86.0}}
            },
        },
        attempts_by_phase={},
        target_context_paths={
            "pkg.Foo": {
                "source_abs": "/repo/src/main/java/pkg/Foo.java",
                "context_abs": "/repo/context.md",
                "symbols_abs": "/repo/symbols.md",
            }
        },
        turn_status="completed",
        turn_session_id="mutation-session",
    )

    prompt = backend.render_prompt("fix_mutation", repair_state)
    interpreted = backend.interpret(
        "fix_mutation", repair_state, {"status": "completed"}
    )

    assert measured["phase_outcome"] == "repair"
    assert "55.0" in prompt
    assert "86.0" in prompt
    assert "calculate" in prompt
    assert "src/test/java/pkg/FooTest.java" in prompt
    assert "make no test edit and stop promptly" in prompt
    assert interpreted["phase_outcome"] == "passed"
    assert interpreted["attempts_by_phase"]["fix_mutation"] == 1


def test_java_measure_mutation_routes_non_green_pit_suite_to_test_repair(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "uta.language.java.phases.ports.run_pitest",
        lambda *args, **kwargs: (
            False,
            "Mutation testing requires a green suite",
        ),
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.parse_pitest_green_suite_failure",
        lambda output: {
            "failing_test_count": 1,
            "test_class": "pkg.FooTest",
        },
    )

    result = JavaGenerationCycleBackend().run_phase(
        "measure_mutation", _state(tmp_path, mutation_gate=80)
    )

    assert result["phase_outcome"] == "tests_failed"
    assert result["evidence"]["failure_reason"] == (
        "mutation_requires_green_suite"
    )
    assert result["evidence"]["test_selector"] == "FooTest"


def test_java_measure_mutation_stops_at_the_hard_cap(tmp_path, monkeypatch):
    _patch_java_pit(monkeypatch, score=55.0, survived=4)

    state = _state(
        tmp_path,
        mutation_gate=80,
        attempts_by_phase={"fix_mutation": 4},
        max_attempts_by_phase={"fix_mutation": 4},
    )
    result = apply_repair_progress(
        "measure_mutation",
        state,
        JavaGenerationCycleBackend().run_phase("measure_mutation", state),
    )

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == (
        "mutation_repair_attempts_exhausted"
    )


def test_java_targeted_tests_route_to_delegated_gate_when_configured(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_test_selector",
        lambda *args, **kwargs: (True, "ok"),
    )

    result = JavaGenerationCycleBackend().run_phase(
        "verify_tests",
        _state(tmp_path, quality_gate_backend="maven_enforcer"),
    )

    assert result["phase_outcome"] == "delegated"
    assert result["evidence"]["tests_pass"] is True


def test_java_delegated_gate_is_skipped_for_builtin_quality(tmp_path):
    result = JavaGenerationCycleBackend().run_phase(
        "delegated_quality_gate_verify", _state(tmp_path)
    )

    assert result == {
        "phase_outcome": "skipped",
        "evidence": {"reason": "delegated_quality_gate_not_configured"},
    }


def test_java_delegated_gate_passes_without_opening_a_repair_turn(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_delegated_quality_gate_once",
        lambda state, repo, **kwargs: calls.append((repo, kwargs))
        or {
            "passed": True,
            "status": "passed",
            "summary": "all delegated gates passed",
            "command": ["mvn", "verify"],
        },
    )

    result = JavaGenerationCycleBackend().run_phase(
        "delegated_quality_gate_verify",
        _state(tmp_path, quality_gate_backend="maven_enforcer"),
    )

    assert result["phase_outcome"] == "passed"
    assert calls == [
        (
            str(tmp_path),
            {"batch": ["pkg.Foo"], "target_scoped": True},
        )
    ]


def test_java_delegated_failure_renders_scoped_repair_then_reverifies(
    tmp_path, monkeypatch
):
    gate = {
        "passed": False,
        "status": "failed",
        "summary": "diff coverage gate failed",
        "stdout": "[test-enforcer] diff line coverage below required",
        "command": ["mvn", "verify"],
    }
    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_delegated_quality_gate_once",
        lambda *args, **kwargs: gate,
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports.expected_ci_incremental_java_test_paths",
        lambda state, batch: ["src/test/java/pkg/FooTest.java"],
    )
    backend = JavaGenerationCycleBackend()
    verified = backend.run_phase(
        "delegated_quality_gate_verify",
        _state(tmp_path, quality_gate_backend="maven_enforcer"),
    )
    repair_state = _state(
        tmp_path,
        quality_gate_backend="maven_enforcer",
        phase_results={"delegated_quality_gate_verify": verified},
        attempts_by_phase={},
        turn_status="completed",
        turn_session_id="delegated-session",
    )

    prompt = backend.render_prompt("delegated_quality_gate", repair_state)
    interpreted = backend.interpret(
        "delegated_quality_gate", repair_state, {"status": "completed"}
    )

    assert verified["phase_outcome"] == "repair"
    assert "diff coverage gate failed" in prompt
    assert "src/test/java/pkg/FooTest.java" in prompt
    assert "Do not edit unrelated existing tests" in prompt
    assert interpreted["phase_outcome"] == "passed"
    assert interpreted["attempts_by_phase"]["delegated_quality_gate"] == 1


def test_java_delegated_gate_fails_closed_for_another_module(
    tmp_path, monkeypatch
):
    gate = {
        "passed": False,
        "status": "failed",
        "summary": "Failed to execute goal on project other-module",
        "stdout": "[INFO] other-module ........ FAILURE",
        "command": ["mvn", "verify"],
    }
    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_delegated_quality_gate_once",
        lambda *args, **kwargs: gate,
    )
    monkeypatch.setattr(
        "uta.language.java.phases.ports._delegated_gate_failure_matches_batch",
        lambda *args, **kwargs: False,
    )

    result = JavaGenerationCycleBackend().run_phase(
        "delegated_quality_gate_verify",
        _state(tmp_path, quality_gate_backend="maven_enforcer"),
    )

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == (
        "delegated_gate_failure_outside_batch"
    )
    assert result["evidence"]["quality_gate_result"][
        "outOfScopeGateFailure"
    ]["currentBatch"] == ["pkg.Foo"]


def test_java_delegated_gate_stops_after_existing_repair_budget(
    tmp_path, monkeypatch
):
    from uta.testgen.repair_progress import apply_repair_progress
    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_delegated_quality_gate_once",
        lambda *args, **kwargs: {
            "passed": False,
            "status": "failed",
            "summary": "mutation gate failed",
            "command": ["mvn", "verify"],
        },
    )

    state = _state(
        tmp_path,
        quality_gate_backend="maven_enforcer",
        attempts_by_phase={"delegated_quality_gate": 2},
        max_attempts_by_phase={"delegated_quality_gate": 2},
    )
    result = apply_repair_progress(
        "delegated_quality_gate_verify", state,
        JavaGenerationCycleBackend().run_phase("delegated_quality_gate_verify", state),
    )

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == "mutation_repair_attempts_exhausted"


def test_delegated_pit_scores_reach_progress_policy(tmp_path, monkeypatch):
    from uta.testgen.repair_progress import apply_repair_progress

    monkeypatch.setattr(
        "uta.language.java.phases.ports._run_delegated_quality_gate_once",
        lambda *args, **kwargs: {
            "passed": False, "status": "failed", "summary": "mutation gate failed",
            "command": ["mvn", "verify"],
            "stdout": ">> Generated 92 mutations Killed 88 (96%)\n"
            ">> Mutations with no coverage 1. Test strength 97%\n"
            "[ERROR] Test strength score of 97 is below threshold of 100",
        },
    )
    state = _state(tmp_path, quality_gate_backend="maven_enforcer",
                   max_attempts_by_phase={"delegated_quality_gate": 6})
    backend = JavaGenerationCycleBackend()
    measured = backend.run_phase("delegated_quality_gate_verify", state)
    assert measured["evidence"]["scores_by_target"]["pkg.Foo"] == 88 / 91 * 100
    first = apply_repair_progress("delegated_quality_gate_verify", state, measured)
    state.update(best_scores_by_phase=first["best_scores_by_phase"],
                 attempts_by_phase={"delegated_quality_gate": 1})
    corrective = apply_repair_progress("delegated_quality_gate_verify", state, measured)
    assert corrective["phase_outcome"] == "repair"
    state.update(corrective)
    state["attempts_by_phase"] = {"delegated_quality_gate": 2}
    stopped = apply_repair_progress("delegated_quality_gate_verify", state, measured)
    assert stopped["evidence"]["failure_reason"] == "mutation_repair_no_progress"


def test_java_plan_and_generation_are_phase_sized_agent_turns(tmp_path):
    context = tmp_path / "Foo.context.md"
    symbols = tmp_path / "Foo.symbols.md"
    source = tmp_path / "Foo.java"
    context.write_text("# Target\n", encoding="utf-8")
    symbols.write_text("# Symbols\n", encoding="utf-8")
    source.write_text("class Foo {}\n", encoding="utf-8")
    backend = JavaGenerationCycleBackend()
    state = _state(
        tmp_path,
        context_dir=str(tmp_path),
        target_context_paths={
            "pkg.Foo": {
                "context_abs": str(context),
                "symbols_abs": str(symbols),
                "source_abs": str(source),
            }
        },
        strict_coverage_classes=[],
        roi_enabled=False,
        plan_index_query_command="uta index query",
        generation_index_query_command="uta index query",
        project_prompt_paths={},
        ci_diff_coverage_gate=80,
        ci_diff_mutation_gate=0,
        phase_results={},
        attempts_by_phase={},
        max_attempts_by_phase={"plan_tests": 1, "generate_tests": 1},
        turn_status="completed",
        turn_text="Plan every public branch.",
    )

    plan_prompt = backend.render_prompt("plan_tests", state)
    planned = backend.interpret(
        "plan_tests",
        state,
        {"status": "completed", "text": "Plan every public branch.", "session_id": "plan-1"},
    )
    generation_state = {
        **state,
        "phase_results": {"plan_tests": planned},
        "turn_text": "generated",
    }
    generation_prompt = backend.render_prompt("generate_tests", generation_state)
    test_file = tmp_path / "src/test/java/pkg/FooTest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("class FooTest {}\n", encoding="utf-8")
    generated = backend.interpret(
        "generate_tests",
        generation_state,
        {"status": "completed", "text": "generated", "session_id": "gen-1"},
    )

    assert "pkg.Foo" in plan_prompt
    assert planned["phase_outcome"] == "passed"
    assert (tmp_path / ".uta_cache/context/latest_generation_plan.md").is_file()
    assert "APPROVED TEST PLAN" in generation_prompt
    assert generated["phase_outcome"] == "passed"
    assert generated["evidence"]["missing_test_files"] == []


def test_java_generation_retries_only_the_missing_batch_files(tmp_path):
    backend = JavaGenerationCycleBackend()
    state = _state(
        tmp_path,
        batch=["pkg.Foo", "pkg.Bar"],
        phase_results={},
        attempts_by_phase={},
        max_attempts_by_phase={"generate_tests": 1},
        turn_status="completed",
    )

    first = backend.interpret(
        "generate_tests", state, {"status": "completed", "session_id": "gen-1"}
    )
    recovery = backend.render_prompt(
        "generate_tests", {**state, "phase_results": {"generate_tests": first}}
    )

    assert first["phase_outcome"] == "retry"
    assert first["attempts_by_phase"]["generate_tests"] == 1
    assert "src/test/java/pkg/FooTest.java" in recovery
    assert "src/test/java/pkg/BarTest.java" in recovery


def test_java_completion_projects_durable_phase_and_turn_evidence(tmp_path):
    test_file = tmp_path / "src/test/java/pkg/FooTest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("class FooTest {}\n", encoding="utf-8")
    state = _state(
        tmp_path,
        phase_results={
            "precheck_existing_tests": {"phase_outcome": "proceed"},
            "plan_tests": {"phase_outcome": "passed", "evidence": {}},
            "generate_tests": {
                "phase_outcome": "passed",
                "evidence": {"output": "generated tests"},
            },
            "verify_compile": {
                "phase_outcome": "passed",
                "evidence": {"elapsed_seconds": 0.4},
            },
            "verify_tests": {
                "phase_outcome": "passed",
                "evidence": {"tests_pass": True, "elapsed_seconds": 0.8},
            },
            "measure_coverage": {
                "phase_outcome": "passed",
                "evidence": {"coverage_by_class": {"pkg.Foo": 92.0}},
            },
            "measure_mutation": {
                "phase_outcome": "passed",
                "evidence": {
                    "mutation_stats_by_class": {
                        "pkg.Foo": {"score": 85.0, "total": 20, "killed": 17, "survived": 3}
                    },
                    "elapsed_seconds": 1.2,
                },
            },
        },
        turn_history=[
            {
                "phase": "generate_tests",
                "session_id": "gen-1",
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "elapsed_seconds": 2.5,
            }
        ],
    )

    result = JavaGenerationCycleBackend().run_phase("complete_generation", state)
    target = result["results"]["pkg.Foo"]

    assert result["phase_outcome"] == "passed"
    assert target["status"] == "PASS"
    assert target["coverage"] == 92.0
    assert target["mutation_score"] == 85.0
    assert target["session_ids"] == ["gen-1"]
    assert result["session_token_usage"] == {"input_tokens": 100, "output_tokens": 20}


def test_java_generation_result_never_projects_raw_model_text(tmp_path):
    state = _state(
        tmp_path,
        batch=["pkg.Foo"],
        turn_status="failed",
        turn_text="PRIVATE MODEL OUTPUT AND TOOL DETAIL",
    )

    interpreted = JavaGenerationCycleBackend().interpret(
        "generate_tests",
        state,
        {
            "status": "failed",
            "session_id": "gen-1",
            "text": "PRIVATE MODEL OUTPUT AND TOOL DETAIL",
        },
    )

    serialized = json.dumps(interpreted, sort_keys=True)
    assert "PRIVATE MODEL OUTPUT" not in serialized
    assert '"output"' not in serialized


def test_java_delegated_quality_gate_preserves_explicit_target_scope(tmp_path, monkeypatch):
    from uta.language.java.generation.quality import _run_delegated_quality_gate_once

    captured = {}

    def fake_enforce(*args, **kwargs):
        captured.update(kwargs)
        return {"passed": True, "status": "passed"}

    monkeypatch.setattr(
        "uta.language.java.enforcement.run_java_enforcement",
        fake_enforce,
    )

    state = _state(
        tmp_path,
        backend_context={
            "enforcement": {"command": "mvn test"},
            "selection": {"targets": [{"class_fqn": "pkg.Foo", "source_path": "src/main/java/pkg/Foo.java"}]},
        },
    )
    _run_delegated_quality_gate_once(
        state,
        str(tmp_path),
        batch=["pkg.Foo"],
        target_scoped=True,
    )
    assert captured.get("preserve_explicit_target_scope") is True


def test_java_later_mutation_rounds_narrow_and_see_the_previous_round(
    tmp_path, monkeypatch
):
    """Round two must differ from round one.

    `focused_group_count=len(groups)` made every later round select the whole
    survivor set, so a second round was round one with the ROI guidance
    dropped; and a round state carrying only the attempt index left the
    planner's `no_progress` signal permanently false, so it could not tell a
    repair that moved survivors from one that changed nothing.
    """
    _patch_java_pit(monkeypatch, score=55.0, survived=4)
    monkeypatch.setattr(
        "uta.language.java.phases.ports.summarize_surviving_mutants",
        lambda *args, **kwargs: [
            {"method": f"m{i}", "family": "conditional", "count": 1, "mutator": "NEG",
             "lines": [i], "survivors": [{"line": i, "mutation_type": "NEG"}]}
            for i in range(1, 9)
        ],
    )
    monkeypatch.setattr(
        "uta.language.java.phases.mutation.uta_settings.mutation_repair_groups_per_round",
        2,
    )
    backend = JavaGenerationCycleBackend()

    first = backend.run_phase(
        "measure_mutation", _state(tmp_path, mutation_gate=80, batch=["pkg.Foo"])
    )
    second = backend.run_phase(
        "measure_mutation",
        _state(
            tmp_path,
            mutation_gate=80,
            batch=["pkg.Foo"],
            attempts_by_phase={"fix_mutation": 1},
            phase_results={"measure_mutation": first},
        ),
    )

    assert first["evidence"]["repair_round_by_class"]["pkg.Foo"] == "full_roi"
    assert len(first["evidence"]["families_by_class"]["pkg.Foo"]) == 8
    # The cleanup round narrows onto the top groups instead of repeating all.
    assert second["evidence"]["repair_round_by_class"]["pkg.Foo"] == "focused_roi"
    assert len(second["evidence"]["families_by_class"]["pkg.Foo"]) == 2

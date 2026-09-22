"""Java implementation of the neutral test-generation lifecycle contracts."""

from __future__ import annotations

import time
from typing import Any, Dict

from uta.language.java.baseline import baseline_compile
from uta.language.java.baseline import _mockito_api_guidance
from uta.language.java.candidates import parse_context, scan_and_select
from uta.language.java.generation_plan import (
    _plan_needs_stricter_replan,
    _write_generation_plan,
    _write_generation_plan_candidate,
    _compress_plan_for_generation,
)
from uta.language.java.phases.compile import (
    interpret_fix_compile,
    render_fix_compile_prompt,
    verify_compile,
)
from uta.language.java.phases.delegated_quality import (
    interpret_delegated_quality_gate,
    render_delegated_quality_gate_prompt,
    verify_delegated_quality_gate,
)
from uta.language.java.phases.completion import complete_generation
from uta.language.java.phases.generation_turn import (
    interpret_generate_tests,
    render_generate_tests_prompt,
)
from uta.language.java.phases.coverage import (
    interpret_fix_coverage,
    measure_coverage,
    render_fix_coverage_prompt,
)
from uta.language.java.phases.precheck import phase_result, precheck_existing_tests
from uta.language.java.phases.planning import (
    interpret_plan_tests,
    render_plan_tests_prompt,
)
from uta.language.java.phases.mutation import (
    interpret_fix_mutation,
    measure_mutation,
    render_fix_mutation_prompt,
)
from uta.language.java.phases.test_verification import verify_tests
from uta.language.java.phases.test_repair import (
    interpret_fix_tests,
    render_fix_tests_prompt,
)
from uta.language.java.phases.ports import (
    java_compile_ports,
    java_completion_ports,
    java_coverage_ports,
    java_delegated_quality_ports,
    java_mutation_ports,
    java_precheck_ports,
    java_test_repair_ports,
    java_test_verification_ports,
    select_next_class,
)
from uta.testgen.delivery import commit_to_branch, store_and_push
from uta.testgen.workspace_setup import setup_branch

# Java retains its class-oriented implementation internally while satisfying
# the target-oriented workflow contract at the adapter boundary.
prepare_workspace = setup_branch
baseline_validate = baseline_compile
select_targets = scan_and_select
prepare_context = parse_context
select_next_target = select_next_class
deliver_target = commit_to_branch
finalize = store_and_push


class JavaGenerationCycleBackend:
    """Phase-sized Java backend consumed by the durable child workflow."""

    language = "java"

    @staticmethod
    def output_paths(state: Dict[str, Any]):
        from uta.language.java.workspace import expected_test_file_rel

        return [
            expected_test_file_rel(state.get("module"), target)
            for target in state.get("batch") or []
        ]

    def scoring_survivors(self, state: Dict[str, Any]):
        """The survivors the stalled delegated gate counted, for the review.

        CI repair measures mutation through the delegated UTA gate, so its
        result -- not a per-class PIT run -- is what the review must match.
        """
        from uta.language.java.equivalence import java_scoring_survivors

        verified = (state.get("phase_results") or {}).get("delegated_quality_gate_verify") or {}
        gate = (verified.get("evidence") or {}).get("quality_gate_result")
        if not isinstance(gate, dict) or not gate:
            return None
        return java_scoring_survivors(gate, state["repo_path"])

    def run_phase(self, phase: str, state: Dict[str, Any]) -> Dict[str, Any]:
        handler = getattr(self, str(phase), None)
        if handler is None or not callable(handler):
            raise NotImplementedError(f"Java generation phase is not migrated: {phase}")
        return dict(handler(state))

    def render_prompt(self, phase: str, state: Dict[str, Any]) -> str:
        if str(phase) == "plan_tests":
            return render_plan_tests_prompt(state)
        if str(phase) == "generate_tests":
            return render_generate_tests_prompt(
                state,
                ports=_JavaGenerationPromptPorts(),
            )
        if str(phase) == "fix_compile":
            return render_fix_compile_prompt(state, ports=java_compile_ports())
        if str(phase) == "fix_tests":
            return render_fix_tests_prompt(state, ports=java_test_repair_ports())
        if str(phase) == "fix_coverage":
            return render_fix_coverage_prompt(state, ports=java_coverage_ports())
        if str(phase) == "fix_mutation":
            return render_fix_mutation_prompt(state, ports=java_mutation_ports())
        if str(phase) == "delegated_quality_gate":
            return render_delegated_quality_gate_prompt(state)
        raise NotImplementedError(f"Java generation prompt is not migrated: {phase}")

    def interpret(
        self, phase: str, state: Dict[str, Any], turn: Dict[str, Any]
    ) -> Dict[str, Any]:
        if str(phase) == "plan_tests":
            return interpret_plan_tests(
                state, turn or {}, ports=_JavaPlanningPorts()
            )
        if str(phase) == "generate_tests":
            return interpret_generate_tests(state, turn or {})
        if str(phase) == "fix_compile":
            return interpret_fix_compile(state, turn or {})
        if str(phase) == "fix_tests":
            return interpret_fix_tests(state, turn or {})
        if str(phase) == "fix_coverage":
            return interpret_fix_coverage(state, turn or {})
        if str(phase) == "fix_mutation":
            return interpret_fix_mutation(state, turn or {})
        if str(phase) == "delegated_quality_gate":
            return interpret_delegated_quality_gate(state, turn or {})
        raise NotImplementedError(f"Java generation interpreter is not migrated: {phase}")

    def verify_compile(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return verify_compile(state, ports=java_compile_ports())

    def verify_tests(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return verify_tests(state, ports=java_test_verification_ports())

    def measure_coverage(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return measure_coverage(state, ports=java_coverage_ports())

    def measure_mutation(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return measure_mutation(state, ports=java_mutation_ports())

    def delegated_quality_gate_verify(
        self, state: Dict[str, Any]
    ) -> Dict[str, Any]:
        return verify_delegated_quality_gate(
            state, ports=java_delegated_quality_ports()
        )

    def complete_generation(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return complete_generation(state, ports=java_completion_ports())

    def precheck_existing_tests(self, state: Dict[str, Any]) -> Dict[str, Any]:
        batch = list(state.get("batch") or [])
        if not batch:
            return {
                "phase_outcome": "failed",
                "error": "Java precheck requires a non-empty stable batch",
            }
        prechecked = precheck_existing_tests(
            state={
                **state,
                "current_batch": batch,
                "results": dict(state.get("results") or {}),
            },
            repo_path=str(state["repo_path"]),
            module=state.get("module"),
            batch=batch,
            coverage_gate=int(state.get("coverage_gate") or 0),
            mutation_gate_score=int(state.get("mutation_gate") or 0),
            node_started=time.perf_counter(),
            ports=java_precheck_ports(),
        )
        return phase_result(prechecked)


class _JavaPlanningPorts:
    plan_needs_stricter_replan = staticmethod(_plan_needs_stricter_replan)
    write_plan_candidate = staticmethod(_write_generation_plan_candidate)
    write_plan = staticmethod(_write_generation_plan)


class _JavaGenerationPromptPorts:
    mockito_api_guidance = staticmethod(_mockito_api_guidance)
    compress_plan = staticmethod(_compress_plan_for_generation)


__all__ = [
    "prepare_workspace",
    "baseline_validate",
    "select_targets",
    "prepare_context",
    "select_next_target",
    "deliver_target",
    "finalize",
    "JavaGenerationCycleBackend",
]

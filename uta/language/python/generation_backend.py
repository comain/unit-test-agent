"""Python implementation of the neutral test-generation lifecycle contract."""

from __future__ import annotations

from typing import Any, Dict, Optional

from uta.testgen.batch import BatchGenerationRequest, batch_agent_turn_state
from uta.testgen.progress import set_stage
from uta.language.python import phases as cycle_phases


def build_python_initial_state(
    request: BatchGenerationRequest,
    *,
    enforcer: Optional[Any] = None,
    prompt_artifact_scope: Optional[Any] = None,
    harness_factory: Optional[Any] = None,
) -> Dict[str, Any]:
    target_selections = [target.as_selection() for target in request.targets]
    return {
        **batch_agent_turn_state(request),
        "repo_path": str(request.repo_path),
        "language": "python",
        "candidates": [target.target_id for target in request.targets],
        "target_candidates": target_selections,
        "current_target": None,
        "current_target_batch": [],
        "current_class": None,
        "current_batch": [],
        "results": {},
        "session_ids": [],
        "session_refs": [],
        "session_token_usage": {},
        "session_retrospect": {},
        "phase_token_usage": {},
        "phase_timings": {},
        "current_stage": "startup",
        "error": None,
        "finished": False,
        "stopped_early": False,
        "task_id": request.task_id,
        "task_db_path": str(request.task_db_path) if request.task_db_path else None,
        "backend_context": {
            "request": request,
            "enforcer": enforcer,
            "prompt_artifact_scope": prompt_artifact_scope,
            "harness_factory": harness_factory,
            "targets_by_id": {target.target_id: target for target in request.targets},
        },
    }


def run_python_generation_workflow(
    request: BatchGenerationRequest,
    *,
    enforcer: Optional[Any] = None,
    workflow_app: Optional[Any] = None,
    prompt_artifact_scope: Optional[Any] = None,
    harness_factory: Optional[Any] = None,
) -> Dict[str, Any]:
    if workflow_app is None:
        from uta.testgen.graph.workflow import build_workflow

        workflow_app = build_workflow()
    return workflow_app.invoke(
        build_python_initial_state(
            request,
            enforcer=enforcer,
            prompt_artifact_scope=prompt_artifact_scope,
            harness_factory=harness_factory,
        )
    )


def prepare_workspace(state: Dict[str, Any]) -> Dict[str, Any]:
    return set_stage(state, "prepare_workspace", "use caller-provided Python workspace")


def baseline_validate(state: Dict[str, Any]) -> Dict[str, Any]:
    return set_stage(state, "baseline_validate", "defer Python verification to each target")


def select_targets(state: Dict[str, Any]) -> Dict[str, Any]:
    from uta.language.python.selection import prepare_python_batch_targets

    context = dict(state["backend_context"])
    targets, prepared_results, _manager = prepare_python_batch_targets(context["request"])
    target_selections = [target.as_selection() for target in targets]
    context["targets_by_id"] = {target.target_id: target for target in targets}
    return {
        **set_stage(state, "select_targets", f"targets={len(targets)}"),
        "backend_context": context,
        "candidates": [target.target_id for target in targets],
        "target_candidates": target_selections,
        "results": prepared_results,
    }


def prepare_context(state: Dict[str, Any]) -> Dict[str, Any]:
    return set_stage(state, "prepare_context", "build Python context per target during generation")


def select_next_target(state: Dict[str, Any]) -> Dict[str, Any]:
    remaining = [
        target
        for target in state.get("target_candidates") or []
        if target.get("target_id") not in (state.get("results") or {})
    ]
    if not remaining:
        return {
            "finished": True,
            "current_target": None,
            "current_target_batch": [],
            "current_class": None,
            "current_batch": [],
            "current_stage": "select_next_target",
        }
    return {
        "finished": False,
        "current_target": remaining[0],
        "current_target_batch": [remaining[0]],
        "current_class": None,
        "current_batch": [str(remaining[0]["target_id"])],
        "current_stage": "select_next_target",
    }


def deliver_target(state: Dict[str, Any]) -> Dict[str, Any]:
    # The child stops at verified result evidence. Commit/push and the product
    # terminal projection belong here, after the outer graph checkpoints it.
    from uta.testgen.delivery import commit_to_branch

    return commit_to_branch(state)


def finalize(state: Dict[str, Any]) -> Dict[str, Any]:
    return set_stage(state, "finalize", "Python results already synchronized by generation")


class PythonGenerationCycleBackend:
    """Python's phase contract with one unit-wide repair budget."""

    language = "python"

    @staticmethod
    def output_paths(state: Dict[str, Any]):
        path = state.get("generated_test_path")
        return [str(path)] if path else []

    def __init__(self, *, enforcer=None):
        self.enforcer = enforcer

    def run_phase(self, phase: str, state: Dict[str, Any]) -> Dict[str, Any]:
        handlers = {
            "precheck_existing_tests": lambda: cycle_phases.precheck_existing_tests(
                state, enforcer=self.enforcer
            ),
            "verify_compile": lambda: cycle_phases.verify_compile(state),
            "verify_tests": lambda: cycle_phases.verify_tests(
                state, enforcer=self.enforcer
            ),
            "measure_coverage": lambda: cycle_phases.measure_coverage(state),
            "measure_mutation": lambda: cycle_phases.measure_mutation(
                state, enforcer=self.enforcer
            ),
            "delegated_quality_gate_verify": lambda: {
                "phase_outcome": "skipped",
                "evidence": {"reason": "unsupported_by_current_python_policy"},
            },
            "complete_generation": lambda: cycle_phases.complete_generation(state),
        }
        try:
            return dict(handlers[str(phase)]())
        except KeyError:
            raise NotImplementedError(f"Python generation phase is not migrated: {phase}") from None

    def scoring_survivors(self, state: Dict[str, Any]):
        """The survivors the stalled `measure_mutation` counted, for the review."""
        from uta.language.python.equivalence import python_scoring_survivors

        measured = ((state.get("phase_results") or {}).get("measure_mutation") or {}).get("evidence") or {}
        return python_scoring_survivors(measured.get("mutation"), state["repo_path"])

    def render_prompt(self, phase: str, state: Dict[str, Any]) -> str:
        if phase == "generate_tests":
            return cycle_phases.render_generate_prompt(state)
        if phase in {"fix_compile", "fix_tests", "fix_coverage", "fix_mutation"}:
            return cycle_phases.render_repair_prompt(state, phase)
        raise NotImplementedError(f"Python generation prompt is not migrated: {phase}")

    def interpret(self, phase: str, state: Dict[str, Any], turn: Dict[str, Any]) -> Dict[str, Any]:
        if phase == "generate_tests":
            return cycle_phases.interpret_generate(state, turn or {})
        if phase in {"fix_compile", "fix_tests", "fix_coverage", "fix_mutation"}:
            return cycle_phases.interpret_repair(state, turn or {}, phase)
        raise NotImplementedError(f"Python generation interpreter is not migrated: {phase}")


__all__ = [
    "prepare_workspace",
    "baseline_validate",
    "select_targets",
    "prepare_context",
    "select_next_target",
    "deliver_target",
    "finalize",
    "PythonGenerationCycleBackend",
]

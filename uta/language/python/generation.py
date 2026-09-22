"""Public Python batch API over the durable generation workflow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from uta.testgen.batch import BatchGenerationRequest, BatchGenerationResult
from uta.language.python.test_artifacts import (
    render_generated_test_file,
    write_generated_test_file,
)
from uta.shared.targets import TargetRef


@dataclass(frozen=True)
class PythonBatchGenerationResult(BatchGenerationResult):
    pass


class PythonBatchGenerator:
    """Execute Python generation through the sole declarative workflow."""

    language = "python"

    def __init__(
        self,
        *,
        enforcer: Optional[Callable[..., Dict[str, Any]]] = None,
        workflow_app: Optional[Any] = None,
        harness_factory: Optional[Callable[[Path], Any]] = None,
    ):
        self.enforcer = enforcer
        self.workflow_app = workflow_app
        self.harness_factory = harness_factory

    def run(self, request: BatchGenerationRequest) -> PythonBatchGenerationResult:
        if request.language != "python":
            raise ValueError(
                f"PythonBatchGenerator cannot run language={request.language!r}"
            )
        from uta.language.python.generation_backend import (
            run_python_generation_workflow,
        )
        from uta.testgen.standalone_execution import (
            open_standalone_generation_execution,
        )

        if request.task_id is None and request.task_db_path is None:
            with open_standalone_generation_execution(request) as execution:
                final_state = run_python_generation_workflow(
                    execution.request,
                    enforcer=self.enforcer,
                    workflow_app=self.workflow_app,
                    prompt_artifact_scope=execution.prompt_artifact_scope,
                    harness_factory=self.harness_factory,
                )
                projected = execution.project(final_state)
            return _python_batch_result(projected)

        final_state = run_python_generation_workflow(
            request,
            enforcer=self.enforcer,
            workflow_app=self.workflow_app,
            harness_factory=self.harness_factory,
        )
        return _python_batch_result(final_state)


def run_python_batch_generation(
    *,
    repo_path: Path,
    targets: Iterable[TargetRef],
    task_id: Optional[int] = None,
    task_db_path: Optional[Path] = None,
    enforcer: Optional[Callable[..., Dict[str, Any]]] = None,
    harness_factory: Optional[Callable[[Path], Any]] = None,
    model_id: Optional[str] = None,
    coverage_gate: Optional[float] = None,
    mutation_gate: Optional[float] = None,
    timeout_seconds: int = 1800,
    spec_context: str = "",
    quality_mode: str = "class_batch",
) -> PythonBatchGenerationResult:
    request = BatchGenerationRequest(
        language="python",
        repo_path=Path(repo_path),
        targets=list(targets or []),
        task_id=task_id,
        task_db_path=Path(task_db_path) if task_db_path else None,
        model_id=model_id,
        coverage_gate=coverage_gate,
        mutation_gate=mutation_gate,
        timeout_seconds=timeout_seconds,
        spec_context=spec_context,
        quality_mode=quality_mode,
    )
    return PythonBatchGenerator(
        enforcer=enforcer,
        harness_factory=harness_factory,
    ).run(request)


def _python_batch_result(final_state: Dict[str, Any]) -> PythonBatchGenerationResult:
    return PythonBatchGenerationResult(
        results=final_state.get("results", {}) or {},
        session_ids=list(final_state.get("session_ids") or []),
        session_token_usage=final_state.get("session_token_usage", {}) or {},
        session_retrospect=final_state.get("session_retrospect", {}) or {},
        phase_token_usage=final_state.get("phase_token_usage", {}) or {},
        phase_timings=final_state.get("phase_timings", {}) or {},
        final_state=final_state,
        final_error=(
            str(final_state["error"]) if final_state.get("error") else None
        ),
        stopped=bool(final_state.get("stopped_early")),
    )


__all__ = [
    "PythonBatchGenerationResult",
    "PythonBatchGenerator",
    "render_generated_test_file",
    "run_python_batch_generation",
    "write_generated_test_file",
]

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from uta.testgen.batch import (
    BatchGenerationRequest,
    BatchGenerationResult,
    batch_agent_turn_state,
)
from uta.shared.targets import coerce_targets
from uta.language.java.test_quality import attach_java_test_quality


@dataclass(frozen=True)
class JavaBatchGenerationRequest(BatchGenerationRequest):
    language: str = "java"
    module: Optional[str] = None
    module_filter: Optional[str] = None
    days: int = 30
    max_files: int = 10
    select_all_files: bool = False
    class_fqns: List[str] = field(default_factory=list)
    explicit_targets: List[str] = field(default_factory=list)
    classes_per_run: int = 1
    branch_name: str = "unit-code-gen"
    started_at: float = 0.0
    stop_after_stage: Optional[str] = None
    resume: bool = False
    preserve_branch: bool = False
    quality_gate_backend: str = "builtin"
    quality_gate_command: str = ""
    rdc_context: Dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    session_ids: List[str] = field(default_factory=list)
    run_log_path: Optional[str] = None
    production: bool = False
    language_decision: Dict[str, Any] = field(default_factory=dict)
    phase_timings: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_class_fqns(
        cls,
        *,
        repo_path: Path,
        class_fqns: Iterable[str],
        module: Optional[str] = None,
        task_id: Optional[int] = None,
        task_db_path: Optional[Path] = None,
        **kwargs: Any,
    ) -> "JavaBatchGenerationRequest":
        classes = list(dict.fromkeys(str(item) for item in class_fqns or []))
        targets = coerce_targets(classes)
        return cls(
            repo_path=Path(repo_path),
            targets=targets,
            class_fqns=classes,
            module=module,
            module_filter=module,
            task_id=task_id,
            task_db_path=Path(task_db_path) if task_db_path else None,
            **kwargs,
        )


@dataclass(frozen=True)
class JavaBatchGenerationResult(BatchGenerationResult):
    pass


class JavaBatchGenerator:
    language = "java"

    def __init__(
        self,
        workflow_app: Optional[Any] = None,
        harness_factory: Optional[Any] = None,
    ):
        self.workflow_app = workflow_app
        self.harness_factory = harness_factory

    def run(self, request: BatchGenerationRequest) -> JavaBatchGenerationResult:
        if not isinstance(request, JavaBatchGenerationRequest):
            raise TypeError("JavaBatchGenerator requires JavaBatchGenerationRequest")
        return run_java_batch_generation(
            request,
            workflow_app=self.workflow_app,
            harness_factory=self.harness_factory,
        )


def build_java_initial_state(request: JavaBatchGenerationRequest) -> Dict[str, Any]:
    class_fqns = list(request.class_fqns or [target.target_id for target in request.targets])
    return {
        **batch_agent_turn_state(request),
        "repo_path": str(Path(request.repo_path)),
        "module": request.module,
        "module_filter": request.module_filter if request.module_filter is not None else request.module,
        "days": request.days,
        "max_files": request.max_files,
        "select_all_files": request.select_all_files,
        "explicit_class_fqns": class_fqns,
        "language": "java",
        "language_decision": request.language_decision,
        "explicit_targets": list(request.explicit_targets or []),
        "current_target": None,
        "current_target_batch": [],
        "coverage_gate": request.coverage_gate,
        "mutation_gate": request.mutation_gate,
        "quality_mode": request.quality_mode,
        "quality_gate_backend": request.quality_gate_backend,
        "quality_gate_command": request.quality_gate_command,
        "rdc_context": dict(request.rdc_context or {}),
        "spec_context": str(request.spec_context or (request.rdc_context or {}).get("specContext") or ""),
        "classes_per_agent_run": request.classes_per_run,
        "branch_name": request.branch_name,
        "started_at": request.started_at,
        "stop_after_stage": request.stop_after_stage,
        "resume": request.resume,
        "preserve_branch": request.preserve_branch,
        "candidates": [],
        "current_class": None,
        "current_batch": [],
        "graph": None,
        "flows": [],
        "session_id": request.session_id,
        "session_ids": list(request.session_ids or ([request.session_id] if request.session_id else [])),
        "session_refs": [],
        "results": {},
        "phase_timings": dict(request.phase_timings or {}),
        "phase_token_usage": {},
        "session_retrospect": {},
        "session_token_usage": {},
        "run_log_path": request.run_log_path,
        "production": request.production,
        "task_id": request.task_id,
        "task_db_path": str(request.task_db_path) if request.task_db_path else None,
        "current_stage": "startup",
        "error": None,
        "finished": False,
        "stopped_early": False,
    }


def run_java_batch_generation(
    request: JavaBatchGenerationRequest,
    *,
    workflow_app: Optional[Any] = None,
    harness_factory: Optional[Any] = None,
) -> JavaBatchGenerationResult:
    from uta.testgen.standalone_execution import open_standalone_generation_execution

    if workflow_app is None:
        from uta.testgen.graph.workflow import build_workflow

        workflow_app = build_workflow()
    if request.task_id is None and request.task_db_path is None:
        with open_standalone_generation_execution(request) as execution:
            final_state = _invoke_java_workflow(
                execution.request,
                workflow_app=workflow_app,
                prompt_artifact_scope=execution.prompt_artifact_scope,
                harness_factory=harness_factory,
            )
            projected = execution.project(final_state)
        return _java_batch_result(request, projected)
    final_state = _invoke_java_workflow(
        request,
        workflow_app=workflow_app,
        prompt_artifact_scope=None,
        harness_factory=harness_factory,
    )
    return _java_batch_result(request, final_state)


def _invoke_java_workflow(
    request: JavaBatchGenerationRequest,
    *,
    workflow_app: Any,
    prompt_artifact_scope: Any,
    harness_factory: Optional[Any],
) -> Dict[str, Any]:
    initial_state = build_java_initial_state(request)
    initial_state["backend_context"] = {
        **dict(initial_state.get("backend_context") or {}),
        "prompt_artifact_scope": prompt_artifact_scope,
        "harness_factory": harness_factory,
    }
    return workflow_app.invoke(initial_state)


def _java_batch_result(
    request: JavaBatchGenerationRequest, final_state: Dict[str, Any]
) -> JavaBatchGenerationResult:
    results = final_state.get("results", {})
    attach_java_test_quality(request.repo_path, results)
    final_error = str(final_state["error"]) if final_state.get("error") else None
    return JavaBatchGenerationResult(
        results=results,
        session_ids=list(final_state.get("session_ids") or []),
        session_token_usage=final_state.get("session_token_usage", {}) or {},
        session_retrospect=final_state.get("session_retrospect", {}) or {},
        phase_token_usage=final_state.get("phase_token_usage", {}) or {},
        phase_timings=final_state.get("phase_timings", {}) or {},
        final_state=final_state,
        final_error=final_error,
        stopped=bool(final_state.get("stopped_early")),
    )

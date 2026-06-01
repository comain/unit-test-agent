from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from uta.engine.batch import BatchGenerationRequest, BatchGenerationResult
from uta.engine.targets import TargetRef, coerce_targets


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
    quality_mode: str = "class_batch"
    quality_gate_backend: str = "builtin"
    quality_gate_command: str = ""
    ci_context: Dict[str, Any] = field(default_factory=dict)
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

    def __init__(self, workflow_app: Optional[Any] = None):
        self.workflow_app = workflow_app

    def run(self, request: BatchGenerationRequest) -> JavaBatchGenerationResult:
        if not isinstance(request, JavaBatchGenerationRequest):
            raise TypeError("JavaBatchGenerator requires JavaBatchGenerationRequest")
        return run_java_batch_generation(request, workflow_app=self.workflow_app)


def build_java_initial_state(request: JavaBatchGenerationRequest) -> Dict[str, Any]:
    class_fqns = list(request.class_fqns or [target.target_id for target in request.targets])
    return {
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
        "ci_context": dict(request.ci_context or {}),
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
) -> JavaBatchGenerationResult:
    if workflow_app is None:
        from uta.graph.workflow import build_workflow

        workflow_app = build_workflow()
    final_state = workflow_app.invoke(build_java_initial_state(request))
    results = final_state.get("results", {})
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

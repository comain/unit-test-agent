from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol

from uta.shared.targets import TargetRef, coerce_targets


@dataclass(frozen=True)
class BatchGenerationRequest:
    """Input contract for one language-neutral batch generation run.

    Language-specific batch generators consume this request shape so the
    scheduler, CLI, and task DB do not need Java/Python specific parameters.
    """

    repo_path: Path
    targets: List[TargetRef]
    language: str
    task_id: Optional[int] = None
    task_db_path: Optional[Path] = None
    model_id: Optional[str] = None
    coverage_gate: Optional[float] = None
    mutation_gate: Optional[float] = None
    timeout_seconds: int = 1800
    spec_context: str = ""
    quality_mode: str = "class_batch"

    @classmethod
    def from_targets(
        cls,
        *,
        language: str,
        repo_path: Path,
        targets: Iterable[Any],
        task_id: Optional[int] = None,
        task_db_path: Optional[Path] = None,
        model_id: Optional[str] = None,
        coverage_gate: Optional[float] = None,
        mutation_gate: Optional[float] = None,
        timeout_seconds: int = 1800,
        spec_context: str = "",
        quality_mode: str = "class_batch",
    ) -> "BatchGenerationRequest":
        return cls(
            language=language,
            repo_path=Path(repo_path),
            targets=coerce_targets(targets),
            task_id=task_id,
            task_db_path=Path(task_db_path) if task_db_path else None,
            model_id=model_id,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            timeout_seconds=timeout_seconds,
            spec_context=spec_context,
            quality_mode=quality_mode,
        )


def batch_agent_turn_state(request: BatchGenerationRequest) -> Dict[str, Any]:
    """Project language-neutral agent-turn settings into workflow state.

    Agent-core resolves these values from serializable workflow state. Keeping
    the projection here prevents a language backend from silently falling back
    to the harness default model or timeout.
    """
    # Phase policy belongs to the shared workflow, not a language backend.
    # Keep these explicit: an omitted node timeout otherwise inherits the
    # harness-wide fallback, which is intentionally much larger than a repair
    # turn and can prevent deterministic verification from regaining control.
    from uta.shared.config import settings

    return {
        "model_id": request.model_id,
        "coverage_gate": request.coverage_gate,
        "mutation_gate": request.mutation_gate,
        "timeout_seconds": request.timeout_seconds,
        "planning_timeout_seconds": int(settings.opencode_planning_timeout_seconds),
        "compile_fix_timeout_seconds": int(settings.opencode_compile_fix_timeout_seconds),
        "repair_timeout_seconds": int(settings.opencode_repair_timeout_seconds),
        "quality_mode": request.quality_mode,
    }


@dataclass(frozen=True)
class BatchGenerationResult:
    """Output contract returned by any language batch generator.

    It normalizes generated test results, token accounting, timings, and final
    workflow state so downstream reporting can stay language-agnostic.
    """

    results: Dict[str, Dict[str, Any]]
    session_ids: List[str] = field(default_factory=list)
    session_token_usage: Dict[str, Any] = field(default_factory=dict)
    session_retrospect: Dict[str, Any] = field(default_factory=dict)
    phase_token_usage: Dict[str, Any] = field(default_factory=dict)
    phase_timings: Dict[str, float] = field(default_factory=dict)
    final_state: Dict[str, Any] = field(default_factory=dict)
    final_error: Optional[str] = None
    stopped: bool = False


class BatchGenerator(Protocol):
    """Protocol implemented by each language batch generation backend."""

    language: str

    def run(self, request: BatchGenerationRequest) -> BatchGenerationResult:
        ...

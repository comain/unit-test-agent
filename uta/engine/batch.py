from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol

from uta.engine.targets import TargetRef, coerce_targets


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
        )


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

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional, Protocol

from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.shared.ci_models import CiTaskRecord
from uta.enforcement.enforcement import QualityGateResult

if TYPE_CHECKING:
    from uta.enforcement.equivalent_mutants import ScoringSurvivors
    from uta.tasks.manager import TaskManager


class EnforcementRunner(Protocol):
    """Subordinate infrastructure port: run one local quality gate command.

    This is the single definition of the port. It executes an external command
    in a repository and returns the classified :class:`QualityGateResult`. It is
    deliberately *not* the product enforcement API -- that contract lives in
    ``uta_enforce_core``. ``uta.app.ci`` re-exports this name rather than
    declaring a second, competing protocol of the same name.
    """

    def run(self, repo_path: Path) -> QualityGateResult:
        ...


class CiLanguageHandler(Protocol):
    """Adapts CI repair and enforcement behavior for one backend language.

    The API trigger asks handlers to match a task record and create the right
    incremental generation task without branching on Java/Python in core code.
    """

    language: str
    quality_gate_backend: str
    runner: Optional[EnforcementRunner]

    def runner_for(self, record: CiTaskRecord) -> Optional[EnforcementRunner]:
        ...

    def matches(self, record: CiTaskRecord) -> bool:
        ...

    def create_repair_task(
        self,
        *,
        task_manager: TaskManager,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        priority: int,
        base_ref: str,
        coverage_gate: float,
        mutation_gate: float,
        rdc_context: Dict[str, Any],
        rdc_context_path: Optional[str],
    ) -> int:
        ...

    def repair_target_ids(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        base_ref: str,
    ) -> tuple[str, ...]:
        ...

    def validate_repair_context(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        base_ref: str,
        rdc_context: Dict[str, Any],
        target_ids: tuple[str, ...],
    ) -> None:
        ...

    def completed_task_enforcement_result(
        self,
        *,
        record: CiTaskRecord,
        task_manager: TaskManager,
        repo_task: Dict[str, Any],
    ):
        ...

    def should_rerun_after_repair_workspace_refresh(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
    ) -> bool:
        ...

    def run_repair_preflight(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
    ):
        ...

    def scoring_survivors(
        self,
        *,
        record: CiTaskRecord,
        result: QualityGateResult,
        repo_path: Path,
    ) -> Optional["ScoringSurvivors"]:
        ...

    def gate_failure_flags(
        self,
        *,
        record: CiTaskRecord,
        result: QualityGateResult,
    ) -> Optional[Dict[str, bool]]:
        ...


class CiLanguageHandlerRegistry:
    """Registry that selects the CI language handler for a task record."""

    def __init__(self, handlers: Iterable[CiLanguageHandler], default_language: str = "java") -> None:
        self._handlers = {handler.language: handler for handler in handlers}
        self.default_language = default_language

    @property
    def handlers(self) -> tuple[CiLanguageHandler, ...]:
        return tuple(self._handlers[language] for language in sorted(self._handlers))

    def handler_for(self, record: CiTaskRecord) -> Optional[CiLanguageHandler]:
        for handler in self.handlers:
            if handler.matches(record):
                return handler
        return self._handlers.get(self.default_language)

    def runner_for(self, record: CiTaskRecord) -> Optional[EnforcementRunner]:
        handler = self.handler_for(record)
        if handler is None:
            return None
        select_runner = getattr(handler, "runner_for", None)
        return select_runner(record) if select_runner else handler.runner


class BaseCiLanguageHandler:
    """Shared matching and command helpers for concrete CI handlers."""

    language: str
    quality_gate_backend: str

    def __init__(self, runner: Optional[EnforcementRunner]) -> None:
        self.runner = runner

    def matches(self, record: CiTaskRecord) -> bool:
        enforcement = record.enforcement_result or {}
        return (
            getattr(record.request, "language", "java") == self.language
            or enforcement.get("language") == self.language
            or enforcement.get("backend") == self.quality_gate_backend
        )

    def runner_for(self, record: CiTaskRecord) -> Optional[EnforcementRunner]:
        return self.runner

    def _quality_gate_command(self, record: CiTaskRecord) -> str:
        return shlex.join([str(item) for item in (record.enforcement_result or {}).get("command") or []])

    def repair_target_ids(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        base_ref: str,
    ) -> tuple[str, ...]:
        return ()

    def validate_repair_context(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        base_ref: str,
        rdc_context: Dict[str, Any],
        target_ids: tuple[str, ...],
    ) -> None:
        return None

    def completed_task_enforcement_result(
        self,
        *,
        record: CiTaskRecord,
        task_manager: TaskManager,
        repo_task: Dict[str, Any],
    ):
        return None

    def should_rerun_after_repair_workspace_refresh(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
    ) -> bool:
        """Whether repair creation should run a full gate before creating a task.

        The default is false because the repair task owns prechecks and final
        rerun enforcement. Running a full gate during session creation duplicates
        slow work and can race with the task-level verifier.
        """
        return False

    def run_repair_preflight(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
    ):
        """Run optional language-owned evidence preparation before repair."""
        return None

    def scoring_survivors(
        self,
        *,
        record: CiTaskRecord,
        result: QualityGateResult,
        repo_path: Path,
    ) -> Optional["ScoringSurvivors"]:
        """Every mutant a gate result counted against its score, or None.

        None means "not proven complete", which keeps the equivalent-mutant
        exception closed. A language that cannot rebuild identities says so.
        """
        return None

    def gate_failure_flags(
        self,
        *,
        record: CiTaskRecord,
        result: QualityGateResult,
    ) -> Optional[Dict[str, bool]]:
        """`tests_passed`, `coverage_passed`, `mutation_only_failure`, or None if unknown."""
        return None


__all__ = [
    "BaseCiLanguageHandler",
    "CiLanguageHandler",
    "CiLanguageHandlerRegistry",
    "EnforcementRunner",
]

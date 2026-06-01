from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Protocol

from uta.ci_plugin.fix_sessions import CreateFixSessionRequest
from uta.ci_plugin.models import CiTaskRecord
from uta.tasks.manager import TaskManager


class EnforcementRunner(Protocol):
    """Runs a local quality gate command and returns enforcement evidence."""

    def run(self, repo_path: Path):
        ...


class CiLanguageHandler(Protocol):
    """Adapts CI repair and enforcement behavior for one backend language.

    The CI plugin asks handlers to match a task record and create the right
    incremental generation task without branching on Java/Python in core code.
    """

    language: str
    quality_gate_backend: str
    runner: Optional[EnforcementRunner]

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
        ci_context: Dict[str, Any],
        ci_context_path: Optional[str],
    ) -> int:
        ...

    def completed_task_enforcement_result(
        self,
        *,
        record: CiTaskRecord,
        task_manager: TaskManager,
        repo_task: Dict[str, Any],
    ):
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
        return handler.runner if handler else None


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

    def _quality_gate_command(self, record: CiTaskRecord) -> str:
        return shlex.join([str(item) for item in (record.enforcement_result or {}).get("command") or []])

    def completed_task_enforcement_result(
        self,
        *,
        record: CiTaskRecord,
        task_manager: TaskManager,
        repo_task: Dict[str, Any],
    ):
        return None

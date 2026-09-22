"""Language-neutral project-coverage recomputation seam.

The task summary can re-run a target's tests to recompute aggregate coverage
from authoritative tool output. How that recompute happens (JaCoCo for Java,
coverage.py for Python, ...) is backend-specific, so the task layer dispatches
through this registry instead of importing a tool runner directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol
from uta.shared.backends import make_all


@dataclass(frozen=True)
class CoverageRecomputeResult:
    """Outcome of a project-coverage recompute.

    ``recalc`` is the per-module detail surfaced in the task summary;
    ``coverage_total`` is the recomputed aggregate (or the caller's existing
    value when the recompute produced no usable evidence).
    """

    recalc: Dict[str, Any] = field(default_factory=lambda: {"ran": False})
    coverage_total: Optional[float] = None

    @classmethod
    def not_run(cls, coverage_total: Optional[float] = None) -> "CoverageRecomputeResult":
        return cls(recalc={"ran": False}, coverage_total=coverage_total)


class ProjectCoverageRecomputer(Protocol):
    """Recomputes aggregate coverage for one backend language."""

    language: str

    def recompute(
        self,
        repo_path: str,
        class_rows: List[Dict[str, Any]],
        *,
        current_total: Optional[float] = None,
    ) -> CoverageRecomputeResult:
        ...


class CoverageRecomputerRegistry:
    """Selects the project-coverage recomputer for a language."""

    def __init__(self, recomputers: Iterable[ProjectCoverageRecomputer]) -> None:
        self._by_language = {recomputer.language: recomputer for recomputer in recomputers}

    def recomputer_for(self, language: str) -> Optional[ProjectCoverageRecomputer]:
        return self._by_language.get(str(language or "").strip().lower())

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_language))


def default_coverage_recomputer_registry() -> CoverageRecomputerRegistry:
    # Only Java recomputes project coverage today; `make_all` returns whatever
    # is registered, so adding Python is a table entry rather than an edit here.
    return CoverageRecomputerRegistry(make_all("coverage_recompute"))

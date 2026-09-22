"""Language-neutral plan validation data structures and protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol, Set


@dataclass(frozen=True)
class PlanCallable:
    """Normalized callable metadata used by plan validators.

    Language extractors convert parser-specific functions or methods into this
    shape so breadth and feasibility checks can stay language-neutral.
    """

    name: str
    qualified_name: str = ""
    kind: str = "method"
    visibility_rank: int = 0
    start_line: int = 0
    end_line: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_roi_method(self) -> Dict[str, Any]:
        body_lines = max(1, int(self.end_line or self.start_line or 1) - int(self.start_line or 1) + 1)
        return {
            "name": self.name,
            "fqn": self.qualified_name or self.name,
            "visibility_rank": self.visibility_rank,
            "missed_lines": body_lines,
            "planning_reach_lines": body_lines,
            "planning_score": float(body_lines),
            "effort_score": 1,
        }


@dataclass(frozen=True)
class PlanContext:
    """Normalized set of callables available for validating a generated plan."""

    callables: List[PlanCallable]
    language: str = "unknown"

    @property
    def public_method_names(self) -> Set[str]:
        return {
            item.name
            for item in self.callables
            if item.name and int(item.visibility_rank) == 0
        }


class PlanContextExtractor(Protocol):
    """Converts language-specific context payloads into PlanContext."""

    language: str

    def can_extract(self, context: Any) -> bool:
        ...

    def extract(self, context: Any) -> PlanContext:
        ...

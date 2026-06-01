from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Protocol

from uta.engine.targets import TargetRef


@dataclass(frozen=True)
class TargetScoreResult:
    """Normalized scoring output for one generation target.

    Scorers use this to pass planning priority, method ranking, and provenance
    into prompts and later reporting without exposing language parser details.
    """

    language: str
    target_id: str
    methods: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "language": self.language,
            "target_id": self.target_id,
            "methods": list(self.methods),
            "summary": dict(self.summary),
            "provenance": dict(self.provenance),
        }


class TargetScorer(Protocol):
    """Scores one target so generation can prioritize high-value callables."""

    language: str

    def score_target(self, repo_path: Path, target: TargetRef, **kwargs: Any) -> TargetScoreResult:
        ...


class TargetScorerRegistry:
    """Registry that selects the target scorer for a language."""

    def __init__(self, scorers: Iterable[TargetScorer]) -> None:
        self._scorers = {scorer.language: scorer for scorer in scorers}

    def scorer_for(self, language: str) -> TargetScorer:
        normalized = str(language or "").strip().lower()
        try:
            return self._scorers[normalized]
        except KeyError:
            raise ValueError(f"Unsupported scoring language: {language}") from None

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(sorted(self._scorers))


def default_scorer_registry() -> TargetScorerRegistry:
    from uta.language.java.scoring import JavaTargetScorer
    from uta.language.python.scoring import PythonTargetScorer

    return TargetScorerRegistry((JavaTargetScorer(), PythonTargetScorer()))

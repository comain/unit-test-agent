from __future__ import annotations

from typing import Iterable

from uta.shared.backends import make_all
from uta.shared.scoring import TargetScoreResult, TargetScorer

__all__ = [
    "TargetScoreResult",
    "TargetScorer",
    "TargetScorerRegistry",
    "default_scorer_registry",
]


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
    return TargetScorerRegistry(make_all("scoring"))

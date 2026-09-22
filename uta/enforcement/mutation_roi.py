"""Language-neutral mutation kill-per-effort ROI math.

Both backends rank surviving-mutant groups by ``(killability × count) / effort``
so the repair prompt tackles the cheapest high-value mutants first. The effort
delta per mutation family, the effort banding, and the ROI formula are identical
across languages (the family taxonomy is shared — see
``uta.enforcement.mutation_repair``); only how each backend *sources* the per-symbol
base effort and decides "likely equivalent" stays language-specific.
"""

from __future__ import annotations

# Mutation family -> effort delta added on top of the per-symbol base effort.
FAMILY_EFFORT_DELTA = {
    "boundary": 0,
    "conditional": 0,
    "return_value": 0,
    "math": 0,
    "negation": 0,
    "side_effect": 2,
    "other": 1,
}


def family_effort(base_effort: int, family: str, detail: str = "") -> int:
    """Effort to kill a family: base symbol effort + family delta (+1 removed-call)."""
    delta = FAMILY_EFFORT_DELTA.get(family, 1)
    # Removed-call on void methods adds observability difficulty (PIT-only signal;
    # harmless for languages whose diff detail never contains "removed call").
    if "removed call" in (detail or "").lower():
        delta += 1
    return max(1, int(base_effort) + delta)


def effort_band(score: int) -> str:
    if score <= 2:
        return "cheap"
    if score <= 5:
        return "medium"
    return "expensive"


def mutation_roi_score(count: int, killability_score: int, effort: int) -> float:
    """ROI = (count × killability) / effort, rounded to 2dp."""
    return round((int(count) * int(killability_score)) / max(int(effort), 1), 2)

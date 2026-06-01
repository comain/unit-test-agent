"""Mutation ROI scorer: rank surviving mutant families by kill-per-effort.

Reuses per-method effort scores from ``coverage_roi.compute_class_roi`` (no
re-walking of the graph) and layers per-family adjustments on top. The output
feeds ``summarize_surviving_mutants`` so the LLM's mutation-repair prompt gets
survivors ranked by ``(killability × count) / effort`` instead of pure count.

Equivalent-mutant heuristics deprioritize families that are not worth writing
tests for (metric counters, getter side effects, removed logging calls).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

MUTATION_ROI_VERSION = "2026-04-21-v1"

# Family → effort delta added on top of method base effort.
_FAMILY_EFFORT_DELTA = {
    "boundary": 0,
    "conditional": 0,
    "return_value": 0,
    "math": 0,
    "negation": 0,
    "side_effect": 2,
    "other": 1,
}

# Method-name tokens that typically produce equivalent side-effect mutants.
_GETTER_TOKENS = ("get", "is", "has", "size", "length", "count", "hash", "tostring", "equals")
_LOG_TOKENS = ("log", "logger", "slf4j", "debug(", "info(", "warn(", "trace(")


def _likely_equivalent(method_name: str, family: str, detail: str) -> bool:
    """Return True for mutants that are likely equivalent / not worth killing."""
    lowered_method = (method_name or "").lower()
    lowered_detail = (detail or "").lower()

    # Removed-call to a logger is observationally equivalent.
    if "removed call" in lowered_detail and any(tok in lowered_detail for tok in _LOG_TOKENS):
        return True

    # side_effect on a pure accessor almost never has an observable change
    # worth writing a whole test around.
    if family == "side_effect":
        for token in _GETTER_TOKENS:
            if lowered_method == token or lowered_method.startswith(token):
                # Avoid false positives like "isValid" (real boolean logic) —
                # require either exact match or a getter-style camelCase pattern
                # where the next char is uppercase or end-of-string.
                idx = len(token)
                if idx >= len(lowered_method) or not lowered_method[idx].isalpha():
                    return True
                # Check original-case method name for camelCase boundary
                if idx < len(method_name) and method_name[idx].isupper():
                    return True
    return False


def _family_effort(base_effort: int, family: str, detail: str) -> int:
    delta = _FAMILY_EFFORT_DELTA.get(family, 1)
    # Removed-call on void methods adds observability difficulty.
    if "removed call" in (detail or "").lower():
        delta += 1
    return max(1, int(base_effort) + delta)


def _effort_band(score: int) -> str:
    if score <= 2:
        return "cheap"
    if score <= 5:
        return "medium"
    return "expensive"


def _method_effort_lookup(method_efforts: Optional[Iterable[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """Build {simple_method_name: effort_dict} from compute_class_roi output."""
    lookup: Dict[str, Dict[str, Any]] = {}
    for eff in method_efforts or []:
        name = eff.get("name") or (eff.get("fqn") or "").split(".")[-1]
        if not name:
            continue
        # If multiple overloads collide, keep the higher-effort one (conservative).
        existing = lookup.get(name)
        if existing is None or int(eff.get("effort_score", 0) or 0) > int(existing.get("effort_score", 0) or 0):
            lookup[name] = eff
    return lookup


def score_families(
    families: List[Dict[str, Any]],
    method_efforts: Optional[Iterable[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Attach ROI fields to each family dict in-place and return the list.

    Each family gains: ``effort_score``, ``effort_band``, ``roi``,
    ``likely_equivalent``. ``deprioritized`` is OR'd with ``likely_equivalent``
    so the existing sort contract still holds.
    """
    lookup = _method_effort_lookup(method_efforts)
    for fam in families:
        method = fam.get("method", "") or ""
        simple = method.split(".")[-1] if method else ""
        base = int((lookup.get(simple) or {}).get("effort_score", 2) or 2)
        family = fam.get("family", "other")
        first_detail = ""
        examples = fam.get("examples") or []
        if examples:
            first_detail = examples[0].get("detail", "") or ""

        effort = _family_effort(base, family, first_detail)
        kill = int(fam.get("killability_score", 0) or 0)
        count = int(fam.get("count", 0) or 0)
        equivalent = _likely_equivalent(simple, family, first_detail)

        roi = 0.0 if equivalent else round((count * kill) / max(effort, 1), 2)

        fam["effort_score"] = effort
        fam["effort_band"] = _effort_band(effort)
        fam["roi"] = roi
        fam["likely_equivalent"] = equivalent
        if equivalent:
            fam["deprioritized"] = True
    return families


def roi_sort_key(fam: Dict[str, Any]):
    """Sort key for ROI-ranked families (higher ROI → earlier)."""
    return (
        bool(fam.get("deprioritized", False)),
        -float(fam.get("roi", 0.0) or 0.0),
        -int(fam.get("killability_score", 0) or 0),
        fam.get("method", ""),
        (fam.get("lines") or [0])[0],
    )

"""Shared pre-generation policy for Python mutation verification."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from uta_py_enforce.mutation_candidates import collect_python_mutation_opportunities


def build_generation_policy(
    source_file: Path,
    *,
    source_path: str,
    changed_lines: Iterable[int],
    covered_lines: Iterable[int],
    hard_cap: int,
) -> dict[str, Any]:
    """Select useful changed-line opportunities before mutmut generates code."""

    changed = tuple(sorted({int(line) for line in changed_lines if int(line) > 0}))
    plan = collect_python_mutation_opportunities(
        source_file,
        source_path=source_path,
        changed_lines=changed,
        covered_lines=covered_lines,
    )
    selected_before_cap = tuple(plan.selected)
    selected = _hard_cap(selected_before_cap, hard_cap)
    selected_keys = {(item.source_path, int(item.line)) for item in selected}
    selected_lines = sorted({int(item.line) for item in selected})
    selected_operators = _expand_generation_policy_line_operators(plan.eligible, selected_keys)
    return {
        "sourcePath": source_path,
        "sourceBytes": source_file.stat().st_size,
        "changedLines": list(changed),
        "changedLineCount": len(changed),
        "allowedLines": selected_lines,
        "selectedLines": selected_lines,
        "eligibleOpportunities": len(plan.eligible),
        "selectedOpportunitiesBeforeCap": len(selected_before_cap),
        "hardCapSelectedOpportunities": len(selected),
        "selectedOpportunities": len(selected),
        "omittedByCap": max(0, len(selected_before_cap) - len(selected)),
        "omittedByHardCap": max(0, len(selected_before_cap) - len(selected)),
        "omittedByRepresentativeSelection": 0,
        "selectedOperatorOpportunities": len(selected_operators),
        "hardCapSelectedOperatorOpportunities": len(selected_operators),
        "retainedOperatorAlternatives": max(0, len(selected_operators) - len(selected)),
        "omittedByOnePerLine": 0,
        "suppressedOpportunities": len(plan.suppressed),
        "truncated": len(selected) < len(selected_before_cap),
        "truncationReasons": (
            [f"selectedOpportunities={len(selected_before_cap)} capped to {int(hard_cap)}"]
            if int(hard_cap) > 0 and len(selected) < len(selected_before_cap)
            else []
        ),
        "capProfile": "full",
        "caps": {"maxSelected": int(hard_cap)},
        "selected": [item.as_dict() for item in selected],
        "selectedLineRepresentatives": [item.as_dict() for item in selected],
        "selectedBeforeCap": [item.as_dict() for item in plan.eligible],
        "hardCapSelected": [item.as_dict() for item in selected],
        "omittedByCapOpportunities": [
            item.as_dict()
            for item in selected_before_cap
            if (item.source_path, int(item.line)) not in selected_keys
        ],
        "omittedByOnePerLineOpportunities": [],
        "suppressed": [item.as_dict() for item in plan.suppressed],
        # The adapter emits at most one materialized mutant per selected line.
        # Leaving this empty lets it choose the first operator mutmut can
        # actually materialize while preserving the deterministic line plan.
        "operatorByLine": {},
        "opportunityIdByLine": {},
    }


def _expand_generation_policy_line_operators(
    eligible: Sequence[Any],
    selected_keys: set[tuple[str, int]],
) -> tuple[Any, ...]:
    return tuple(
        sorted(
            (
                item
                for item in eligible
                if (str(getattr(item, "source_path", "")), int(getattr(item, "line", 0) or 0)) in selected_keys
            ),
            key=lambda item: tuple(getattr(item, "selection_rank", ()) or ()),
        )
    )


def suppression_counts(policy: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in policy.get("suppressed") or ():
        if not isinstance(item, Mapping):
            continue
        reason = str(item.get("reasonCode") or "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _hard_cap(selected: Sequence[Any], limit: int) -> tuple[Any, ...]:
    if int(limit) <= 0 or len(selected) <= int(limit):
        return tuple(selected)
    ranked = sorted(
        selected,
        key=lambda item: (
            -int(item.operator_priority),
            hashlib.sha256(str(item.opportunity_id).encode("utf-8")).hexdigest(),
            tuple(item.selection_rank),
        ),
    )
    return tuple(sorted(ranked[: int(limit)], key=lambda item: tuple(item.selection_rank)))

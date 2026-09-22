"""Mutation generation policy: which changed lines mutmut is allowed to mutate.

Deciding *what* to mutate is a separate concern from running the mutation, and
it is the part that has to be deterministic: the caps, the ranking used when a
cap bites, the round-robin representative selection used under the CI profile,
and the JSON policy artifact that both the adapter and the candidate plan read
back. Nothing here executes a command, so the selection is reproducible from
the source file and settings alone.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from uta.enforcement.mutation_candidates import make_opportunity_id
from uta.language.python.mutation_candidates import collect_python_mutation_opportunities
from uta.language.python.verification.mutmut_runtime import (
    _normalize_changed_lines,
    _normalize_relpath,
)
from uta.shared.config import settings


def _write_mutmut_generation_policy(
    mutation_dir: Path,
    *,
    source_file: Path,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    covered_lines: Iterable[int],
    use_ci_cap_profile: bool = False,
) -> Dict[str, Any]:
    normalized_source = _normalize_relpath(source_path)
    normalized_changed = _normalize_changed_lines(changed_lines) or {}
    target_changed = tuple(sorted(normalized_changed.get(normalized_source, set())))
    source_bytes = source_file.stat().st_size if source_file.exists() else 0
    payload: Dict[str, Any] = {
        "sourcePath": normalized_source,
        "sourceBytes": int(source_bytes),
        "changedLines": list(target_changed),
        "changedLineCount": len(target_changed),
        "allowedLines": [],
        "selectedLines": [],
        "eligibleOpportunities": 0,
        "selectedOpportunitiesBeforeCap": 0,
        "selectedOpportunities": 0,
        "omittedByCap": 0,
        "omittedByOnePerLine": 0,
        "suppressedOpportunities": 0,
        "truncated": False,
        "truncationReasons": [],
    }
    caps = _generation_policy_caps(use_ci_cap_profile=use_ci_cap_profile)
    hard_caps = _generation_policy_caps(use_ci_cap_profile=False) if use_ci_cap_profile else caps
    payload["capProfile"] = "ci" if use_ci_cap_profile else "full"
    payload["caps"] = caps
    if use_ci_cap_profile:
        payload["hardCapProfile"] = "full"
        payload["hardCaps"] = hard_caps
    truncation_reasons = _generation_policy_preplan_truncation_reasons(payload, hard_caps)
    source_size_cap = int(hard_caps.get("maxSourceBytes") or 0)
    if source_size_cap > 0 and source_bytes > source_size_cap:
        selected_before_cap = tuple(
            _synthetic_generation_policy_opportunity(
                source_path=normalized_source,
                line=line,
                covered=int(line) in set(int(value) for value in covered_lines or []),
            )
            for line in target_changed
        )
        selected_after_hard_cap, hard_cap_reasons = _cap_generation_policy_payloads(selected_before_cap, hard_caps)
        selected_after_cap, representative_reasons = _representative_generation_policy_payloads(
            selected_after_hard_cap,
            caps,
        ) if use_ci_cap_profile else (selected_after_hard_cap, [])
        truncation_reasons.extend(hard_cap_reasons)
        truncation_reasons.extend(representative_reasons)
        selected_lines = tuple(sorted(int(item["line"]) for item in selected_after_cap))
        payload.update(
            {
                "preplanSkippedReason": "source_size_cap",
                "allowedLines": list(selected_lines),
                "selectedLines": list(selected_lines),
                "eligibleOpportunities": len(selected_before_cap),
                "selectedOpportunitiesBeforeCap": len(selected_before_cap),
                "hardCapSelectedOpportunities": len(selected_after_hard_cap),
                "selectedOpportunities": len(selected_after_cap),
                "omittedByCap": max(0, len(selected_before_cap) - len(selected_after_cap)),
                "omittedByHardCap": max(0, len(selected_before_cap) - len(selected_after_hard_cap)),
                "omittedByRepresentativeSelection": max(0, len(selected_after_hard_cap) - len(selected_after_cap)),
                "selected": list(selected_after_cap),
                "selectedBeforeCap": list(selected_before_cap),
                "omittedByCapOpportunities": [
                    item for item in selected_before_cap if int(item["line"]) not in set(selected_lines)
                ],
                "hardCapSelected": list(selected_after_hard_cap),
                "operatorByLine": {},
                "opportunityIdByLine": {},
            }
        )
    else:
        try:
            plan = collect_python_mutation_opportunities(
                source_file,
                source_path=normalized_source,
                changed_lines=target_changed,
                covered_lines=covered_lines,
            )
            selected_before_cap = tuple(plan.selected)
            selected_after_hard_cap, hard_cap_reasons = _cap_generation_policy_selected(selected_before_cap, hard_caps)
            selected_after_cap, representative_reasons = _representative_generation_policy_selected(
                selected_after_hard_cap,
                caps,
            ) if use_ci_cap_profile else (selected_after_hard_cap, [])
            truncation_reasons.extend(hard_cap_reasons)
            truncation_reasons.extend(representative_reasons)
            selected_lines = tuple(sorted({int(item.line) for item in selected_after_cap}))
            selected_after_cap_operators = _expand_generation_policy_line_operators(plan.eligible, selected_after_cap)
            selected_after_hard_cap_operators = _expand_generation_policy_line_operators(
                plan.eligible,
                selected_after_hard_cap,
            )
            selected_after_cap_payloads = [item.as_dict() for item in selected_after_cap]
            selected_after_hard_cap_payloads = [item.as_dict() for item in selected_after_hard_cap]
            selected_line_keys = _generation_policy_line_keys(selected_after_cap)
            hard_cap_line_keys = _generation_policy_line_keys(selected_after_hard_cap)
            payload.update(
                {
                    "allowedLines": list(selected_lines),
                    "selectedLines": list(selected_lines),
                    "eligibleOpportunities": len(plan.eligible),
                    "selectedOpportunitiesBeforeCap": len(selected_before_cap),
                    "hardCapSelectedOpportunities": len(selected_after_hard_cap),
                    "selectedOpportunities": len(selected_after_cap),
                    "omittedByCap": max(0, len(selected_before_cap) - len(selected_after_cap)),
                    "omittedByHardCap": max(0, len(selected_before_cap) - len(selected_after_hard_cap)),
                    "omittedByRepresentativeSelection": max(0, len(selected_after_hard_cap) - len(selected_after_cap)),
                    "selectedOperatorOpportunities": len(selected_after_cap_operators),
                    "hardCapSelectedOperatorOpportunities": len(selected_after_hard_cap_operators),
                    "retainedOperatorAlternatives": max(0, len(selected_after_cap_operators) - len(selected_after_cap)),
                    "omittedByOnePerLine": 0,
                    "suppressedOpportunities": len(plan.suppressed),
                    "selected": selected_after_cap_payloads,
                    "selectedLineRepresentatives": [item.as_dict() for item in selected_after_cap],
                    "selectedBeforeCap": [item.as_dict() for item in plan.eligible],
                    "hardCapSelected": selected_after_hard_cap_payloads,
                    "omittedByCapOpportunities": [
                        item.as_dict()
                        for item in selected_before_cap
                        if (item.source_path, int(item.line)) not in selected_line_keys
                    ],
                    "omittedByRepresentativeOpportunities": [
                        item.as_dict()
                        for item in selected_after_hard_cap
                        if (item.source_path, int(item.line)) not in selected_line_keys
                    ],
                    "omittedByHardCapOpportunities": [
                        item.as_dict()
                        for item in selected_before_cap
                        if (item.source_path, int(item.line)) not in hard_cap_line_keys
                    ],
                    "omittedByOnePerLineOpportunities": [],
                    "suppressed": [item.as_dict() for item in plan.suppressed],
                    "operatorByLine": {},
                    "opportunityIdByLine": {},
                }
            )
            if use_ci_cap_profile:
                payload["representativeSelection"] = {
                    "enabled": True,
                    "inputOpportunities": len(selected_after_hard_cap),
                    "selectedOpportunities": len(selected_after_cap),
                    "omittedOpportunities": max(0, len(selected_after_hard_cap) - len(selected_after_cap)),
                    "strategy": "deterministic_symbol_operator_round_robin_v1",
                }
        except Exception as exc:
            payload["failure"] = f"Python mutation candidate policy failed: {exc}"
    if truncation_reasons:
        payload["truncated"] = True
        payload["truncationReasons"] = sorted(set(truncation_reasons))
    policy_path = mutation_dir / f"{_safe_artifact_stem(normalized_source)}.generation-policy.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    payload["path"] = str(policy_path)
    return payload


def _generation_policy_caps(*, use_ci_cap_profile: bool) -> Dict[str, int]:
    if use_ci_cap_profile:
        return {
            "maxSourceBytes": int(getattr(settings, "python_mutation_generation_max_source_bytes", 0) or 0),
            "maxChangedLines": int(getattr(settings, "python_mutation_generation_ci_max_changed_lines", 0) or 0),
            "maxOpportunities": int(getattr(settings, "python_mutation_generation_ci_max_opportunities", 0) or 0),
            "maxSelected": int(getattr(settings, "python_mutation_generation_ci_max_selected", 0) or 0),
        }
    return {
        "maxSourceBytes": int(getattr(settings, "python_mutation_generation_max_source_bytes", 0) or 0),
        "maxChangedLines": int(getattr(settings, "python_mutation_generation_max_changed_lines", 0) or 0),
        "maxOpportunities": int(getattr(settings, "python_mutation_generation_max_opportunities", 0) or 0),
        "maxSelected": int(getattr(settings, "python_mutation_generation_max_selected", 0) or 0),
    }


def _generation_policy_preplan_truncation_reasons(
    payload: Mapping[str, Any],
    caps: Mapping[str, int],
) -> List[str]:
    reasons: List[str] = []
    checks = (("sourceBytes", "maxSourceBytes"), ("changedLines", "maxChangedLines"))
    for value_key, cap_key in checks:
        cap = int(caps.get(cap_key) or 0)
        if cap <= 0:
            continue
        value = payload.get(value_key)
        actual = len(value) if isinstance(value, list) else int(value or 0)
        if actual > cap:
            reasons.append(f"{value_key}={actual} exceeds {cap_key}={cap}")
    return reasons


def _cap_generation_policy_selected(
    selected: Sequence[Any],
    caps: Mapping[str, int],
) -> Tuple[Tuple[Any, ...], List[str]]:
    limits = [
        int(caps.get("maxChangedLines") or 0),
        int(caps.get("maxSelected") or 0),
    ]
    positive_limits = [limit for limit in limits if limit > 0]
    if not positive_limits:
        return tuple(selected), []
    limit = min(positive_limits)
    if len(selected) <= limit:
        return tuple(selected), []
    ranked = sorted(
        selected,
        key=lambda item: (
            -int(getattr(item, "operator_priority", 0) or 0),
            _stable_policy_hash(str(getattr(item, "opportunity_id", ""))),
            tuple(getattr(item, "selection_rank", ()) or ()),
        ),
    )
    kept = tuple(sorted(ranked[:limit], key=lambda item: tuple(getattr(item, "selection_rank", ()) or ())))
    return kept, [f"selectedOpportunities={len(selected)} capped to {limit}"]


def _representative_generation_policy_selected(
    selected: Sequence[Any],
    caps: Mapping[str, int],
) -> Tuple[Tuple[Any, ...], List[str]]:
    limit = _generation_policy_limit(caps)
    selected_tuple = tuple(selected)
    if limit <= 0 or len(selected_tuple) <= limit:
        return selected_tuple, []
    kept = _representative_generation_policy_items(selected_tuple, limit, _opportunity_attr)
    return kept, [f"selectedOpportunities={len(selected_tuple)} representative-selected to {limit}"]


def _cap_generation_policy_payloads(
    selected: Sequence[Mapping[str, Any]],
    caps: Mapping[str, int],
) -> Tuple[Tuple[Dict[str, Any], ...], List[str]]:
    limits = [
        int(caps.get("maxChangedLines") or 0),
        int(caps.get("maxSelected") or 0),
    ]
    positive_limits = [limit for limit in limits if limit > 0]
    payloads = tuple(dict(item) for item in selected)
    if not positive_limits:
        return payloads, []
    limit = min(positive_limits)
    if len(payloads) <= limit:
        return payloads, []
    ranked = sorted(
        payloads,
        key=lambda item: (
            -int(item.get("operatorPriority") or 0),
            _stable_policy_hash(str(item.get("opportunityId") or "")),
            tuple(item.get("selectionRank") or ()),
        ),
    )
    kept = tuple(sorted(ranked[:limit], key=lambda item: tuple(item.get("selectionRank") or ())))
    return kept, [f"selectedOpportunities={len(payloads)} capped to {limit}"]


def _representative_generation_policy_payloads(
    selected: Sequence[Mapping[str, Any]],
    caps: Mapping[str, int],
) -> Tuple[Tuple[Dict[str, Any], ...], List[str]]:
    limit = _generation_policy_limit(caps)
    payloads = tuple(dict(item) for item in selected)
    if limit <= 0 or len(payloads) <= limit:
        return payloads, []
    kept = _representative_generation_policy_items(payloads, limit, _payload_attr)
    return kept, [f"selectedOpportunities={len(payloads)} representative-selected to {limit}"]


def _expand_generation_policy_line_operators(
    eligible: Sequence[Any],
    selected_lines: Sequence[Any],
) -> Tuple[Any, ...]:
    selected_keys = _generation_policy_line_keys(selected_lines)
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


def _generation_policy_line_keys(items: Sequence[Any]) -> set[Tuple[str, int]]:
    return {
        (str(getattr(item, "source_path", "")), int(getattr(item, "line", 0) or 0))
        for item in items or ()
        if int(getattr(item, "line", 0) or 0) > 0
    }


def _generation_policy_limit(caps: Mapping[str, int]) -> int:
    limits = [
        int(caps.get("maxChangedLines") or 0),
        int(caps.get("maxSelected") or 0),
    ]
    positive_limits = [limit for limit in limits if limit > 0]
    return min(positive_limits) if positive_limits else 0


def _representative_generation_policy_items(
    selected: Sequence[Any],
    limit: int,
    attr: Callable[[Any, str], Any],
) -> Tuple[Any, ...]:
    groups: Dict[Tuple[str, str], List[Any]] = {}
    for item in selected:
        groups.setdefault(
            (
                str(attr(item, "symbol") or "<module>"),
                str(attr(item, "operator_name") or attr(item, "operatorName") or "statement"),
            ),
            [],
        ).append(item)
    for key, items in list(groups.items()):
        groups[key] = sorted(items, key=lambda item: _generation_policy_item_rank(item, attr))
    group_order = sorted(
        groups,
        key=lambda key: (
            -int(attr(groups[key][0], "operator_priority") or attr(groups[key][0], "operatorPriority") or 0),
            _stable_policy_hash("|".join(key)),
            _generation_policy_item_rank(groups[key][0], attr),
        ),
    )
    kept: List[Any] = []
    index = 0
    while len(kept) < limit:
        added = False
        for key in group_order:
            items = groups[key]
            if index < len(items):
                kept.append(items[index])
                added = True
                if len(kept) >= limit:
                    break
        if not added:
            break
        index += 1
    return tuple(sorted(kept, key=lambda item: tuple(attr(item, "selection_rank") or attr(item, "selectionRank") or ())))


def _generation_policy_item_rank(item: Any, attr: Callable[[Any, str], Any]) -> Tuple[Any, ...]:
    return (
        -int(attr(item, "operator_priority") or attr(item, "operatorPriority") or 0),
        _stable_policy_hash(str(attr(item, "opportunity_id") or attr(item, "opportunityId") or "")),
        tuple(attr(item, "selection_rank") or attr(item, "selectionRank") or ()),
    )


def _opportunity_attr(item: Any, name: str) -> Any:
    return getattr(item, name, None)


def _payload_attr(item: Any, name: str) -> Any:
    if not isinstance(item, Mapping):
        return None
    return item.get(name)


def _synthetic_generation_policy_opportunity(
    *,
    source_path: str,
    line: int,
    covered: bool,
) -> Dict[str, Any]:
    source_line = ""
    diff_hunk = f"@@ {int(line)} @@"
    opportunity_id = make_opportunity_id(
        language="python",
        source_path=source_path,
        line=int(line),
        symbol="<module>",
        operator_name="statement",
        source_line=source_line,
        diff_hunk=diff_hunk,
    )
    return {
        "language": "python",
        "sourcePath": source_path,
        "line": int(line),
        "lineSpan": [int(line), int(line)],
        "symbol": "<module>",
        "opportunityId": opportunity_id,
        "operatorName": "statement",
        "familyHint": "other",
        "diffHunk": diff_hunk,
        "operatorPriority": 20,
        "roiScore": 20.0,
        "selectionRank": [0 if covered else 1, -20, source_path, int(line), "<module>", "statement", opportunity_id],
        "covered": bool(covered),
        "executable": True,
        "selectionReason": "source-size cap fallback",
    }


def _operator_by_line_from_policy_payloads(payloads: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for item in payloads:
        line = str(int(item.get("line") or 0))
        operator_name = str(item.get("operatorName") or "").strip()
        if line != "0" and operator_name:
            result.setdefault(line, [])
            if operator_name not in result[line]:
                result[line].append(operator_name)
    return result


def _opportunity_id_by_line_from_policy_payloads(payloads: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    grouped: Dict[str, set[Tuple[str, str]]] = {}
    for item in payloads:
        line = str(int(item.get("line") or 0))
        operator_name = str(item.get("operatorName") or "").strip()
        opportunity_id = str(item.get("opportunityId") or "").strip()
        if line != "0" and operator_name and opportunity_id:
            grouped.setdefault(line, set()).add((operator_name, opportunity_id))
    result: Dict[str, str] = {}
    for line, values in grouped.items():
        if len(values) == 1:
            result[line] = next(iter(values))[1]
    return result


def _stable_policy_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_artifact_stem(source_path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(source_path or "target").replace("\\", "/")).strip("_") or "target"

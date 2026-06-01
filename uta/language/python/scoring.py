from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List

from uta.engine.scoring import TargetScoreResult
from uta.language.python.context_builder import PythonContextBuilder
from uta.engine.targets import TargetRef


PYTHON_SCORER_VERSION = "2026-05-25-v1"


class PythonTargetScorer:
    language = "python"

    def score_target(self, repo_path: Path, target: TargetRef, **kwargs: Any) -> TargetScoreResult:
        context = kwargs.get("context_payload")
        if not context:
            context = PythonContextBuilder(Path(repo_path)).build_target_context(target)
        methods = _score_python_callables(context.get("symbols") or [], context.get("side_effect_hints") or [], target)
        methods.sort(
            key=lambda item: (
                item["visibility_rank"],
                -float(item.get("planning_score", 0.0) or 0.0),
                -int(item.get("missed_lines", 0) or 0),
                int(item.get("effort_score", 0) or 0),
                item.get("name", ""),
            )
        )
        return TargetScoreResult(
            language="python",
            target_id=target.target_id,
            methods=methods,
            summary={
                "total_methods": len(methods),
                "cheap_count": sum(1 for item in methods if item.get("effort_band") == "cheap"),
                "medium_count": sum(1 for item in methods if item.get("effort_band") == "medium"),
                "expensive_count": sum(1 for item in methods if item.get("effort_band") == "expensive"),
                "estimated_coverage_lines": sum(int(item.get("missed_lines", 0) or 0) for item in methods),
            },
            provenance={
                "target_scorer_version": PYTHON_SCORER_VERSION,
                "source_path": context.get("source_path") or target.source_path or "",
                "parser_backend": (context.get("syntax") or {}).get("parser_backend") or "",
            },
        )


def _score_python_callables(
    symbols: Iterable[Dict[str, Any]],
    side_effects: Iterable[Dict[str, Any]],
    target: TargetRef,
) -> List[Dict[str, Any]]:
    target_symbol = target.symbol
    rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        if symbol.get("kind") not in {"function", "method"}:
            continue
        if target_symbol and symbol.get("qualified_name") != target_symbol and symbol.get("name") != target_symbol:
            continue
        start = int(symbol.get("line") or 0)
        end = int(symbol.get("end_line") or start or 0)
        body_lines = max(1, end - start + 1)
        params = list(symbol.get("params") or [])
        side_effect_count = _side_effect_count(side_effects, start, end)
        effort = _effort_score(body_lines=body_lines, param_count=len(params), side_effect_count=side_effect_count)
        visibility_rank = 2 if str(symbol.get("name") or "").startswith("_") else 0
        planning_score = round(body_lines / max(effort, 1), 1)
        rows.append(
            {
                "name": symbol.get("name") or "",
                "fqn": symbol.get("qualified_name") or symbol.get("name") or "",
                "signature": _signature(symbol),
                "start_line": start,
                "body_lines": body_lines,
                "missed_lines": body_lines,
                "missed_branches": 0,
                "roi_score": planning_score,
                "planning_score": planning_score,
                "planning_reach_lines": body_lines,
                "has_coverage_gain": body_lines > 0,
                "visibility_rank": visibility_rank,
                "effort_score": effort,
                "effort_band": _effort_band(effort),
                "effort_reasons": _effort_reasons(body_lines, len(params), side_effect_count),
                "detail": {
                    "body_lines": body_lines,
                    "param_count": len(params),
                    "side_effect_count": side_effect_count,
                },
            }
        )
    return rows


def _side_effect_count(side_effects: Iterable[Dict[str, Any]], start: int, end: int) -> int:
    count = 0
    for hint in side_effects:
        line = int(hint.get("line") or 0)
        if start <= line <= end:
            count += 1
    return count


def _effort_score(*, body_lines: int, param_count: int, side_effect_count: int) -> int:
    score = 0
    if body_lines > 80:
        score += 3
    elif body_lines > 35:
        score += 2
    elif body_lines > 15:
        score += 1
    if param_count > 5:
        score += 2
    elif param_count > 2:
        score += 1
    score += min(3, side_effect_count)
    return max(1, score)


def _effort_band(score: int) -> str:
    if score <= 2:
        return "cheap"
    if score <= 5:
        return "medium"
    return "expensive"


def _effort_reasons(body_lines: int, param_count: int, side_effect_count: int) -> List[str]:
    reasons = []
    if body_lines > 15:
        reasons.append(f"body_lines={body_lines}")
    if param_count > 2:
        reasons.append(f"params={param_count}")
    if side_effect_count:
        reasons.append(f"side_effects={side_effect_count}")
    return reasons or ["simple"]


def _signature(symbol: Dict[str, Any]) -> str:
    params = ", ".join(str(item) for item in symbol.get("params") or [])
    return f"{symbol.get('name') or ''}({params})"

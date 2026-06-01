from typing import Optional

from uta.engine.validation import (
    FeasibilityVerdict,
    validate_plan_feasibility,
)


def _roi_method(
    name: str,
    *,
    planning_score: float,
    missed_lines: int,
    reach: Optional[int] = None,
    visibility_rank: int = 0,
    effort_score: int = 2,
):
    return {
        "name": name,
        "planning_score": planning_score,
        "missed_lines": missed_lines,
        "planning_reach_lines": missed_lines if reach is None else reach,
        "visibility_rank": visibility_rank,
        "effort_score": effort_score,
    }


def test_feasibility_passes_when_gate_methods_cover_anchor_methods():
    roi_methods = [
        _roi_method("finished", planning_score=120.0, missed_lines=100),
        _roi_method("pickDown", planning_score=110.0, missed_lines=95),
        _roi_method("queryPickingTaskRoutes", planning_score=90.0, missed_lines=80),
        _roi_method("queryPickingTaskAreas", planning_score=85.0, missed_lines=75),
        _roi_method("receivePicking", planning_score=70.0, missed_lines=60),
        _roi_method("markPickingOrdersDowngrade", planning_score=65.0, missed_lines=55),
        _roi_method("batchFinished", planning_score=25.0, missed_lines=22),
        _roi_method("queryById", planning_score=8.0, missed_lines=6),
    ]
    plan = """
5. METHODS REQUIRED FOR GATE
- `finished`
- `pickDown`
- `queryPickingTaskRoutes`
- `queryPickingTaskAreas`
- `receivePicking`
- `markPickingOrdersDowngrade`
"""
    result = validate_plan_feasibility(plan, roi_methods, coverage_gate=80)
    assert result.verdict == FeasibilityVerdict.PASS
    assert result.top_methods_covered >= 4


def test_feasibility_rejects_wrapper_heavy_gate_methods():
    roi_methods = [
        _roi_method("finished", planning_score=120.0, missed_lines=100),
        _roi_method("pickDown", planning_score=110.0, missed_lines=95),
        _roi_method("queryPickingTaskRoutes", planning_score=90.0, missed_lines=80),
        _roi_method("queryPickingTaskAreas", planning_score=85.0, missed_lines=75),
        _roi_method("receivePicking", planning_score=70.0, missed_lines=60),
        _roi_method("markPickingOrdersDowngrade", planning_score=65.0, missed_lines=55),
        _roi_method("batchFinished", planning_score=25.0, missed_lines=22),
        _roi_method("queryById", planning_score=8.0, missed_lines=6),
        _roi_method("queryByIds", planning_score=7.0, missed_lines=5),
    ]
    plan = """
5. METHODS REQUIRED FOR GATE
- `batchFinished`
- `queryById`
- `queryByIds`
- `receivePicking`
"""
    result = validate_plan_feasibility(plan, roi_methods, coverage_gate=80)
    assert result.verdict == FeasibilityVerdict.UNDER
    assert "finished" in result.missing_anchor_methods


def test_feasibility_uses_full_plan_when_gate_section_missing():
    roi_methods = [
        _roi_method("finished", planning_score=120.0, missed_lines=100),
        _roi_method("pickDown", planning_score=110.0, missed_lines=95),
        _roi_method("queryPickingTaskRoutes", planning_score=90.0, missed_lines=80),
        _roi_method("queryPickingTaskAreas", planning_score=85.0, missed_lines=75),
        _roi_method("receivePicking", planning_score=70.0, missed_lines=60),
        _roi_method("markPickingOrdersDowngrade", planning_score=65.0, missed_lines=55),
    ]
    plan = """
1. PUBLIC METHODS
- `finished`
- `pickDown`
- `queryPickingTaskRoutes`
- `queryPickingTaskAreas`
- `receivePicking`
- `markPickingOrdersDowngrade`
"""
    result = validate_plan_feasibility(plan, roi_methods, coverage_gate=80)
    assert result.verdict == FeasibilityVerdict.PASS

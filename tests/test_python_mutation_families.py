"""Spec for the mutmut diff -> mutation-family classifier.

mutmut emits a diff, not a named mutator (unlike Java PIT), so the family is
inferred from what the diff changed. These fixtures pin the mapping onto the
shared family taxonomy so Python can populate the same ROI-rankable
mutation-repair model as Java.
"""

import pytest

from uta.enforcement.mutation_repair import (
    MutationRepairContext,
    MutationRepairGroup,
    format_mutation_repair_context,
)
from uta.language.python.mutation_context import (
    classify_mutmut_family,
    python_mutation_killability,
)


def _diff(old: str, new: str) -> str:
    return f"--- a/jobs/forecast.py\n+++ b/jobs/forecast.py\n@@ -10,1 +10,1 @@\n-{old}\n+{new}\n"


@pytest.mark.parametrize(
    "old,new,family",
    [
        ("    if x < 5:", "    if x <= 5:", "boundary"),
        ("    if x >= 5:", "    if x > 5:", "boundary"),
        ("    if x == 5:", "    if x != 5:", "conditional"),
        ("    if x < 5:", "    if x > 5:", "conditional"),
        ("    if a and b:", "    if a or b:", "conditional"),
        ("    if x in items:", "    if x not in items:", "conditional"),
        ("    if x is None:", "    if x is not None:", "conditional"),
        ("    flag = True", "    flag = False", "negation"),
        ("    if ok:", "    if not ok:", "negation"),
        ("    total = a + b", "    total = a - b", "math"),
        ("    total = a * b", "    total = a / b", "math"),
        ("    n = count + 1", "    n = count + 2", "math"),
        ("        break", "        continue", "side_effect"),
        ("    return result", "    return None", "return_value"),
        ('    name = "sku"', '    name = "XXskuXX"', "other"),
    ],
)
def test_classify_mutmut_family(old, new, family):
    assert classify_mutmut_family(_diff(old, new)) == family


def test_classify_empty_diff_is_other():
    assert classify_mutmut_family("") == "other"
    assert classify_mutmut_family("--- a\n+++ b\n@@ -1 +1 @@\n") == "other"


def test_renderer_surfaces_family_and_killability_when_set():
    context = MutationRepairContext(
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=("tests/test_forecast.py",),
        reproduce_command="mutmut run",
        survivor_count=2,
        groups=(
            MutationRepairGroup(symbol="forecast_for_store", count=2, lines=(10, 11), family="boundary", killability="high"),
        ),
    )
    rendered = format_mutation_repair_context(context)
    assert "- mutation family: `boundary`, killability high" in rendered


def test_renderer_omits_family_line_when_unset():
    context = MutationRepairContext(
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=("tests/test_forecast.py",),
        reproduce_command="mutmut run",
        survivor_count=1,
        groups=(MutationRepairGroup(symbol="forecast_for_store", count=1, lines=(10,)),),
    )
    assert "mutation family" not in format_mutation_repair_context(context)


def _context(group: MutationRepairGroup, count: int = 1) -> MutationRepairContext:
    return MutationRepairContext(
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=("tests/test_forecast.py",),
        reproduce_command="mutmut run",
        survivor_count=count,
        groups=(group,),
    )


def test_renderer_python_minimal_line_has_no_superset_clauses():
    # A Python-style group sets only family/killability; the superset clauses
    # (mutator, ROI, priority note, ROI header) must NOT appear — this pins the
    # "Python output unchanged until Java is switched over" guarantee.
    rendered = format_mutation_repair_context(
        _context(MutationRepairGroup(symbol="forecast_for_store", count=2, lines=(10, 11), family="boundary", killability="high"))
    )
    assert "- mutation family: `boundary`, killability high" in rendered
    for clause in (" via `", "roi ", "effort ", "target early", "Ranked by kill-per-effort"):
        assert clause not in rendered


def test_renderer_surfaces_java_superset():
    rendered = format_mutation_repair_context(
        _context(
            MutationRepairGroup(
                symbol="applyDiscount", count=3, lines=(42,), family="boundary", killability="high",
                mutator="ConditionalsBoundaryMutator", effort_score="2", effort_band="low", roi=4.5,
            )
        )
    )
    assert "_Ranked by kill-per-effort ROI — tackle the top entry first._" in rendered
    assert (
        "- mutation family: `boundary` via `ConditionalsBoundaryMutator`, killability high, "
        "effort 2 (low), roi 4.5, target early" in rendered
    )


def test_renderer_priority_notes():
    equiv = format_mutation_repair_context(
        _context(MutationRepairGroup(symbol="m", count=1, family="math", killability="medium", mutator="MathMutator", likely_equivalent=True))
    )
    assert "likely_equivalent — skip, not worth a test" in equiv
    assert "target early" not in equiv

    deprio = format_mutation_repair_context(
        _context(MutationRepairGroup(symbol="m", count=1, family="side_effect", killability="low", mutator="VoidMethodCallMutator", deprioritized=True))
    )
    assert "deprioritize unless there is an easy seam" in deprio
    assert "target early" not in deprio


def test_rank_groups_by_roi_scores_and_sorts():
    # J1 step 2b: Python groups gain kill-per-effort ROI via the shared engine
    # math and re-order highest-ROI first (Python counterpart of Java score_families).
    from uta.enforcement.mutation_repair import MutationRepairGroup
    from uta.language.python.mutation_context import _rank_groups_by_roi

    groups = (
        MutationRepairGroup(symbol="combine", count=1, lines=(8,), family="math", killability="medium"),
        MutationRepairGroup(symbol="grade", count=1, lines=(2,), family="boundary", killability="high"),
    )
    efforts = [
        {"name": "grade", "effort_score": 1, "effort_band": "cheap"},
        {"name": "combine", "effort_score": 6, "effort_band": "expensive"},
    ]
    ranked = _rank_groups_by_roi(groups, efforts)
    # cheap high-killability boundary outranks expensive medium math
    assert [g.symbol for g in ranked] == ["grade", "combine"]
    grade = ranked[0]
    assert grade.roi == 3.0 and grade.effort_score == "1" and grade.effort_band == "cheap"
    assert ranked[1].roi == 0.17  # (1 * 1) / 6


def test_rank_groups_defaults_effort_for_unscored_symbol():
    from uta.enforcement.mutation_repair import MutationRepairGroup
    from uta.language.python.mutation_context import _rank_groups_by_roi

    ranked = _rank_groups_by_roi(
        (MutationRepairGroup(symbol="unknown", count=2, lines=(5,), family="conditional", killability="high"),),
        [],
    )
    # base effort defaults to 2 -> roi = (2 * 3) / 2 = 3.0
    assert ranked[0].roi == 3.0 and ranked[0].effort_score == "2"


@pytest.mark.parametrize(
    "family,expected",
    [
        ("boundary", (3, "high")),
        ("conditional", (3, "high")),
        ("return_value", (3, "high")),
        ("math", (1, "medium")),
        ("negation", (1, "medium")),
        ("side_effect", (1, "low")),
        ("other", (1, "low")),
    ],
)
def test_python_mutation_killability(family, expected):
    assert python_mutation_killability(family) == expected

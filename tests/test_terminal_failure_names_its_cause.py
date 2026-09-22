"""A unit that stops early must say why, not just which check noticed.

Beta task 38 ran a full generation pipeline -- plan, generate, verify, measure,
fix_coverage -- and then died with:

    terminal product evidence is invalid for uta:test-generation-cycle:v2:38:
    unit-0001-35d3878a6c3f:...: no active complete_generation/deterministic
    workflow operation

which is true and useless. The unit exited through `terminate_generation`,
which by design does not run `complete_generation`, so the missing completion
row is the *expected consequence* of terminating -- never the cause. The cause
was recorded in state by that node (`terminal_reason`, `error`) and then thrown
away here. The run log carried no reconciliation reason at all, so there was
nothing left to diagnose from.
"""

from __future__ import annotations

import pytest

from uta.testgen.graph.application import _terminal_detail


MISSING_ROW = "no active complete_generation/deterministic workflow operation"


def test_an_early_stop_reports_the_reconciliation_outcome():
    """The task-38 shape."""
    detail = _terminal_detail(
        {
            "stopped_early": True,
            "terminal_reason": "fail_indeterminate",
            "error": "generation stopped: fail_indeterminate",
        },
        ValueError(MISSING_ROW),
    )

    assert "fail_indeterminate" in detail
    assert MISSING_ROW in detail, "the check that noticed is still worth keeping"


def test_a_distinct_error_is_kept_alongside_the_reason():
    detail = _terminal_detail(
        {
            "stopped_early": True,
            "terminal_reason": "fail_unsafe",
            "error": "workspace has uncommitted edits outside the unit",
        },
        ValueError(MISSING_ROW),
    )

    assert "fail_unsafe" in detail
    assert "uncommitted edits outside the unit" in detail


def test_a_reason_that_only_restates_itself_is_not_repeated():
    detail = _terminal_detail(
        {"stopped_early": True, "terminal_reason": "fail_unsafe", "error": "fail_unsafe"},
        ValueError(MISSING_ROW),
    )

    assert detail.count("fail_unsafe") == 1


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"stopped_early": False, "terminal_reason": "fail_unsafe"},
        {"stopped_early": True, "terminal_reason": ""},
    ],
    ids=["no-early-stop", "explicitly-not-early", "early-stop-without-a-reason"],
)
def test_a_unit_that_did_not_stop_early_is_unchanged(state):
    """There the failing check really is the finding, and prefixing it with a
    manufactured 'stopped early' would be the same mistake pointed the other
    way."""
    detail = _terminal_detail(state, ValueError("terminal generation operation is not completed"))

    assert detail == "terminal generation operation is not completed"

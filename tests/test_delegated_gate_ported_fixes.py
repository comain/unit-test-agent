"""Three java quality-gate fixes ported from `main`.

All three targeted `uta/graph/nodes.py`, a module this branch split into
`uta/language/java/phases/*` and `uta/language/java/generation/quality.py`, so
none could be cherry-picked. In particular main's imperative
`_run_delegated_quality_gate_fix_loop` is the graph's retry cycle here, and
the behaviour that lived inside that loop now belongs to the verify phase.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from uta.language.java.phases.precheck import phase_result
from uta.language.java.phases.delegated_quality import (
    _unresolved_authoritative_failure,
    _user_repair_context_section,
)


class _Ports:
    """Only what the helpers under test call."""

    def __init__(self, matches: bool = True):
        self._matches = matches

    def failure_matches_batch(self, state, repo_path, result, batch) -> bool:
        return self._matches


def _state(initial: Dict[str, Any] | None, **extra) -> Dict[str, Any]:
    """Build the state the way the graph builds it.

    Through `phase_result`, the precheck node's own producer, rather than by
    hand. Hand-writing this shape is what hid the defect the first time: the
    projection carries `delegated_quality_gate` at its top level, the test
    put it under `evidence`, and the code read `evidence` -- so test and code
    agreed with each other and neither agreed with the graph.
    """
    projection = phase_result(
        {
            "_precheck_action": "delegated_repair",
            "delegated_quality_gate": initial,
        }
        if initial
        else {"_precheck_action": "delegated_repair"}
    )
    state: Dict[str, Any] = {"phase_results": {"precheck_existing_tests": projection}}
    state.update(extra)
    return state


# -- b54d928: a scoped pass must not clear an authoritative failure ------------


def test_a_scoped_pass_does_not_clear_the_authoritative_failure():
    """The target-scoped gate is narrower than the one that failed. Accepting
    its pass on the first attempt closes the task on evidence that never
    covered the failure."""
    state = _state({"passed": False, "stage": "coverage"})

    unresolved = _unresolved_authoritative_failure(
        state, "/repo", ["a.B"], ports=_Ports(), attempts=0
    )

    assert unresolved == {"passed": False, "stage": "coverage"}


def test_after_a_repair_turn_the_scoped_gate_is_believed():
    """Once a repair has actually run, the scoped gate reports on work that
    happened and is the better evidence -- otherwise the task could never
    clear."""
    state = _state({"passed": False})

    assert _unresolved_authoritative_failure(
        state, "/repo", ["a.B"], ports=_Ports(), attempts=1
    ) is None


def test_another_modules_failure_is_not_this_batchs_to_answer():
    state = _state({"passed": False})

    assert _unresolved_authoritative_failure(
        state, "/repo", ["a.B"], ports=_Ports(matches=False), attempts=0
    ) is None


@pytest.mark.parametrize(
    "initial",
    [None, {"passed": True}],
    ids=["no-precheck-failure", "precheck-passed"],
)
def test_nothing_to_preserve_when_the_gate_never_failed(initial):
    assert _unresolved_authoritative_failure(
        _state(initial), "/repo", ["a.B"], ports=_Ports(), attempts=0
    ) is None


# -- a1b202f: the requester's words reach the repair prompt --------------------


def test_the_user_context_becomes_a_prompt_section():
    section = _user_repair_context_section(
        {"rdc_context": {"user": {"context": "keep the documented fallback"}}}
    )

    assert section.startswith("### USER-SUPPLIED REPAIR CONTEXT")
    assert "keep the documented fallback" in section
    assert section.endswith("\n\n"), "the next section must not run into this one"


def test_no_section_without_context():
    """An empty heading tells the agent there is context and then shows none."""
    assert _user_repair_context_section({"rdc_context": {"user": {"context": "  "}}}) == ""
    assert _user_repair_context_section({}) == ""


def test_a_long_context_keeps_its_tail():
    """If it has to be cut, the end is where the specifics usually are."""
    context = "START-MARKER " + "opening waffle " * 1000 + "SPECIFIC REQUIREMENT"
    section = _user_repair_context_section(
        {"rdc_context": {"user": {"context": context}}}, max_chars=200
    )

    assert "SPECIFIC REQUIREMENT" in section
    assert "START-MARKER" not in section, "the head was kept instead of the tail"
    assert len(section) < len(context)


def test_the_override_path_actually_runs():
    """Exercises `verify_delegated_quality_gate`, not just its helper.

    Every other test here calls `_unresolved_authoritative_failure` directly,
    so none of them executed the branch that *uses* it -- which called
    `logger.warning` in a module that had no logger. Beta task 46 ran its whole
    repair to `complete_generation` and then died on
    `name 'logger' is not defined`, in the one code path a scoped pass over an
    authoritative failure takes.
    """
    from uta.language.java.phases.delegated_quality import verify_delegated_quality_gate
    from uta.language.java.phases.precheck import phase_result

    class _Ports:
        def run_gate(self, state, repo_path, *, batch, target_scoped):
            return {"passed": True}

        def failure_stage(self, result):
            return "coverage_fix"

        def gate_feedback(self, result):
            return "scoped gate passed"

        def failure_matches_batch(self, state, repo_path, result, batch):
            return True

        def prompt_feedback(self, repo_path, stage, attempt, output):
            return "feedback for " + stage

        def expected_test_paths(self, state, batch):
            return []

    projection = phase_result(
        {
            "_precheck_action": "delegated_repair",
            "delegated_quality_gate": {"passed": False, "stage": "coverage"},
        }
    )
    state = {
        "quality_gate_backend": "maven_enforcer",
        "batch": ["a.B"],
        "repo_path": "/repo",
        "attempts_by_phase": {},
        "phase_results": {"precheck_existing_tests": projection},
    }

    result = verify_delegated_quality_gate(state, ports=_Ports())

    assert result["evidence"]["scoped_pass_overridden"] is True
    assert result["phase_outcome"] == "repair", (
        "a scoped pass cleared an authoritative failure instead of repairing"
    )

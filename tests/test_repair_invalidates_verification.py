"""A repair makes the verification before it stale, and must say so.

Beta task 38 and task 40 both ran a full java generation pipeline -- plan,
generate, verify_compile, verify_tests, measure_coverage, fix_coverage -- and
died one second after `fix_coverage/interpret` completed:

    unit stopped early (fail_unsafe)

Nothing had corrupted the workspace. `fix_coverage` had *edited* it, which is
its entire job, and the graph then routed back to `verify_tests_reconcile`.
The reconciler builds an operation identity from (phase, step, attempt), and
the attempt for `verify_tests` was still 0 -- the repair only ever advanced its
own. So the re-verification landed on the previous COMPLETED `verify_tests`
row, whose recorded workspace fingerprint no longer matched the workspace the
repair had just changed, and the reconciler correctly called that unsafe.

Correctly: the check is right, the input was wrong. Re-running verification
after a repair is new work, not a replay, and it needs a new attempt.

The python lane never hit this because its target needed no coverage repair --
task 41 passed on the same deploy. That is the shape of the bug: it only
appears once a repair actually fires.
"""

from __future__ import annotations

import pytest

from uta.language.repair_attempts import (
    INVALIDATED_BY_REPAIR,
    advance_verification_attempts,
)


def test_every_verification_phase_moves_on():
    attempts = advance_verification_attempts({})

    assert set(INVALIDATED_BY_REPAIR) <= set(attempts)
    assert all(attempts[phase] == 1 for phase in INVALIDATED_BY_REPAIR)


def test_existing_counts_advance_rather_than_reset():
    attempts = advance_verification_attempts({"verify_tests": 2})

    assert attempts["verify_tests"] == 3


def test_unrelated_phases_are_untouched():
    """The repair's own counter is the caller's business, and the totals that
    gate retries must not be moved by this."""
    attempts = advance_verification_attempts(
        {"fix_coverage": 1, "python_repair_total": 4}
    )

    assert attempts["fix_coverage"] == 1
    assert attempts["python_repair_total"] == 4


def test_the_input_mapping_is_not_mutated():
    original = {"verify_tests": 1}

    advance_verification_attempts(original)

    assert original == {"verify_tests": 1}


@pytest.mark.parametrize(
    "interpret",
    [
        pytest.param("python", id="python"),
        pytest.param("java_coverage", id="java-fix-coverage"),
        pytest.param("java_tests", id="java-fix-tests"),
        pytest.param("java_compile", id="java-fix-compile"),
        pytest.param("java_mutation", id="java-fix-mutation"),
        pytest.param("java_delegated", id="java-delegated-quality-gate"),
    ],
)
def test_each_repair_interpreter_invalidates_verification(interpret):
    """All six repair paths, because one that forgets strands its unit and
    the symptom is a workspace-safety error that names no repair at all.
    Five were fixed first; the sixth was found the hard way, on beta."""
    if interpret == "python":
        from uta.language.python.phases import interpret_repair

        result = interpret_repair({"attempts_by_phase": {}}, {}, "fix_coverage")
    elif interpret == "java_coverage":
        from uta.language.java.phases.coverage import interpret_fix_coverage

        result = interpret_fix_coverage({"attempts_by_phase": {}}, {})
    elif interpret == "java_tests":
        from uta.language.java.phases.test_repair import interpret_fix_tests

        result = interpret_fix_tests({"attempts_by_phase": {}}, {})
    elif interpret == "java_compile":
        from uta.language.java.phases.compile import interpret_fix_compile

        result = interpret_fix_compile({"attempts_by_phase": {}}, {})
    elif interpret == "java_mutation":
        from uta.language.java.phases.mutation import interpret_fix_mutation

        result = interpret_fix_mutation({"attempts_by_phase": {}}, {})
    else:
        # The one that was missed the first time round: it repairs the
        # workspace like the others and was left out, so beta task 44 died
        # with the same `fail_unsafe`, one phase over.
        from uta.language.java.phases.delegated_quality import (
            interpret_delegated_quality_gate,
        )

        result = interpret_delegated_quality_gate({"attempts_by_phase": {}}, {})

    attempts = result["attempts_by_phase"]
    assert attempts["verify_tests"] == 1, interpret
    assert attempts["measure_coverage"] == 1, interpret


def test_every_verification_phase_in_the_cycle_is_listed():
    """Derived from the graph, not from memory.

    This list has been wrong twice: `delegated_quality_gate` was missed when
    the repair interpreters were fixed, and `delegated_quality_gate_verify`
    was missed when the phase list was written. Each miss cost a beta run that
    died with a workspace-safety error naming no repair at all. Reading the
    cycle definition means a new verification phase shows up here as a failing
    test rather than as a stranded unit.
    """
    import pathlib
    import re

    cycle = (
        pathlib.Path(__file__).resolve().parents[1]
        / "uta" / "testgen" / "graph" / "generation-cycle.yaml"
    ).read_text(encoding="utf-8")
    phases = set(re.findall(r"^\s+phase:\s*(\S+)\s*$", cycle, re.M))

    # A repair turn edits the workspace; the lifecycle bookends do not verify.
    repairs = {
        "plan_tests", "generate_tests", "fix_compile", "fix_tests",
        "fix_coverage", "fix_mutation", "delegated_quality_gate",
    }
    lifecycle = {"precheck_existing_tests", "complete_generation"}
    # The equivalent-mutant review is a model turn that is forbidden to edit
    # (an edit terminates the unit), and it only routes to completion: nothing
    # it does can make an earlier verification stale.
    reviews = {"review_equivalent_mutants"}
    verification = phases - repairs - lifecycle - reviews

    missing = sorted(verification - set(INVALIDATED_BY_REPAIR))
    assert missing == [], (
        "verification phases a repair invalidates but nothing advances: " + ", ".join(missing)
    )

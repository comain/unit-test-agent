"""Mutants the tests never reach are a score, not a backend fault.

Replaying production task 37f46d55 on beta: production reported
`mutation_gate_failed` at 94.44%, beta reported `mutation_backend_failed --
backend failed before executing 3 selected mutants`. Same repo, same branch,
different verdict.

The three mutants were on `common/monitor_constants.py`, and mutmut said in
its own words why:

    tests do not cover any code that we mutated

The run completed and reported a result. The binding counted any
`not_checked > 0` as a backend failure, so one constants file aborted a
fifty-six module run and hid a 96.99% aggregate that would have passed the
95% gate. The clean-test probe in the same evidence passed five tests, which
is what makes "backend failed" the wrong word.

They now count as survived: not killed, not a fault. Scoring them as "no
coverage" alone would leave an empty denominator, and an empty denominator
reads as 100% -- the same vacuous pass an empty changed-line set produces.
"""

from __future__ import annotations

import pytest

from uta_py_enforce.mutation import _mutants_uncovered_by_tests


MUTMUT_UNCOVERED = {
    "exitCode": 1,
    "stdout": (
        "1/3  ...\n"
        "The tests do not cover any code that we mutated.\n"
        "You can set debug=true to see the executed test names in the output above.\n"
    ),
    "stderr": "",
}


def test_the_mutmut_wording_is_recognised():
    assert _mutants_uncovered_by_tests(MUTMUT_UNCOVERED) is True


def test_the_alternate_phrasing_is_recognised():
    assert _mutants_uncovered_by_tests(
        {"stdout": "The tests did not cover any code that we mutated."}
    ) is True


def test_stderr_is_searched_too():
    assert _mutants_uncovered_by_tests(
        {"stdout": "", "stderr": "tests do not cover any code that we mutated"}
    ) is True


@pytest.mark.parametrize(
    "command",
    [None, {}, {"stdout": "ImportError: no module named foo", "stderr": ""},
     {"stdout": "", "stderr": "Traceback (most recent call last)"}],
    ids=["none", "empty", "import-error", "traceback"],
)
def test_a_real_fault_is_not_mistaken_for_uncovered(command):
    """The point of the guard: a genuine backend fault must still fail closed.
    Widening this predicate would turn crashes into quiet score reductions."""
    assert _mutants_uncovered_by_tests(command) is False

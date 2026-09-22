"""Each language says whether a failed gate failed on mutation alone."""

from __future__ import annotations

from uta.language.java.ci import JavaCiLanguageHandler
from uta.language.java.equivalence import java_gate_failure_flags
from uta.language.python.equivalence import python_gate_failure_flags

COVERAGE_OK = "[test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
COVERAGE_LOW = "test-enforcer check-coverage failed: diff line coverage 87.50% is below required 95.00%\n"
MUTATION_LOW = "test-enforcer check-mutation failed: diff mutation score 66.67% for demo.biz (2/3 detected; 1 survived; 0 no coverage excluded) is below required 100.00%\n"
MUTATION_OK = "[test-enforcer] diff mutation score 100.00% passed for demo.biz (3/3 detected; 0 survived; 0 no coverage excluded)\n"


def _java(stdout, passed=False):
    return {"status": "passed" if passed else "failed", "passed": passed, "stdout": stdout, "stderr": "", "summary": ""}


def test_java_mutation_only_failure():
    assert java_gate_failure_flags(_java(COVERAGE_OK + MUTATION_LOW)) == {
        "tests_passed": True, "coverage_passed": True, "mutation_only_failure": True,
    }


def test_java_coverage_failure_is_not_mutation_only():
    flags = java_gate_failure_flags(_java(COVERAGE_LOW + MUTATION_LOW))
    assert flags["coverage_passed"] is False
    assert flags["mutation_only_failure"] is False


def test_java_red_suite_blocks_mutation_and_fails_tests():
    blocked = COVERAGE_OK + "[ERROR] Mutation testing requires a green suite.\n" + MUTATION_LOW
    flags = java_gate_failure_flags(_java(blocked))
    assert flags["tests_passed"] is False
    assert flags["mutation_only_failure"] is False


def test_java_without_both_gate_lines_is_unknown():
    assert java_gate_failure_flags(_java(MUTATION_LOW)) is None
    assert java_gate_failure_flags(_java(COVERAGE_OK)) is None
    assert java_gate_failure_flags(_java(COVERAGE_OK + MUTATION_OK, passed=True))["mutation_only_failure"] is False


def _python(coverage_passed, mutation_passed, passed=False, tests=(True,)):
    return {
        "passed": passed,
        "evidence": {
            "coverage": None if coverage_passed is None else {"passed": coverage_passed},
            "mutation": None if mutation_passed is None else {"passed": mutation_passed},
            "targetResults": [{"testsPass": value} for value in tests],
        },
    }


def test_python_flags_come_from_evidence():
    assert python_gate_failure_flags(_python(True, False)) == {
        "tests_passed": True, "coverage_passed": True, "mutation_only_failure": True,
    }
    assert python_gate_failure_flags(_python(False, False))["mutation_only_failure"] is False
    assert python_gate_failure_flags(_python(None, False)) is None
    assert python_gate_failure_flags(_python(True, None)) is None


def test_handler_exposes_the_language_flags():
    from uta.enforcement.enforcement import QualityGateResult, QualityGateStatus

    result = QualityGateResult(
        status=QualityGateStatus.failed, passed=False, command=["mvn"], stdout=COVERAGE_OK + MUTATION_LOW
    )
    flags = JavaCiLanguageHandler(runner=None).gate_failure_flags(record=None, result=result)
    assert flags["mutation_only_failure"] is True


def test_python_red_suite_outside_coverage_is_not_mutation_only():
    """A target whose tests fail never produces coverage, so the aggregate
    coverage can still read passed; the per-target test verdict must veto."""
    flags = python_gate_failure_flags(_python(True, False, tests=(True, False)))
    assert flags["tests_passed"] is False
    assert flags["mutation_only_failure"] is False
    missing = _python(True, False)
    missing["evidence"].pop("targetResults")
    assert python_gate_failure_flags(missing) is None

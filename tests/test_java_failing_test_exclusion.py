"""Diff-unrelated failing tests must leave both enforcement gates together.

docs/plan-baseline-red-exclusion.md. The run tolerates a red suite, so JaCoCo
credits lines that failing tests walked through while PIT discards those same
tests. These tests pin the rule that ends the disagreement, and the two things
that make it real rather than cosmetic: both Maven properties, and a cleared
`jacoco.exec` before the second run.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from fake_maven_metadata import DIFF_MUTATION_OK, with_resolved_enforcer

from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.language.java.enforcement_runner.failing_test_scope import (
    partition_failing_tests,
)
from uta.language.java.enforcement_runner.parsing import (
    _failed_surefire_test_classes,
    _surefire_report_snapshot,
    _surefire_supports_includes_file,
    _surefire_test_class_results,
    _surefire_test_classes_by_module,
)
from uta.language.java.enforcement_runner.planning import _with_selected_test_classes

_PLUGIN_OUTPUT = (
    "[INFO] --- surefire:3.5.4:test (default-test) @ demo ---\n"
    "[INFO] --- test-enforcer:1.0.16:filter-diff (filter-diff) @ demo ---\n"
    "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
    "pitest.targets=1 [com.demo.service.OrderService*]\n"
    "Diff coverage: 100%\n"
    "PIT generated=4 killed=4 survived=0 test-strength=100%\n"
    + DIFF_MUTATION_OK
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@example.test")
    git("config", "user.name", "t")
    (repo / "pom.xml").write_text("<project><artifactId>demo</artifactId></project>\n", encoding="utf-8")
    (repo / "service").mkdir()
    (repo / "service/pom.xml").write_text(
        "<project><artifactId>service</artifactId></project>\n", encoding="utf-8"
    )
    prod = repo / "service/src/main/java/com/demo/service/OrderService.java"
    own_test = repo / "service/src/test/java/com/demo/service/OrderServiceTest.java"
    legacy = repo / "service/src/test/java/com/demo/legacy/BillingContextTest.java"
    for path in (prod, own_test, legacy):
        path.parent.mkdir(parents=True, exist_ok=True)
    prod.write_text("package com.demo.service; class OrderService {}\n", encoding="utf-8")
    own_test.write_text(
        "package com.demo.service;\n"
        "public class OrderServiceTest { OrderService s = new OrderService(); }\n",
        encoding="utf-8",
    )
    legacy.write_text(
        "package com.demo.legacy;\npublic class BillingContextTest { }\n", encoding="utf-8"
    )
    git("add", ".")
    git("commit", "-q", "-m", "base")
    git("update-ref", "refs/remotes/origin/master", "HEAD")
    prod.write_text("package com.demo.service; class OrderService { int v; }\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "change")
    return repo


def _surefire_report(repo: Path, class_fqn: str, *, errors: int = 0, failures: int = 0) -> None:
    reports = repo / "service/target/surefire-reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"TEST-{class_fqn}.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite name="%s" tests="4" errors="%d" failures="%d" skipped="0"/>\n'
        % (class_fqn, errors, failures),
        encoding="utf-8",
    )


def _runner(calls, *, enabled=True, output=_PLUGIN_OUTPUT):
    @with_resolved_enforcer
    def fake_run(cmd, *args, **kwargs):
        if kwargs.get("cwd"):
            _mark_reports_current(Path(kwargs["cwd"]))
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")

    return MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
        full_run=True,
        exclude_unrelated_failing_tests=enabled,
    )


def _mark_reports_current(repo: Path) -> None:
    for report in repo.glob("**/target/surefire-reports/TEST-*.xml"):
        stat = report.stat()
        os.utime(report, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


# T1 -- the uncapped reader ---------------------------------------------------

def test_every_failing_class_is_read_not_just_the_first_twelve(tmp_path):
    """The human-readable reader stops at twelve. This one decides what leaves
    the run, so a truncated answer would silently retain classes."""
    repo = _repo(tmp_path)
    for index in range(140):
        _surefire_report(repo, "com.demo.legacy.Context%03dTest" % index, errors=4)

    assert len(_failed_surefire_test_classes(repo)) == 140


def test_passing_classes_and_malformed_reports_are_not_failures(tmp_path):
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.GreenTest")
    _surefire_report(repo, "com.demo.legacy.RedTest", failures=1)
    (repo / "service/target/surefire-reports/TEST-broken.xml").write_text("<not-xml", encoding="utf-8")

    assert _failed_surefire_test_classes(repo) == ["com.demo.legacy.RedTest"]
    assert _surefire_test_class_results(repo) == {
        "com.demo.legacy.GreenTest": False,
        "com.demo.legacy.RedTest": True,
    }


def test_current_run_inventory_ignores_unchanged_stale_reports(tmp_path):
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.ui.FormGenerate", failures=1)
    previous = _surefire_report_snapshot(repo)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")

    assert _surefire_test_class_results(repo, previous) == {
        "com.demo.service.OrderServiceTest": False,
    }


# T2 -- the rule --------------------------------------------------------------

def test_a_failing_test_the_diff_never_touched_or_named_is_excluded(tmp_path):
    repo = _repo(tmp_path)

    partition = partition_failing_tests(
        ["com.demo.legacy.BillingContextTest"],
        repo_path=repo,
        changed_java=["service/src/main/java/com/demo/service/OrderService.java"],
        changed_production_java=["service/src/main/java/com/demo/service/OrderService.java"],
    )

    assert partition.excluded == ("com.demo.legacy.BillingContextTest",)
    assert partition.reason == ""


def test_a_failing_test_that_names_a_changed_class_is_retained(tmp_path):
    """The blind spot is indirect wiring, so the one signal available -- the test
    referencing the changed type -- must keep it in the verdict."""
    repo = _repo(tmp_path)

    partition = partition_failing_tests(
        ["com.demo.service.OrderServiceTest"],
        repo_path=repo,
        changed_java=["service/src/main/java/com/demo/service/OrderService.java"],
        changed_production_java=["service/src/main/java/com/demo/service/OrderService.java"],
    )

    assert partition.excluded == ()
    assert partition.retained == ("com.demo.service.OrderServiceTest",)
    assert partition.reason == "all-failures-related"


def test_a_failing_test_the_diff_touched_is_retained(tmp_path):
    repo = _repo(tmp_path)

    partition = partition_failing_tests(
        ["com.demo.legacy.BillingContextTest"],
        repo_path=repo,
        changed_java=["service/src/test/java/com/demo/legacy/BillingContextTest.java"],
        changed_production_java=[],
    )

    assert partition.excluded == ()
    assert partition.retained == ("com.demo.legacy.BillingContextTest",)


def test_no_diff_means_no_exclusion(tmp_path):
    """Relatedness cannot be evaluated without a diff, and a guess is not
    something the report could defend."""
    repo = _repo(tmp_path)

    partition = partition_failing_tests(
        ["com.demo.legacy.BillingContextTest"],
        repo_path=repo,
        changed_java=None,
        changed_production_java=None,
    )

    assert partition.excluded == ()
    assert partition.reason == "no-diff-available"


# T3 -- command shaping -------------------------------------------------------

def test_both_stages_get_the_same_inventory_without_negative_surefire_patterns():
    """Surefire runs the known passing inventory while PIT gets the drop list."""
    cmd = _with_selected_test_classes(
        ["mvn", "verify"],
        ["com.demo.service.OrderServiceTest"],
        ["com.demo.legacy.BillingContextTest"],
    )

    assert "-Dtest=OrderServiceTest" in cmd
    assert "-DexcludedTestClasses=com.demo.legacy.BillingContextTest" in cmd
    assert not any("!" in item for item in cmd)


def test_nothing_to_exclude_leaves_the_command_untouched():
    assert _with_selected_test_classes(["mvn", "verify"], ["KeepTest"], []) == [
        "mvn",
        "verify",
    ]


def test_existing_selector_is_replaced_by_the_observed_positive_inventory():
    cmd = _with_selected_test_classes(
        ["mvn", "-Dtest=BroadTest", "-DexcludedTestClasses=com.demo.Kept", "verify"],
        ["com.demo.service.OrderServiceTest"],
        ["com.demo.legacy.BillingContextTest"],
    )

    assert "-Dtest=OrderServiceTest" in cmd
    assert "-Dtest=BroadTest" not in cmd
    assert "-DexcludedTestClasses=com.demo.Kept,com.demo.legacy.BillingContextTest" in cmd


def test_a_name_that_is_not_a_java_fqn_never_reaches_the_command_line():
    cmd = _with_selected_test_classes(
        ["mvn", "verify"], ["KeepTest"], ["; rm -rf /", "-Dfoo=bar", "a b"]
    )

    assert cmd == ["mvn", "verify"]


def test_legacy_positive_selector_refuses_an_oversized_single_argument():
    kept = ["com.demo.Test%05d%s" % (index, "X" * 80) for index in range(1200)]

    cmd = _with_selected_test_classes(
        ["mvn", "verify"], kept, ["com.demo.BillingContextTest"]
    )

    assert cmd == ["mvn", "verify"]


def test_a_default_package_class_is_a_fully_qualified_name(tmp_path):
    """`CardTest` in the default package IS its own FQN. Demanding a dot dropped
    it from both properties while the report still called it excluded, so it ran
    again and failed again -- and the baseline stayed red."""
    cmd = _with_selected_test_classes(
        ["mvn", "verify"], ["OrderServiceTest"], ["CardTest", "com.demo.CardBizTest"]
    )

    assert "-Dtest=OrderServiceTest" in cmd
    assert "-DexcludedTestClasses=CardTest,com.demo.CardBizTest" in cmd


def test_a_name_the_command_line_would_drop_is_reported_as_retained(tmp_path):
    """The evidence has to describe the run that happened. A name that cannot be
    handed to Maven is retained, never listed among the excluded."""
    repo = _repo(tmp_path)

    partition = partition_failing_tests(
        ["com.demo.legacy.BillingContextTest", "-Dnot=a.class"],
        repo_path=repo,
        changed_java=["service/src/main/java/com/demo/service/OrderService.java"],
        changed_production_java=["service/src/main/java/com/demo/service/OrderService.java"],
    )

    assert partition.excluded == ("com.demo.legacy.BillingContextTest",)
    assert "-Dnot=a.class" in partition.retained


# T5 -- wiring ----------------------------------------------------------------

def test_the_flag_off_is_exactly_todays_behaviour(tmp_path):
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    calls = []

    _runner(calls, enabled=False).run(repo)

    assert len(calls) == 1
    assert not [item for item in calls[0] if item.startswith("-DexcludedTestClasses=")]


def test_a_green_suite_is_never_run_twice(tmp_path):
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []

    _runner(calls).run(repo)

    assert len(calls) == 1


def test_unrelated_failures_trigger_one_rerun_with_both_properties(tmp_path):
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []

    result = _runner(calls).run(repo)

    assert len(calls) == 2
    rerun = " ".join(calls[1])
    assert "-Dsurefire.includes=__uta_no_test_matches__" in rerun
    assert "-Dsurefire.includesFile=" in rerun
    assert "-DexcludedTestClasses=com.demo.legacy.BillingContextTest" in rerun
    assert result.evidence["excludedFailingTestClasses"] == ["com.demo.legacy.BillingContextTest"]


def test_related_failures_alone_do_not_trigger_a_rerun(tmp_path):
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.service.OrderServiceTest", failures=1)
    calls = []

    result = _runner(calls).run(repo)

    assert len(calls) == 1
    assert result.evidence["exclusionReason"] == "all-failures-related"
    assert result.evidence["retainedFailingTestClasses"] == ["com.demo.service.OrderServiceTest"]


def test_emptying_a_modules_suite_is_refused(tmp_path):
    """PIT fails a module with no tests left, and an empty Surefire selection
    would pass vacuously -- neither is a verdict worth taking."""
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    calls = []

    result = _runner(calls).run(repo)

    assert len(calls) == 1
    assert result.evidence["excludedFailingTestClasses"] == []
    assert result.evidence["exclusionReason"] == "would-empty-module-suite"


def _module_surefire_report(
    repo: Path, module: str, class_fqn: str, *, errors: int = 0, failures: int = 0
) -> None:
    reports = repo / module / "target/surefire-reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"TEST-{class_fqn}.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite name="%s" tests="4" errors="%d" failures="%d" skipped="0"/>\n'
        % (class_fqn, errors, failures),
        encoding="utf-8",
    )


def test_the_emptied_module_is_judged_per_module_not_repo_wide(tmp_path):
    """Maven applies `-Dtest` to the whole reactor. A repo-wide count says
    "plenty of tests left" while one module runs none, Surefire aborts it with
    `No tests were executed!`, and every later module is skipped -- so the gates
    never report at all. The module that would be emptied keeps its tests; the
    others still get the exclusion."""
    repo = _repo(tmp_path)
    # `service` has two tests, one of them red -- excluding it leaves one behind.
    _module_surefire_report(repo, "service", "com.demo.legacy.BillingContextTest", errors=4)
    _module_surefire_report(repo, "service", "com.demo.service.OrderServiceTest")
    # `reporting` has exactly one test, and it is red. Excluding it empties it.
    _module_surefire_report(repo, "reporting", "com.demo.reporting.LedgerTest", errors=1)
    calls = []

    result = _runner(calls).run(repo)

    assert len(calls) == 2, "the exclusion re-run should still happen"
    excluded = result.evidence["excludedFailingTestClasses"]
    assert excluded == ["com.demo.legacy.BillingContextTest"]
    assert "com.demo.reporting.LedgerTest" in result.evidence["retainedFailingTestClasses"]
    rerun = " ".join(calls[1])
    assert "BillingContextTest" in rerun
    assert "LedgerTest" not in rerun, "the only test in `reporting` must still run"


def test_a_reactor_wide_emptying_still_refuses_the_whole_rerun(tmp_path):
    """When protecting every emptied module leaves nothing to exclude, the
    re-run falls away and the reason says why."""
    repo = _repo(tmp_path)
    _module_surefire_report(repo, "service", "com.demo.legacy.BillingContextTest", errors=4)
    _module_surefire_report(repo, "reporting", "com.demo.reporting.LedgerTest", errors=1)
    calls = []

    result = _runner(calls).run(repo)

    assert len(calls) == 1
    assert result.evidence["excludedFailingTestClasses"] == []
    assert result.evidence["exclusionReason"] == "would-empty-module-suite"


def test_surefire_results_are_grouped_by_the_module_that_ran_them(tmp_path):
    repo = _repo(tmp_path)
    _module_surefire_report(repo, "service", "com.demo.service.OrderServiceTest")
    _module_surefire_report(repo, "reporting", "com.demo.reporting.LedgerTest")

    by_module = _surefire_test_classes_by_module(repo)

    assert by_module["service"] == {"com.demo.service.OrderServiceTest"}
    assert by_module["reporting"] == {"com.demo.reporting.LedgerTest"}


# T4 -- the second run must not inherit the first -----------------------------

def test_stale_coverage_and_reports_are_cleared_before_the_rerun(tmp_path):
    """JaCoCo's agent appends. Without clearing, the second run reports the
    first run's coverage and the exclusion silently does nothing."""
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    exec_file = repo / "service/target/jacoco.exec"
    exec_file.write_text("stale", encoding="utf-8")
    seen = {}

    @with_resolved_enforcer
    def fake_run(cmd, *args, **kwargs):
        if not seen and kwargs.get("cwd"):
            _mark_reports_current(Path(kwargs["cwd"]))
        seen[len(seen)] = (exec_file.exists(), (repo / "service/target/surefire-reports").exists())
        return subprocess.CompletedProcess(cmd, 0, stdout=_PLUGIN_OUTPUT, stderr="")

    MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
        full_run=True,
        exclude_unrelated_failing_tests=True,
    ).run(repo)

    assert seen[0] == (True, True), "the first run sees what was already there"
    assert seen[1] == (False, False), "the second run must start from a clean target/"


# T9 -- the production task that motivated this ------------------------------

def test_the_cigarette_man_shape_excludes_all_140_context_init_failures(tmp_path):
    """Task 2d91c62a65f24aafb8eb9202c9001344: 140 test classes died at Spring
    context init on a one-class obligation, and PIT was handed 568 test classes
    while JaCoCo counted all of them. None of the 140 is related to the diff."""
    repo = _repo(tmp_path)
    for index in range(140):
        _surefire_report(repo, "com.tempoon.cigarette.man.web.Ctx%03dTest" % index, errors=8)
    for index in range(3):
        _surefire_report(repo, "com.demo.service.Green%03dTest" % index)
    calls = []

    result = _runner(calls).run(repo)

    excluded = result.evidence["excludedFailingTestClasses"]
    assert len(excluded) == 140
    assert len(calls) == 2
    rerun = calls[1]
    pit = [item for item in rerun if item.startswith("-DexcludedTestClasses=")][0]
    includes_file = [item for item in rerun if item.startswith("-Dsurefire.includesFile=")][0]
    assert set(pit.split("=", 1)[1].split(",")) == set(excluded)
    selected = Path(includes_file.split("=", 1)[1]).read_text(encoding="utf-8").splitlines()
    assert selected == ["**/Green000Test.java", "**/Green001Test.java", "**/Green002Test.java"]


def test_the_first_runs_coverage_is_recorded_for_comparison(tmp_path):
    """Whether exclusion moved a verdict is the whole production question, and
    it is unanswerable without the number the first run reported."""
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []

    result = _runner(calls).run(repo)

    assert result.evidence["coverageBeforeExclusion"] == 100.0


# T7 -- the positive inventory dialect ---------------------------------------

_OLD_SUREFIRE_OUTPUT = _PLUGIN_OUTPUT.replace(
    "--- surefire:3.5.4:test", "--- maven-surefire-plugin:2.5:test"
)


def test_surefire_includes_file_support_is_read_from_the_run():
    assert _surefire_supports_includes_file("[INFO] --- surefire:3.5.4:test (x) @ a ---")
    assert not _surefire_supports_includes_file(
        "[INFO] --- maven-surefire-plugin:2.5:test (x) @ a ---"
    )
    assert not _surefire_supports_includes_file("nothing about surefire here")
    assert not _surefire_supports_includes_file(
        "--- surefire:3.5.4:test (x) @ a ---\n--- maven-surefire-plugin:2.5:test (y) @ b ---"
    )


def test_an_old_surefire_is_told_what_to_run_not_what_to_skip(tmp_path):
    """Surefire before 2.13 receives the same inventory through `-Dtest`."""
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []

    result = _runner(calls, output=_OLD_SUREFIRE_OUTPUT).run(repo)

    assert len(calls) == 2, "the exclusion re-run should still happen"
    rerun = calls[1]
    assert "-Dtest=OrderServiceTest" in rerun
    assert not any("!" in item for item in rerun), "2.5 cannot read a negation"
    assert "-DexcludedTestClasses=com.demo.legacy.BillingContextTest" in rerun
    assert "-DfailIfNoTests=false" in rerun
    assert result.evidence["excludedFailingTestClasses"] == [
        "com.demo.legacy.BillingContextTest"
    ]


def test_a_modern_surefire_uses_a_file_backed_positive_inventory(tmp_path):
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []

    result = _runner(calls).run(repo)

    assert len(calls) == 2
    rerun = calls[1]
    assert not any(item.startswith("-Dtest=") for item in rerun)
    includes_file = next(item for item in rerun if item.startswith("-Dsurefire.includesFile="))
    assert Path(includes_file.split("=", 1)[1]).read_text(encoding="utf-8") == (
        "**/OrderServiceTest.java\n"
    )
    assert result.evidence["excludedFailingTestClasses"] == [
        "com.demo.legacy.BillingContextTest"
    ]


# T8 -- the re-run may never replace a verdict with silence -------------------

def test_a_rerun_that_loses_the_evidence_is_discarded(tmp_path):
    """The re-run exists to describe the same run more honestly, never to trade a
    real verdict for none. This is the shape shelf-hermes hit in production."""
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []

    @with_resolved_enforcer
    def fake_run(cmd, *args, **kwargs):
        if not calls and kwargs.get("cwd"):
            _mark_reports_current(Path(kwargs["cwd"]))
        calls.append(list(cmd))
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 0, stdout=_PLUGIN_OUTPUT, stderr="")
        return subprocess.CompletedProcess(
            cmd, 1, stdout="[ERROR] No tests were executed!", stderr=""
        )

    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
        full_run=True,
        exclude_unrelated_failing_tests=True,
    )
    result = runner.run(repo)

    assert len(calls) == 2, "the re-run is attempted"
    assert result.evidence["excludedFailingTestClasses"] == []
    assert result.evidence["exclusionReason"] == "rerun-produced-no-evidence"
    assert result.passed, "the first run's verdict is what survives"


def test_a_simple_name_collision_excludes_every_matching_test(tmp_path):
    """`-Dtest` speaks simple names in both dialects, so two `FooTest` classes in
    different packages are one word to Surefire. Exclude both when either is an
    unrelated baseline failure."""
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _module_surefire_report(repo, "other", "com.demo.other.BillingContextTest")
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []

    result = _runner(calls).run(repo)

    assert len(calls) == 2
    rerun = calls[1]
    includes_file = next(
        item for item in rerun if item.startswith("-Dsurefire.includesFile=")
    )
    assert Path(includes_file.split("=", 1)[1]).read_text(encoding="utf-8") == (
        "**/OrderServiceTest.java\n"
    )
    excluded = next(item for item in rerun if item.startswith("-DexcludedTestClasses="))
    assert set(excluded.split("=", 1)[1].split(",")) == {
        "com.demo.legacy.BillingContextTest",
        "com.demo.other.BillingContextTest",
    }
    assert result.evidence["excludedFailingTestClasses"] == [
        "com.demo.legacy.BillingContextTest"
    ]
    assert result.evidence["excludedCollidingTestClasses"] == [
        "com.demo.other.BillingContextTest"
    ]
    assert result.evidence["exclusionReason"] == "simple-name-collision-expanded"


def test_an_old_reactor_that_stopped_early_is_not_given_a_positive_list(tmp_path):
    """The positive dialect can only name tests the first run reported. Maven is
    fail-fast, so a module after the failure writes no Surefire reports at all --
    naming only what we saw would drop it from the re-run entirely and report its
    covered lines as uncovered."""
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []
    output = _OLD_SUREFIRE_OUTPUT + "[INFO] demo-reporting ......... SKIPPED\n"

    result = _runner(calls, output=output).run(repo)

    assert len(calls) == 1, "the re-run would have been measured against half a reactor"
    assert result.evidence["excludedFailingTestClasses"] == []
    assert result.evidence["exclusionReason"] == "incomplete-test-inventory"


def test_a_modern_reactor_that_stopped_early_is_not_given_an_incomplete_inventory(tmp_path):
    repo = _repo(tmp_path)
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []
    output = _PLUGIN_OUTPUT + "[INFO] demo-reporting ......... SKIPPED\n"

    result = _runner(calls, output=output).run(repo)

    assert len(calls) == 1
    assert result.evidence["excludedFailingTestClasses"] == []
    assert result.evidence["exclusionReason"] == "incomplete-test-inventory"


def _module(repo: Path, module: str, artifact_id: str, *tests: str, name: str = "") -> None:
    (repo / module).mkdir(parents=True, exist_ok=True)
    name_xml = f"<name>{name}</name>" if name else ""
    (repo / module / "pom.xml").write_text(
        f"<project><artifactId>{artifact_id}</artifactId>{name_xml}</project>\n", encoding="utf-8"
    )
    for fqn in tests:
        package, simple = fqn.rsplit(".", 1)
        path = repo / module / "src/test/java" / (fqn.replace(".", "/") + ".java")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"package {package};\npublic class {simple} {{ }}\n", encoding="utf-8")


def test_a_module_skipped_after_the_failure_is_inventoried_from_source(tmp_path):
    """cvs-usercenter-web, 2026-09-15: PIT stopped user-center-biz (third of four)
    on a diff-unrelated flaky test, user-center-web was SKIPPED, and the exclusion
    was refused as incomplete -- in exactly the situation it exists for. The
    skipped module's tests are now named from source, within Surefire's default
    includes."""
    repo = _repo(tmp_path)
    _module(
        repo, "web", "demo-web",
        "com.demo.web.WebControllerTest", "com.demo.web.WebFixtures", name="Demo Web",
    )
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []
    output = _PLUGIN_OUTPUT + "[INFO] Demo Web ........................ SKIPPED\n"

    result = _runner(calls, output=output).run(repo)

    assert len(calls) == 2
    assert result.evidence["excludedFailingTestClasses"] == ["com.demo.legacy.BillingContextTest"]
    assert result.evidence["sourceInventoriedModules"] == ["web"]
    includes_file = next(item for item in calls[1] if item.startswith("-Dsurefire.includesFile="))
    patterns = Path(includes_file.split("=", 1)[1]).read_text(encoding="utf-8")
    assert patterns == "**/OrderServiceTest.java\n**/WebControllerTest.java\n"


def test_an_old_reactor_names_the_skipped_module_in_its_positive_selector(tmp_path):
    repo = _repo(tmp_path)
    _module(repo, "web", "demo-web", "com.demo.web.WebControllerTest")
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []
    output = _OLD_SUREFIRE_OUTPUT + "[INFO] demo-web 2.0.0 ......... SKIPPED\n"

    result = _runner(calls, output=output).run(repo)

    assert len(calls) == 2
    selector = next(item for item in calls[1] if item.startswith("-Dtest="))
    assert "WebControllerTest" in selector and "OrderServiceTest" in selector
    assert "BillingContextTest" not in selector
    assert result.evidence["sourceInventoriedModules"] == ["web"]


def test_a_skipped_name_shared_by_two_modules_still_refuses_the_rerun(tmp_path):
    repo = _repo(tmp_path)
    _module(repo, "web", "demo-web", "com.demo.web.WebControllerTest")
    _module(repo, "legacy/web", "demo-web", "com.demo.old.OldWebTest")
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []
    output = _PLUGIN_OUTPUT + "[INFO] demo-web ......... SKIPPED\n"

    result = _runner(calls, output=output).run(repo)

    assert len(calls) == 1
    assert result.evidence["excludedFailingTestClasses"] == []
    assert result.evidence["exclusionReason"] == "incomplete-test-inventory"


def test_surefire_3_defaults_add_the_tests_suffix_for_source_inventory(tmp_path):
    from uta.language.java.enforcement_runner.parsing import (
        _reactor_skipped_modules,
        _surefire_defaults_include_tests_suffix,
    )
    from uta.language.java.enforcement_runner.planning import _declared_test_classes_by_module

    repo = _repo(tmp_path)
    _module(repo, "web", "demo-web", "com.demo.web.WebTests", "com.demo.web.WebTest")

    assert _reactor_skipped_modules(_PLUGIN_OUTPUT + "[INFO] demo-web ... SKIPPED\n") == ["demo-web"]
    assert _surefire_defaults_include_tests_suffix(_PLUGIN_OUTPUT)
    assert not _surefire_defaults_include_tests_suffix(_OLD_SUREFIRE_OUTPUT)
    assert _declared_test_classes_by_module(repo, ["demo-web"]) == {"web": {"com.demo.web.WebTest"}}
    assert _declared_test_classes_by_module(repo, ["demo-web"], include_tests_suffix=True) == {
        "web": {"com.demo.web.WebTest", "com.demo.web.WebTests"}
    }
    assert _declared_test_classes_by_module(repo, ["no-such-module"]) is None


def test_positive_inventory_does_not_discover_nonstandard_at_test_utility(tmp_path):
    """A negative-only -Dtest broadens Surefire discovery and ran FormGenerate."""
    repo = _repo(tmp_path)
    utility = repo / "service/src/test/java/com/demo/ui/FormGenerate.java"
    utility.parent.mkdir(parents=True, exist_ok=True)
    utility.write_text("class FormGenerate { @org.junit.Test public void qgpOrder() {} }\n")
    _surefire_report(repo, "com.demo.legacy.BillingContextTest", errors=4)
    _surefire_report(repo, "com.demo.service.OrderServiceTest")
    calls = []

    _runner(calls).run(repo)

    includes_file = next(
        item for item in calls[1] if item.startswith("-Dsurefire.includesFile=")
    )
    patterns = Path(includes_file.split("=", 1)[1]).read_text(encoding="utf-8")
    assert patterns == "**/OrderServiceTest.java\n"
    assert "FormGenerate" not in patterns


def test_obligation_filter_decides_which_changed_sources_retain_failures():
    """Relatedness must be judged against the sources the plugin obligates.

    The plugin exempts changed sources from `pitest.targets` by rules UTA
    cannot see, and UTA never generates for, mutates, or repairs an exempt
    source. Partitioning failures on the unfiltered diff therefore blamed the
    run for code nothing was ever asked to cover: on mmc_horae_core one exempt
    shared helper (`WConfigService`) retained six failing classes that merely
    referenced it, and no repair could ever clear them.
    """
    from uta.language.java.enforcement_runner import _obligated_changed_production_files

    unfiltered = [
        "biz/src/main/java/com/demo/QaKnowledgeService.java",
        "common/src/main/java/com/demo/WConfigService.java",
    ]
    obligated = ["biz/src/main/java/com/demo/QaKnowledgeService.java"]

    assert _obligated_changed_production_files(
        {"filteredChangedProductionFiles": obligated}, unfiltered
    ) == obligated
    # An empty filter is an answer: nothing changed carries an obligation, so
    # no failing test is this diff's responsibility.
    assert _obligated_changed_production_files(
        {"filteredChangedProductionFiles": []}, unfiltered
    ) == []
    # Only a missing key falls back -- the pre-plugin evidence shape.
    assert _obligated_changed_production_files({}, unfiltered) == unfiltered
    assert _obligated_changed_production_files({}, None) is None


def test_a_failing_test_naming_only_an_exempt_changed_class_is_excluded(tmp_path):
    """The end-to-end shape of the mmc_horae_core failure, at the partitioner."""
    repo = _repo(tmp_path)
    from uta.language.java.enforcement_runner import _obligated_changed_production_files

    changed_java = [
        "service/src/main/java/com/demo/service/OrderService.java",
        "service/src/main/java/com/demo/service/ExemptHelper.java",
    ]
    evidence = {
        # The plugin obligated only OrderService.
        "filteredChangedProductionFiles": [
            "service/src/main/java/com/demo/service/OrderService.java"
        ]
    }

    partition = partition_failing_tests(
        ["com.demo.service.ExemptHelperTest"],
        repo_path=repo,
        changed_java=changed_java,
        changed_production_java=_obligated_changed_production_files(evidence, changed_java),
    )

    assert partition.retained == ()
    assert partition.excluded == ("com.demo.service.ExemptHelperTest",)


from uta.shared.targets import TargetIdentity
from uta.language.java.verification.runner import JavaRuntimeConfig, JavaVerificationResult, verify_java_target


def test_verify_java_target_passes_with_jacoco_and_pitest_evidence(tmp_path):
    target = TargetIdentity.java_class("com.example.FooService")

    result = verify_java_target(
        tmp_path,
        target,
        coverage_gate=80.0,
        mutation_gate=70.0,
        config=JavaRuntimeConfig(module="biz"),
        jacoco_runner=lambda repo, test, module, timeout: (True, "tests passed"),
        jacoco_finder=lambda repo, module: "target/site/jacoco/jacoco.xml",
        jacoco_parser=lambda xml, class_fqn: {"line": 92.5, "branch": 80.0},
        pitest_runner=lambda repo, class_fqn, test_fqn, module, timeout: (True, "pit passed"),
        pitest_finder=lambda repo, module: "target/pit-reports/1/mutations.xml",
        mutation_stats_computer=lambda xml, class_fqn: {
            "total": 10,
            "killed": 9,
            "survived": 1,
            "no_coverage": 0,
            "score": 90.0,
            "status_counts": {"KILLED": 9, "SURVIVED": 1},
        },
    )

    assert isinstance(result, JavaVerificationResult)
    assert result.status == "passed"
    assert result.tests_pass is True
    assert result.coverage.line_rate == 92.5
    assert result.mutation.rate == 90.0
    fields = result.as_result_fields()
    assert fields["status"] == "PASS"
    assert fields["coverage"] == 92.5
    assert fields["mutation_score"] == 90.0
    assert fields["verification_commands"][0]["name"] == "jacoco"
    assert fields["verification_commands"][1]["name"] == "pitest"


def test_verify_java_target_reports_coverage_gate_failure(tmp_path):
    result = verify_java_target(
        tmp_path,
        TargetIdentity.java_class("com.example.FooService"),
        coverage_gate=95.0,
        mutation_gate=70.0,
        jacoco_runner=lambda repo, test, module, timeout: (True, "tests passed"),
        jacoco_finder=lambda repo, module: "jacoco.xml",
        jacoco_parser=lambda xml, class_fqn: {"line": 82.0},
        pitest_runner=lambda *args: (_ for _ in ()).throw(AssertionError("mutation should not run")),
    )

    assert result.status == "failed"
    assert result.reason_code == "coverage_gate_failed"
    # Coverage failures map to the valid CLASS_TASK_STATUS `FAIL` (there is no
    # COVERAGE_FAIL); only the mutation gate has its own terminal status.
    assert result.as_result_fields()["status"] == "FAIL"


def test_verify_java_target_reports_mutation_gate_failure(tmp_path):
    result = verify_java_target(
        tmp_path,
        TargetIdentity.java_class("com.example.FooService"),
        coverage_gate=80.0,
        mutation_gate=95.0,
        jacoco_runner=lambda repo, test, module, timeout: (True, "tests passed"),
        jacoco_finder=lambda repo, module: "jacoco.xml",
        jacoco_parser=lambda xml, class_fqn: {"line": 100.0},
        pitest_runner=lambda repo, class_fqn, test_fqn, module, timeout: (True, "pit passed"),
        pitest_finder=lambda repo, module: "mutations.xml",
        mutation_stats_computer=lambda xml, class_fqn: {
            "total": 10,
            "killed": 8,
            "survived": 2,
            "no_coverage": 0,
            "score": 80.0,
        },
    )

    assert result.status == "failed"
    assert result.reason_code == "mutation_gate_failed"
    assert result.as_result_fields()["status"] == "MUTATION_FAIL"


def test_verify_java_target_defaults_test_names_from_class_fqn(tmp_path):
    captured = {}

    def fake_jacoco(repo, test, module, timeout):
        captured["test_selector"] = test
        return True, "tests passed"

    def fake_pitest(repo, class_fqn, test_fqn, module, timeout):
        captured["class_fqn"] = class_fqn
        captured["test_fqn"] = test_fqn
        return True, "pit passed"

    verify_java_target(
        tmp_path,
        TargetIdentity.java_class("com.example.FooService"),
        coverage_gate=80.0,
        mutation_gate=70.0,
        jacoco_runner=fake_jacoco,
        jacoco_finder=lambda repo, module: "jacoco.xml",
        jacoco_parser=lambda xml, class_fqn: {"line": 100.0},
        pitest_runner=fake_pitest,
        pitest_finder=lambda repo, module: "mutations.xml",
        mutation_stats_computer=lambda xml, class_fqn: {"total": 1, "killed": 1, "survived": 0, "score": 100.0},
    )

    assert captured == {
        "test_selector": "FooServiceTest",
        "class_fqn": "com.example.FooService",
        "test_fqn": "com.example.FooServiceTest",
    }


def test_verify_java_target_records_target_scoped_command_evidence(tmp_path):
    captured = {}

    def fake_jacoco(repo, test, module, timeout):
        captured["jacoco"] = (repo, test, module, timeout)
        return True, "tests passed"

    def fake_pitest(repo, class_fqn, test_fqn, module, timeout):
        captured["pitest"] = (repo, class_fqn, test_fqn, module, timeout)
        return True, "pit passed"

    result = verify_java_target(
        tmp_path,
        TargetIdentity.java_class("com.example.FooService"),
        test_class_fqn="com.example.FooServiceFocusedTest",
        test_selector="FooServiceFocusedTest#coversDiff",
        coverage_gate=80.0,
        mutation_gate=70.0,
        config=JavaRuntimeConfig(module="biz", timeout_seconds=123, pitest_timeout_seconds=456),
        jacoco_runner=fake_jacoco,
        jacoco_finder=lambda repo, module: "jacoco.xml",
        jacoco_parser=lambda xml, class_fqn: {"line": 100.0},
        pitest_runner=fake_pitest,
        pitest_finder=lambda repo, module: "mutations.xml",
        mutation_stats_computer=lambda xml, class_fqn: {"total": 1, "killed": 1, "survived": 0, "score": 100.0},
    )

    fields = result.as_result_fields()
    assert captured["jacoco"] == (str(tmp_path), "FooServiceFocusedTest#coversDiff", "biz", 123)
    assert captured["pitest"] == (
        str(tmp_path),
        "com.example.FooService",
        "com.example.FooServiceFocusedTest",
        "biz",
        456,
    )
    assert fields["verification_commands"][0]["command"] == [
        "mvn",
        "jacoco:test",
        "-Dtest=FooServiceFocusedTest#coversDiff",
    ]
    assert fields["verification_commands"][1]["command"] == [
        "mvn",
        "pitest",
        "-DtargetClasses=com.example.FooService",
        "-DtargetTests=com.example.FooServiceFocusedTest",
    ]


def test_verify_java_target_classifies_jacoco_timeout_as_test_failure(tmp_path):
    result = verify_java_target(
        tmp_path,
        TargetIdentity.java_class("com.example.FooService"),
        jacoco_runner=lambda repo, test, module, timeout: (False, "Jacoco test run timed out"),
    )

    assert result.status == "failed"
    assert result.reason_code == "test_failed"
    assert result.message == "Java test/Jacoco run failed"
    assert result.commands[0].exit_code == 1
    assert "timed out" in result.commands[0].stderr


def test_verify_java_target_reports_missing_coverage_evidence(tmp_path):
    result = verify_java_target(
        tmp_path,
        TargetIdentity.java_class("com.example.FooService"),
        jacoco_runner=lambda repo, test, module, timeout: (True, "tests passed"),
        jacoco_finder=lambda repo, module: None,
    )

    assert result.status == "failed"
    assert result.reason_code == "missing_coverage_report"
    assert result.as_result_fields()["status"] == "FAIL"


def test_verify_java_target_classifies_pitest_timeout_as_mutation_command_failure(tmp_path):
    result = verify_java_target(
        tmp_path,
        TargetIdentity.java_class("com.example.FooService"),
        coverage_gate=80.0,
        mutation_gate=70.0,
        jacoco_runner=lambda repo, test, module, timeout: (True, "tests passed"),
        jacoco_finder=lambda repo, module: "jacoco.xml",
        jacoco_parser=lambda xml, class_fqn: {"line": 100.0},
        pitest_runner=lambda repo, class_fqn, test_fqn, module, timeout: (False, "Pitest timed out"),
    )

    assert result.status == "failed"
    assert result.reason_code == "mutation_command_failed"
    assert result.as_result_fields()["status"] == "MUTATION_FAIL"
    assert "timed out" in result.message


def test_verify_java_target_reports_missing_mutation_evidence(tmp_path):
    result = verify_java_target(
        tmp_path,
        TargetIdentity.java_class("com.example.FooService"),
        coverage_gate=80.0,
        mutation_gate=70.0,
        jacoco_runner=lambda repo, test, module, timeout: (True, "tests passed"),
        jacoco_finder=lambda repo, module: "jacoco.xml",
        jacoco_parser=lambda xml, class_fqn: {"line": 100.0},
        pitest_runner=lambda repo, class_fqn, test_fqn, module, timeout: (True, "pit passed"),
        pitest_finder=lambda repo, module: None,
    )

    assert result.status == "failed"
    assert result.reason_code == "missing_mutation_report"
    assert result.as_result_fields()["status"] == "MUTATION_FAIL"

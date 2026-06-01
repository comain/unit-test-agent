from pathlib import Path

from uta.tasks.targets import TargetIdentity
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
    assert result.as_result_fields()["status"] == "COVERAGE_FAIL"


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

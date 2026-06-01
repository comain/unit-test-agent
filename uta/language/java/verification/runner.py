from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from uta.maven.jacoco import find_jacoco_report, parse_jacoco_report, run_test_with_jacoco
from uta.maven.pitest import compute_mutation_stats, find_latest_pitest_report, run_pitest
from uta.tasks.targets import TargetRef


@dataclass(frozen=True)
class CommandEvidence:
    name: str
    command: List[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass(frozen=True)
class JavaCoverageSummary:
    line_rate: float
    gate: float
    passed: bool
    xml_path: str = ""
    counters: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "line_rate": self.line_rate,
            "gate": self.gate,
            "passed": self.passed,
            "xml_path": self.xml_path,
            "counters": dict(self.counters),
        }


@dataclass(frozen=True)
class JavaMutationSummary:
    generated: int
    killed: int
    survived: int
    no_coverage: int
    rate: float
    gate: float
    passed: bool
    report_path: str = ""
    status_counts: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "generated": self.generated,
            "killed": self.killed,
            "survived": self.survived,
            "no_coverage": self.no_coverage,
            "rate": self.rate,
            "gate": self.gate,
            "passed": self.passed,
            "report_path": self.report_path,
            "status_counts": dict(self.status_counts),
        }


@dataclass(frozen=True)
class JavaRuntimeConfig:
    module: Optional[str] = None
    timeout_seconds: int = 900
    pitest_timeout_seconds: int = 600


@dataclass(frozen=True)
class JavaVerificationResult:
    status: str
    reason_code: str
    tests_pass: bool = False
    coverage: Optional[JavaCoverageSummary] = None
    mutation: Optional[JavaMutationSummary] = None
    commands: List[CommandEvidence] = field(default_factory=list)
    message: str = ""

    def as_result_fields(self) -> Dict[str, Any]:
        mutation_score = self.mutation.rate if self.mutation else 0.0
        coverage_rate = self.coverage.line_rate if self.coverage else 0.0
        return {
            "status": _task_status_for_verification(self),
            "coverage": coverage_rate,
            "tests_pass": self.tests_pass,
            "mutation_score": mutation_score,
            "surviving_mutants": self.mutation.survived if self.mutation else 0,
            "total_mutants": self.mutation.generated if self.mutation else 0,
            "killed_mutants": self.mutation.killed if self.mutation else 0,
            "no_coverage_mutants": self.mutation.no_coverage if self.mutation else 0,
            "verification_status": self.status,
            "verification_reason": self.reason_code,
            "verification_message": self.message,
            "verification_commands": [command.as_dict() for command in self.commands],
            "coverage_summary": self.coverage.as_dict() if self.coverage else None,
            "mutation_summary": self.mutation.as_dict() if self.mutation else None,
        }


JacocoRunner = Callable[[str, str, Optional[str], int], Tuple[bool, str]]
JacocoFinder = Callable[[str, Optional[str]], Optional[str]]
JacocoParser = Callable[[str, str], Dict[str, float]]
PitestRunner = Callable[[str, str, str, Optional[str], int], Tuple[bool, str]]
PitestFinder = Callable[[str, Optional[str]], Optional[str]]
MutationStatsComputer = Callable[[str, str], Dict[str, Any]]


def verify_java_target(
    repo_path: Path,
    target: TargetRef,
    *,
    test_class_fqn: Optional[str] = None,
    test_selector: Optional[str] = None,
    coverage_gate: float = 80.0,
    mutation_gate: float = 70.0,
    config: Optional[JavaRuntimeConfig] = None,
    jacoco_runner: Optional[JacocoRunner] = None,
    jacoco_finder: Optional[JacocoFinder] = None,
    jacoco_parser: Optional[JacocoParser] = None,
    pitest_runner: Optional[PitestRunner] = None,
    pitest_finder: Optional[PitestFinder] = None,
    mutation_stats_computer: Optional[MutationStatsComputer] = None,
) -> JavaVerificationResult:
    config = config or JavaRuntimeConfig()
    repo = Path(repo_path)
    class_fqn = target.target_id
    test_class_fqn = test_class_fqn or _default_test_class_fqn(class_fqn)
    test_selector = test_selector or test_class_fqn.rsplit(".", 1)[-1]
    commands: List[CommandEvidence] = []

    run_jacoco = jacoco_runner or run_test_with_jacoco
    ok, output = run_jacoco(str(repo), test_selector, config.module, int(config.timeout_seconds))
    commands.append(
        CommandEvidence(
            name="jacoco",
            command=["mvn", "jacoco:test", f"-Dtest={test_selector}"],
            exit_code=0 if ok else 1,
            stdout=output if ok else "",
            stderr="" if ok else output,
        )
    )
    if not ok:
        return _failed("test_failed", "Java test/Jacoco run failed", tests_pass=False, commands=commands)

    find_report = jacoco_finder or find_jacoco_report
    parse_report = jacoco_parser or parse_jacoco_report
    jacoco_xml = find_report(str(repo), config.module)
    if not jacoco_xml:
        return _failed("missing_coverage_report", "Java Jacoco XML report was not found", tests_pass=True, commands=commands)

    counters = parse_report(jacoco_xml, class_fqn)
    line_rate = float(counters.get("line", 0.0) or 0.0)
    coverage = JavaCoverageSummary(
        line_rate=line_rate,
        gate=float(coverage_gate),
        passed=line_rate >= float(coverage_gate),
        xml_path=str(jacoco_xml),
        counters=dict(counters),
    )
    if not coverage.passed:
        return JavaVerificationResult(
            status="failed",
            reason_code="coverage_gate_failed",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message=f"Java coverage {line_rate:.2f}% is below gate {float(coverage_gate):.2f}%",
        )

    if float(mutation_gate or 0.0) <= 0:
        return JavaVerificationResult(
            status="passed",
            reason_code="passed",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message="Java tests and coverage gate passed; mutation gate disabled",
        )

    run_mutation = pitest_runner or run_pitest
    pit_ok, pit_output = run_mutation(
        str(repo),
        class_fqn,
        test_class_fqn,
        config.module,
        int(config.pitest_timeout_seconds),
    )
    commands.append(
        CommandEvidence(
            name="pitest",
            command=["mvn", "pitest", f"-DtargetClasses={class_fqn}", f"-DtargetTests={test_class_fqn}"],
            exit_code=0 if pit_ok else 1,
            stdout=pit_output if pit_ok else "",
            stderr="" if pit_ok else pit_output,
        )
    )
    if not pit_ok:
        return JavaVerificationResult(
            status="failed",
            reason_code="mutation_command_failed",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message=pit_output or "Java PIT mutation command failed",
        )

    find_pitest = pitest_finder or find_latest_pitest_report
    compute_stats = mutation_stats_computer or compute_mutation_stats
    pit_report = find_pitest(str(repo), config.module)
    if not pit_report:
        return JavaVerificationResult(
            status="failed",
            reason_code="missing_mutation_report",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message="Java PIT mutations.xml report was not found",
        )

    stats = compute_stats(pit_report, class_fqn)
    mutation = JavaMutationSummary(
        generated=int(stats.get("total", 0) or 0),
        killed=int(stats.get("killed", 0) or 0),
        survived=int(stats.get("survived", 0) or 0),
        no_coverage=int(stats.get("no_coverage", 0) or 0),
        rate=float(stats.get("score", 0.0) or 0.0),
        gate=float(mutation_gate),
        passed=float(stats.get("score", 0.0) or 0.0) >= float(mutation_gate),
        report_path=str(pit_report),
        status_counts=dict(stats.get("status_counts") or {}),
    )
    if not mutation.passed:
        return JavaVerificationResult(
            status="failed",
            reason_code="mutation_gate_failed",
            tests_pass=True,
            coverage=coverage,
            mutation=mutation,
            commands=commands,
            message=f"Java mutation score {mutation.rate:.2f}% is below gate {float(mutation_gate):.2f}%",
        )

    return JavaVerificationResult(
        status="passed",
        reason_code="passed",
        tests_pass=True,
        coverage=coverage,
        mutation=mutation,
        commands=commands,
        message="Java tests, coverage, and mutation gates passed",
    )


def _default_test_class_fqn(class_fqn: str) -> str:
    package = ".".join(class_fqn.split(".")[:-1])
    simple = f"{class_fqn.split('.')[-1]}Test"
    return f"{package}.{simple}" if package else simple


def _failed(
    reason_code: str,
    message: str,
    *,
    tests_pass: bool,
    commands: List[CommandEvidence],
) -> JavaVerificationResult:
    return JavaVerificationResult(
        status="failed",
        reason_code=reason_code,
        tests_pass=tests_pass,
        commands=commands,
        message=message,
    )


def _task_status_for_verification(result: JavaVerificationResult) -> str:
    if result.status == "passed":
        return "PASS"
    if result.reason_code == "coverage_gate_failed":
        return "COVERAGE_FAIL"
    if result.reason_code in {"mutation_gate_failed", "mutation_command_failed", "missing_mutation_report"}:
        return "MUTATION_FAIL"
    if result.reason_code in {"test_failed", "missing_coverage_report"}:
        return "FAIL"
    return "FAIL"

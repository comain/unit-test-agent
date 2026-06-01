from __future__ import annotations

import shlex
import subprocess
from enum import Enum
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from pydantic import BaseModel

from uta.language.java.maven_project import test_enforcement_tooling_status, with_default_profile_args
from uta.language.python.enforcement import validate_python_enforcement_evidence


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
TEST_ENFORCEMENT_USAGE_GUIDE = str(
    Path(__file__).resolve().parents[2] / "docs" / "test-enforce-usage.md"
)
MISSING_EVIDENCE_SUMMARY = (
    "Missing test-enforcement plugin/profile: Maven completed but no "
    "coverage/mutation evidence was produced. Required Maven plugin: "
    "resolved test-enforcer >= 1.0.12. Configure a shared parent/profile or "
    "a project-local test-enforcement profile that binds coverage and mutation checks."
)


class EnforcementResultStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    timeout = "timeout"
    command_error = "command_error"
    missing_evidence = "missing_evidence"
    skipped = "skipped"


class EnforcementResult(BaseModel):
    status: EnforcementResultStatus
    passed: bool
    command: List[str]
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    summary: str = ""
    usage_guide: str = TEST_ENFORCEMENT_USAGE_GUIDE
    language: str = "java"
    backend: str = "maven_enforcer"
    evidence: Optional[Dict[str, Any]] = None


class MavenEnforcementRunner:
    def __init__(
        self,
        command: str,
        timeout_seconds: int = 1800,
        run_command: Optional[RunCommand] = None,
        base_ref: str = "origin/master",
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.run_command = run_command or subprocess.run
        self.base_ref = base_ref

    def run(self, repo_path: Path) -> EnforcementResult:
        cmd = shlex.split(self.command)
        self._validate_command(cmd)
        changed_java = self._changed_java_files(repo_path)
        changed_production_java = self._changed_production_java_files(changed_java)
        if changed_production_java == []:
            return EnforcementResult(
                status=EnforcementResultStatus.passed,
                passed=True,
                command=cmd,
                stdout="No changed production Java files under origin/master...HEAD.",
                summary="test-enforcement passed; no changed production Java files",
            )
        if changed_production_java is not None:
            target_tests = self._pitest_target_tests(repo_path, changed_java, changed_production_java)
            if not target_tests:
                tooling = test_enforcement_tooling_status(
                    repo_path,
                    maven_bin=cmd[0],
                    run_maven_command=self._run_command,
                    profile_source_cmd=cmd,
                )
                if not tooling.available:
                    return EnforcementResult(
                        status=EnforcementResultStatus.missing_evidence,
                        passed=False,
                        command=cmd,
                        summary=MISSING_EVIDENCE_SUMMARY,
                        evidence={
                            "coverage": {"covered": 0, "total": 0, "rate": 0.0, "passed": False},
                            "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
                            "tooling": {
                                "available": False,
                                "artifactId": tooling.artifact_id,
                                "version": tooling.version,
                                "reason": tooling.reason,
                            },
                        },
                    )
                return EnforcementResult(
                    status=EnforcementResultStatus.missing_evidence,
                    passed=False,
                    command=cmd,
                    summary=(
                        "test-enforcement cannot run PIT safely because no related "
                        "targetTests were found for changed production Java files"
                    ),
                    evidence={
                        "coverage": {"covered": 0, "total": 0, "rate": 0.0, "passed": False},
                        "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
                        "tooling": {
                            "available": True,
                            "artifactId": tooling.artifact_id,
                            "version": tooling.version,
                            "reason": tooling.reason,
                        },
                    },
                )
            cmd = self._with_target_tests(cmd, target_tests)
        cmd = with_default_profile_args(cmd, repo_path)
        try:
            completed = self._run_command(cmd, repo_path)
        except subprocess.TimeoutExpired as exc:
            return EnforcementResult(
                status=EnforcementResultStatus.timeout,
                passed=False,
                command=cmd,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                summary="test-enforcement timed out",
            )
        except OSError as exc:
            return EnforcementResult(
                status=EnforcementResultStatus.command_error,
                passed=False,
                command=cmd,
                stderr=str(exc),
                summary="test-enforcement command failed to start",
            )

        return self._classify_completed(cmd, completed, repo_path)

    def _run_command(self, cmd: List[str], repo_path: Path) -> subprocess.CompletedProcess[str]:
        return self.run_command(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
        )

    def _classify_completed(
        self,
        cmd: List[str],
        completed: subprocess.CompletedProcess[str],
        repo_path: Path,
    ) -> EnforcementResult:
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        output = f"{stdout}\n{stderr}"
        pitest_baseline_failed = self._looks_pitest_baseline_failure(output)
        if self._looks_skipped(output):
            return EnforcementResult(
                status=EnforcementResultStatus.skipped,
                passed=False,
                command=cmd,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                summary="test-enforcement was skipped",
            )
        if self._looks_gate_failed(output):
            return EnforcementResult(
                status=EnforcementResultStatus.failed,
                passed=False,
                command=cmd,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                summary="test-enforcement failed",
            )
        if self._looks_build_broken(output):
            return EnforcementResult(
                status=EnforcementResultStatus.failed,
                passed=False,
                command=cmd,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                summary="test-enforcement failed because Maven build did not compile or resolve",
            )
        if pitest_baseline_failed:
            return EnforcementResult(
                status=EnforcementResultStatus.failed,
                passed=False,
                command=cmd,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                summary="test-enforcement failed because PIT baseline tests were not green",
            )
        if self._has_required_evidence(output):
            summary = "test-enforcement passed"
            if completed.returncode != 0:
                summary = "test-enforcement passed; Maven exited non-zero after gate evidence"
            return EnforcementResult(
                status=EnforcementResultStatus.passed,
                passed=True,
                command=cmd,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                summary=summary,
            )
        if completed.returncode != 0:
            return EnforcementResult(
                status=EnforcementResultStatus.failed,
                passed=False,
                command=cmd,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                summary="test-enforcement failed before evidence was produced",
            )
        return EnforcementResult(
            status=EnforcementResultStatus.missing_evidence,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary=MISSING_EVIDENCE_SUMMARY,
        )

    @staticmethod
    def _validate_command(cmd: List[str]) -> None:
        normalized = " ".join(cmd).strip()
        if normalized == "mvn test":
            raise ValueError(
                "Configured command must run test-enforcement, not plain mvn test. "
                f"See {TEST_ENFORCEMENT_USAGE_GUIDE}"
            )
        has_enforcement_flag = any("test.enforcement.enabled=true" in item for item in cmd)
        has_enforcement_goal = any("test-enforcement" in item or "test.enforcer" in item for item in cmd)
        if not has_enforcement_flag and not has_enforcement_goal:
            raise ValueError(
                "Configured command must run test-enforcement. "
                f"See {TEST_ENFORCEMENT_USAGE_GUIDE}"
            )

    @staticmethod
    def _has_required_evidence(output: str) -> bool:
        has_coverage = bool(
            re.search(r"\bdiff(?:\s+line)?\s+coverage\s*:?\s+[0-9]+(?:\.[0-9]+)?%", output, flags=re.IGNORECASE)
        )
        has_mutation = bool(
            re.search(r"\bdiff\s+mutation\s+score\s+[0-9]+(?:\.[0-9]+)?%", output, flags=re.IGNORECASE)
            or re.search(r"\bPIT\s+generated=\d+\s+killed=\d+.*?test-strength=", output, flags=re.IGNORECASE)
            or (MavenEnforcementRunner._has_scoped_pitest_targets(output) and MavenEnforcementRunner._has_pitest_summary(output))
            or re.search(r"\bpitest\.targets=0\b", output, flags=re.IGNORECASE)
        )
        return has_coverage and has_mutation

    @staticmethod
    def _has_scoped_pitest_targets(output: str) -> bool:
        for match in re.finditer(r"\bpitest\.targets=(\d+)\s+\[([^\]]*)\]", output, flags=re.IGNORECASE):
            if int(match.group(1)) > 0 and match.group(2).strip():
                return True
        return False

    @staticmethod
    def _has_pitest_summary(output: str) -> bool:
        return bool(
            re.search(r"Generated\s+\d+\s+mutations\s+Killed\s+\d+", output, flags=re.IGNORECASE)
            and re.search(r"Test strength\s+[0-9]+(?:\.[0-9]+)?%", output, flags=re.IGNORECASE)
        )

    @staticmethod
    def _looks_skipped(output: str) -> bool:
        lowered = output.lower()
        return "skip test enforcement" in lowered or "test-enforcement skipped" in lowered

    @staticmethod
    def _looks_gate_failed(output: str) -> bool:
        lowered = output.lower()
        failure_markers = (
            "test-enforcement failed",
            "test enforcement failed",
            "diff coverage failed",
            "diff coverage check failed",
            "coverage gate failed",
            "mutation gate failed",
            "mutation coverage failed",
            "mutation score is below",
            "test-strength failed",
            "test strength failed",
            "test-enforcer check-coverage failed",
            "test-enforcer check-mutation failed",
            "check-coverage failed",
            "check-mutation failed",
        )
        return any(marker in lowered for marker in failure_markers) or (
            "diff line coverage" in lowered and "below required" in lowered
        )

    @staticmethod
    def _looks_pitest_baseline_failure(output: str) -> bool:
        lowered = output.lower()
        return (
            "mutation testing requires a green suite" in lowered
            or "tests failing without mutation" in lowered
            or "tests did not pass without mutation" in lowered
        )

    @staticmethod
    def _with_target_tests(cmd: List[str], target_tests: Sequence[str]) -> List[str]:
        updated = list(cmd)
        if not any(item.startswith("-DtargetTests=") for item in updated):
            updated.append(f"-DtargetTests={','.join(target_tests)}")
        if not any(item.startswith("-Dtest=") for item in updated):
            test_selector = ",".join(MavenEnforcementRunner._surefire_test_selector(target_tests))
            if test_selector:
                updated.append(f"-Dtest={test_selector}")
        if any(item.startswith("-Dtest=") for item in updated) and not any(
            item.startswith("-Dsurefire.failIfNoSpecifiedTests=") for item in updated
        ):
            updated.append("-Dsurefire.failIfNoSpecifiedTests=false")
        return updated

    @staticmethod
    def _surefire_test_selector(target_tests: Sequence[str]) -> List[str]:
        selectors = []
        seen = set()
        for target_test in target_tests:
            selector = target_test.rsplit(".", 1)[-1].strip()
            if selector and selector not in seen:
                selectors.append(selector)
                seen.add(selector)
        return selectors

    @staticmethod
    def _pitest_target_tests(
        repo_path: Path,
        changed_java: Optional[List[str]],
        changed_production_java: List[str],
    ) -> List[str]:
        target_tests = set()
        changed_test_class_names = set()
        for path_text in changed_java or []:
            if "src/test/java/" in path_text and path_text.endswith(".java"):
                fqn = MavenEnforcementRunner._java_fqn_from_path(path_text, "src/test/java/")
                if fqn:
                    target_tests.add(fqn)
                    changed_test_class_names.add(fqn.rsplit(".", 1)[-1])

        for path_text in changed_production_java:
            simple_name = Path(path_text).stem
            if any(simple_name in test_name for test_name in changed_test_class_names):
                continue
            for test_path in repo_path.glob(f"**/src/test/java/**/*{simple_name}*Test.java"):
                fqn = MavenEnforcementRunner._java_fqn_from_path(
                    test_path.relative_to(repo_path).as_posix(),
                    "src/test/java/",
                )
                if fqn:
                    target_tests.add(fqn)
        return sorted(target_tests)

    @staticmethod
    def _java_fqn_from_path(path_text: str, source_root: str) -> Optional[str]:
        if source_root not in path_text or not path_text.endswith(".java"):
            return None
        package_path = path_text.split(source_root, 1)[1][:-5]
        return package_path.replace("/", ".").strip(".")

    @staticmethod
    def _looks_build_broken(output: str) -> bool:
        lowered = output.lower()
        build_break_markers = (
            "dependencyresolutionexception",
            "could not resolve dependencies",
            "could not find artifact",
            "failed to collect dependencies",
            "compilation failure",
            "compilation error",
            "fatal error compiling",
            "maven-compiler-plugin",
            "cannot find symbol",
        )
        return any(marker in lowered for marker in build_break_markers) or (
            "package " in lowered and " does not exist" in lowered
        )

    def _changed_java_files(self, repo_path: Path) -> Optional[List[str]]:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--name-only", f"{self.base_ref}...HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return [
            item.strip()
            for item in completed.stdout.splitlines()
            if item.strip().endswith(".java")
        ]

    @staticmethod
    def _changed_production_java_files(changed_java: Optional[List[str]]) -> Optional[List[str]]:
        if changed_java is None:
            return None
        return [item for item in changed_java if "src/main/java/" in item]


class PythonEnforcementRunner:
    def __init__(
        self,
        command: str,
        timeout_seconds: int = 1800,
        run_command: Optional[RunCommand] = None,
        base_ref: str = "origin/master",
        coverage_gate: float = 95.0,
        mutation_gate: float = 100.0,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.run_command = run_command or subprocess.run
        self.base_ref = base_ref
        self.coverage_gate = float(coverage_gate)
        self.mutation_gate = float(mutation_gate)

    def run(self, repo_path: Path) -> EnforcementResult:
        cmd = self._build_command()
        try:
            completed = self.run_command(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return EnforcementResult(
                status=EnforcementResultStatus.timeout,
                passed=False,
                command=cmd,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                summary="Python enforcement timed out",
                language="python",
                backend="python_enforcer",
            )
        except OSError as exc:
            return EnforcementResult(
                status=EnforcementResultStatus.command_error,
                passed=False,
                command=cmd,
                stderr=str(exc),
                summary="Python enforcement command failed to start",
                language="python",
                backend="python_enforcer",
            )
        return self._classify_completed(cmd, completed, Path(repo_path))

    def _build_command(self) -> List[str]:
        cmd = shlex.split(self.command)
        if not cmd:
            raise ValueError("Python enforcement command must not be empty")
        normalized = " ".join(cmd)
        if "python-enforce" not in normalized:
            raise ValueError("Configured command must run UTA python-enforce")
        if not _has_cli_option(cmd, "--repo"):
            cmd.extend(["--repo", "."])
        if not _has_cli_option(cmd, "--base-ref"):
            cmd.extend(["--base-ref", self.base_ref])
        if not _has_cli_option(cmd, "--coverage-gate"):
            cmd.extend(["--coverage-gate", str(self.coverage_gate)])
        if not _has_cli_option(cmd, "--mutation-gate"):
            cmd.extend(["--mutation-gate", str(self.mutation_gate)])
        if not _has_cli_option(cmd, "--json-output"):
            cmd.append("--json-output")
        return cmd

    def _classify_completed(
        self,
        cmd: List[str],
        completed: subprocess.CompletedProcess[str],
        repo_path: Path,
    ) -> EnforcementResult:
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        evidence = self._extract_evidence(stdout, stderr)
        if evidence is None:
            status = EnforcementResultStatus.failed if completed.returncode else EnforcementResultStatus.missing_evidence
            return EnforcementResult(
                status=status,
                passed=False,
                command=cmd,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                summary="Python enforcement did not produce UTA evidence",
                language="python",
                backend="python_enforcer",
            )

        expected_head = self._git_head(repo_path)
        if not expected_head:
            return EnforcementResult(
                status=EnforcementResultStatus.command_error,
                passed=False,
                command=cmd,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                summary="Python enforcement could not resolve workspace HEAD for evidence validation",
                language="python",
                backend="python_enforcer",
                evidence=dict(evidence),
            )
        verdict = validate_python_enforcement_evidence(evidence, expected_head=expected_head)
        evidence_status = str(evidence.get("status") or "")
        if verdict.passed:
            return EnforcementResult(
                status=EnforcementResultStatus.passed,
                passed=True,
                command=cmd,
                returncode=completed.returncode,
                stdout=stdout,
                stderr=stderr,
                summary=verdict.message or "Python enforcement passed",
                language="python",
                backend="python_enforcer",
                evidence=dict(evidence),
            )
        status = EnforcementResultStatus.missing_evidence if evidence_status == "missing_evidence" else EnforcementResultStatus.failed
        return EnforcementResult(
            status=status,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary=f"Python enforcement failed: {verdict.reason_code}: {verdict.message}",
            language="python",
            backend="python_enforcer",
            evidence=dict(evidence),
        )

    @staticmethod
    def _extract_evidence(stdout: str, stderr: str) -> Optional[Dict[str, Any]]:
        output = f"{stdout or ''}\n{stderr or ''}"
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("UTA_PYTHON_ENFORCEMENT_EVIDENCE="):
                payload = stripped.split("=", 1)[1]
                return _json_object(payload)
        return _json_object((stdout or "").strip())

    @staticmethod
    def _git_head(repo_path: Path) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""


def _json_object(payload: str) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _has_cli_option(cmd: Sequence[str], option: str) -> bool:
    return any(arg == option or arg.startswith(f"{option}=") for arg in cmd)

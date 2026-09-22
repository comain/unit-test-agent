"""Python enforcement gate runner."""

from __future__ import annotations

import configparser
import copy
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from uta.enforcement.enforcement import (
    QualityGateResult,
    QualityGateStatus,
    RunCommand,
    has_cli_option,
    json_object,
    run_bounded_command,
    run_resource_bounded_command,
)
from uta.language.python.enforcement import (
    CI_REPORT_ENFORCEMENT_PROFILE,
    INTERNAL_ENFORCEMENT_PROFILE_ENV,
    run_python_enforcement,
    validate_python_enforcement_evidence,
)
from uta.language.python.workspace import restore_python_verifier_overlays


PythonEnforcer = Callable[..., Dict[str, Any]]


class PythonEnforcementRunner:
    def __init__(
        self,
        command: str,
        timeout_seconds: int = 1800,
        run_command: Optional[RunCommand] = None,
        base_ref: str = "origin/master",
        coverage_gate: float = 95.0,
        mutation_gate: float = 100.0,
        use_in_process_adapter: bool = False,
        in_process_enforcer: Optional[PythonEnforcer] = None,
        memory_limit_mb: int = 3072,
        runtime_overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.run_command = run_command or subprocess.run
        self.base_ref = base_ref
        self.coverage_gate = float(coverage_gate)
        self.mutation_gate = float(mutation_gate)
        self.use_in_process_adapter = bool(use_in_process_adapter)
        self.in_process_enforcer = in_process_enforcer or run_python_enforcement
        self.memory_limit_bytes = max(1, int(memory_limit_mb)) * 1024 * 1024
        self.runtime_overrides = dict(runtime_overrides or {})

    def with_runtime_overrides(self, overrides: Dict[str, Any]) -> "PythonEnforcementRunner":
        """Clone this runner with one app's trusted runtime recipe."""
        selected = copy.copy(self)
        selected.runtime_overrides = dict(overrides)
        return selected

    def run(self, repo_path: Path) -> QualityGateResult:
        return self._run(repo_path, enable_ci_mutation_sampling=True)

    def run_full(self, repo_path: Path) -> QualityGateResult:
        """Run the authoritative full-cap profile used to derive repair targets."""
        return self._run(repo_path, enable_ci_mutation_sampling=False)

    def _run(self, repo_path: Path, *, enable_ci_mutation_sampling: bool) -> QualityGateResult:
        repo = Path(repo_path)
        cmd = self._build_command(repo)
        setup_failure = self._prepare_managed_runtime(repo, cmd)
        if setup_failure is not None:
            return setup_failure
        if self.use_in_process_adapter:
            return self._run_in_process(
                repo,
                cmd,
                enable_ci_mutation_sampling=enable_ci_mutation_sampling,
            )
        with tempfile.TemporaryDirectory(prefix="uta-python-evidence-") as temp_dir:
            evidence_path = self._configure_evidence_output(cmd, repo, Path(temp_dir))
            try:
                if self.run_command is subprocess.run:
                    completed = run_resource_bounded_command(
                        cmd,
                        cwd=repo,
                        timeout=self.timeout_seconds,
                        env=self._child_env(enable_ci_mutation_sampling=enable_ci_mutation_sampling),
                        memory_limit_bytes=self.memory_limit_bytes,
                    )
                else:
                    completed = run_bounded_command(
                        self.run_command,
                        cmd,
                        cwd=repo,
                        timeout=self.timeout_seconds,
                        env=self._child_env(enable_ci_mutation_sampling=enable_ci_mutation_sampling),
                    )
            except subprocess.TimeoutExpired as exc:
                return QualityGateResult(
                    status=QualityGateStatus.timeout,
                    passed=False,
                    command=cmd,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
                    summary="Python enforcement timed out",
                    language="python",
                    backend="python_enforcer",
                )
            except OSError as exc:
                return QualityGateResult(
                    status=QualityGateStatus.command_error,
                    passed=False,
                    command=cmd,
                    stderr=str(exc),
                    summary="Python enforcement command failed to start",
                    language="python",
                    backend="python_enforcer",
                )
            finally:
                self._cleanup_verifier_overlays(repo)
            if "UTA_RESOURCE_EXHAUSTED" in str(completed.stderr or ""):
                return self._resource_exhausted_result(cmd, completed, repo)
            evidence = self._read_evidence_output(evidence_path)
            return self._classify_completed(cmd, completed, repo, evidence=evidence)

    def _prepare_managed_runtime(
        self,
        repo: Path,
        enforcement_command: List[str],
    ) -> Optional[QualityGateResult]:
        setup_command = self.runtime_overrides.get("setup_command")
        if not setup_command:
            return None
        try:
            completed = run_bounded_command(
                self.run_command,
                list(setup_command),
                cwd=repo,
                timeout=self.timeout_seconds,
                env=self._child_env(enable_ci_mutation_sampling=False),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return QualityGateResult(
                status=QualityGateStatus.command_error,
                passed=False,
                command=enforcement_command,
                stderr=str(exc),
                summary="UTA-managed Python environment setup failed",
                language="python",
                backend="python_enforcer",
            )
        if completed.returncode == 0:
            return None
        return QualityGateResult(
            status=QualityGateStatus.command_error,
            passed=False,
            command=enforcement_command,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            summary="UTA-managed Python environment setup failed",
            language="python",
            backend="python_enforcer",
        )

    @staticmethod
    def _configure_evidence_output(cmd: List[str], repo: Path, temp_dir: Path) -> Path:
        configured = PythonEnforcementRunner._first_cli_option_value(cmd, "--evidence-output")
        path = Path(configured).expanduser() if configured else temp_dir / "evidence.json"
        if not path.is_absolute():
            path = repo / path
        if not configured:
            cmd.extend(["--evidence-output", str(path)])
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return path

    @staticmethod
    def _read_evidence_output(path: Path) -> Optional[Dict[str, Any]]:
        try:
            return json_object(path.read_text(encoding="utf-8"))
        except OSError:
            return None

    def _resource_exhausted_result(
        self,
        cmd: List[str],
        completed: subprocess.CompletedProcess[str],
        repo: Path,
    ) -> QualityGateResult:
        plans = self._persisted_candidate_plan_summaries(repo)
        limit_gib = self.memory_limit_bytes / (1024 ** 3)
        evidence = {
            "schemaVersion": 1,
            "language": "python",
            "backend": "python_enforcer",
            "status": "failed",
            "passed": False,
            "reasonCode": "mutation_resource_exhausted",
            "summary": f"Python mutation enforcement exceeded its {limit_gib:.2f} GiB process-group memory limit",
            "headCommit": self._git_head(repo),
            "targets": plans,
        }
        return QualityGateResult(
            status=QualityGateStatus.failed,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            summary=evidence["summary"],
            language="python",
            backend="python_enforcer",
            evidence=evidence,
        )

    @staticmethod
    def _persisted_candidate_plan_summaries(repo: Path) -> List[Dict[str, Any]]:
        summaries: List[Dict[str, Any]] = []
        for path in sorted((repo / ".uta_cache" / "python" / "mutation").glob("*.candidate-plan.json")):
            try:
                plan = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            summaries.append(
                {
                    "targetId": plan.get("targetId"),
                    "sourcePath": plan.get("sourcePath"),
                    "eligibleCandidates": len(plan.get("eligibleMutationOpportunities") or []),
                    "selectedCandidates": len(plan.get("activeSelected") or []),
                    "candidatePlanArtifactPath": path.relative_to(repo).as_posix(),
                }
            )
        return summaries

    @staticmethod
    def _cleanup_verifier_overlays(repo_path: Path) -> None:
        restore_python_verifier_overlays(str(repo_path))

    def _build_command(self, repo_path: Path) -> List[str]:
        cmd = shlex.split(self.command)
        if not cmd:
            raise ValueError("Python enforcement command must not be empty")
        normalized = " ".join(cmd)
        if "python-enforce" not in normalized:
            raise ValueError("Configured command must run UTA python-enforce")
        if not has_cli_option(cmd, "--repo"):
            cmd.extend(["--repo", "."])
        if not has_cli_option(cmd, "--base-ref"):
            cmd.extend(["--base-ref", self.base_ref])
        if not has_cli_option(cmd, "--coverage-gate"):
            cmd.extend(["--coverage-gate", str(self.coverage_gate)])
        if not has_cli_option(cmd, "--mutation-gate"):
            cmd.extend(["--mutation-gate", str(self.mutation_gate)])
        if not has_cli_option(cmd, "--test-path"):
            for test_path in self._discover_test_paths(repo_path):
                cmd.extend(["--test-path", test_path])
        if not has_cli_option(cmd, "--json-output"):
            cmd.append("--json-output")
        return cmd

    def _child_env(self, *, enable_ci_mutation_sampling: bool = True) -> Dict[str, str]:
        child_env = dict(os.environ)
        child_env["UTA_PYTHON_GATE_TIMEOUT_SECONDS"] = str(self.timeout_seconds)
        # The subprocess boundary provides the memory/process-group isolation CI
        # needs. Preserve the CI-only mutation profile across that boundary;
        # ordinary CLI and dev-skills invocations intentionally do not set it.
        if enable_ci_mutation_sampling:
            child_env[INTERNAL_ENFORCEMENT_PROFILE_ENV] = CI_REPORT_ENFORCEMENT_PROFILE
        else:
            # The full-cap rerun must not inherit a sampling profile from the
            # parent CI process, or the "authoritative" evidence would be
            # sampled too.
            child_env.pop(INTERNAL_ENFORCEMENT_PROFILE_ENV, None)
        ci_cap = self.runtime_overrides.get("ci_mutation_max_selected")
        if enable_ci_mutation_sampling and ci_cap is not None:
            child_env["UTA_PYTHON_MUTATION_GENERATION_CI_MAX_SELECTED"] = str(ci_cap)
        elif ci_cap is not None:
            child_env.pop("UTA_PYTHON_MUTATION_GENERATION_CI_MAX_SELECTED", None)
        env_names = {
            "python_bin": "UTA_PYTHON_BIN",
            "python2_bin": "UTA_PYTHON2_BIN",
            "mutmut_bin": "UTA_PYTHON_MUTMUT_BIN",
            "python2_mutmut_bin": "UTA_PYTHON2_MUTMUT_BIN",
            "setup_command": "UTA_PYTHON_SETUP_COMMAND",
            "dependency_overlay_enabled": "UTA_PYTHON_DEPENDENCY_OVERLAY_ENABLED",
            "environment_profile": "UTA_PYTHON_ENVIRONMENT_PROFILE",
        }
        for key, env_name in env_names.items():
            value = self.runtime_overrides.get(key)
            if value is None:
                continue
            if key == "setup_command":
                child_env[env_name] = shlex.join(value)
            elif key == "dependency_overlay_enabled":
                child_env[env_name] = "1" if value else "0"
            else:
                child_env[env_name] = str(value)
        return child_env

    def _run_in_process(
        self,
        repo: Path,
        cmd: List[str],
        *,
        enable_ci_mutation_sampling: bool = True,
    ) -> QualityGateResult:
        try:
            evidence = self.in_process_enforcer(
                repo_path=repo,
                target_values=self._cli_option_values(cmd, "--target"),
                test_paths=self._cli_option_values(cmd, "--test-path"),
                base_ref=self._first_cli_option_value(cmd, "--base-ref") or self.base_ref,
                coverage_gate=float(self._first_cli_option_value(cmd, "--coverage-gate") or self.coverage_gate),
                mutation_gate=float(self._first_cli_option_value(cmd, "--mutation-gate") or self.mutation_gate),
                syntax_version=self._first_cli_option_value(cmd, "--syntax-version") or "python3",
                enable_ci_mutation_sampling=enable_ci_mutation_sampling,
                runtime_overrides=self.runtime_overrides,
            )
        except Exception as exc:
            return QualityGateResult(
                status=QualityGateStatus.command_error,
                passed=False,
                command=cmd,
                stderr=str(exc),
                summary="Python in-process enforcement failed to run",
                language="python",
                backend="python_enforcer",
            )
        finally:
            self._cleanup_verifier_overlays(repo)
        completed = subprocess.CompletedProcess(
            cmd,
            0 if evidence.get("passed") else 1,
            stdout=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            stderr="",
        )
        return self._classify_completed(cmd, completed, repo)

    @staticmethod
    def _first_cli_option_value(cmd: List[str], option: str) -> str:
        values = PythonEnforcementRunner._cli_option_values(cmd, option)
        return values[0] if values else ""

    @staticmethod
    def _cli_option_values(cmd: List[str], option: str) -> List[str]:
        values: List[str] = []
        for index, item in enumerate(cmd):
            if item == option and index + 1 < len(cmd):
                values.append(cmd[index + 1])
            elif item.startswith(f"{option}="):
                values.append(item.split("=", 1)[1])
        return values

    @staticmethod
    def _discover_test_paths(repo_path: Path) -> List[str]:
        repo_path = Path(repo_path)
        discovered: List[str] = []
        for candidate in PythonEnforcementRunner._configured_pytest_paths(repo_path):
            PythonEnforcementRunner._append_existing_test_path(repo_path, candidate, discovered)
        for candidate in PythonEnforcementRunner._common_test_dirs(repo_path):
            PythonEnforcementRunner._append_existing_test_path(repo_path, candidate, discovered)
        if discovered:
            return discovered
        for candidate in PythonEnforcementRunner._top_level_test_files(repo_path):
            PythonEnforcementRunner._append_existing_test_path(repo_path, candidate, discovered)
        return discovered

    @staticmethod
    def _configured_pytest_paths(repo_path: Path) -> List[Path]:
        paths: List[Path] = []
        for config_name in ("pytest.ini", "tox.ini", "setup.cfg"):
            config_path = repo_path / config_name
            if not config_path.is_file():
                continue
            parser = configparser.ConfigParser()
            try:
                parser.read(config_path, encoding="utf-8")
            except configparser.Error:
                continue
            for section in ("pytest", "tool:pytest"):
                if parser.has_option(section, "testpaths"):
                    paths.extend(
                        repo_path / item
                        for item in parser.get(section, "testpaths").split()
                        if item.strip()
                    )
        pyproject_path = repo_path / "pyproject.toml"
        if pyproject_path.is_file():
            paths.extend(PythonEnforcementRunner._pyproject_test_paths(repo_path, pyproject_path))
        return paths

    @staticmethod
    def _pyproject_test_paths(repo_path: Path, pyproject_path: Path) -> List[Path]:
        try:
            import tomllib
        except ModuleNotFoundError:
            return []
        try:
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return []
        raw_paths = (
            data.get("tool", {})
            .get("pytest", {})
            .get("ini_options", {})
            .get("testpaths", [])
        )
        if isinstance(raw_paths, str):
            raw_paths = raw_paths.split()
        if not isinstance(raw_paths, list):
            return []
        return [repo_path / str(item) for item in raw_paths if str(item).strip()]

    @staticmethod
    def _common_test_dirs(repo_path: Path) -> List[Path]:
        ignored = {
            ".git",
            ".hg",
            ".svn",
            ".tox",
            ".nox",
            ".venv",
            "venv",
            "env",
            "__pycache__",
            ".uta_cache",
            ".uta_reports",
            "node_modules",
            "site-packages",
        }
        candidates: List[Path] = []
        for path in repo_path.rglob("*"):
            if not path.is_dir():
                continue
            try:
                relative = path.relative_to(repo_path)
            except ValueError:
                continue
            parts = relative.parts
            if not parts or len(parts) > 3 or any(part in ignored or part.startswith(".") for part in parts):
                continue
            if path.name not in {"test", "tests"}:
                continue
            if any(child.is_file() and child.suffix == ".py" for child in path.rglob("*.py")):
                candidates.append(path)
        return sorted(candidates, key=lambda item: (len(item.relative_to(repo_path).parts), item.as_posix()))

    @staticmethod
    def _top_level_test_files(repo_path: Path) -> List[Path]:
        candidates = list(repo_path.glob("test_*.py")) + list(repo_path.glob("*_test.py"))
        return sorted(path for path in candidates if path.is_file())

    @staticmethod
    def _append_existing_test_path(repo_path: Path, candidate: Path, discovered: List[str]) -> None:
        try:
            relative = candidate.resolve().relative_to(repo_path.resolve())
        except (OSError, ValueError):
            return
        if not candidate.exists():
            return
        value = relative.as_posix()
        if value and value not in discovered:
            discovered.append(value)

    def _classify_completed(
        self,
        cmd: List[str],
        completed: subprocess.CompletedProcess[str],
        repo_path: Path,
        *,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> QualityGateResult:
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        evidence = evidence or self._extract_evidence(stdout, stderr)
        if evidence is None:
            status = QualityGateStatus.failed if completed.returncode else QualityGateStatus.missing_evidence
            return QualityGateResult(
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
            return QualityGateResult(
                status=QualityGateStatus.command_error,
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
            return QualityGateResult(
                status=QualityGateStatus.passed,
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
        status = QualityGateStatus.missing_evidence if evidence_status == "missing_evidence" else QualityGateStatus.failed
        return QualityGateResult(
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
                return json_object(payload)
        return json_object((stdout or "").strip())

    @staticmethod
    def _git_head(repo_path: Path) -> str:
        from uta.shared.git import git

        completed = git(timeout=30).run(
            repo_path,
            "rev-parse",
            "HEAD",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""

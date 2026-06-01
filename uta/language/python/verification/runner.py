from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tokenize
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from uta.tasks.targets import TargetRef


RunCommand = Callable[..., subprocess.CompletedProcess]

_COVERAGE_RUNTIME_OMIT = ",".join(
    [
        "mutants/*",
        "*/mutants/*",
        ".uta_cache/*",
        "*/.uta_cache/*",
        ".uta_reports/*",
        "*/.uta_reports/*",
        ".pytest_cache/*",
        "*/.pytest_cache/*",
    ]
)


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
class CoverageSummary:
    covered: int
    total: int
    rate: float
    gate: float
    passed: bool
    xml_path: str
    scope: str = "target_file"
    changed_lines: Dict[str, List[int]] = field(default_factory=dict)
    no_executable_changed_lines: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "covered": self.covered,
            "total": self.total,
            "rate": self.rate,
            "gate": self.gate,
            "passed": self.passed,
            "xml_path": self.xml_path,
            "scope": self.scope,
            "changed_lines": dict(self.changed_lines),
            "no_executable_changed_lines": self.no_executable_changed_lines,
        }


@dataclass(frozen=True)
class MutationSummary:
    runtime_lane: str
    generated: int
    killed: int
    survived: int
    no_coverage: int
    rate: float
    gate: float
    passed: bool
    survivors: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: Dict[str, str] = field(default_factory=dict)
    scope: str = "target_file"
    changed_lines: Dict[str, List[int]] = field(default_factory=dict)
    diff_survivors: List[Dict[str, Any]] = field(default_factory=list)
    changed_line_mutants_generated: int = 0
    changed_line_mutants_killed: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "runtime_lane": self.runtime_lane,
            "generated": self.generated,
            "killed": self.killed,
            "survived": self.survived,
            "no_coverage": self.no_coverage,
            "rate": self.rate,
            "gate": self.gate,
            "passed": self.passed,
            "survivors": list(self.survivors),
            "artifacts": dict(self.artifacts),
            "scope": self.scope,
            "changed_lines": dict(self.changed_lines),
            "diff_survivors": list(self.diff_survivors),
            "changedLineMutantsGenerated": self.changed_line_mutants_generated,
            "changedLineMutantsKilled": self.changed_line_mutants_killed,
        }


@dataclass(frozen=True)
class PythonRuntimeConfig:
    python_bin: str = "python3"
    python2_bin: Optional[str] = None
    mutmut_bin: str = "mutmut"
    python2_mutmut_bin: Optional[str] = None
    setup_command: Sequence[str] = ()
    environment_profile: str = "default"
    timeout_seconds: int = 1800
    artifact_dir: str = ".uta_cache/python"
    dependency_fingerprints: Dict[str, str] = field(default_factory=dict)
    cache_key: str = "python-env:default"
    config_sources: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PythonVerificationResult:
    status: str
    reason_code: str
    tests_pass: bool = False
    coverage: Optional[CoverageSummary] = None
    mutation: Optional[MutationSummary] = None
    commands: List[CommandEvidence] = field(default_factory=list)
    message: str = ""
    setup_status: str = "skipped"
    environment_profile: str = "default"
    dependency_fingerprints: Dict[str, str] = field(default_factory=dict)
    cache_key: str = ""

    def as_result_fields(self) -> Dict[str, Any]:
        mutation_score = self.mutation.rate if self.mutation else 0.0
        coverage_rate = self.coverage.rate if self.coverage else 0.0
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
            "verification_setup": {
                "setup_status": self.setup_status,
                "environment_profile": self.environment_profile,
                "dependency_fingerprints": dict(self.dependency_fingerprints),
                "cache_key": self.cache_key,
            },
            "dependency_fingerprints": dict(self.dependency_fingerprints),
            "verification_cache_key": self.cache_key,
            "verification_commands": [command.as_dict() for command in self.commands],
            "coverage_summary": self.coverage.as_dict() if self.coverage else None,
            "mutation_summary": self.mutation.as_dict() if self.mutation else None,
        }


def resolve_python_runtime_config(
    repo_path: Path,
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> PythonRuntimeConfig:
    repo = Path(repo_path)
    env = os.environ if environ is None else environ
    values: Dict[str, Any] = {
        "python_bin": "python3",
        "python2_bin": None,
        "mutmut_bin": "mutmut",
        "python2_mutmut_bin": None,
        "setup_command": (),
        "environment_profile": "default",
        "timeout_seconds": 1800,
        "artifact_dir": ".uta_cache/python",
    }
    sources = {field_name: "default" for field_name in values}

    for config_path in (".uta/python-enforce.toml",):
        parsed = _read_simple_toml(repo / config_path)
        for key, value in parsed.items():
            if key not in values:
                continue
            values[key] = _coerce_config_value(key, value)
            sources[key] = config_path

    env_map = {
        "python_bin": "UTA_PYTHON_BIN",
        "python2_bin": "UTA_PYTHON2_BIN",
        "mutmut_bin": "UTA_PYTHON_MUTMUT_BIN",
        "python2_mutmut_bin": "UTA_PYTHON2_MUTMUT_BIN",
        "setup_command": "UTA_PYTHON_SETUP_COMMAND",
        "environment_profile": "UTA_PYTHON_ENVIRONMENT_PROFILE",
        "timeout_seconds": "UTA_PYTHON_GATE_TIMEOUT_SECONDS",
        "artifact_dir": "UTA_PYTHON_ARTIFACT_DIR",
    }
    for key, env_name in env_map.items():
        if env_name not in env:
            continue
        values[key] = _coerce_config_value(key, env[env_name])
        sources[key] = f"env:{env_name}"

    for key, value in (overrides or {}).items():
        if key not in values or value is None:
            continue
        values[key] = _coerce_config_value(key, value)
        sources[key] = "cli"

    fingerprints = _dependency_fingerprints(repo)
    cache_key = _python_cache_key(values, fingerprints)
    return PythonRuntimeConfig(
        python_bin=str(values["python_bin"]),
        python2_bin=_optional_str(values["python2_bin"]),
        mutmut_bin=str(values["mutmut_bin"]),
        python2_mutmut_bin=_optional_str(values["python2_mutmut_bin"]),
        setup_command=tuple(values["setup_command"] or ()),
        environment_profile=str(values["environment_profile"] or "default"),
        timeout_seconds=int(values["timeout_seconds"]),
        artifact_dir=str(values["artifact_dir"]),
        dependency_fingerprints=fingerprints,
        cache_key=cache_key,
        config_sources=sources,
    )


def verify_python_target(
    repo_path: Path,
    target: TargetRef,
    *,
    test_paths: Sequence[str],
    syntax_version: str = "python3",
    coverage_gate: float = 80.0,
    mutation_gate: float = 70.0,
    config: Optional[PythonRuntimeConfig] = None,
    run_command: Optional[RunCommand] = None,
    changed_lines: Optional[Mapping[str, Iterable[int]]] = None,
) -> PythonVerificationResult:
    config = config or resolve_python_runtime_config(repo_path)
    runner = run_command or _subprocess_run
    repo = Path(repo_path)
    artifact_dir = repo / config.artifact_dir
    coverage_dir = artifact_dir / "coverage"
    mutation_dir = artifact_dir / "mutation"
    coverage_xml = coverage_dir / "coverage.xml"
    commands: List[CommandEvidence] = []
    setup_status = "skipped"
    lane = _runtime_lane(syntax_version)
    python_bin = _python_bin(config, lane)

    if config.setup_command:
        setup_status = "executed"
        setup = _run_command("setup", list(config.setup_command), repo, config.timeout_seconds, runner)
        commands.append(setup)
        if setup.exit_code != 0:
            return _failed(
                "setup_failed",
                commands,
                f"Python dependency setup failed: {setup.stderr or setup.stdout}",
                config=config,
                setup_status="failed",
            )

    python_version = _run_command("python_version", [python_bin, "--version"], repo, config.timeout_seconds, runner)
    commands.append(python_version)
    if python_version.exit_code != 0:
        reason = "missing_python2_runtime" if lane == "mutmut-legacy-py2" else "missing_python_runtime"
        return _failed(reason, commands, python_version.stderr or python_version.stdout, config=config, setup_status=setup_status)

    pytest_check = _run_command("pytest_version", [python_bin, "-m", "pytest", "--version"], repo, config.timeout_seconds, runner)
    commands.append(pytest_check)
    if pytest_check.exit_code != 0:
        fallback_bin = _python_runtime_fallback_bin(config, lane, python_bin)
        if fallback_bin:
            fallback_version = _run_command("python_version_fallback", [fallback_bin, "--version"], repo, config.timeout_seconds, runner)
            commands.append(fallback_version)
            fallback_pytest = _run_command("pytest_version_fallback", [fallback_bin, "-m", "pytest", "--version"], repo, config.timeout_seconds, runner)
            commands.append(fallback_pytest)
            if fallback_version.exit_code == 0 and fallback_pytest.exit_code == 0:
                python_bin = fallback_bin
            else:
                return _failed("missing_pytest", commands, fallback_pytest.stderr or fallback_pytest.stdout or pytest_check.stderr or pytest_check.stdout, config=config, setup_status=setup_status)
        else:
            return _failed("missing_pytest", commands, pytest_check.stderr or pytest_check.stdout, config=config, setup_status=setup_status)

    coverage_check = _run_command("coverage_version", [python_bin, "-m", "coverage", "--version"], repo, config.timeout_seconds, runner)
    commands.append(coverage_check)
    if coverage_check.exit_code != 0:
        fallback_bin = _python_runtime_fallback_bin(config, lane, python_bin)
        if fallback_bin:
            fallback_coverage = _run_command("coverage_version_fallback", [fallback_bin, "-m", "coverage", "--version"], repo, config.timeout_seconds, runner)
            commands.append(fallback_coverage)
            if fallback_coverage.exit_code == 0:
                python_bin = fallback_bin
            else:
                return _failed("missing_coverage", commands, fallback_coverage.stderr or fallback_coverage.stdout or coverage_check.stderr or coverage_check.stdout, config=config, setup_status=setup_status)
        else:
            return _failed("missing_coverage", commands, coverage_check.stderr or coverage_check.stdout, config=config, setup_status=setup_status)

    coverage_dir.mkdir(parents=True, exist_ok=True)
    coverage_run_cmd = [
        python_bin,
        "-m",
        "coverage",
        "run",
        "--source",
        ".",
        f"--omit={_COVERAGE_RUNTIME_OMIT}",
        "-m",
        "pytest",
        *[str(path) for path in test_paths],
    ]
    coverage_run = _run_command("pytest_coverage_run", coverage_run_cmd, repo, config.timeout_seconds, runner)
    commands.append(coverage_run)
    if coverage_run.exit_code != 0:
        return _failed("test_failed", commands, coverage_run.stderr or coverage_run.stdout, config=config, setup_status=setup_status)

    coverage_xml_cmd = [
        python_bin,
        "-m",
        "coverage",
        "xml",
        "-i",
        f"--omit={_COVERAGE_RUNTIME_OMIT}",
        "-o",
        str(coverage_xml),
    ]
    coverage_xml_result = _run_command("coverage_xml", coverage_xml_cmd, repo, config.timeout_seconds, runner)
    commands.append(coverage_xml_result)
    if coverage_xml_result.exit_code != 0 or not coverage_xml.exists():
        return _failed("missing_coverage", commands, coverage_xml_result.stderr or coverage_xml_result.stdout, config=config, setup_status=setup_status)

    source_path = target.source_path or _source_path_from_target(target.target_id)
    coverage = parse_coverage_xml(coverage_xml, [source_path], gate=coverage_gate, changed_lines=changed_lines)
    if not coverage.passed:
        return _verification_result(
            "failed",
            "coverage_gate_failed",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message=f"Python coverage {coverage.rate:.2f}% is below gate {coverage.gate:.2f}%",
            config=config,
            setup_status=setup_status,
        )
    if coverage.no_executable_changed_lines:
        return _verification_result(
            "passed",
            "passed",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message="Python tests and coverage gate passed; changed lines are not executable",
            config=config,
            setup_status=setup_status,
        )

    if mutation_gate <= 0:
        return _verification_result(
            "passed",
            "passed",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message="Python tests and coverage gate passed; mutation disabled by gate <= 0",
            config=config,
            setup_status=setup_status,
        )

    mutmut_bin = _mutmut_bin(config, lane, python_bin)
    mutmut_check = _check_mutmut_version(mutmut_bin, repo, config.timeout_seconds, runner, commands)
    if mutmut_check.exit_code != 0:
        reason = "missing_python2_mutmut" if lane == "mutmut-legacy-py2" else "missing_mutmut"
        return _verification_result(
            "failed",
            reason,
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message=mutmut_check.stderr or mutmut_check.stdout,
            config=config,
            setup_status=setup_status,
        )

    if lane == "mutmut-legacy-py2" and "1.5.0" not in (mutmut_check.stdout + mutmut_check.stderr):
        return _verification_result(
            "failed",
            "missing_python2_mutmut",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message="Python 2 verification requires mutmut==1.5.0",
            config=config,
            setup_status=setup_status,
        )

    _cleanup_mutation_state(repo)
    mutation_dir.mkdir(parents=True, exist_ok=True)
    source_file = repo / source_path
    mutation_mask = _apply_changed_line_mutation_mask(source_file, changed_lines, source_path)
    mutmut_config = _mutmut_config_overlay(
        repo,
        source_path,
        test_paths,
        mutmut_check.stdout + mutmut_check.stderr,
        python_bin=python_bin,
    )
    mutation_patch = _write_mutation_patch_file(
        mutation_dir,
        source_file=source_file,
        source_path=source_path,
        changed_lines=changed_lines,
        enabled=not mutmut_config.configured,
    )
    mutation_cmd = _mutmut_run_command(
        mutmut_bin,
        source_path,
        mutmut_config.configured,
        repo=repo,
        python_bin=python_bin,
        test_paths=test_paths,
        patch_file=mutation_patch,
    )
    try:
        mutmut_config.apply()
        mutation_result = _run_command("mutmut_run", mutation_cmd, repo, config.timeout_seconds, runner)
        commands.append(mutation_result)
        mutation_output = "\n".join(part for part in (mutation_result.stdout, mutation_result.stderr) if part)
        output_path = mutation_dir / "mutmut-output.txt"
        output_path.write_text(mutation_output, encoding="utf-8")
        mutation = parse_mutmut_summary(mutation_output, gate=mutation_gate, runtime_lane=lane)
        artifacts: Dict[str, str] = {"mutmut_output": str(output_path)}
        if mutation_mask.masked:
            artifacts["mutation_scope"] = "changed_lines"
        if mutation_patch:
            artifacts["mutation_patch"] = str(mutation_patch)
        survivors: List[Dict[str, Any]] = []
        if mutation.survived > 0:
            results = _run_command("mutmut_results", [mutmut_bin, "results"], repo, config.timeout_seconds, runner)
            commands.append(results)
            results_output = "\n".join(part for part in (results.stdout, results.stderr) if part)
            results_path = mutation_dir / "mutmut-results.txt"
            results_path.write_text(results_output, encoding="utf-8")
            artifacts["mutmut_results"] = str(results_path)
            survivors = parse_mutmut_survivors(results_output)
            survivors_path = mutation_dir / "survivors.json"
            survivors_path.write_text(json.dumps(survivors, indent=2, sort_keys=True), encoding="utf-8")
            artifacts["survivors"] = str(survivors_path)
        mutation = replace(mutation, survivors=survivors, artifacts=artifacts)
    finally:
        mutmut_config.restore()
        mutation_mask.restore()
    _cleanup_mutation_state(repo)
    if mutation.generated <= 0 or not _is_expected_mutmut_exit(mutation_result.exit_code):
        return _verification_result(
            "failed",
            _mutation_backend_reason(mutation_output),
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message=mutation_output,
            config=config,
            setup_status=setup_status,
        )
    mutation = _scope_mutation_to_changed_lines(
        mutation,
        source_path=source_path,
        changed_lines=changed_lines,
        mutation_masked=mutation_mask.masked,
    )
    if not mutation.passed:
        return _verification_result(
            "failed",
            "mutation_gate_failed",
            tests_pass=True,
            coverage=coverage,
            mutation=mutation,
            commands=commands,
            message=f"Python mutation score {mutation.rate:.2f}% is below gate {mutation.gate:.2f}%",
            config=config,
            setup_status=setup_status,
        )
    return _verification_result(
        "passed",
        "passed",
        tests_pass=True,
        coverage=coverage,
        mutation=mutation,
        commands=commands,
        message="Python pytest, coverage, and mutation gates passed",
        config=config,
        setup_status=setup_status,
    )


def parse_coverage_xml(
    xml_path: Path,
    source_paths: Iterable[str],
    *,
    gate: float,
    changed_lines: Optional[Mapping[str, Iterable[int]]] = None,
) -> CoverageSummary:
    normalized_sources = {_normalize_relpath(path) for path in source_paths if path}
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    requested_changed_lines = (
        normalized_changed_lines is not None
        and any(normalized_changed_lines.get(source, set()) for source in normalized_sources)
    )
    root = ET.parse(xml_path).getroot()
    covered = 0
    total = 0
    source_seen = False
    for class_node in root.findall(".//class"):
        filename = _normalize_relpath(class_node.attrib.get("filename", ""))
        if filename not in normalized_sources:
            continue
        source_seen = True
        for line in class_node.findall(".//line"):
            line_number = int(line.attrib.get("number") or 0)
            if normalized_changed_lines is not None and line_number not in normalized_changed_lines.get(filename, set()):
                continue
            total += 1
            if int(line.attrib.get("hits") or 0) > 0:
                covered += 1
    no_executable_changed_lines = requested_changed_lines and source_seen and total == 0
    if requested_changed_lines and total == 0 and not source_seen:
        rate = 0.0
    else:
        rate = 100.0 if total == 0 else round((covered / total) * 100.0, 4)
    return CoverageSummary(
        covered=covered,
        total=total,
        rate=rate,
        gate=float(gate),
        passed=rate >= float(gate),
        xml_path=str(xml_path),
        scope="changed_lines" if normalized_changed_lines is not None else "target_file",
        changed_lines=_changed_line_payload(normalized_changed_lines, normalized_sources),
        no_executable_changed_lines=no_executable_changed_lines,
    )


def parse_mutmut_summary(text: str, *, gate: float, runtime_lane: str) -> MutationSummary:
    generated = _extract_count(text, "generated")
    killed = _extract_count(text, "killed")
    survived = _extract_count(text, "survived")
    no_coverage = _extract_count(text, "no coverage", "no_coverage", "no-coverage")
    if generated == 0:
        progress = _extract_mutmut_progress_counts(text)
        if progress:
            generated = progress["generated"]
            killed = progress["killed"]
            survived = progress["survived"]
            no_coverage = progress["no_coverage"]
    if generated == 0:
        generated = killed + survived + no_coverage
    denominator = killed + survived
    rate = 100.0 if denominator == 0 else round((killed / denominator) * 100.0, 4)
    return MutationSummary(
        runtime_lane=runtime_lane,
        generated=generated,
        killed=killed,
        survived=survived,
        no_coverage=no_coverage,
        rate=rate,
        gate=float(gate),
        passed=rate >= float(gate),
    )


def parse_mutmut_survivors(text: str) -> List[Dict[str, Any]]:
    survivors: List[Dict[str, Any]] = []
    for line in str(text or "").splitlines():
        match = re.search(r"\bSURVIVED\b\s+([^:\s]+):(\d+)\s*(.*)$", line, flags=re.IGNORECASE)
        if not match:
            continue
        survivors.append(
            {
                "file": _normalize_relpath(match.group(1)),
                "line": int(match.group(2)),
                "description": match.group(3).strip(),
            }
        )
    return survivors


def _mutation_backend_reason(output: str) -> str:
    normalized = output.lower()
    if "whatthepatch" in normalized or "mutmut[patch]" in normalized:
        return "missing_mutmut_patch_dependency"
    return "mutation_backend_failed"


def _subprocess_run(cmd: Sequence[str], cwd: Optional[Path] = None, timeout: Optional[int] = None, env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        env=dict(env) if env else None,
        capture_output=True,
        check=False,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        stdout=_decode_output(result.stdout),
        stderr=_decode_output(result.stderr),
    )


def _run_command(name: str, cmd: List[str], repo: Path, timeout: int, runner: RunCommand) -> CommandEvidence:
    try:
        result = runner(cmd, cwd=repo, timeout=timeout, env=os.environ.copy())
    except FileNotFoundError as exc:
        return CommandEvidence(name=name, command=cmd, exit_code=127, stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        return CommandEvidence(name=name, command=cmd, exit_code=124, stdout=stdout, stderr=stderr or "command timed out")
    return CommandEvidence(
        name=name,
        command=list(cmd),
        exit_code=int(result.returncode),
        stdout=_decode_output(result.stdout),
        stderr=_decode_output(result.stderr),
    )


def _check_mutmut_version(
    mutmut_bin: str,
    repo: Path,
    timeout: int,
    runner: RunCommand,
    commands: List[CommandEvidence],
) -> CommandEvidence:
    check = _run_command("mutmut_version", [mutmut_bin, "--version"], repo, timeout, runner)
    commands.append(check)
    if check.exit_code == 0 or "No such option: --version" not in (check.stderr + check.stdout):
        return check
    fallback = _run_command("mutmut_version_command", [mutmut_bin, "version"], repo, timeout, runner)
    commands.append(fallback)
    return fallback if fallback.exit_code == 0 else check


def _failed(
    reason_code: str,
    commands: List[CommandEvidence],
    message: str,
    *,
    config: Optional[PythonRuntimeConfig] = None,
    setup_status: str = "skipped",
) -> PythonVerificationResult:
    return _verification_result(
        "failed",
        reason_code,
        commands=commands,
        message=message,
        config=config,
        setup_status=setup_status,
    )


def _verification_result(
    status: str,
    reason_code: str,
    *,
    commands: List[CommandEvidence],
    message: str,
    config: Optional[PythonRuntimeConfig],
    setup_status: str,
    tests_pass: bool = False,
    coverage: Optional[CoverageSummary] = None,
    mutation: Optional[MutationSummary] = None,
) -> PythonVerificationResult:
    return PythonVerificationResult(
        status=status,
        reason_code=reason_code,
        tests_pass=tests_pass,
        coverage=coverage,
        mutation=mutation,
        commands=commands,
        message=message,
        setup_status=setup_status,
        environment_profile=config.environment_profile if config else "default",
        dependency_fingerprints=dict(config.dependency_fingerprints) if config else {},
        cache_key=config.cache_key if config else "",
    )


def _runtime_lane(syntax_version: str) -> str:
    return "mutmut-legacy-py2" if str(syntax_version or "").lower().startswith("python2") else "mutmut-modern"


def _python_bin(config: PythonRuntimeConfig, lane: str) -> str:
    if lane == "mutmut-legacy-py2":
        return config.python2_bin or "python2"
    return config.python_bin


def _python_runtime_fallback_bin(config: PythonRuntimeConfig, lane: str, current_python_bin: str) -> Optional[str]:
    if lane == "mutmut-legacy-py2":
        return None
    if (config.config_sources or {}).get("python_bin") != "default":
        return None
    candidates = [
        os.environ.get("UTA_SERVICE_PYTHON_BIN"),
        sys.executable,
    ]
    current = str(current_python_bin or "")
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and value != current:
            return value
    return None


def _mutmut_bin(config: PythonRuntimeConfig, lane: str, python_bin: str) -> str:
    if lane == "mutmut-legacy-py2":
        return config.python2_mutmut_bin or "mutmut"
    if (config.config_sources or {}).get("mutmut_bin") == "default":
        sibling = Path(str(python_bin)).expanduser().parent / "mutmut"
        if sibling.exists():
            return sibling.as_posix()
    return config.mutmut_bin


def _extract_count(text: str, *labels: str) -> int:
    haystack = str(text or "")
    for label in labels:
        patterns = [
            rf"(\d+)\s+{re.escape(label)}",
            rf"{re.escape(label)}\s*[=:]\s*(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, haystack, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
    return 0


def _extract_mutmut_progress_counts(text: str) -> Optional[Dict[str, int]]:
    best: Optional[Dict[str, int]] = None
    for line in str(text or "").splitlines():
        match = re.search(r"(?P<done>\d+)\s*/\s*(?P<generated>\d+)", line)
        if not match:
            continue
        counts = [int(value) for value in re.findall(r"\b\d+\b", line[match.end() :])]
        if len(counts) < 4:
            continue
        generated = int(match.group("generated"))
        # Mutmut progress lines carry at least four counts after done/generated:
        # killed, timeout, suspicious, survived. Newer output may append skipped.
        best = {
            "generated": generated,
            "killed": counts[0],
            "survived": counts[3],
            "no_coverage": 0,
        }
    return best


def _source_path_from_target(target_id: str) -> str:
    raw = str(target_id or "")
    if raw.startswith("pyfile:"):
        return raw[len("pyfile:") :]
    if raw.startswith("pysymbol:"):
        raw = raw[len("pysymbol:") :]
    return raw.split("::", 1)[0]


def _scope_mutation_to_changed_lines(
    mutation: MutationSummary,
    *,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    mutation_masked: bool = False,
) -> MutationSummary:
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    if normalized_changed_lines is None:
        return mutation
    normalized_source = _normalize_relpath(source_path)
    target_lines = normalized_changed_lines.get(normalized_source, set())
    diff_survivors = [
        survivor
        for survivor in mutation.survivors
        if _normalize_relpath(str(survivor.get("file") or "")) == normalized_source
        and int(survivor.get("line") or 0) in target_lines
    ]
    survived = len(diff_survivors)
    changed_line_generated = mutation.generated if mutation_masked else 0
    changed_line_killed = max(changed_line_generated - survived - mutation.no_coverage, 0)
    has_mutation_evidence = changed_line_generated > 0 or not target_lines
    rate = 100.0 if survived == 0 and has_mutation_evidence else 0.0
    return replace(
        mutation,
        generated=mutation.generated,
        killed=changed_line_killed if mutation_masked else mutation.killed,
        survived=survived,
        no_coverage=mutation.no_coverage,
        rate=rate,
        passed=has_mutation_evidence and rate >= mutation.gate,
        scope="changed_lines",
        changed_lines=_changed_line_payload(normalized_changed_lines, {normalized_source}),
        diff_survivors=diff_survivors,
        changed_line_mutants_generated=changed_line_generated,
        changed_line_mutants_killed=changed_line_killed,
    )


@dataclass(frozen=True)
class _MutationMask:
    path: Path
    original_text: Optional[str] = None

    @property
    def masked(self) -> bool:
        return self.original_text is not None

    def restore(self) -> None:
        if self.original_text is not None:
            self.path.write_text(self.original_text, encoding="utf-8")


def _apply_changed_line_mutation_mask(
    source_file: Path,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    source_path: str,
) -> _MutationMask:
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    if normalized_changed_lines is None:
        return _MutationMask(source_file)
    target_lines = normalized_changed_lines.get(_normalize_relpath(source_path), set())
    if not target_lines or not source_file.exists():
        return _MutationMask(source_file)

    original = source_file.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    maskable_lines = _maskable_pragma_lines(original)
    masked_lines = [
        _append_no_mutate_pragma(line)
        if index in maskable_lines and index not in target_lines and not _is_pragma_safe_skip(line)
        else line
        for index, line in enumerate(lines, start=1)
    ]
    masked = "".join(masked_lines)
    if masked == original:
        return _MutationMask(source_file)
    source_file.write_text(masked, encoding="utf-8")
    return _MutationMask(source_file, original)


def _is_pragma_safe_skip(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("#")
        or stripped.endswith("\\")
        or "pragma: no mutate" in stripped
    )


@dataclass(frozen=True)
class _MutmutConfigOverlay:
    path: Path
    source_path: str
    repo: Path
    python_bin: str
    support_copy_paths: Sequence[str] = field(default_factory=tuple)
    test_paths: Sequence[str] = field(default_factory=tuple)
    original_text: Optional[str] = None
    configured: bool = False

    def apply(self) -> None:
        if not self.configured:
            return
        from configparser import ConfigParser

        parser = ConfigParser()
        if self.original_text is not None:
            parser.read_string(self.original_text)
        if not parser.has_section("mutmut"):
            parser.add_section("mutmut")
        parser.set("mutmut", "paths_to_mutate", self.source_path)
        if self.test_paths:
            parser.set("mutmut", "tests_dir", _config_list_value(self.test_paths))
            parser.set(
                "mutmut",
                "runner",
                _mutmut_runner_command(self.python_bin, self.test_paths, repo=self.repo),
            )
        if self.support_copy_paths:
            also_copy = _merge_config_list(
                parser.get("mutmut", "also_copy", fallback=""),
                self.support_copy_paths,
            )
            parser.set("mutmut", "also_copy", _config_list_value(also_copy))
        with io.StringIO() as buffer:
            parser.write(buffer)
            self.path.write_text(buffer.getvalue(), encoding="utf-8")

    def restore(self) -> None:
        if not self.configured:
            return
        if self.original_text is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(self.original_text, encoding="utf-8")


def _mutmut_config_overlay(
    repo: Path,
    source_path: str,
    test_paths: Sequence[str],
    version_output: str,
    *,
    python_bin: str,
) -> _MutmutConfigOverlay:
    setup_cfg = repo / "setup.cfg"
    original = setup_cfg.read_text(encoding="utf-8") if setup_cfg.exists() else None
    configured = _mutmut_major_version(version_output) >= 3
    return _MutmutConfigOverlay(
        setup_cfg,
        source_path,
        repo,
        python_bin,
        _mutmut_support_copy_paths(repo, source_path),
        tuple(str(path) for path in test_paths),
        original,
        configured,
    )


def _merge_config_list(existing: str, values: Sequence[str]) -> List[str]:
    result: List[str] = []
    existing_values = [line.strip() for line in existing.splitlines()]
    for value in existing_values + [str(value) for value in values]:
        if value and value not in result:
            result.append(value)
    return result


def _mutmut_support_copy_paths(repo: Path, source_path: str) -> List[str]:
    excluded = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".uta_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "mutants",
        "test",
        "tests",
        "venv",
    }
    support_paths: List[str] = []

    def add(path: str) -> None:
        if path and path not in excluded and path not in support_paths:
            support_paths.append(path)

    for child in sorted(repo.iterdir(), key=lambda item: item.name):
        name = child.name
        if name in excluded:
            continue
        if child.is_file() and child.suffix == ".py":
            add(name)
        elif child.is_dir() and (child / "__init__.py").exists():
            add(name)

    source = Path(source_path)
    if source.parent != Path("."):
        add(source.parts[0])
        add(source.parent.as_posix())
    elif source.suffix != ".py":
        add(source.as_posix())

    return support_paths


def _config_list_value(values: Sequence[str]) -> str:
    items = [str(value) for value in values if str(value)]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return "\n" + "\n".join(items)


def _mutmut_run_command(
    mutmut_bin: str,
    source_path: str,
    config_overlay: bool,
    *,
    repo: Path,
    python_bin: str,
    test_paths: Sequence[str],
    patch_file: Optional[Path] = None,
) -> List[str]:
    if config_overlay:
        return [mutmut_bin, "run"]
    command = [mutmut_bin, "run", "--paths-to-mutate", source_path]
    if patch_file is not None:
        command.extend(["--use-patch-file", str(patch_file)])
    tests_dir = _mutmut_tests_dir(test_paths)
    if tests_dir:
        command.extend(["--tests-dir", tests_dir])
    runner = _mutmut_runner_command(python_bin, test_paths, repo=repo)
    if runner:
        command.extend(["--runner", runner])
    return command


def _mutmut_runner_command(python_bin: str, test_paths: Sequence[str], *, repo: Path) -> str:
    parts = [python_bin, "-m", "pytest", "-x", "--assert=plain", *[str(path) for path in test_paths]]
    command = " ".join(shlex.quote(part) for part in parts)
    return f"PYTHONPATH={shlex.quote(repo.as_posix())}:$PYTHONPATH {command}"


def _mutmut_tests_dir(test_paths: Sequence[str]) -> str:
    if not test_paths:
        return "."
    parent = Path(str(test_paths[0])).parent
    return "." if str(parent) in {"", "."} else str(parent)


def _write_mutation_patch_file(
    mutation_dir: Path,
    *,
    source_file: Path,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    enabled: bool,
) -> Optional[Path]:
    if not enabled:
        return None
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    if normalized_changed_lines is None:
        return None
    normalized_source = _normalize_relpath(source_path)
    target_lines = sorted(normalized_changed_lines.get(normalized_source, set()))
    if not target_lines or not source_file.exists():
        return None
    source_lines = source_file.read_text(encoding="utf-8").splitlines()
    hunks = [
        f"@@ -{line_number - 1},0 +{line_number},1 @@\n+{source_lines[line_number - 1] if line_number <= len(source_lines) else ''}\n"
        for line_number in target_lines
    ]
    patch_text = (
        f"--- {normalized_source}\n"
        f"+++ {normalized_source}\n"
        + "".join(hunks)
    )
    patch_path = mutation_dir / "changed-lines.patch"
    patch_path.write_text(patch_text, encoding="utf-8")
    return patch_path


def _mutmut_major_version(version_output: str) -> int:
    match = re.search(r"\b(\d+)\.\d+(?:\.\d+)?\b", str(version_output or ""))
    return int(match.group(1)) if match else 0


def _maskable_pragma_lines(source: str) -> set[int]:
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        return {token.end[0] for token in tokens if token.type == tokenize.NEWLINE}
    except tokenize.TokenError:
        return set()


def _append_no_mutate_pragma(line: str) -> str:
    newline = ""
    body = line
    if line.endswith("\r\n"):
        body, newline = line[:-2], "\r\n"
    elif line.endswith("\n"):
        body, newline = line[:-1], "\n"
    return f"{body}  # pragma: no mutate{newline}"


def _normalize_changed_lines(
    changed_lines: Optional[Mapping[str, Iterable[int]]],
) -> Optional[Dict[str, set[int]]]:
    if changed_lines is None:
        return None
    normalized: Dict[str, set[int]] = {}
    for path, lines in changed_lines.items():
        normalized_path = _normalize_relpath(str(path))
        normalized[normalized_path] = {int(line) for line in lines or [] if int(line) > 0}
    return normalized


def _changed_line_payload(
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    source_paths: Iterable[str],
) -> Dict[str, List[int]]:
    if changed_lines is None:
        return {}
    normalized_sources = {_normalize_relpath(path) for path in source_paths if path}
    return {
        path: sorted(int(line) for line in lines)
        for path, lines in changed_lines.items()
        if not normalized_sources or path in normalized_sources
    }


def _normalize_relpath(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _decode_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_expected_mutmut_exit(exit_code: int) -> bool:
    # Mutmut versions differ on non-zero gate/result exits, but signal-style
    # and shell failure codes still indicate the backend did not complete.
    return int(exit_code) in {0, 1, 2}


def _cleanup_mutation_state(repo: Path) -> None:
    for relative in (".mutmut-cache", ".coverage"):
        path = repo / relative
        if path.is_dir():
            shutil.rmtree(str(path), ignore_errors=True)
        elif path.exists():
            path.unlink()


def _read_simple_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    values: Dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _parse_simple_toml_value(value.strip())
    return values


def _parse_simple_toml_value(value: str) -> Any:
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        items = []
        for item in value[1:-1].split(","):
            parsed = _parse_simple_toml_value(item.strip())
            if parsed != "":
                items.append(parsed)
        return items
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def _coerce_config_value(key: str, value: Any) -> Any:
    if key == "setup_command":
        if isinstance(value, str):
            return tuple(shlex.split(value))
        return tuple(str(part) for part in (value or ()))
    if key == "timeout_seconds":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1800
    return value


def _dependency_fingerprints(repo: Path) -> Dict[str, str]:
    candidates = (
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "noxfile.py",
        ".uta/python-enforce.toml",
    )
    fingerprints: Dict[str, str] = {}
    for relative in candidates:
        path = repo / relative
        if path.is_file():
            fingerprints[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return fingerprints


def _python_cache_key(values: Mapping[str, Any], fingerprints: Mapping[str, str]) -> str:
    payload = {
        "python_bin": values.get("python_bin"),
        "python2_bin": values.get("python2_bin"),
        "mutmut_bin": values.get("mutmut_bin"),
        "python2_mutmut_bin": values.get("python2_mutmut_bin"),
        "setup_command": list(values.get("setup_command") or ()),
        "environment_profile": values.get("environment_profile"),
        "dependency_fingerprints": dict(sorted(fingerprints.items())),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"python-env:{digest}"


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _task_status_for_verification(result: PythonVerificationResult) -> str:
    if result.status == "passed":
        return "PASS"
    if result.reason_code == "mutation_gate_failed":
        return "MUTATION_FAIL"
    return "FAIL"

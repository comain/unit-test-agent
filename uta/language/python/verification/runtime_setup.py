"""Runtime and dependency setup performed before the gates run.

Two preparation steps that must happen before any measurement, and that both
mutate the machine rather than the repository under test: installing the
target's nearest nested requirements into a locked, digest-keyed overlay
directory instead of into UTA's own environment, and making sure the mutmut on
PATH is the version UTA owns. Both return ``CommandEvidence`` so their side
effects stay visible in the verification record.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from uta.language.python.verification.dependency_requirements import (
    nearest_requirements_manifest as _nearest_requirements_manifest,
    prepare_dependency_overlay as _prepare_dependency_overlay,
)
from uta.language.python.verification.evidence import CoverageSummary
from uta.language.python.verification.models import (
    CommandEvidence,
    PythonRuntimeConfig,
    PythonVerificationResult,
    RunCommand,
)
from uta.language.python.verification.process import _run_command, _runner_with_env
from uta.language.python.verification.mutmut_runtime import _mutmut_major_version
from uta.language.python.verification.results import (
    _command_output,
    _failed,
    _py_compile_indicates_runtime_incompatibility,
    _runtime_incompatible_coverage_summary,
    _runtime_incompatible_mutation_summary,
    _verification_result,
)
from uta.shared.config import settings
from uta.language.python.verification.runtime_config import (
    _mutmut_bin,
    _python_runtime_fallback_bin,
)


UTA_OWNED_MUTMUT_VERSION = "3.5.0"


_UTA_OWNED_MUTMUT_SPEC = f"mutmut=={UTA_OWNED_MUTMUT_VERSION}"


def _select_overlay_runtime(
    *,
    repo: Path,
    lane: str,
    python_bin: str,
    config: PythonRuntimeConfig,
    runner: RunCommand,
    commands: List[CommandEvidence],
) -> str:
    """Choose the verifier interpreter before ABI-specific dependencies install."""

    def _usable(candidate: str, suffix: str) -> bool:
        probes = (
            (f"python_version{suffix}", [candidate, "--version"]),
            (f"pytest_version{suffix}", [candidate, "-m", "pytest", "--version"]),
            (f"coverage_version{suffix}", [candidate, "-m", "coverage", "--version"]),
        )
        results = []
        for name, command in probes:
            result = _run_command(name, command, repo, config.timeout_seconds, runner)
            commands.append(result)
            results.append(result)
        return all(result.exit_code == 0 for result in results)

    if _usable(python_bin, "_overlay_preflight"):
        return python_bin
    fallback_bin = _python_runtime_fallback_bin(config, lane, python_bin)
    if fallback_bin and _usable(fallback_bin, "_overlay_fallback"):
        return fallback_bin
    return python_bin


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


def _ensure_uta_owned_mutmut_version(
    mutmut_bin: str,
    python_bin: str,
    repo: Path,
    timeout: int,
    runner: RunCommand,
    commands: List[CommandEvidence],
    config: PythonRuntimeConfig,
    lane: str,
) -> Optional[CommandEvidence]:
    if lane == "mutmut-legacy-py2":
        return None
    source = (config.config_sources or {}).get("mutmut_bin")
    if source not in {"env:UTA_PYTHON_MUTMUT_BIN", "cli"}:
        return None

    check = _run_command("mutmut_version_preflight", [mutmut_bin, "--version"], repo, timeout, runner)
    commands.append(check)
    if check.exit_code != 0:
        return check
    if UTA_OWNED_MUTMUT_VERSION in (check.stdout + check.stderr):
        return None

    install = _run_command(
        "mutmut_pin_install",
        [python_bin, "-m", "pip", "install", "-q", _UTA_OWNED_MUTMUT_SPEC],
        repo,
        timeout,
        runner,
    )
    commands.append(install)
    if install.exit_code != 0:
        return install
    return None


def _prepare_nested_requirements_overlay(
    repo: Path,
    source_path: str,
    python_bin: str,
    config: PythonRuntimeConfig,
    runner: RunCommand,
    *,
    test_paths: Sequence[str] = (),
) -> Optional[Tuple[CommandEvidence, Path, PythonRuntimeConfig]]:
    """Install the target's nearest requirements without polluting UTA."""
    manifest = _nearest_requirements_manifest(repo, source_path)
    if manifest is None:
        return None
    relative_manifest = manifest.relative_to(repo).as_posix()
    raw_evidence, dependency_dir, content_digest = _prepare_dependency_overlay(
        repo,
        source_path,
        test_paths=test_paths,
        python_bin=python_bin,
        timeout_seconds=config.timeout_seconds,
        artifact_dir=config.artifact_dir,
        runtime_cache_key=config.cache_key,
        runner=lambda command, cwd=None, timeout=None, env=None: _run_command(
            "dependency_overlay_install",
            command,
            Path(cwd or repo),
            int(timeout or config.timeout_seconds),
            runner,
        ),
    )
    if raw_evidence is None or content_digest is None:
        return None
    evidence = CommandEvidence(
        name=str(raw_evidence["name"]),
        command=list(raw_evidence["command"]),
        exit_code=int(raw_evidence["exitCode"]),
        stdout=str(raw_evidence.get("stdout", "")),
        stderr=str(raw_evidence.get("stderr", "")),
    )
    fingerprints = dict(config.dependency_fingerprints)
    fingerprints[relative_manifest] = content_digest
    cache_digest = hashlib.sha256(
        f"{config.cache_key}:{relative_manifest}:{content_digest}".encode("utf-8")
    ).hexdigest()[:16]
    return evidence, dependency_dir, replace(
        config,
        dependency_fingerprints=fingerprints,
        cache_key=f"python-env:{cache_digest}",
    )


@dataclass(frozen=True)
class RuntimePreparation:
    """What the gates need to start, or the result that ends the run early."""

    config: PythonRuntimeConfig
    runner: RunCommand
    python_bin: str
    setup_status: str
    failure: Optional[PythonVerificationResult] = None


def _skipped_overlay_evidence(repo: Path, source_path: str) -> CommandEvidence:
    """Say why no dependency overlay was installed, at the moment it is decided.

    With neither a manifest nor a configured ``setup_command``, a target's
    third-party imports resolve only if UTA's own environment happens to carry
    them. The first symptom is otherwise a ModuleNotFoundError raised from the
    repository's own conftest -- once per target, with nothing anywhere in the
    record naming the cause. One production run failed all 39 targets that way.

    Recorded as exit 0 because skipping is not itself a failure: repositories
    with no third-party requirements are ordinary, and this must not turn them
    red. It exists so the evidence answers "why was nothing installed".
    """
    repo_resolved = repo.resolve()
    searched: List[str] = []
    for directory in ((repo / source_path).resolve().parent, *(repo / source_path).resolve().parents):
        searched.append(directory.as_posix())
        if directory == repo_resolved:
            break
    detail = (
        "no dependency overlay: no requirements.txt declaring an installable "
        "requirement from the target directory up to the repository root "
        f"(searched: {', '.join(searched) or 'nothing'})"
    )
    return CommandEvidence(name="dependency_overlay_skipped", command=[], exit_code=0, stderr=detail)


def prepare_verification_runtime(
    *,
    repo: Path,
    source_path: str,
    test_paths: Sequence[str],
    lane: str,
    config: PythonRuntimeConfig,
    runner: RunCommand,
    commands: List[CommandEvidence],
    python_bin: str,
    setup_status: str,
    coverage_gate: float,
    mutation_gate: float,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
) -> RuntimePreparation:
    """Install dependencies, probe the toolchain, and rule out an unusable target.

    Ordering is behaviour here, not tidiness: an automatic dependency overlay
    is installed only after choosing the interpreter that will execute it, and
    py_compile runs before the selected tests, so repair cannot shadow-copy a
    source test past an interpreter mismatch. Every command issued is appended
    to ``commands`` in the order it ran.
    """

    def _prepared(failure: Optional[PythonVerificationResult] = None) -> RuntimePreparation:
        return RuntimePreparation(config, runner, python_bin, setup_status, failure)

    if config.setup_command:
        setup_status = "executed"
        setup = _run_command("setup", list(config.setup_command), repo, config.timeout_seconds, runner)
        commands.append(setup)
        if setup.exit_code != 0:
            return _prepared(_failed(
                "setup_failed",
                commands,
                f"Python dependency setup failed: {setup.stderr or setup.stdout}",
                config=config,
                setup_status="failed",
            ))
    elif lane != "mutmut-legacy-py2":
        if _nearest_requirements_manifest(repo, source_path) is not None:
            python_bin = _select_overlay_runtime(
                repo=repo,
                lane=lane,
                python_bin=python_bin,
                config=config,
                runner=runner,
                commands=commands,
            )
        prepared = _prepare_nested_requirements_overlay(
            repo,
            source_path,
            python_bin,
            config,
            runner,
            test_paths=test_paths,
        )
        if prepared is not None:
            setup, dependency_dir, config = prepared
            commands.append(setup)
            setup_status = (
                "cached" if setup.name.endswith("_cached")
                else "compat" if setup.name == "dependency_overlay_compat_install"
                else "executed"
            )
            if setup.exit_code != 0:
                return _prepared(_failed(
                    "setup_failed",
                    commands,
                    f"Python dependency setup failed: {setup.stderr or setup.stdout}",
                    config=config,
                    setup_status="failed",
                ))
            runner = _runner_with_env(
                runner,
                {"PYTHONPATH": str(dependency_dir)},
            )
        else:
            commands.append(_skipped_overlay_evidence(repo, source_path))

    python_version = _run_command("python_version", [python_bin, "--version"], repo, config.timeout_seconds, runner)
    commands.append(python_version)
    if python_version.exit_code != 0:
        reason = "missing_python2_runtime" if lane == "mutmut-legacy-py2" else "missing_python_runtime"
        return _prepared(_failed(reason, commands, python_version.stderr or python_version.stdout, config=config, setup_status=setup_status))

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
                return _prepared(_failed("missing_pytest", commands, fallback_pytest.stderr or fallback_pytest.stdout or pytest_check.stderr or pytest_check.stdout, config=config, setup_status=setup_status))
        else:
            return _prepared(_failed("missing_pytest", commands, pytest_check.stderr or pytest_check.stdout, config=config, setup_status=setup_status))

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
                return _prepared(_failed("missing_coverage", commands, fallback_coverage.stderr or fallback_coverage.stdout or coverage_check.stderr or coverage_check.stdout, config=config, setup_status=setup_status))
        else:
            return _prepared(_failed("missing_coverage", commands, coverage_check.stderr or coverage_check.stdout, config=config, setup_status=setup_status))

    compile_timeout = max(1, min(int(config.timeout_seconds), 60))
    # Runtime compatibility is decided before selected tests run, so repair
    # cannot create shadow-copied source tests to bypass an interpreter mismatch.
    compile_check = _run_command(
        "python_target_py_compile",
        [python_bin, "-m", "py_compile", source_path],
        repo,
        compile_timeout,
        runner,
    )
    commands.append(compile_check)
    if compile_check.exit_code != 0:
        compile_output = _command_output(compile_check)
        if _py_compile_indicates_runtime_incompatibility(compile_check):
            coverage = _runtime_incompatible_coverage_summary(
                source_path=source_path,
                changed_lines=changed_lines,
                gate=coverage_gate,
            )
            mutation = _runtime_incompatible_mutation_summary(
                source_path=source_path,
                changed_lines=changed_lines,
                gate=mutation_gate,
                runtime_lane=lane,
            )
            return _prepared(_verification_result(
                "passed",
                "python_runtime_incompatible_skipped",
                tests_pass=True,
                coverage=coverage,
                mutation=mutation,
                commands=commands,
                message=(
                    "Python target is incompatible with the configured verification runtime; "
                    "UTA skipped coverage, mutation, and LLM repair for this target. "
                    f"py_compile output: {compile_output}"
                ),
                config=config,
                setup_status=setup_status,
            ))
        return _prepared(_failed(
            "python_target_compile_failed",
            commands,
            compile_output,
            config=config,
            setup_status=setup_status,
        ))

    return _prepared()


@dataclass(frozen=True)
class MutmutPreflight:
    """The mutmut binary and version banner to run with, or the failing result."""

    mutmut_bin: str = ""
    version_output: str = ""
    failure: Optional[PythonVerificationResult] = None


def mutmut_preflight(
    *,
    repo: Path,
    lane: str,
    python_bin: str,
    config: PythonRuntimeConfig,
    runner: RunCommand,
    commands: List[CommandEvidence],
    coverage: CoverageSummary,
    setup_status: str,
) -> MutmutPreflight:
    """Pick the mutmut binary and prove it is a version this lane can score with.

    Each lane admits exactly one mutation backend -- mutmut 1.5.0 for the legacy
    Python 2 lane, mutmut>=3 with the candidate-plan adapter otherwise -- and a
    mismatch is a failed gate with its own reason code, never a fallback.
    """
    version_output = ""

    def _preflight(failure: Optional[PythonVerificationResult] = None) -> MutmutPreflight:
        return MutmutPreflight(mutmut_bin, version_output, failure)

    mutmut_bin = _mutmut_bin(config, lane, python_bin)
    mutmut_pin = _ensure_uta_owned_mutmut_version(
        mutmut_bin,
        python_bin,
        repo,
        config.timeout_seconds,
        runner,
        commands,
        config,
        lane,
    )
    if mutmut_pin is not None:
        return _preflight(_verification_result(
            "failed",
            "mutmut_pin_failed",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message=mutmut_pin.stderr or mutmut_pin.stdout,
            config=config,
            setup_status=setup_status,
        ))
    mutmut_check = _check_mutmut_version(mutmut_bin, repo, config.timeout_seconds, runner, commands)
    if mutmut_check.exit_code != 0:
        reason = "missing_python2_mutmut" if lane == "mutmut-legacy-py2" else "missing_mutmut"
        return _preflight(_verification_result(
            "failed",
            reason,
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message=mutmut_check.stderr or mutmut_check.stdout,
            config=config,
            setup_status=setup_status,
        ))

    if lane == "mutmut-legacy-py2" and "1.5.0" not in (mutmut_check.stdout + mutmut_check.stderr):
        return _preflight(_verification_result(
            "failed",
            "missing_python2_mutmut",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message="Python 2 verification requires mutmut==1.5.0",
            config=config,
            setup_status=setup_status,
        ))
    version_output = mutmut_check.stdout + mutmut_check.stderr
    if lane != "mutmut-legacy-py2":
        if _mutmut_major_version(version_output) < 3:
            return _preflight(_verification_result(
                "failed",
                "unsupported_mutmut_version",
                tests_pass=True,
                coverage=coverage,
                commands=commands,
                message="Python 3 mutation verification requires mutmut>=3 and the candidate-plan adapter path",
                config=config,
                setup_status=setup_status,
            ))
        if not bool(getattr(settings, "python_mutation_candidate_plan_enabled", False)):
            return _preflight(_verification_result(
                "failed",
                "mutation_candidate_plan_disabled",
                tests_pass=True,
                coverage=coverage,
                commands=commands,
                message="Python 3 mutation verification requires the candidate-plan adapter path",
                config=config,
                setup_status=setup_status,
            ))

    return _preflight()

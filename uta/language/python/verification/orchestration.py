"""Orchestration of one Python verification run.

``verify_python_target`` is deliberately thin: it resolves the run's paths and
lane, then hands off to runtime setup, coverage execution and -- if the
coverage gate passes and mutation is wanted -- the mutation phase. What stays
here is only the sequencing and the gate decisions between the phases, which is
the part that has to be read as one story.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

from uta.language.python.verification.coverage_execution import (
    _coverage_include_patterns,
    run_coverage_execution,
)
from uta.language.python.verification.models import (
    CommandEvidence,
    PythonRuntimeConfig,
    PythonVerificationResult,
    RunCommand,
)
from uta.language.python.verification.mutation_phase import run_mutation_phase
from uta.language.python.verification.process import _subprocess_run
from uta.language.python.verification.results import _verification_result
from uta.language.python.verification.runtime_config import (
    _python_bin,
    _runtime_lane,
    _source_path_from_target,
    resolve_python_runtime_config,
)
from uta.language.python.verification.runtime_setup import prepare_verification_runtime
from uta.shared.targets import TargetRef


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
    enable_mutation_sampling: bool = False,
    run_mutation: bool = True,
    base_ref: str = "",
    base_commit: str = "",
    head_commit: str = "",
    repo_url: str = "",
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
    source_path = target.source_path or _source_path_from_target(target.target_id)
    coverage_include = _coverage_include_patterns(source_path)

    prepared = prepare_verification_runtime(
        repo=repo,
        source_path=source_path,
        test_paths=test_paths,
        lane=lane,
        config=config,
        runner=runner,
        commands=commands,
        python_bin=python_bin,
        setup_status=setup_status,
        coverage_gate=coverage_gate,
        mutation_gate=mutation_gate,
        changed_lines=changed_lines,
    )
    if prepared.failure is not None:
        return prepared.failure
    config = prepared.config
    runner = prepared.runner
    python_bin = prepared.python_bin
    setup_status = prepared.setup_status

    executed = run_coverage_execution(
        repo=repo,
        coverage_dir=coverage_dir,
        coverage_xml=coverage_xml,
        source_path=source_path,
        test_paths=test_paths,
        python_bin=python_bin,
        coverage_include=coverage_include,
        config=config,
        runner=runner,
        commands=commands,
        coverage_gate=coverage_gate,
        changed_lines=changed_lines,
        setup_status=setup_status,
    )
    if executed.failure is not None:
        return executed.failure
    pytest_context = executed.pytest_context
    coverage = executed.coverage
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

    if not run_mutation:
        return _verification_result(
            "passed",
            "coverage_gate_passed_mutation_deferred",
            tests_pass=True,
            coverage=coverage,
            commands=commands,
            message="Python tests and coverage gate passed; mutation deferred for fast coverage repair",
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

    return run_mutation_phase(
        repo=repo,
        target=target,
        source_path=source_path,
        test_paths=test_paths,
        mutation_dir=mutation_dir,
        pytest_context=pytest_context,
        coverage=coverage,
        mutation_gate=mutation_gate,
        changed_lines=changed_lines,
        enable_mutation_sampling=enable_mutation_sampling,
        lane=lane,
        python_bin=python_bin,
        config=config,
        runner=runner,
        commands=commands,
        setup_status=setup_status,
        base_ref=base_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        repo_url=repo_url,
    )

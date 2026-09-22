"""Construction of the verification outcome, and the prechecks that skip a target.

Every ``PythonVerificationResult`` the runner returns is built here, so the
status/reason-code/message shape and the setup and cache fields attached to it
cannot drift between the success path and the many failure paths. The
runtime-incompatibility precheck lives with them because it is nothing but a
py_compile probe plus the skip result it produces -- enforcement calls it
before reporting missing test evidence so an interpreter-incompatible target
never opens an LLM repair. The large-change precheck is the same shape over a
different question: how much of the file the diff actually rewrote.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable, List, Mapping, Optional

from uta.language.python.verification.evidence import CoverageSummary, MutationSummary
from uta.language.python.verification.models import (
    CommandEvidence,
    PythonRuntimeConfig,
    PythonVerificationResult,
    RunCommand,
)
from uta.language.python.verification.mutation_scoping import _empty_mutation_summary
from uta.language.python.verification.mutmut_runtime import (
    _changed_line_payload,
    _normalize_changed_lines,
    _normalize_relpath,
)
from uta.language.python.verification.process import _run_command, _subprocess_run
from uta.language.python.verification.runtime_config import (
    _python_bin,
    _runtime_lane,
    _source_path_from_target,
    resolve_python_runtime_config,
)
from uta.shared.config import settings
from uta.shared.targets import TargetRef


def precheck_python_large_change(
    target: TargetRef,
    *,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    coverage_gate: float = 80.0,
    mutation_gate: float = 70.0,
    syntax_version: str = "python3",
    config: Optional[PythonRuntimeConfig] = None,
    max_changed_lines: Optional[int] = None,
) -> Optional[PythonVerificationResult]:
    """Return a PASS/skip result when a target's diff is too large to enforce.

    Coverage and mutation are diff-scoped, so their cost tracks changed lines.
    A file the branch rewrote wholesale therefore costs what a whole repo costs,
    and the per-file mutant caps do not help: they trim what gets *scored* after
    the generation pass has already run. Skipping the target keeps the rest of
    the report -- the files whose diffs are actually incremental -- instead of
    losing every target to a run-level timeout.

    The skip is advisory: it passes, and the message says what was not verified.
    """
    limit = int(settings.python_enforcement_max_changed_lines_per_file if max_changed_lines is None else max_changed_lines)
    if limit <= 0:
        return None
    source_path = target.source_path or _source_path_from_target(target.target_id)
    normalized_source = _normalize_relpath(source_path)
    normalized_changed = _normalize_changed_lines(changed_lines) or {}
    changed_count = len(normalized_changed.get(normalized_source, set()))
    if changed_count <= limit:
        return None

    lane = _runtime_lane(syntax_version)
    return _verification_result(
        "passed",
        "python_large_change_skipped",
        tests_pass=True,
        coverage=_skip_coverage_summary(
            source_path=source_path,
            changed_lines=changed_lines,
            gate=coverage_gate,
            scope="large_change_skipped",
        ),
        mutation=_skip_mutation_summary(
            source_path=source_path,
            changed_lines=changed_lines,
            gate=mutation_gate,
            runtime_lane=lane,
            scope="large_change_skipped",
            artifacts={"large_change": f"{changed_count}_changed_lines"},
        ),
        commands=[],
        message=(
            f"Python target changed {changed_count} lines, above the {limit}-line per-file "
            "enforcement limit; UTA skipped coverage, mutation, and LLM repair for this target. "
            "Coverage and mutation for this file were not verified."
        ),
        config=config,
        setup_status="skipped",
    )


def precheck_python_target_runtime_incompatibility(
    repo_path: Path,
    target: TargetRef,
    *,
    syntax_version: str = "python3",
    coverage_gate: float = 80.0,
    mutation_gate: float = 70.0,
    config: Optional[PythonRuntimeConfig] = None,
    run_command: Optional[RunCommand] = None,
    changed_lines: Optional[Mapping[str, Iterable[int]]] = None,
) -> Optional[PythonVerificationResult]:
    """Return a PASS/skip result when a target cannot compile on this lane.

    Enforcement runs this before reporting missing test evidence, so an
    interpreter-incompatible target is skipped without opening an LLM repair.
    """
    config = config or resolve_python_runtime_config(repo_path)
    runner = run_command or _subprocess_run
    repo = Path(repo_path)
    lane = _runtime_lane(syntax_version)
    python_bin = _python_bin(config, lane)
    source_path = target.source_path or _source_path_from_target(target.target_id)
    commands: List[CommandEvidence] = []

    python_version = _run_command("python_version", [python_bin, "--version"], repo, config.timeout_seconds, runner)
    commands.append(python_version)
    if python_version.exit_code != 0:
        return None

    compile_check = _run_command(
        "python_target_py_compile",
        [python_bin, "-m", "py_compile", source_path],
        repo,
        max(1, min(int(config.timeout_seconds), 60)),
        runner,
    )
    commands.append(compile_check)
    if not _py_compile_indicates_runtime_incompatibility(compile_check):
        return None

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
    return _verification_result(
        "passed",
        "python_runtime_incompatible_skipped",
        tests_pass=True,
        coverage=coverage,
        mutation=mutation,
        commands=commands,
        message=(
            "Python target is incompatible with the configured verification runtime; "
            "UTA skipped coverage, mutation, and LLM repair for this target. "
            f"py_compile output: {_command_output(compile_check)}"
        ),
        config=config,
        setup_status="skipped",
    )


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


def _command_output(command: CommandEvidence) -> str:
    return "\n".join(part for part in (command.stdout, command.stderr) if part).strip()


def _py_compile_indicates_runtime_incompatibility(command: CommandEvidence) -> bool:
    output = _command_output(command)
    return command.exit_code != 0 and "SyntaxError" in output


def _skip_coverage_summary(
    *,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    gate: float,
    scope: str,
    no_executable_changed_lines: bool = False,
) -> CoverageSummary:
    """A passing, empty coverage summary for a target that was not verified.

    `no_executable_changed_lines` is a claim about the diff, not about the skip:
    only the runtime-incompatible lane can make it, because a file the
    interpreter cannot parse has no executable changed lines on this lane. A
    large-change skip leaves it False -- those lines are executable, they were
    just not measured.
    """
    normalized_changed_lines = _normalize_changed_lines(changed_lines) or {}
    normalized_source = _normalize_relpath(source_path)
    return CoverageSummary(
        covered=0,
        total=0,
        rate=100.0,
        gate=float(gate),
        passed=True,
        xml_path="",
        scope=scope,
        changed_lines=_changed_line_payload(normalized_changed_lines, {normalized_source}),
        no_executable_changed_lines=no_executable_changed_lines,
    )


def _skip_mutation_summary(
    *,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    gate: float,
    runtime_lane: str,
    scope: str,
    artifacts: Mapping[str, str],
) -> MutationSummary:
    """A passing, empty mutation summary for a target that was not verified."""
    normalized_changed_lines = _normalize_changed_lines(changed_lines) or {}
    normalized_source = _normalize_relpath(source_path)
    return replace(
        _empty_mutation_summary(gate=gate, runtime_lane=runtime_lane),
        scope=scope,
        changed_lines=_changed_line_payload(normalized_changed_lines, {normalized_source}),
        artifacts=dict(artifacts),
    )


def _runtime_incompatible_coverage_summary(
    *,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    gate: float,
) -> CoverageSummary:
    return _skip_coverage_summary(
        source_path=source_path,
        changed_lines=changed_lines,
        gate=gate,
        scope="runtime_incompatible",
        no_executable_changed_lines=True,
    )


def _runtime_incompatible_mutation_summary(
    *,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    gate: float,
    runtime_lane: str,
) -> MutationSummary:
    return _skip_mutation_summary(
        source_path=source_path,
        changed_lines=changed_lines,
        gate=gate,
        runtime_lane=runtime_lane,
        scope="runtime_incompatible",
        artifacts={"runtime_compatibility": "py_compile_failed"},
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

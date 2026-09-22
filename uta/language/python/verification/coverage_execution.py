"""Coverage execution: run the selected tests under coverage and read the XML.

The coverage gate is one command pair -- ``coverage run -m pytest`` and
``coverage xml`` -- plus the include/omit patterns that keep UTA's own caches
and generated mutants out of the measurement. Those patterns are the reason
this is a module rather than two inline calls: they must be identical across
both commands, and the omit list is part of the evidence a target's coverage
number rests on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence

from uta.language.python.verification.evidence import CoverageSummary, parse_coverage_xml
from uta.language.python.verification.models import (
    CommandEvidence,
    PythonRuntimeConfig,
    PythonVerificationResult,
    RunCommand,
)
from uta.language.python.verification.mutmut_runtime import _normalize_relpath
from uta.language.python.verification.process import _run_command
from uta.language.python.verification.pytest_execution import (
    _PytestExecutionContext,
    _prepare_pytest_execution_context,
)
from uta.language.python.verification.results import _failed


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
class CoverageExecution:
    """Either a parsed coverage summary, or the result that ends the run."""

    pytest_context: _PytestExecutionContext
    coverage: Optional[CoverageSummary] = None
    failure: Optional[PythonVerificationResult] = None


def _coverage_include_patterns(source_path: str) -> str:
    normalized = _normalize_relpath(source_path)
    if not normalized:
        return "*"
    return f"{normalized},*/{normalized}"


def run_coverage_execution(
    *,
    repo: Path,
    coverage_dir: Path,
    coverage_xml: Path,
    source_path: str,
    test_paths: Sequence[str],
    python_bin: str,
    coverage_include: str,
    config: PythonRuntimeConfig,
    runner: RunCommand,
    commands: List[CommandEvidence],
    coverage_gate: float,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    setup_status: str,
) -> CoverageExecution:
    """Run the selected tests under coverage and parse the resulting report.

    Appends every command it issues to ``commands`` so the evidence order is
    the order the commands ran in.
    """
    coverage_dir.mkdir(parents=True, exist_ok=True)
    pytest_context = _prepare_pytest_execution_context(
        repo,
        test_paths,
        source_path=source_path,
    )
    from uta_py_enforce.pytest_env import pytest_process_command

    coverage_run_cmd = pytest_process_command(
        python_bin, pytest_context.test_paths,
        coverage_include=coverage_include, coverage_omit=_COVERAGE_RUNTIME_OMIT,
    )
    coverage_run = _run_command(
        "pytest_coverage_run",
        coverage_run_cmd,
        repo,
        config.timeout_seconds,
        runner,
        env_overrides=pytest_context.env_overrides,
    )
    commands.append(coverage_run)
    if coverage_run.exit_code != 0:
        failure_output = "\n".join(
            part.strip()
            for part in (coverage_run.stdout, coverage_run.stderr)
            if part and part.strip()
        )
        return CoverageExecution(
            pytest_context,
            failure=_failed("test_failed", commands, failure_output, config=config, setup_status=setup_status),
        )

    coverage_xml_cmd = [
        python_bin,
        "-m",
        "coverage",
        "xml",
        "-i",
        f"--include={coverage_include}",
        f"--omit={_COVERAGE_RUNTIME_OMIT}",
        "-o",
        str(coverage_xml),
    ]
    coverage_xml_result = _run_command("coverage_xml", coverage_xml_cmd, repo, config.timeout_seconds, runner)
    commands.append(coverage_xml_result)
    if coverage_xml_result.exit_code != 0 or not coverage_xml.exists():
        return CoverageExecution(
            pytest_context,
            failure=_failed("missing_coverage", commands, coverage_xml_result.stderr or coverage_xml_result.stdout, config=config, setup_status=setup_status),
        )

    coverage = parse_coverage_xml(coverage_xml, [source_path], gate=coverage_gate, changed_lines=changed_lines)
    return CoverageExecution(pytest_context, coverage=coverage)

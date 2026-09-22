"""Turning one completed Maven run into one enforcement verdict.

The order of these checks is the policy: skipped before failed, an explicit
gate failure (attributed when unrelated failing tests were excluded) before a
below-gate mutation score, a vacuous mutation score before a broken build, a
broken build before a PIT baseline failure, and evidence-present before any
exit-code reading. Changing the order changes what UTA reports, so it lives in
one readable place.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from uta.enforcement.enforcement import QualityGateResult, QualityGateStatus
from uta.language.java.enforcement_runner.evidence import (
    MISSING_EVIDENCE_SUMMARY,
    _with_filtered_target_evidence,
    _with_surefire_failure_evidence,
)
from uta.language.java.enforcement_runner.parsing import (
    _diff_mutation_scores,
    _has_required_evidence,
    _looks_build_broken,
    _looks_gate_failed,
    _looks_no_enforceable_java_lines,
    _looks_pitest_baseline_failure,
    _looks_skipped,
    _pit_modules_without_covered_mutants,
    bounded_reasons,
    gate_miss_reasons,
    measured_gate_details,
)


def _mutation_score_below_ci_gate(output: str, mutation_gate: float) -> bool:
    if mutation_gate <= 0:
        return False
    diff_scores = _diff_mutation_scores(output)
    return bool(diff_scores) and min(diff_scores) < mutation_gate


def _gate_failure_summary(
    evidence: Optional[Dict[str, Any]],
    output: str = "",
    mutation_gate: float = 0.0,
) -> str:
    """Name the gate that missed, and say when failing tests stopped propping it up.

    A coverage number that drops once unrelated failing tests are excluded is not
    a weak generated test, and reading it as one sends repair after the wrong
    lines. "UTA test-enforcement failed" on its own says neither, so the
    measured numbers lead and the attribution follows.
    """
    summary = "UTA test-enforcement failed"
    details = gate_miss_reasons(output) or measured_gate_details(output, mutation_gate)
    if details:
        summary += ": " + bounded_reasons(details)
    excluded = (evidence or {}).get("excludedFailingTestClasses") or []
    if not excluded:
        return summary
    return (
        "%s; %d failing test class(es) unrelated to this "
        "change were excluded from both gates, so lines only they reached no "
        "longer count as covered" % (summary, len(excluded))
    )


def _classify_completed(
    cmd: List[str],
    completed: subprocess.CompletedProcess[str],
    repo_path: Path,
    evidence: Optional[Dict[str, Any]] = None,
    *,
    mutation_gate: float,
    detect_termination: bool = False,
    tooling_evidence: Callable[..., Dict[str, Any]],
) -> QualityGateResult:
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    output = f"{stdout}\n{stderr}"
    evidence = _with_filtered_target_evidence(evidence, output)
    if completed.returncode != 0 or _looks_pitest_baseline_failure(output):
        evidence = _with_surefire_failure_evidence(evidence, repo_path)
    pitest_baseline_failed = _looks_pitest_baseline_failure(output)
    if _looks_skipped(output):
        return QualityGateResult(
            status=QualityGateStatus.skipped,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary="UTA test-enforcement was skipped",
            evidence=evidence,
        )
    if _looks_gate_failed(output):
        return QualityGateResult(
            status=QualityGateStatus.failed,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary=_gate_failure_summary(evidence, output, mutation_gate),
            evidence=evidence,
        )
    if _mutation_score_below_ci_gate(output, mutation_gate):
        return QualityGateResult(
            status=QualityGateStatus.failed,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary=_gate_failure_summary(evidence, output, mutation_gate),
            evidence=evidence,
        )
    uncovered = _pit_modules_without_covered_mutants(output)
    if uncovered:
        return QualityGateResult(
            status=QualityGateStatus.failed,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary=(
                "UTA test-enforcement failed because PIT scored %d mutation(s) with no "
                "covering test, so its test strength is 100%% of nothing"
                % sum(uncovered)
            ),
            evidence=evidence,
        )
    if detect_termination and (completed.returncode < 0 or completed.returncode in (137, 143)):
        return QualityGateResult(
            status=QualityGateStatus.command_error,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary="UTA test-enforcement was terminated by a signal before evidence was produced",
            evidence={
                **(evidence or {}),
                "repairEligible": False,
                "terminationSignal": (
                    -completed.returncode
                    if completed.returncode < 0
                    else completed.returncode - 128
                ),
            },
        )
    if _looks_build_broken(output):
        evidence = tooling_evidence(repo_path, cmd, evidence)
        return QualityGateResult(
            status=QualityGateStatus.failed,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary="UTA test-enforcement failed because Maven build did not compile or resolve",
            evidence=evidence,
        )
    if pitest_baseline_failed:
        return QualityGateResult(
            status=QualityGateStatus.failed,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary="UTA test-enforcement failed because PIT baseline tests were not green",
            evidence=evidence,
        )
    if _has_required_evidence(output):
        summary = "UTA test-enforcement passed"
        if completed.returncode != 0:
            summary = "UTA test-enforcement passed; Maven exited non-zero after gate evidence"
        return QualityGateResult(
            status=QualityGateStatus.passed,
            passed=True,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary=summary,
            evidence=evidence,
        )
    if completed.returncode == 0 and _looks_no_enforceable_java_lines(output):
        return QualityGateResult(
            status=QualityGateStatus.passed,
            passed=True,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary="UTA test-enforcement passed; no changed Java source lines after filtering",
            evidence=evidence,
        )
    if completed.returncode != 0:
        return QualityGateResult(
            status=QualityGateStatus.failed,
            passed=False,
            command=cmd,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            summary="UTA test-enforcement failed before evidence was produced",
            evidence=evidence,
        )
    return QualityGateResult(
        status=QualityGateStatus.missing_evidence,
        passed=False,
        command=cmd,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        summary=MISSING_EVIDENCE_SUMMARY,
        evidence=tooling_evidence(repo_path, cmd, evidence),
    )


__all__ = ["_classify_completed", "_mutation_score_below_ci_gate"]

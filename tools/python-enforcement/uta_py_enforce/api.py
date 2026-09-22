"""Canonical language-neutral Python enforcement binding implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from uta_enforce_core.contracts import (
    EnforcementCapabilities,
    EnforcementInvocationContext,
    EnforcementRequest,
    EnforcementResult,
    EnforcementStatus,
    TargetEnforcementResult,
)
from uta_enforce_core.evidence import BACKEND, SCHEMA_VERSION, finalize

from .coverage import run_coverage
from .dependency_overlay import dependency_overlay_env, prepare_dependency_overlay
from .mutation import empty_mutation, run_mutation
from .runtime import check_python_syntax_compatibility, resolve_python_runtime, run_command
from .targets import (
    changed_lines_for_targets,
    has_only_non_executable_changed_lines,
    non_executable_target_evidence,
    resolve_enforcement_targets,
)
from .test_selection import strict_test_candidates


class PythonEnforcementBinding:
    """Canonical Python enforcement binding implementing the sole neutral contract."""

    language = "python"

    def capabilities(self) -> EnforcementCapabilities:
        return EnforcementCapabilities(
            language="python",
            supports_coverage=True,
            supports_mutation=True,
            supported_runtime_versions=("python3", "python2"),
            accepts_sampling_policy=True,
        )

    def enforce(
        self,
        request: EnforcementRequest,
        context: EnforcementInvocationContext,
    ) -> EnforcementResult:
        repo = Path(request.repo_path).resolve()
        syntax_version = request.runtime.syntax_version or "python3"
        runtime = resolve_python_runtime(
            syntax_version=syntax_version,
            python_bin=request.runtime.python_executable,
        )

        targets = resolve_enforcement_targets(repo, request.targets, base_ref=request.base_ref)
        if not targets:
            evidence = {
                "schemaVersion": SCHEMA_VERSION,
                "backend": BACKEND,
                "status": "passed",
                "reasonCode": "no_targets",
                "message": "No matching production Python targets found",
                "environmentProfile": runtime.environment_profile,
                "commands": [],
            }
            return EnforcementResult(
                status=EnforcementStatus.PASSED,
                evidence=finalize(evidence),
                target_results=(),
            )

        changed_lines = changed_lines_for_targets(repo, request.base_ref, targets)

        # Pre-check syntax compatibility if running under python3
        source_paths = [t.source_path for t in targets if t.source_path]
        if not runtime.is_python2:
            syntax_fail = check_python_syntax_compatibility(repo, source_paths, target_syntax="python3")
            if syntax_fail:
                failed_file, err_msg = syntax_fail
                evidence = {
                    "schemaVersion": SCHEMA_VERSION,
                    "backend": BACKEND,
                    "status": "error",
                    "reasonCode": "syntax_error",
                    "message": f"Syntax error in {failed_file}: {err_msg}",
                    "environmentProfile": runtime.environment_profile,
                    "commands": [],
                }
                return EnforcementResult(
                    status=EnforcementStatus.ERROR,
                    evidence=finalize(evidence),
                    target_results=(),
                )

        commands: list[dict[str, Any]] = []
        target_results: list[TargetEnforcementResult] = []
        target_evidences: list[dict[str, Any]] = []

        timeout = request.runtime.timeout_seconds
        cov_gate = (request.quality_gates.diff_coverage_min or 0.0) * 100.0 if request.quality_gates.diff_coverage_min is not None else 0.0
        mut_gate = (request.quality_gates.diff_mutation_min or 0.0) * 100.0 if request.quality_gates.diff_mutation_min is not None else 0.0
        mutation_requested = request.quality_gates.diff_mutation_min is not None

        for target in targets:
            src = target.source_path
            src_changed = changed_lines.get(src, [])

            # Check non-executable shortcut
            if src_changed and has_only_non_executable_changed_lines(repo, src, src_changed):
                target_ev = non_executable_target_evidence(src, src_changed, coverage_gate=cov_gate, mutation_gate=mut_gate)
                target_evidences.append(target_ev)
                target_results.append(
                    TargetEnforcementResult(
                        target=target,
                        status=EnforcementStatus.PASSED,
                        evidence=target_ev,
                    )
                )
                continue

            # Test selection
            selected_tests = strict_test_candidates(
                repo,
                src,
                configured=target.test_paths or request.test_paths,
            )
            if not selected_tests:
                target_ev = {
                    "status": "failed",
                    "reasonCode": "missing_test_file",
                    "message": f"No unit test file found targeting {src}",
                    "coverage": {
                        "covered": 0,
                        "total": len(src_changed),
                        "rate": 0.0,
                        "gate": cov_gate,
                        "passed": False,
                        "scope": "changed_lines",
                        "changedLines": {src: list(src_changed)},
                    },
                    "mutation": {
                        "runtimeLane": "not_run",
                        "generated": 0,
                        "killed": 0,
                        "survived": 0,
                        "rate": 0.0,
                        "gate": mut_gate,
                        "passed": False,
                        "scope": "changed_lines",
                        "changedLines": {src: list(src_changed)},
                    },
                    "artifacts": {},
                }
                target_evidences.append(target_ev)
                target_results.append(
                    TargetEnforcementResult(
                        target=target,
                        status=EnforcementStatus.FAILED,
                        evidence=target_ev,
                    )
                )
                continue

            # Legacy parity: walk the strict candidates until one passes.
            # A target with two strict candidate tests passes if either
            # verifies it; stopping at the first would fail targets the
            # lane it replaces certifies, on nothing but candidate order.
            first_candidate: dict[str, Any] | None = None
            candidate_results: list[dict[str, Any]] = []
            target_ev = {}
            for candidate_test in selected_tests:
                # Where this target's commands start in the shared list, so its own
                # evidence can carry the commands its verification issued.
                commands_start = len(commands)

                # Prepare dependency overlay if present
                if request.runtime.dependency_overlay_enabled:
                    dep_evidence, dep_dir, _dep_digest = prepare_dependency_overlay(
                        repo,
                        src,
                        test_paths=(candidate_test,),
                        python_bin=runtime.python_bin,
                        timeout_seconds=timeout,
                        artifact_dir=".uta_cache/python-enforcement",
                        runner=lambda cmd, cwd=None, timeout=None, env=None: run_command(
                            "dependency_overlay", cmd, cwd or repo, timeout or 60, commands, env=env
                        ),
                    )
                    if dep_evidence and (not commands or commands[-1] is not dep_evidence):
                        commands.append(dep_evidence)
                    execution_env = dependency_overlay_env(dep_dir)
                else:
                    dep_evidence = None
                    execution_env = {}

                # Run coverage
                cov_summary = run_coverage(
                    repo,
                    src,
                    candidate_test,
                    changed_lines,
                    cov_gate,
                    runtime.python_bin,
                    timeout,
                    commands,
                    execution_env=execution_env,
                )

                cov_passed = bool(cov_summary.get("passed", False))
                tests_pass = bool(cov_summary.get("testsPass", True))
                if cov_passed and tests_pass and mutation_requested:
                    mut_summary = run_mutation(
                        repo,
                        src,
                        candidate_test,
                        changed_lines,
                        mut_gate,
                        runtime.mutmut_bin,
                        timeout,
                        commands,
                        python_bin=runtime.python_bin,
                        syntax_version=syntax_version,
                        sampling_policy=context.sampling_policy,
                        execution_env=execution_env,
                    )
                elif cov_passed and tests_pass:
                    mut_summary = empty_mutation(
                        changed_lines.get(src, ()), mut_gate, passed=True
                    )
                    mut_summary["reasonCode"] = "mutation_not_requested"
                else:
                    mut_summary = empty_mutation(
                        changed_lines.get(src, ()), mut_gate, passed=False
                    )
                    mut_summary["reasonCode"] = "coverage_not_passed"
                mut_passed = bool(mut_summary.get("passed", False))
                target_passed = cov_passed and mut_passed

                target_artifacts: dict[str, Any] = {}
                if isinstance(cov_summary.get("artifacts"), Mapping):
                    target_artifacts.update(cov_summary["artifacts"])
                if isinstance(mut_summary.get("artifacts"), Mapping):
                    target_artifacts.update(mut_summary["artifacts"])
                # A failed overlay install returns the same shape as a good one,
                # so the run continued against an empty directory and whatever
                # the verifier venv happened to hold. That is not the declared
                # dependency set, and every downstream failure it causes is
                # reported as something else -- a mutmut backend error, a
                # collection error -- with the actual cause buried in pip output
                # nothing reads. Naming it here does not change the verdict; it
                # makes the verdict legible.
                if dep_evidence and int(dep_evidence.get("exitCode") or 0) != 0:
                    target_artifacts["dependency_overlay"] = "install_failed"

                status = EnforcementStatus.PASSED if target_passed else EnforcementStatus.FAILED
                target_ev = {
                    "status": status.value,
                    # Order matters: a test that failed is a test failure, not a
                    # coverage failure -- coverage is zero *because* nothing ran.
                    # And where a stage reported *why* it failed, that reason is
                    # kept: "mutation_failed" for a mutmut workspace that could not
                    # collect tests tells the reader nothing they can act on.
                    "reasonCode": (
                        "passed" if target_passed
                        else "test_failed" if not tests_pass
                        else str(cov_summary.get("reasonCode") or "coverage_failed") if not cov_passed
                        else str(mut_summary.get("reasonCode") or "mutation_failed")
                    ),
                    "message": _failure_message(
                        cov_summary, mut_summary,
                        tests_pass=tests_pass, cov_passed=cov_passed,
                        cov_gate=cov_gate, mut_gate=mut_gate,
                    ) if not target_passed else "",
                    "testsPass": tests_pass,
                    "coverage": cov_summary,
                    "mutation": mut_summary,
                    "selectedTest": candidate_test,
                    "commands": commands[commands_start:],
                    "artifacts": target_artifacts,
                }
                candidate_results.append(
                    {
                        "testPaths": [candidate_test],
                        "status": target_ev["status"],
                        "reasonCode": target_ev["reasonCode"],
                        "testsPass": target_ev["testsPass"],
                        "message": target_ev["message"],
                        "coverage": cov_summary,
                        "mutation": mut_summary,
                    }
                )
                if first_candidate is None:
                    first_candidate = target_ev
                if target_passed:
                    break
            else:
                # None passed. The first candidate's result is the
                # target's, so the reported failure is the one a reader
                # can act on rather than whichever ran last.
                target_ev = first_candidate or target_ev

            target_ev["candidateResults"] = candidate_results
            target_ev["candidateTestPaths"] = list(selected_tests)
            status = (
                EnforcementStatus.PASSED
                if target_ev["status"] == EnforcementStatus.PASSED.value
                else EnforcementStatus.FAILED
            )
            target_evidences.append(target_ev)
            target_results.append(
                TargetEnforcementResult(
                    target=target,
                    status=status,
                    evidence=target_ev,
                )
            )

        # Aggregate overall outcome
        all_passed = all(tr.status == EnforcementStatus.PASSED for tr in target_results) if target_results else True
        overall_status = EnforcementStatus.PASSED if all_passed else EnforcementStatus.FAILED

        # Aggregate coverage totals
        cov_covered = sum(int((t.get("coverage") or {}).get("covered") or 0) for t in target_evidences)
        cov_total = sum(int((t.get("coverage") or {}).get("total") or 0) for t in target_evidences)
        cov_rate = 100.0 if cov_total == 0 else round((cov_covered / cov_total) * 100.0, 4)
        cov_passed = cov_rate >= cov_gate

        # Aggregate mutation totals
        mut_generated = sum(int((t.get("mutation") or {}).get("generated") or 0) for t in target_evidences)
        mut_killed = sum(int((t.get("mutation") or {}).get("killed") or 0) for t in target_evidences)
        mut_survived = sum(int((t.get("mutation") or {}).get("survived") or 0) for t in target_evidences)
        mut_denominator = mut_killed + mut_survived
        mut_rate = 100.0 if mut_denominator == 0 else round((mut_killed / mut_denominator) * 100.0, 4)
        mut_passed = mut_rate >= mut_gate

        overall_artifacts: dict[str, list[Any]] = {}
        for item in target_evidences:
            for key, value in (item.get("artifacts") or {}).items():
                overall_artifacts.setdefault(key, []).append(value)

        overall_evidence = {
            "schemaVersion": SCHEMA_VERSION,
            "backend": BACKEND,
            "status": overall_status.value,
            "reasonCode": "passed" if all_passed else "quality_gate_failed",
            "coverage": {
                "covered": cov_covered,
                "total": cov_total,
                "rate": cov_rate,
                "gate": cov_gate,
                "passed": cov_passed,
                "scope": "changed_lines",
            },
            "mutation": {
                "generated": mut_generated,
                "killed": mut_killed,
                "survived": mut_survived,
                "rate": mut_rate,
                "gate": mut_gate,
                "passed": mut_passed,
                "scope": "changed_lines",
            },
            "targets": target_evidences,
            "artifacts": overall_artifacts,
            "commands": commands,
            "environmentProfile": runtime.environment_profile,
        }

        return EnforcementResult(
            status=overall_status,
            evidence=finalize(overall_evidence),
            target_results=tuple(target_results),
        )



def _failure_message(
    coverage: Mapping[str, Any],
    mutation: Mapping[str, Any],
    *,
    tests_pass: bool,
    cov_passed: bool,
    cov_gate: float,
    mut_gate: float,
) -> str:
    """A sentence a human can act on, for the stage that actually failed.

    The wording is the frozen wording: these strings reach reports and CI
    output, so they are the compatibility surface, not decoration.
    """
    if not tests_pass:
        return "Python tests failed"
    if not cov_passed:
        rate = float(coverage.get("rate") or 0.0)
        return f"Python coverage {rate:.2f}% is below gate {cov_gate:.2f}%"

    reason = str(mutation.get("reasonCode") or "")
    if reason == "mutation_test_collection_failed":
        return "Python mutation test collection failed in the isolated mutmut workspace"
    if reason == "mutation_backend_failed":
        return (
            "Python mutation backend failed before executing "
            f"{int(mutation.get('notChecked') or 0)} selected mutants"
        )
    if reason == "mutation_no_tests":
        return (
            "Python mutation found no tests for "
            f"{int(mutation.get('noTests') or 0)} selected mutants"
        )
    rate = float(mutation.get("rate") or 0.0)
    return f"Python mutation score {rate:.2f}% is below gate {mut_gate:.2f}%"


def create_python_enforcement_binding() -> PythonEnforcementBinding:
    """Factory creating the canonical Python language enforcement binding."""
    return PythonEnforcementBinding()

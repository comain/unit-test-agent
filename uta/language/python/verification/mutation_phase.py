"""The mutation half of a Python verification run.

Reached only once pytest and the coverage gate have passed. It owns the mutmut
preflight, the changed-line mask and generation policy, the choice between the
batched and single-pass adapter routes, survivor collection, and every mutation
outcome the run can end on. It is a phase rather than an orchestrator: it is
handed the coverage evidence and the command log, and returns the finished
verification result.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from uta_py_enforce.pytest_env import mutation_pytest_env

from uta.language.python.verification.candidate_planning import (
    _candidate_plan_generated_tool_keys,
    _candidate_plan_has_eligible_opportunities,
    _candidate_plan_with_execution_evidence,
    _legacy_mutmut15_candidate_plan,
    _modern_candidate_plan_enabled,
    _persist_candidate_plan_artifact,
)
from uta.language.python.verification.evidence import (
    CoverageSummary,
    parse_mutmut_summary,
    parse_mutmut_survivors,
)
from uta.language.python.verification.models import (
    CommandEvidence,
    PythonRuntimeConfig,
    PythonVerificationResult,
    RunCommand,
)
from uta.language.python.verification.mutation_execution import (
    _annotate_mutmut_survivor_diffs,
    _candidate_plan_with_keyed_execution_evidence,
    _modern_mutmut3_candidate_plan,
    _run_batched_modern_mutation,
)
from uta.language.python.verification.mutation_policy import _write_mutmut_generation_policy
from uta.language.python.verification.mutation_scoping import (
    _MutationMask,
    _ScopedMutationCounts,
    _apply_changed_line_mutation_mask,
    _empty_mutation_summary,
    _filter_changed_lines_for_mutation,
    _mark_no_mutatable_candidates,
    _mutation_backend_reason,
    _mutation_changed_lines_empty_for_source,
    _mutation_no_scored_lines_summary,
    _mutmut_meta_mutant_count,
    _reconcile_no_test_association,
    _scope_mutation_to_changed_lines,
    _zero_mutants_means_no_candidates,
)
from uta.language.python.verification.mutmut_runtime import (
    _MutmutConfigOverlay,
    _cleanup_mutation_state,
    _is_expected_mutmut_exit,
    _mutmut_config_overlay,
    _mutmut_generate_metadata_command,
    _mutmut_process_env,
    _mutmut_run_command,
    _normalize_changed_lines,
    _normalize_relpath,
    _write_mutation_patch_file,
    _write_mutmut_import_compat,
)
from uta.language.python.verification.process import _prepend_env_path, _run_command
from uta.language.python.verification.pytest_execution import _PytestExecutionContext
from uta.language.python.verification.results import _verification_result
from uta.language.python.verification.runtime_setup import mutmut_preflight
from uta.shared.config import settings
from uta.shared.targets import TargetRef
from uta_enforce_core.mutation_candidates import all_selected_mutants_unassociated


def run_mutation_phase(
    *,
    repo: Path,
    target: TargetRef,
    source_path: str,
    test_paths: Sequence[str],
    mutation_dir: Path,
    pytest_context: _PytestExecutionContext,
    coverage: CoverageSummary,
    mutation_gate: float,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    enable_mutation_sampling: bool,
    lane: str,
    python_bin: str,
    config: PythonRuntimeConfig,
    runner: RunCommand,
    commands: List[CommandEvidence],
    setup_status: str,
    base_ref: str,
    base_commit: str,
    head_commit: str,
    repo_url: str,
) -> PythonVerificationResult:
    """Run mutation for one target and return the finished verification result."""
    preflight = mutmut_preflight(
        repo=repo,
        lane=lane,
        python_bin=python_bin,
        config=config,
        runner=runner,
        commands=commands,
        coverage=coverage,
        setup_status=setup_status,
    )
    if preflight.failure is not None:
        return preflight.failure
    mutmut_bin = preflight.mutmut_bin
    mutmut_version_output = preflight.version_output
    legacy_candidate_plan = (
        _legacy_mutmut15_candidate_plan(
            repo=repo,
            target=target,
            source_path=source_path,
            test_paths=test_paths,
            changed_lines=changed_lines,
            mutmut_version=mutmut_version_output,
            config=config,
            base_ref=base_ref,
            base_commit=base_commit,
            head_commit=head_commit,
            repo_url=repo_url,
        )
        if lane == "mutmut-legacy-py2"
        else {}
    )

    _cleanup_mutation_state(repo)
    mutation_dir.mkdir(parents=True, exist_ok=True)
    source_file = repo / source_path
    mutation_changed_lines = changed_lines
    mutation_sampling: Dict[str, Any] = {}
    mutation_changed_lines = _filter_changed_lines_for_mutation(source_file, mutation_changed_lines, source_path)
    if _mutation_changed_lines_empty_for_source(mutation_changed_lines, source_path):
        mutation = _mutation_no_scored_lines_summary(
            source_path=source_path,
            changed_lines=mutation_changed_lines,
            gate=mutation_gate,
            runtime_lane=lane,
            sampling=mutation_sampling,
        )
        if legacy_candidate_plan:
            mutation = replace(mutation, candidate_plan=legacy_candidate_plan)
        return _verification_result(
            "passed",
            "passed",
            tests_pass=True,
            coverage=coverage,
            mutation=mutation,
            commands=commands,
            message="Python pytest and coverage gates passed; mutation skipped because changed lines are low-value side effects",
            config=config,
            setup_status=setup_status,
        )
    mutation_mask = _MutationMask(source_file)
    mutmut_config: Optional[_MutmutConfigOverlay] = None
    scoped_counts: Optional[_ScopedMutationCounts] = None
    modern_candidate_plan: Dict[str, Any] = {}
    candidate_plan_failure = ""
    candidate_plan_failure_reason = "mutation_candidate_plan_failed"
    batched_execution_done = False
    mutmut_meta_count: Optional[int] = None
    try:
        mutation_mask = (
            _apply_changed_line_mutation_mask(source_file, mutation_changed_lines, source_path)
            if lane == "mutmut-legacy-py2"
            else _MutationMask(source_file, scoped=_normalize_changed_lines(mutation_changed_lines) is not None)
        )
        import_compat_dir = _write_mutmut_import_compat(mutation_dir, repo=repo, source_path=source_path)
        mutmut_config = _mutmut_config_overlay(
            repo,
            source_path,
            test_paths,
            mutmut_version_output,
            python_bin=python_bin,
            import_compat_dir=import_compat_dir,
            support_test_paths=test_paths,
        )
        mutation_patch = _write_mutation_patch_file(
            mutation_dir,
            source_file=source_file,
            source_path=source_path,
            changed_lines=mutation_changed_lines,
            enabled=not mutmut_config.configured,
        )
        mutmut_config.apply()
        mutation_env = _mutmut_process_env(
            repo,
            source_path=source_path,
            import_compat_dir=import_compat_dir,
        )
        for key, value in pytest_context.env_overrides.items():
            if key == "PYTHONPATH":
                mutation_env[key] = _prepend_env_path(mutation_env.get(key, ""), value)
            else:
                mutation_env[key] = value
        # Mutmut executes selected tests from its generated ``mutants/`` tree.
        # Use the same environment builder as standalone CI so imports resolve
        # the mutant module before the original repository module.
        mutation_env = mutation_pytest_env(
            repo / "mutants",
            test_paths,
            mutation_env,
        )
        artifacts: Dict[str, str] = {}
        if mutation_mask.scoped:
            artifacts["mutation_scope"] = "changed_lines"
        if mutation_patch:
            artifacts["mutation_patch"] = str(mutation_patch)
        artifacts["import_compat"] = str(import_compat_dir)
        use_modern_candidate_plan = lane != "mutmut-legacy-py2" and _modern_candidate_plan_enabled(mutation_changed_lines)
        mutation_result: CommandEvidence = CommandEvidence(name="mutmut_not_required", command=[], exit_code=0)
        mutation_output = ""
        mutation = _empty_mutation_summary(gate=mutation_gate, runtime_lane=lane)
        if use_modern_candidate_plan:
            policy_result = _write_mutmut_generation_policy(
                mutation_dir,
                source_file=source_file,
                source_path=source_path,
                changed_lines=mutation_changed_lines,
                covered_lines=mutation_changed_lines.get(_normalize_relpath(source_path), set()) if coverage.passed else set(),
                use_ci_cap_profile=enable_mutation_sampling,
            )
            policy_path = Path(str(policy_result.get("path") or ""))
            if policy_path.exists():
                artifacts["mutmut_generation_policy"] = str(policy_path)
            generation_strategy = str(
                getattr(settings, "python_mutation_generation_strategy", "hard_cap") or "hard_cap"
            )
            artifacts["mutation_generation_strategy"] = generation_strategy
            if generation_strategy == "batch" and not candidate_plan_failure and not enable_mutation_sampling:
                # Batched generation (ADR-002): partition selected opportunities into
                # byte-budgeted, function-granular batches and generate+score each in turn.
                mutation, modern_candidate_plan, _batch_warnings = _run_batched_modern_mutation(
                    repo=repo,
                    target=target,
                    source_path=source_path,
                    source_file=source_file,
                    test_paths=test_paths,
                    coverage=coverage,
                    mutmut_version=mutmut_version_output,
                    config=config,
                    base_ref=base_ref,
                    base_commit=base_commit,
                    head_commit=head_commit,
                    repo_url=repo_url,
                    mutation_dir=mutation_dir,
                    python_bin=python_bin,
                    mutmut_bin=mutmut_bin,
                    runner=runner,
                    mutation_env=mutation_env,
                    mutation_gate=mutation_gate,
                    lane=lane,
                    full_policy=policy_result,
                    changed_lines=mutation_changed_lines,
                    commands=commands,
                    artifacts=artifacts,
                )
                batched_execution_done = True
                if _batch_warnings:
                    artifacts["mutation_batch_warnings"] = " | ".join(_batch_warnings)
            else:
                metadata_cmd = _mutmut_generate_metadata_command(
                    python_bin,
                    max_children=int(getattr(settings, "python_mutation_max_children", 0) or 1),
                    policy_path=policy_path if policy_path.exists() else None,
                )
                if not candidate_plan_failure:
                    metadata_timeout = int(
                        getattr(settings, "python_mutation_adapter_generation_timeout_seconds", 0)
                        or config.timeout_seconds
                    )
                    metadata_result = _run_command(
                        "mutmut_generate_metadata",
                        metadata_cmd,
                        repo,
                        metadata_timeout,
                        runner,
                        env_overrides=mutation_env,
                    )
                    commands.append(metadata_result)
                    metadata_output = "\n".join(part for part in (metadata_result.stdout, metadata_result.stderr) if part)
                    metadata_output_path = mutation_dir / "mutmut-metadata-output.txt"
                    metadata_output_path.write_text(metadata_output, encoding="utf-8")
                    artifacts["mutmut_metadata_output"] = str(metadata_output_path)
                    if metadata_result.exit_code != 0 and _mutmut_meta_mutant_count(repo, source_path) is None:
                        candidate_plan_failure = metadata_output or "Python mutation candidate metadata generation failed"
                    modern_candidate_plan = _modern_mutmut3_candidate_plan(
                        repo=repo,
                        target=target,
                        source_path=source_path,
                        test_paths=test_paths,
                        changed_lines=mutation_changed_lines,
                        coverage=coverage,
                        mutmut_version=mutmut_version_output,
                        config=config,
                        enable_ci_sampling=enable_mutation_sampling,
                        base_ref=base_ref,
                        base_commit=base_commit,
                        head_commit=head_commit,
                        repo_url=repo_url,
                        generation_policy=policy_result,
                    )
        elif lane == "mutmut-legacy-py2":
            mutation_cmd = _mutmut_run_command(
                mutmut_bin,
                source_path,
                mutmut_config.configured,
                repo=repo,
                python_bin=python_bin,
                test_paths=pytest_context.test_paths,
                import_compat_dir=import_compat_dir,
                patch_file=mutation_patch,
            )
            mutation_result = _run_command(
                "mutmut_run",
                mutation_cmd,
                repo,
                config.timeout_seconds,
                runner,
                env_overrides=mutation_env,
            )
            commands.append(mutation_result)
            mutation_output = "\n".join(part for part in (mutation_result.stdout, mutation_result.stderr) if part)
            output_path = mutation_dir / "mutmut-output.txt"
            output_path.write_text(mutation_output, encoding="utf-8")
            artifacts["mutmut_output"] = str(output_path)
            mutation = parse_mutmut_summary(mutation_output, gate=mutation_gate, runtime_lane=lane)
            if legacy_candidate_plan:
                mutation = replace(mutation, candidate_plan=legacy_candidate_plan)
        else:
            candidate_plan_failure = "Python 3 mutation verification requires changed-line candidate-plan context"
            candidate_plan_failure_reason = "mutation_candidate_plan_missing_context"
        generated_candidate_keys = _candidate_plan_generated_tool_keys(modern_candidate_plan)
        planning_error = str(modern_candidate_plan.get("planningError") or "").strip()
        if planning_error:
            candidate_plan_failure = f"Python mutation candidate plan failed: {planning_error}"
            candidate_plan_failure_reason = "mutation_candidate_plan_failed"
        elif (
            modern_candidate_plan
            and not generated_candidate_keys
            and not batched_execution_done
            and _candidate_plan_has_eligible_opportunities(modern_candidate_plan)
        ):
            generated_meta_count = _mutmut_meta_mutant_count(repo, source_path)
            if generated_meta_count in {None, 0}:
                mutation = _mark_no_mutatable_candidates(
                    mutation,
                    source_path=source_path,
                    changed_lines=mutation_changed_lines,
                )
            else:
                candidate_plan_failure = (
                    "Python mutation candidate plan could not map adapter-generated mutmut metadata to changed-line candidates"
                )
                candidate_plan_failure_reason = "mutation_candidate_plan_failed"
        if generated_candidate_keys and not batched_execution_done:
            adapter_cmd = _mutmut_generate_metadata_command(
                python_bin,
                max_children=int(getattr(settings, "python_mutation_max_children", 0) or 1),
                policy_path=policy_path if policy_path.exists() else None,
                mode="run",
            )
            selected_timeout = int(
                getattr(settings, "python_mutation_selected_execution_timeout_seconds", 0)
                or config.timeout_seconds
            )
            adapter_result = _run_command(
                "mutmut_run_adapter_filtered",
                adapter_cmd,
                repo,
                selected_timeout,
                runner,
                env_overrides=mutation_env,
            )
            commands.append(adapter_result)
            adapter_output = "\n".join(part for part in (adapter_result.stdout, adapter_result.stderr) if part)
            adapter_output_path = mutation_dir / "mutmut-adapter-filtered-output.txt"
            adapter_output_path.write_text(adapter_output, encoding="utf-8")
            artifacts["mutmut_output"] = str(adapter_output_path)
            artifacts["mutation_generated_keys"] = ",".join(generated_candidate_keys)
            mutation_result = adapter_result
            mutation_output = adapter_output
            mutation = parse_mutmut_summary(mutation_output, gate=mutation_gate, runtime_lane=lane)
        survivors: List[Dict[str, Any]] = []
        if mutation.survived > 0 and not batched_execution_done:
            results = _run_command(
                "mutmut_results",
                [mutmut_bin, "results"],
                repo,
                config.timeout_seconds,
                runner,
                env_overrides=mutation_env,
            )
            commands.append(results)
            results_output = "\n".join(part for part in (results.stdout, results.stderr) if part)
            results_path = mutation_dir / "mutmut-results.txt"
            results_path.write_text(results_output, encoding="utf-8")
            artifacts["mutmut_results"] = str(results_path)
            survivors = parse_mutmut_survivors(results_output)
            survivors = _annotate_mutmut_survivor_diffs(
                survivors,
                mutmut_bin=mutmut_bin,
                repo=repo,
                source_path=source_path,
                timeout=config.timeout_seconds,
                runner=runner,
                env_overrides=mutation_env,
            )
            survivors_path = mutation_dir / "survivors.json"
            survivors_path.write_text(json.dumps(survivors, indent=2, sort_keys=True), encoding="utf-8")
            artifacts["survivors"] = str(survivors_path)
        if batched_execution_done:
            # Batch mode already aggregated survivors across batches; keep them.
            mutation = replace(mutation, artifacts=artifacts)
        else:
            mutation = replace(mutation, survivors=survivors, artifacts=artifacts)
            mutmut_meta_count = _mutmut_meta_mutant_count(repo, source_path)
            mutation = _reconcile_no_test_association(
                mutation,
                output=mutation_output,
                metadata_count=mutmut_meta_count,
            )
        if generated_candidate_keys:
            scoped_counts = _ScopedMutationCounts(
                generated=mutation.generated,
                killed=mutation.killed,
                survived=mutation.survived,
                no_tests=mutation.no_tests,
                timeout=mutation.timeout,
                suspicious=mutation.suspicious,
                skipped=mutation.skipped,
                no_coverage=mutation.no_coverage,
            )
        if modern_candidate_plan:
            if not batched_execution_done:
                modern_candidate_plan = _candidate_plan_with_keyed_execution_evidence(
                    modern_candidate_plan,
                    repo=repo,
                    source_path=source_path,
                    mutmut_bin=mutmut_bin,
                    timeout=config.timeout_seconds,
                    runner=runner,
                    env_overrides=mutation_env,
                )
            modern_candidate_plan = _candidate_plan_with_execution_evidence(
                modern_candidate_plan,
                mutation,
            )
            modern_candidate_plan = _persist_candidate_plan_artifact(repo, mutation_dir, modern_candidate_plan)
            mutation = replace(mutation, candidate_plan=modern_candidate_plan)
    finally:
        if mutmut_config is not None:
            mutmut_config.restore()
        mutation_mask.restore()
    if candidate_plan_failure:
        # Empty mutation evidence is not a perfect score when planning failed.
        # Mark it failed before projecting it into CI or repair task records.
        mutation = replace(mutation, rate=0.0, passed=False, artifacts=artifacts)
        if modern_candidate_plan:
            mutation = replace(mutation, candidate_plan=modern_candidate_plan)
        _cleanup_mutation_state(repo)
        return _verification_result(
            "failed",
            candidate_plan_failure_reason,
            tests_pass=True,
            coverage=coverage,
            mutation=mutation,
            commands=commands,
            message=candidate_plan_failure,
            config=config,
            setup_status=setup_status,
        )
    candidate_plan_generated_no_mutants = (
        bool(modern_candidate_plan)
        and not generated_candidate_keys
        and _candidate_plan_has_eligible_opportunities(modern_candidate_plan)
        and mutmut_meta_count is None
    )
    if batched_execution_done and mutation.generated <= 0 and _is_expected_mutmut_exit(mutation_result.exit_code):
        mutation = _mark_no_mutatable_candidates(mutation, source_path=source_path, changed_lines=changed_lines)
        if modern_candidate_plan:
            mutation = replace(mutation, candidate_plan=modern_candidate_plan)
        _cleanup_mutation_state(repo)
        return _verification_result(
            "passed",
            "passed",
            tests_pass=True,
            coverage=coverage,
            mutation=mutation,
            commands=commands,
            message="Python pytest, coverage, and mutation gates passed with no mutatable candidates",
            config=config,
            setup_status=setup_status,
        )
    if mutation.generated <= 0 and _is_expected_mutmut_exit(mutation_result.exit_code):
        if (
            _zero_mutants_means_no_candidates(repo / source_path, source_path, changed_lines, mutation_output)
            or mutmut_meta_count == 0
            or candidate_plan_generated_no_mutants
        ):
            mutation = _mark_no_mutatable_candidates(mutation, source_path=source_path, changed_lines=changed_lines)
            if legacy_candidate_plan:
                mutation = replace(mutation, candidate_plan=legacy_candidate_plan)
            if modern_candidate_plan:
                mutation = replace(mutation, candidate_plan=modern_candidate_plan)
            _cleanup_mutation_state(repo)
            return _verification_result(
                "passed",
                "passed",
                tests_pass=True,
                coverage=coverage,
                mutation=mutation,
                commands=commands,
                message="Python pytest, coverage, and mutation gates passed with no mutatable candidates",
                config=config,
                setup_status=setup_status,
            )
    if mutation.generated <= 0 or not _is_expected_mutmut_exit(mutation_result.exit_code):
        _cleanup_mutation_state(repo)
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
        changed_lines=mutation_changed_lines,
        mutation_masked=mutation_mask.scoped,
        scoped_counts=scoped_counts,
        sampling=mutation_sampling,
    )
    if legacy_candidate_plan:
        legacy_candidate_plan = _candidate_plan_with_execution_evidence(
            legacy_candidate_plan,
            mutation,
        )
        legacy_candidate_plan = _persist_candidate_plan_artifact(repo, mutation_dir, legacy_candidate_plan)
        mutation = replace(mutation, candidate_plan=legacy_candidate_plan)
    if all_selected_mutants_unassociated(
        generated=mutation.changed_line_mutants_generated or mutation.generated,
        scored=mutation.changed_line_mutants_scored,
        no_tests=mutation.no_tests,
    ):
        # Generated candidates with no test association are verifier evidence
        # loss, not a vacuous 100% mutation score. Keep this classification in
        # parity with the standalone CI enforcer.
        mutation = replace(mutation, rate=0.0, passed=False)
        _cleanup_mutation_state(repo)
        return _verification_result(
            "failed",
            "mutation_no_tests",
            tests_pass=True,
            coverage=coverage,
            mutation=mutation,
            commands=commands,
            message=(
                "Python mutation execution could not associate any selected test "
                f"with {mutation.no_tests} generated mutant(s)"
            ),
            config=config,
            setup_status=setup_status,
        )
    if not mutation.passed:
        _cleanup_mutation_state(repo)
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
    _cleanup_mutation_state(repo)
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

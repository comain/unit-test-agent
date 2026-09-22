"""Mutation execution: survivor evidence and the batched generation pass.

The two parts of running mutants that are substantial enough to own their own
module. Survivor annotation turns raw mutmut results into per-mutant diffs
under an explicit call/time budget, preferring the in-process diff over
spawning ``mutmut show``. The batched pass (ADR-002) partitions the selected
opportunities into byte-budgeted, function-granular batches, generates and
scores each in turn, and aggregates -- including the fail-closed parity rules
that keep a batch run from passing silently on zero mutants.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import shlex
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from uta_py_enforce.mutation import MUTMUT_STATUS_BY_EXIT_CODE
from uta_py_enforce.mutmut_adapter_runtime import ADAPTER_TIMEOUT_ENV
from uta.enforcement.mutation_candidates import candidate_plan_requires_mutation_execution
from uta.language.python.mutation_batching import (
    aggregate_batch_candidate_plans,
    partition_units_into_batches,
)
from uta.language.python.mutation_candidates import build_mutmut3_candidate_plan_from_meta
from uta.language.python.verification.candidate_planning import (
    _candidate_plan_generated_tool_keys,
    _candidate_plan_with_execution_evidence,
    _filter_generation_policy_to_lines,
    _generated_module_bytes,
    _modern_mutmut3_candidate_plan as _build_modern_mutmut3_candidate_plan,
    _mutation_units_from_generation_policy,
)
from uta.language.python.verification.evidence import (
    CoverageSummary,
    MutationSummary,
    parse_mutmut_summary,
    parse_mutmut_survivors,
)
from uta.language.python.verification.models import PythonRuntimeConfig, RunCommand
from uta.language.python.verification.mutation_scoping import (
    _empty_mutation_summary,
    _mutmut_meta_mutant_count,
    _reconcile_no_test_association,
)
from uta.language.python.verification.mutmut_runtime import (
    _clean_generated_mutants,
    _is_expected_mutmut_exit,
    _mutmut_generate_metadata_command,
    _normalize_relpath,
)
from uta.language.python.verification.process import _run_command
from uta.shared.config import settings
from uta.shared.targets import TargetRef


@dataclass
class _MutmutShowBudget:
    max_calls: int
    total_seconds: int
    started_at: float = field(default_factory=time.monotonic)
    calls: int = 0
    exhausted_reason: str = ""

    def reserve_timeout(self, per_call_timeout: int) -> Optional[int]:
        if self.max_calls >= 0 and self.calls >= self.max_calls:
            self.exhausted_reason = f"max mutmut show calls reached ({self.max_calls})"
            return None
        remaining = max(0.0, float(self.total_seconds) - (time.monotonic() - self.started_at))
        if remaining <= 0:
            self.exhausted_reason = f"mutmut show total budget exhausted ({self.total_seconds}s)"
            return None
        self.calls += 1
        return max(1, min(int(per_call_timeout), int(remaining)))


def _adapter_self_abort_seconds(command_timeout: int) -> int:
    """When the adapter should dump and exit, given the budget we reap it at.

    Strictly inside the command timeout, so the adapter always wins the race and
    the failure arrives as a thread dump naming the wedged frame rather than as
    a bare exit 124. The margin is 5%, floored at 30s so short budgets keep a
    usable gap and capped at 300s so a long budget does not give away minutes.

    Returns 0 -- disabled -- for budgets too short to carve a margin out of.
    """
    budget = max(int(command_timeout), 0)
    if budget <= 60:
        return 0
    margin = min(max(30, budget // 20), 300)
    return max(budget - margin, 30)


def _mutmut_execution_status_by_key(repo: Path, source_path: str) -> Dict[str, str]:
    """Read mutmut's authoritative per-key outcomes before generated files are removed."""
    meta_path = Path(repo) / "mutants" / f"{_normalize_relpath(source_path)}.meta"
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = payload.get("exit_code_by_key")
    if not isinstance(raw, Mapping):
        return {}
    statuses: Dict[str, str] = {}
    for key, exit_code in raw.items():
        try:
            normalized_code = int(exit_code)
        except (TypeError, ValueError):
            continue
        status = MUTMUT_STATUS_BY_EXIT_CODE.get(normalized_code)
        if status:
            statuses[str(key)] = status
    return statuses


def _candidate_plan_with_keyed_execution_evidence(
    candidate_plan: Mapping[str, Any],
    *,
    repo: Path,
    source_path: str,
    mutmut_bin: str,
    timeout: int,
    runner: RunCommand,
    env_overrides: Optional[Mapping[str, str]],
) -> Dict[str, Any]:
    """Persist exact outcome and failed-mutant diffs while mutmut materialization exists."""
    plan = dict(candidate_plan)
    selected = plan.get("activeSelected")
    if not isinstance(selected, list):
        return plan
    status_by_key = _mutmut_execution_status_by_key(repo, source_path)
    if not status_by_key:
        return plan

    failed: List[Dict[str, Any]] = []
    for item in selected:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("toolCandidateKey") or "").strip()
        status = status_by_key.get(key)
        if status not in {"timeout", "suspicious"}:
            continue
        opportunity = item.get("opportunity") if isinstance(item.get("opportunity"), Mapping) else {}
        failed.append(
            {
                "id": key,
                "file": str(opportunity.get("sourcePath") or source_path),
                "line": int(opportunity.get("line") or 0),
                "description": f"{status} mutation candidate",
            }
        )
    annotated = _annotate_mutmut_survivor_diffs(
        failed,
        mutmut_bin=mutmut_bin,
        repo=repo,
        source_path=source_path,
        timeout=timeout,
        runner=runner,
        env_overrides=env_overrides,
        limit=len(failed),
    ) if failed else []
    annotation_by_key = {str(item.get("id") or ""): item for item in annotated}

    enriched: List[Any] = []
    for raw_item in selected:
        if not isinstance(raw_item, Mapping):
            enriched.append(raw_item)
            continue
        item = dict(raw_item)
        key = str(item.get("toolCandidateKey") or "").strip()
        status = status_by_key.get(key)
        if status:
            item["executionStatus"] = status
        annotation = annotation_by_key.get(key) or {}
        show_command = str(annotation.get("mutmut_show_command") or "").strip()
        show_output = str(annotation.get("mutmut_show_output") or "").strip()
        if show_command:
            item["mutmutShowCommand"] = show_command
        if show_output:
            item["mutmutShowOutput"] = show_output
        enriched.append(item)
    plan["activeSelected"] = enriched
    plan["executionStatusByToolCandidateKey"] = {
        key: status for key, status in status_by_key.items() if key in _candidate_plan_generated_tool_keys(plan)
    }
    return plan


def _annotate_mutmut_survivor_diffs(
    survivors: Sequence[Dict[str, Any]],
    *,
    mutmut_bin: str,
    repo: Path,
    source_path: str = "",
    timeout: int,
    runner: RunCommand,
    env_overrides: Optional[Mapping[str, str]] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    annotated: List[Dict[str, Any]] = []
    configured_limit = max(0, int(settings.python_mutant_verify_show_max_calls))
    effective_limit = min(max(0, int(limit)), configured_limit)
    show_timeout = max(1, min(int(timeout or 60), int(settings.python_mutant_verify_show_timeout_seconds or 10)))
    budget = _MutmutShowBudget(
        max_calls=effective_limit,
        total_seconds=max(1, int(settings.python_mutant_verify_show_total_timeout_seconds or show_timeout)),
    )
    internal_show_cache: Dict[str, Tuple[Any, Any]] = {}
    for index, survivor in enumerate(survivors):
        item = dict(survivor)
        mutant_id = str(item.get("id") or item.get("mutant") or "").strip()
        if mutant_id and index < effective_limit:
            internal_diff = _internal_mutmut_survivor_diff(
                repo,
                item,
                source_path=source_path,
                module_cache=internal_show_cache,
            )
            if internal_diff:
                item["mutmut_show_command"] = str(internal_diff.get("command") or "").strip()
                item["mutmut_show_output"] = _compact_mutmut_show_output(str(internal_diff.get("output") or ""))
                annotated.append(item)
                continue
            show_call_timeout = budget.reserve_timeout(show_timeout)
            if show_call_timeout is None:
                item["mutmut_show_truncated"] = budget.exhausted_reason or "mutmut show budget exhausted"
                annotated.append(item)
                continue
            command = [mutmut_bin, "show", mutant_id]
            result = _run_command(
                "mutmut_show",
                command,
                repo,
                show_call_timeout,
                runner,
                env_overrides=env_overrides,
            )
            output = _compact_mutmut_show_output("\n".join(part for part in (result.stdout, result.stderr) if part))
            if result.exit_code == 0 and output:
                item["mutmut_show_command"] = " ".join(shlex.quote(part) for part in command)
                item["mutmut_show_output"] = output
        annotated.append(item)
    return annotated


def _internal_mutmut_survivor_diff(
    repo: Path,
    survivor: Mapping[str, Any],
    *,
    source_path: str,
    module_cache: Dict[str, Tuple[Any, Any]],
) -> Optional[Dict[str, str]]:
    if not source_path:
        return None
    try:
        from uta.language.python.mutation_context import _mutmut_internal_show_diff

        return _mutmut_internal_show_diff(
            repo,
            survivor,
            source_path=source_path,
            module_cache=module_cache,
        )
    except Exception:
        return None


def _compact_mutmut_show_output(output: str, *, max_lines: int = 80) -> str:
    lines = []
    for line in str(output or "").splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if "/" in stripped and any(token in stripped for token in ("🎉", "🫥", "🙁", "⏰", "🤔")):
            continue
        lines.append(line)
    return "\n".join(lines[:max_lines]).strip()


def _modern_mutmut3_candidate_plan(
    *,
    repo: Path,
    target: TargetRef,
    source_path: str,
    test_paths: Sequence[str],
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    coverage: CoverageSummary,
    mutmut_version: str,
    config: PythonRuntimeConfig,
    enable_ci_sampling: bool,
    base_ref: str = "",
    base_commit: str = "",
    head_commit: str = "",
    repo_url: str = "",
    generation_policy: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return _build_modern_mutmut3_candidate_plan(
        repo=repo,
        target=target,
        source_path=source_path,
        test_paths=test_paths,
        changed_lines=changed_lines,
        coverage=coverage,
        mutmut_version=mutmut_version,
        config=config,
        enable_ci_sampling=enable_ci_sampling,
        base_ref=base_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        repo_url=repo_url,
        generation_policy=generation_policy,
        plan_builder=build_mutmut3_candidate_plan_from_meta,
    )


def _aggregate_mutation_summaries(
    summaries: Sequence[MutationSummary],
    *,
    gate: float,
    runtime_lane: str,
) -> MutationSummary:
    """Sum per-batch mutation counts and recompute rate/passed (same formula as parse)."""
    if not summaries:
        return _empty_mutation_summary(gate=gate, runtime_lane=runtime_lane)

    def total(attr: str) -> int:
        return sum(int(getattr(s, attr, 0) or 0) for s in summaries)

    killed = total("killed")
    survived = total("survived")
    timeout = total("timeout")
    suspicious = total("suspicious")
    caught = total("caught_by_type_check")
    detected = killed  # parse_mutmut_summary already folds caught_by_type_check into killed
    denominator = detected + survived + timeout + suspicious
    rate = 100.0 if denominator == 0 else round((detected / denominator) * 100.0, 4)
    return MutationSummary(
        runtime_lane=runtime_lane,
        generated=total("generated"),
        killed=killed,
        survived=survived,
        no_coverage=total("no_coverage"),
        rate=rate,
        gate=float(gate),
        passed=rate >= float(gate),
        no_tests=total("no_tests"),
        timeout=timeout,
        suspicious=suspicious,
        skipped=total("skipped"),
        caught_by_type_check=caught,
    )


def _batch_execution_failure(
    *,
    batch_index: int,
    metadata_exit_code: int,
    metadata_count: Optional[int],
    execution_exit_code: Optional[int],
    execution_summary: Optional[MutationSummary],
) -> str:
    """Reject partial mutmut batches that cannot provide scored results."""
    if int(metadata_exit_code) != 0:
        return f"batch {batch_index} metadata generation failed (exit {metadata_exit_code})"
    if execution_exit_code is None:
        return ""
    if not _is_expected_mutmut_exit(execution_exit_code):
        return f"batch {batch_index} mutation execution failed (exit {execution_exit_code})"
    if int(metadata_count or 0) > 0 and int(getattr(execution_summary, "generated", 0) or 0) <= 0:
        return (
            f"batch {batch_index} mutation execution produced no results for "
            f"{int(metadata_count or 0)} generated mutants (exit {execution_exit_code})"
        )
    return ""


def _run_batched_modern_mutation(
    *,
    repo: Path,
    target: TargetRef,
    source_path: str,
    source_file: Path,
    test_paths: Sequence[str],
    coverage: "CoverageSummary",
    mutmut_version: str,
    config: "PythonRuntimeConfig",
    base_ref: str,
    base_commit: str,
    head_commit: str,
    repo_url: str,
    mutation_dir: Path,
    python_bin: str,
    mutmut_bin: str,
    runner: RunCommand,
    mutation_env: Mapping[str, str],
    mutation_gate: float,
    lane: str,
    full_policy: Mapping[str, Any],
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    commands: List[Any],
    artifacts: Dict[str, str],
) -> Tuple[MutationSummary, Dict[str, Any], List[str]]:
    """Batched generation pass (ADR-002): partition selected opportunities into byte-budgeted,
    function-granular batches, generate+score each sequentially, and aggregate.

    Returns ``(aggregated_summary, aggregated_plan_dict, warnings)``. The default ``hard_cap``
    path does not call this; it is only reached when the strategy flag selects ``batch``.
    """
    budget = int(getattr(settings, "python_mutation_generation_max_generated_bytes", 0) or 8_000_000)
    max_batches = int(getattr(settings, "python_mutation_generation_max_batches", 0) or 12)
    max_children = int(getattr(settings, "python_mutation_max_children", 0) or 1)
    metadata_timeout = int(
        getattr(settings, "python_mutation_adapter_generation_timeout_seconds", 0) or config.timeout_seconds
    )
    selected_timeout = int(
        getattr(settings, "python_mutation_selected_execution_timeout_seconds", 0) or config.timeout_seconds
    )
    # The adapter aborts itself just before we would reap it, so a wedge that a
    # phase watchdog cannot see -- mutant generation, or waiting on a forked
    # child -- still yields a thread dump instead of a bare exit 124.
    metadata_env = {**mutation_env, ADAPTER_TIMEOUT_ENV: str(_adapter_self_abort_seconds(metadata_timeout))}
    selected_env = {**mutation_env, ADAPTER_TIMEOUT_ENV: str(_adapter_self_abort_seconds(selected_timeout))}

    selected = full_policy.get("selected") or []
    try:
        source_text = source_file.read_text(encoding="utf-8")
    except OSError:
        source_text = ""
    units = _mutation_units_from_generation_policy(source_text, selected)
    # Calibrate the per-line estimate: mutmut emits ~N mutants per line, so scale unit
    # bytes by the configured factor before bin-packing (the raw estimate assumes one
    # mutant per line and under-counts the real module ~2x; see T8 / Appendix A 13.8).
    factor = float(getattr(settings, "python_mutation_generation_bytes_per_line_factor", 0) or 1.0)
    if factor > 1.0:
        units = [replace(u, unit_bytes=int(u.unit_bytes * factor)) for u in units]
    partition = partition_units_into_batches(units, budget=budget, max_batches=max_batches)

    warnings: List[str] = []
    if partition.omitted_by_generated_bytes_cap:
        warnings.append(
            "Mutation coverage reduced: %d line(s) in oversized function(s) exceeded the generated-bytes "
            "budget and were capped (lines %s)."
            % (
                len(partition.omitted_by_generated_bytes_cap),
                ",".join(str(x) for x in partition.omitted_by_generated_bytes_cap),
            )
        )
    if partition.omitted_by_max_batches:
        warnings.append(
            "Mutation coverage reduced: %d line(s) trimmed to fit the max-batch guard (lines %s)."
            % (
                len(partition.omitted_by_max_batches),
                ",".join(str(x) for x in partition.omitted_by_max_batches),
            )
        )

    plans: List[Dict[str, Any]] = []
    summaries: List[MutationSummary] = []
    all_survivors: List[Dict[str, Any]] = []
    batches_meta: List[Dict[str, Any]] = []
    batch_failures: List[str] = []
    total_generated_mutants = 0

    for batch in partition.batches:
        _clean_generated_mutants(repo)
        batch_lines = set(batch.lines)
        batch_policy = _filter_generation_policy_to_lines(full_policy, batch_lines)
        batch_policy_path = mutation_dir / f"generation-policy.batch{batch.index}.json"
        batch_policy_path.write_text(json.dumps(batch_policy), encoding="utf-8")

        meta_cmd = _mutmut_generate_metadata_command(
            python_bin, max_children=max_children, policy_path=batch_policy_path, mode="metadata"
        )
        meta_result = _run_command(
            f"mutmut_generate_metadata_batch_{batch.index}",
            meta_cmd,
            repo,
            metadata_timeout,
            runner,
            env_overrides=metadata_env,
        )
        commands.append(meta_result)
        # Per-batch generation must fail closed (parity with the single-pass path): a metadata
        # generation error that produced no .meta is a batch failure, not silently empty.
        batch_meta_count = _mutmut_meta_mutant_count(repo, source_path)
        if batch_meta_count:
            total_generated_mutants += int(batch_meta_count)
        metadata_failure = _batch_execution_failure(
            batch_index=batch.index,
            metadata_exit_code=meta_result.exit_code,
            metadata_count=batch_meta_count,
            execution_exit_code=None,
            execution_summary=None,
        )
        if metadata_failure:
            batch_failures.append(metadata_failure)

        batch_plan = _modern_mutmut3_candidate_plan(
            repo=repo,
            target=target,
            source_path=source_path,
            test_paths=test_paths,
            changed_lines=changed_lines,
            coverage=coverage,
            mutmut_version=mutmut_version,
            config=config,
            enable_ci_sampling=False,
            base_ref=base_ref,
            base_commit=base_commit,
            head_commit=head_commit,
            repo_url=repo_url,
            generation_policy=batch_policy,
        )
        batch_planning_error = str(batch_plan.get("planningError") or "").strip()
        if batch_planning_error:
            if batch_meta_count == 0:
                batch_plan = dict(batch_plan)
                batch_plan.pop("planningError", None)
            else:
                batch_failures.append(f"batch {batch.index}: {batch_planning_error}")
        generated_bytes = _generated_module_bytes(repo, source_path)
        keys = _candidate_plan_generated_tool_keys(batch_plan)

        summary_i = _empty_mutation_summary(gate=mutation_gate, runtime_lane=lane)
        if keys and not metadata_failure:
            run_cmd = _mutmut_generate_metadata_command(
                python_bin, max_children=max_children, policy_path=batch_policy_path, mode="run"
            )
            run_result = _run_command(
                f"mutmut_run_adapter_filtered_batch_{batch.index}",
                run_cmd,
                repo,
                selected_timeout,
                runner,
                env_overrides=selected_env,
            )
            commands.append(run_result)
            run_output = "\n".join(part for part in (run_result.stdout, run_result.stderr) if part)
            summary_i = parse_mutmut_summary(run_output, gate=mutation_gate, runtime_lane=lane)
            summary_i = _reconcile_no_test_association(
                summary_i,
                output=run_output,
                metadata_count=batch_meta_count,
            )
            execution_failure = _batch_execution_failure(
                batch_index=batch.index,
                metadata_exit_code=meta_result.exit_code,
                metadata_count=batch_meta_count,
                execution_exit_code=run_result.exit_code,
                execution_summary=summary_i,
            )
            if execution_failure:
                batch_failures.append(execution_failure)
            batch_plan = _candidate_plan_with_keyed_execution_evidence(
                batch_plan,
                repo=repo,
                source_path=source_path,
                mutmut_bin=mutmut_bin,
                timeout=config.timeout_seconds,
                runner=runner,
                env_overrides=mutation_env,
            )
            survivors_i: List[Dict[str, Any]] = []
            if summary_i.survived > 0:
                results = _run_command(
                    f"mutmut_results_batch_{batch.index}",
                    [mutmut_bin, "results"],
                    repo,
                    config.timeout_seconds,
                    runner,
                    env_overrides=mutation_env,
                )
                commands.append(results)
                results_output = "\n".join(part for part in (results.stdout, results.stderr) if part)
                survivors_i = _annotate_mutmut_survivor_diffs(
                    parse_mutmut_survivors(results_output),
                    mutmut_bin=mutmut_bin,
                    repo=repo,
                    source_path=source_path,
                    timeout=config.timeout_seconds,
                    runner=runner,
                    env_overrides=mutation_env,
                )
            summary_i = replace(summary_i, survivors=survivors_i)
            all_survivors.extend(survivors_i)
            batch_plan = _candidate_plan_with_execution_evidence(batch_plan, summary_i)

        plans.append(batch_plan)
        summaries.append(summary_i)
        batches_meta.append(
            {
                "index": batch.index,
                "functionCount": len(batch.symbols),
                "allowedLineCount": len(batch.lines),
                "estimatedBytes": int(batch.estimated_bytes),
                "batchGeneratedBytes": int(generated_bytes),
                "overBudget": bool(generated_bytes and generated_bytes > budget),
            }
        )

    aggregated_plan = aggregate_batch_candidate_plans(
        plans,
        partition_signature=partition.signature,
        max_generated_bytes=budget,
        batches_meta=batches_meta,
        omitted_by_generated_bytes_cap=partition.omitted_by_generated_bytes_cap,
        omitted_by_max_batches=partition.omitted_by_max_batches,
    )
    if not aggregated_plan:
        aggregated_plan = {
            "language": "python",
            "targetId": target.target_id,
            "sourcePath": _normalize_relpath(source_path),
            "policyMode": "report_full",
            "exactToolCandidateKeys": [],
            "activeToolCandidateKeys": [],
            "activeSelected": [],
            "reportFullSelected": [],
            "eligibleMutationOpportunities": [],
            "generationStrategy": "batch",
            "batchCount": len(partition.batches),
            "partitionSignature": partition.signature,
            "maxGeneratedBytes": budget,
            "batches": list(batches_meta),
            "runMutants": 0,
            "scoredMutants": 0,
        }
    if aggregated_plan and warnings:
        aggregated_plan["mutationBatchWarnings"] = list(warnings)

    aggregated_summary = _aggregate_mutation_summaries(summaries, gate=mutation_gate, runtime_lane=lane)
    aggregated_summary = replace(aggregated_summary, survivors=all_survivors)
    artifacts["mutation_batch_count"] = str(len(partition.batches))

    aggregated_summary, aggregated_plan = _finalize_batched_outcome(
        aggregated_summary,
        aggregated_plan,
        batch_failures=batch_failures,
        total_generated_mutants=total_generated_mutants,
        source_path=source_path,
        changed_lines=changed_lines,
    )
    return aggregated_summary, aggregated_plan, warnings


def _finalize_batched_outcome(
    aggregated_summary: MutationSummary,
    aggregated_plan: Dict[str, Any],
    *,
    batch_failures: Sequence[str],
    total_generated_mutants: int,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
) -> Tuple[MutationSummary, Dict[str, Any]]:
    """Apply fail-closed / no-mutatable parity to the aggregated batch result (code-review I1/I2).

    - Any per-batch generation failure -> set ``planningError`` so the downstream fails the
      target closed (I1), never a silent empty pass.
    - No generated keys but eligible opportunities exist (I2): if no mutants were generated in
      any batch, mark "nothing mutatable" (passes, parity with the single-pass path); otherwise
      fail closed because metadata could not be mapped to candidates.
    """
    if batch_failures:
        if not aggregated_plan:
            aggregated_plan = {"language": "python", "sourcePath": _normalize_relpath(source_path)}
        aggregated_plan["planningError"] = "; ".join(batch_failures)
        return aggregated_summary, aggregated_plan

    if aggregated_plan and candidate_plan_requires_mutation_execution(aggregated_plan):
        if total_generated_mutants == 0:
            aggregated_plan["planningError"] = (
                "Python mutation candidate plan selected eligible changed-line mutations "
                "but adapter execution materialized zero mutants"
            )
        elif not _candidate_plan_generated_tool_keys(aggregated_plan):
            aggregated_plan["planningError"] = (
                "Python mutation candidate plan could not map adapter-generated mutmut "
                "metadata to changed-line candidates"
            )
    return aggregated_summary, aggregated_plan

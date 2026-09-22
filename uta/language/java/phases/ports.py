"""Composition root for Java's phase-sized domain ports.

This is the one place that knows which concrete Java capability answers each
phase port. It imports those capabilities by name from the module that owns
them -- Maven report parsing from ``maven``, quality-gate policy from
``generation.quality``, and so on -- rather than reaching into a single
generation helper namespace, so a phase's dependencies are readable from the
port it receives instead of from a bucket module.

The declarative backend depends on these phase-sized ports, never on the
retired concrete-agent orchestration.
"""

from __future__ import annotations

from types import SimpleNamespace

from uta.language.java.generation.commands import (
    _compile_test,
    _index_query_command,
    _refresh_test_failure_summary,
    _run_test_selector,
    _write_context_artifact,
)
from uta.language.java.generation.evidence import (
    _delegated_gate_batch_results,
    _sync_task_results_if_available,
)
from uta.language.java.generation.mutation_context import (
    _filter_mutation_families_by_roi,
    _flatten_mutation_family_examples,
)
from uta.language.java.generation.quality import (
    _annotate_out_of_scope_gate_failure,
    _delegated_gate_failure_matches_batch,
    _delegated_gate_failure_stage,
    _delegated_quality_gate_feedback,
    _delegated_quality_gate_prompt_feedback,
    _run_delegated_quality_gate_once,
)
from uta.language.java.generation.selection import select_next_class
from uta.language.java.generation.writeback import _writeback_resolved_symbols
from uta.language.java.maven.jacoco import (
    extract_uncovered_clusters,
    find_jacoco_report,
    format_uncovered_clusters_markdown,
    parse_jacoco_report,
    parse_surefire_results,
    run_tests_with_jacoco_batch,
)
from uta.language.java.maven.pitest import (
    compute_mutation_stats,
    find_latest_pitest_report,
    format_mutation_families_markdown,
    parse_pitest_green_suite_failure,
    run_pitest,
    summarize_surviving_mutants,
)
from uta.language.java.phases.completion import status_with_mutation_gate
from uta.language.java.phases.compile import JavaCompilePorts
from uta.language.java.phases.coverage import JavaCoveragePorts
from uta.language.java.phases.delegated_quality import JavaDelegatedQualityPorts
from uta.language.java.phases.mutation import JavaMutationPorts
from uta.language.java.phases.precheck import JavaPrecheckPorts
from uta.language.java.phases.test_repair import JavaTestRepairPorts
from uta.language.java.phases.test_verification import JavaTestVerificationPorts
from uta.language.java.prompt_bundle import java_prompt_bundle
from uta.language.java.workspace import expected_ci_incremental_java_test_paths
from uta.language.java.workspace import expected_test_file_rel
from uta.testgen.progress import merge_phase_timings, set_stage


def java_precheck_ports() -> JavaPrecheckPorts:
    return JavaPrecheckPorts(
        expected_test_file_rel=expected_test_file_rel,
        set_stage=set_stage,
        run_tests_with_jacoco_batch=run_tests_with_jacoco_batch,
        find_jacoco_report=find_jacoco_report,
        parse_surefire_results=parse_surefire_results,
        parse_jacoco_report=parse_jacoco_report,
        run_pitest=run_pitest,
        find_latest_pitest_report=find_latest_pitest_report,
        compute_mutation_stats=compute_mutation_stats,
        merge_phase_timings=merge_phase_timings,
        run_delegated_quality_gate=_run_delegated_quality_gate_once,
        delegated_gate_batch_results=_delegated_gate_batch_results,
        sync_task_results=_sync_task_results_if_available,
        delegated_gate_failure_stage=_delegated_gate_failure_stage,
    )


def java_compile_ports() -> JavaCompilePorts:
    return JavaCompilePorts(
        prompt_bundle=java_prompt_bundle(),
        compile_test=_compile_test,
        writeback_resolved_symbols=_writeback_resolved_symbols,
    )


def java_test_verification_ports() -> JavaTestVerificationPorts:
    return JavaTestVerificationPorts(
        run_test_selector=_run_test_selector,
        refresh_failure_summary=_refresh_test_failure_summary,
    )


def java_test_repair_ports() -> JavaTestRepairPorts:
    return JavaTestRepairPorts(index_query_command=_index_query_command)


def java_coverage_ports() -> JavaCoveragePorts:
    return JavaCoveragePorts(
        prompt_bundle=java_prompt_bundle(),
        run_tests_with_jacoco_batch=run_tests_with_jacoco_batch,
        find_jacoco_report=find_jacoco_report,
        parse_jacoco_report=parse_jacoco_report,
        extract_uncovered_clusters=extract_uncovered_clusters,
        format_uncovered_clusters=format_uncovered_clusters_markdown,
    )


def java_mutation_ports() -> JavaMutationPorts:
    return JavaMutationPorts(
        prompt_bundle=java_prompt_bundle(),
        run_pitest=run_pitest,
        find_latest_pitest_report=find_latest_pitest_report,
        parse_pitest_green_suite_failure=parse_pitest_green_suite_failure,
        compute_mutation_stats=compute_mutation_stats,
        summarize_surviving_mutants=summarize_surviving_mutants,
        format_mutation_families=format_mutation_families_markdown,
        write_context_artifact=_write_context_artifact,
        filter_mutation_families=_filter_mutation_families_by_roi,
        flatten_mutation_examples=_flatten_mutation_family_examples,
    )


def java_delegated_quality_ports() -> JavaDelegatedQualityPorts:
    return JavaDelegatedQualityPorts(
        run_gate=_run_delegated_quality_gate_once,
        failure_matches_batch=_delegated_gate_failure_matches_batch,
        annotate_out_of_scope_failure=_annotate_out_of_scope_gate_failure,
        gate_feedback=_delegated_quality_gate_feedback,
        prompt_feedback=_delegated_quality_gate_prompt_feedback,
        expected_test_paths=expected_ci_incremental_java_test_paths,
        failure_stage=_delegated_gate_failure_stage,
    )


def java_completion_ports():
    return SimpleNamespace(
        delegated_gate_batch_results=_delegated_gate_batch_results,
        status_with_mutation_gate=status_with_mutation_gate,
    )


__all__ = [
    "java_compile_ports",
    "java_completion_ports",
    "java_coverage_ports",
    "java_delegated_quality_ports",
    "java_mutation_ports",
    "java_precheck_ports",
    "java_test_repair_ports",
    "java_test_verification_ports",
    "select_next_class",
]

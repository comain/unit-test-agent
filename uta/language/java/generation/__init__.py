"""Java generation capabilities, grouped by responsibility.

This package used to be one 1,147-line module that every Java caller reached
into for anything: batch selection, Maven commands, quality-gate policy,
symbol writeback, evidence projection, mutation ROI. The behaviour is
unchanged; it now lives in six focused modules, and this file is only the
compatibility facade.

New code should import the owning module directly --
``uta.language.java.generation.quality``, ``.selection``, ``.commands``,
``.writeback``, ``.evidence``, ``.mutation_context``. The names re-exported
below stay because callers and tests reach them as
``uta.language.java.generation.<name>``; ``__all__`` makes that a contract
rather than an accident.
"""

from uta.language.java.compile import classify_compile_errors
from uta.language.java.ci_evidence import java_gate_failure_summary as _java_gate_failure_summary
from uta.language.java.generation.commands import (
    _compile_test,
    _expected_test_paths_for_batch,
    _filter_compile_errors_to_paths,
    _format_compile_error_block,
    _git_run,
    _index_query_command,
    _refresh_test_failure_summary,
    _render_compile_fix_feedback,
    _run_test,
    _run_test_selector,
    _stage_introspect_section,
    _write_context_artifact,
)
from uta.language.java.generation.evidence import (
    _delegated_gate_batch_results,
    _enforcement_evidence_detail,
    _sync_task_results_if_available,
)
from uta.language.java.generation.mutation_context import (
    _filter_mutation_families_by_roi,
    _flatten_mutation_family_examples,
    _mutation_enhancement_attempts,
    _should_run_mutation,
)
from uta.language.java.generation.quality import (
    _annotate_out_of_scope_gate_failure,
    _ci_incremental_module_identities_for_batch,
    _ci_incremental_modules_for_batch,
    _ci_incremental_target_sources_for_batch,
    _ci_incremental_target_tests_for_batch,
    _delegated_gate_context,
    _delegated_gate_failure_matches_batch,
    _delegated_gate_failure_stage,
    _delegated_quality_gate_feedback,
    _delegated_quality_gate_feedback_excerpt,
    _delegated_quality_gate_prompt_feedback,
    _delegated_target_scoped_maven_command,
    _failed_maven_modules_from_output,
    _failed_test_simple_names_from_output,
    _maven_artifact_id_for_module,
    _remove_maven_option_with_value,
    _run_delegated_quality_gate_once,
    _sanitize_quality_gate_output,
    _xml_child_text,
)
from uta.language.java.generation.selection import (
    _batch_complexity_profile,
    _java_target_selection,
    _plan_breadth_replan_reason,
    _select_smart_batch,
    _should_stop_after,
    _source_complexity_summary,
    _target_alias_update,
    select_next_class,
)
from uta.language.java.generation.writeback import _writeback_resolved_symbols
from uta.language.java.generation_plan import _MAX_PLAN_CHARS
from uta.language.java.generation_plan import (
    _clear_generation_plan,
    _compress_plan_for_generation,
    _extract_plan_body_from_artifact,
    _extract_planned_tests_table,
    _generation_plan_artifact_classes,
    _generation_plan_artifact_session_id,
    _generation_plan_candidate_path,
    _generation_plan_path,
    _load_generation_plan_for_resume,
    _plan_needs_stricter_replan,
    _prepare_continue_artifact_for_phase,
    _recover_plan_text_from_session_artifact,
    _strip_plan_prose,
    _write_generation_plan,
    _write_generation_plan_candidate,
)
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
from uta.language.java.symbol_resolver import format_candidates_markdown, resolve_symbols
from uta.language.java.test_quality import attach_java_test_quality as _attach_java_test_quality
from uta.language.java.workspace import candidate_source_path as _candidate_source_path
from uta.language.java.workspace import class_module as _class_module
from uta.language.java.workspace import (
    discover_ci_incremental_java_test_files as _discover_ci_incremental_java_test_files,
)
from uta.language.java.workspace import (
    expected_ci_incremental_java_test_paths as _expected_ci_incremental_java_test_paths,
)
from uta.language.java.workspace import expected_test_file_rel as _expected_test_file_rel
from uta.language.java.workspace import java_fqn_from_test_path as _java_fqn_from_test_path
from uta.shared.config import settings as uta_settings
from uta.shared.targets import TargetIdentity
from uta.testgen.progress import merge_phase_timings as _merge_phase_timings
from uta.testgen.progress import set_stage as _set_stage
from uta.testgen.project_summary_artifacts import (
    ensure_stage_introspect_file,
    merge_compile_fix_facts,
)
from uta.testgen.workspace_guard import TaskBudgetExceeded
from uta.testgen.workspace_guard import llm_guard_before as _llm_guard_before
from uta.testgen.workspace_guard import (
    verify_task_branch_and_preexisting_diff as _verify_task_branch_and_preexisting_diff,
)

__all__ = [
    "TargetIdentity",
    "TaskBudgetExceeded",
    "_MAX_PLAN_CHARS",
    "_annotate_out_of_scope_gate_failure",
    "_attach_java_test_quality",
    "_batch_complexity_profile",
    "_candidate_source_path",
    "_ci_incremental_module_identities_for_batch",
    "_ci_incremental_modules_for_batch",
    "_ci_incremental_target_sources_for_batch",
    "_ci_incremental_target_tests_for_batch",
    "_class_module",
    "_clear_generation_plan",
    "_compile_test",
    "_compress_plan_for_generation",
    "_delegated_gate_batch_results",
    "_delegated_gate_context",
    "_delegated_gate_failure_matches_batch",
    "_delegated_gate_failure_stage",
    "_delegated_quality_gate_feedback",
    "_delegated_quality_gate_feedback_excerpt",
    "_delegated_quality_gate_prompt_feedback",
    "_delegated_target_scoped_maven_command",
    "_discover_ci_incremental_java_test_files",
    "_enforcement_evidence_detail",
    "_expected_ci_incremental_java_test_paths",
    "_expected_test_file_rel",
    "_expected_test_paths_for_batch",
    "_extract_plan_body_from_artifact",
    "_extract_planned_tests_table",
    "_failed_maven_modules_from_output",
    "_failed_test_simple_names_from_output",
    "_filter_compile_errors_to_paths",
    "_filter_mutation_families_by_roi",
    "_flatten_mutation_family_examples",
    "_format_compile_error_block",
    "_generation_plan_artifact_classes",
    "_generation_plan_artifact_session_id",
    "_generation_plan_candidate_path",
    "_generation_plan_path",
    "_git_run",
    "_index_query_command",
    "_java_fqn_from_test_path",
    "_java_gate_failure_summary",
    "_java_target_selection",
    "_llm_guard_before",
    "_load_generation_plan_for_resume",
    "_maven_artifact_id_for_module",
    "_merge_phase_timings",
    "_mutation_enhancement_attempts",
    "_plan_breadth_replan_reason",
    "_plan_needs_stricter_replan",
    "_prepare_continue_artifact_for_phase",
    "_recover_plan_text_from_session_artifact",
    "_refresh_test_failure_summary",
    "_remove_maven_option_with_value",
    "_render_compile_fix_feedback",
    "_run_delegated_quality_gate_once",
    "_run_test",
    "_run_test_selector",
    "_sanitize_quality_gate_output",
    "_select_smart_batch",
    "_set_stage",
    "_should_run_mutation",
    "_should_stop_after",
    "_source_complexity_summary",
    "_stage_introspect_section",
    "_strip_plan_prose",
    "_sync_task_results_if_available",
    "_target_alias_update",
    "_verify_task_branch_and_preexisting_diff",
    "_write_context_artifact",
    "_write_generation_plan",
    "_write_generation_plan_candidate",
    "_writeback_resolved_symbols",
    "_xml_child_text",
    "classify_compile_errors",
    "compute_mutation_stats",
    "ensure_stage_introspect_file",
    "extract_uncovered_clusters",
    "find_jacoco_report",
    "find_latest_pitest_report",
    "format_candidates_markdown",
    "format_mutation_families_markdown",
    "format_uncovered_clusters_markdown",
    "merge_compile_fix_facts",
    "parse_jacoco_report",
    "parse_pitest_green_suite_failure",
    "parse_surefire_results",
    "resolve_symbols",
    "run_pitest",
    "run_tests_with_jacoco_batch",
    "select_next_class",
    "summarize_surviving_mutants",
    "uta_settings",
]

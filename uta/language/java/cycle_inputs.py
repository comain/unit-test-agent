"""Build the serializable Java child-workflow input outside the checkpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from uta.testgen.project_summary_artifacts import (
    prompt_template_paths,
    sync_project_summaries,
)
from uta.language.java.context_builder import ContextBuilder
from uta.language.java.workspace import expected_test_file_rel
from uta.shared.config import settings


def prepare_java_cycle_state(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Export context/ROI artifacts and return only checkpoint-safe values."""
    batch = list(state.get("current_batch") or state.get("batch") or [])
    if not batch and state.get("current_class"):
        batch = [str(state["current_class"])]
    repo_path = str(state["repo_path"])
    module = state.get("module")
    graph = state.get("graph")
    flows = list(state.get("flows") or [])
    if graph is None:
        raise ValueError("Java cycle preparation requires the parsed code graph")
    builder = ContextBuilder(repo_path, graph, flows)
    context_dir = builder.export_context_files()
    sync_project_summaries(repo_path, graph, module, language="java")

    target_paths: Dict[str, Dict[str, str]] = {}
    complexity: Dict[str, Dict[str, Any]] = {}
    method_efforts: Dict[str, Any] = {}
    from uta.language.java.generation.selection import _source_complexity_summary

    for class_fqn in batch:
        paths = builder.export_target_context_files(
            class_fqn,
            module=module,
            test_file_rel=expected_test_file_rel(module, class_fqn),
        )
        source = builder.get_class_source_path(class_fqn)
        paths["source_abs"] = str(Path(source).resolve()) if source else ""
        target_paths[class_fqn] = paths
        complexity[class_fqn] = _source_complexity_summary(
            source, int(state.get("coverage_gate") or 0)
        )
        try:
            from uta.testgen.learning import preseed_compile_context

            preseed_compile_context(
                repo_path=repo_path,
                class_fqn=class_fqn,
                symbols_abs=paths.get("symbols_abs"),
            )
        except Exception:
            pass

    roi_enabled = bool(settings.roi_enabled)
    if roi_enabled:
        from uta.language.java.scoring.coverage_roi import (
            compute_class_roi,
            is_degenerate_roi_data,
        )

        for class_fqn in batch:
            try:
                roi_data = compute_class_roi(class_fqn, graph)
                method_efforts[class_fqn] = list(roi_data.get("methods") or [])
                if not is_degenerate_roi_data(roi_data):
                    target_paths[class_fqn]["roi_abs"] = builder.export_roi_scores(
                        class_fqn,
                        roi_data,
                        source_path=builder.get_class_source_path(class_fqn),
                        debug=settings.roi_debug,
                    )
                else:
                    target_paths[class_fqn]["roi_abs"] = ""
            except Exception:
                target_paths[class_fqn]["roi_abs"] = ""

    strict = [
        {
            "class_fqn": fqn,
            "line_count": data["line_count"],
            "public_method_count": data["public_method_count"],
        }
        for fqn, data in complexity.items()
        if data.get("strict_coverage")
    ]
    maximums = {
        "plan_tests": 1,
        "generate_tests": 1,
        "fix_compile": 3,
        "fix_tests": 3,
        # Coverage and mutation repair stop on the first round that does not
        # improve the score; these are only the ceilings that bound a score
        # improving forever without meeting its gate.
        "fix_coverage": max(1, int(settings.coverage_repair_max_attempts or 1)),
        "fix_mutation": max(
            int(settings.mutation_repair_max_attempts or 0),
            max(0, int(settings.mutation_enhancement_attempts or 1) - 1),
        ),
        "delegated_quality_gate": max(
            6, int(settings.coverage_repair_max_attempts or 1),
            int(settings.mutation_repair_max_attempts or 1),
        ),
        **dict(state.get("max_attempts_by_phase") or {}),
    }
    return {
        "repo_path": repo_path,
        "task_db_path": str(state.get("task_db_path") or ""),
        "language": "java",
        "module": module,
        "batch": batch,
        "coverage_gate": int(state.get("coverage_gate") or 0),
        "mutation_gate": int(state.get("mutation_gate") or 0),
        "quality_mode": str(state.get("quality_mode") or "class_batch"),
        "quality_gate_backend": str(state.get("quality_gate_backend") or "builtin"),
        "quality_gate_command": str(state.get("quality_gate_command") or ""),
        "rdc_context": dict(state.get("rdc_context") or {}),
        "results": dict(state.get("results") or {}),
        "session_ids": list(state.get("session_ids") or []),
        "session_refs": list(state.get("session_refs") or []),
        "phase_timings": dict(state.get("phase_timings") or {}),
        "target_context_paths": target_paths,
        "method_efforts_by_class": method_efforts,
        "context_dir": str(context_dir.resolve()),
        "project_prompt_paths": prompt_template_paths(repo_path, context_dir),
        "complexity_by_class": complexity,
        "strict_coverage_classes": strict,
        "roi_enabled": roi_enabled,
        "mutation_roi_enabled": bool(settings.mutation_roi_enabled),
        "mutation_roi_skip_expensive": bool(settings.mutation_roi_skip_expensive),
        "ci_diff_coverage_gate": int(settings.ci_diff_coverage_gate or 0),
        "ci_diff_mutation_gate": int(settings.ci_diff_mutation_gate or 0),
        "plan_index_query_command": _index_command(module),
        "generation_index_query_command": _index_command(module),
        "spec_context": str(state.get("spec_context") or ""),
        "stop_after_stage": state.get("stop_after_stage"),
        "attempts_by_phase": dict(state.get("attempts_by_phase") or {}),
        "best_scores_by_phase": dict(state.get("best_scores_by_phase") or {}),
        "max_attempts_by_phase": maximums,
        "phase_results": dict(state.get("phase_results") or {}),
        "turn_history": list(state.get("turn_history") or []),
    }


def _index_command(module) -> str:
    from uta.language.java.generation.commands import _index_query_command

    return _index_query_command(module)


__all__ = ["prepare_java_cycle_state"]

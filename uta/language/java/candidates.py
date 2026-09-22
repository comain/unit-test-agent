"""Discover Java targets and build their shared project context."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict

from uta.testgen.project_summary_artifacts import sync_project_summaries
from uta.shared.source_selection import filter_files, get_all_source_files, get_changed_source_files
from uta.language.java.context import JavaContextProvider
from uta.language.java.selection import is_testable_class as _is_testable_class
from uta.shared.parse import ParseProjectRequest, make_parse_provider
from uta.testgen.graph.state import AgentState
from uta.testgen.progress import merge_phase_timings as _merge_phase_timings
from uta.testgen.progress import set_stage as _set_stage

logger = logging.getLogger("uta")


def scan_and_select(state: AgentState) -> Dict[str, Any]:
    """Select Java source candidates from explicit targets or repository history."""
    started = time.perf_counter()
    repo_path = state["repo_path"]
    days = state["days"]
    module = state["module"]
    max_files = state["max_files"]
    select_all_files = bool(state.get("select_all_files", False))
    explicit_class_fqns = state.get("explicit_class_fqns", [])
    scan_detail = "all production files" if select_all_files else f"days={days} max_files={max_files}"
    _set_stage(state, "scan_candidates", scan_detail)

    if explicit_class_fqns:
        logger.info(
            "Using explicit class override: %d class(es)=%s",
            len(explicit_class_fqns),
            explicit_class_fqns,
        )
        return _scan_result(state, explicit_class_fqns, started)

    if state.get("quality_mode") == "ci_incremental":
        return {
            "error": "CI incremental Java repair requires explicit diff target classes; "
            "refusing recent-history candidate scan",
            **_scan_result(state, [], started),
        }

    if select_all_files:
        files_with_counts = get_all_source_files("java", repo_path, module)
        candidates = [path for path, _count in files_with_counts]
        logger.info("Using all production Java files: %d file(s)", len(candidates))
    else:
        files_with_counts = get_changed_source_files("java", repo_path, days, module)
        candidates = filter_files(files_with_counts, max_files)
    return _scan_result(state, candidates, started)


def _scan_result(state: AgentState, candidates, started: float) -> Dict[str, Any]:
    return {
        "candidates": candidates,
        "current_stage": "scan_candidates",
        "phase_timings": _merge_phase_timings(
            state,
            scan_select_seconds=time.perf_counter() - started,
        ),
    }


def parse_context(state: AgentState) -> Dict[str, Any]:
    """Parse the Java module and retain only valid generation targets."""
    started = time.perf_counter()
    repo_path = state["repo_path"]
    module = state["module"]
    _set_stage(state, "parse_context", "build graph and export cached context")

    parse_result = make_parse_provider("java").parse_project(
        ParseProjectRequest(repo_path=Path(repo_path), module=module)
    )
    graph = parse_result.graph
    flows = parse_result.flows
    JavaContextProvider(repo_path, graph, flows).export_project_context()
    sync_project_summaries(repo_path, graph, module, language="java")

    explicit_class_fqns = state.get("explicit_class_fqns", [])
    if explicit_class_fqns:
        final_candidates = [
            fqn
            for fqn in explicit_class_fqns
            if _retain_explicit_target(parse_result, fqn)
        ]
    else:
        final_candidates = _testable_targets(parse_result, graph, state["candidates"])

    logger.info(
        "Testable candidates: %d out of %d scanned files",
        len(final_candidates),
        len(state["candidates"]),
    )
    _record_production_targets(state, final_candidates, module)
    return {
        "graph": graph,
        "flows": flows,
        "candidates": final_candidates,
        "target_candidates": parse_result.target_selections(final_candidates),
        "current_stage": "parse_context",
        "phase_timings": _merge_phase_timings(
            state,
            parse_context_seconds=time.perf_counter() - started,
        ),
    }


def _retain_explicit_target(parse_result, target_id: str) -> bool:
    if parse_result.contains_target(target_id):
        return True
    logger.info("Explicit candidate not found in parsed graph: %s", target_id)
    return False


def _testable_targets(parse_result, graph, source_paths):
    candidates = []
    for path in source_paths:
        target_id = parse_result.target_id_for_source_path(path)
        if target_id and _is_testable_class(target_id, graph):
            candidates.append(target_id)
        elif target_id:
            logger.info("Filtered out candidate: %s", target_id)
    return candidates


def _record_production_targets(state: AgentState, candidates, module) -> None:
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if not task_id or not task_db_path:
        return
    try:
        from uta.tasks.manager import TaskManager

        manager = TaskManager(task_db_path)
        manager.ensure_class_tasks(int(task_id), candidates, module=module)
        manager.db.update_repo_task(int(task_id), total_classes=len(candidates))
    except Exception:
        logger.debug("Failed to create production child class tasks", exc_info=True)

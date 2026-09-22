"""Build checkpoint-safe Python generation-cycle input."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Mapping

from uta.language.python.context_builder import PythonContextBuilder
from uta.language.python.test_selection import select_python_test_destination
from uta.shared.config import settings
from uta.shared.targets import coerce_target

logger = logging.getLogger(__name__)


def _changed_lines(state: Mapping[str, Any]) -> Dict[str, Any]:
    """The changed lines verification should scope to."""
    explicit = dict(state.get("changed_lines") or {})
    if explicit:
        return explicit
    task_id = state.get("task_id")
    task_db_path = str(state.get("task_db_path") or "")
    if not task_id or not task_db_path:
        return explicit
    try:
        from uta.language.python.selection import changed_lines_from_task_context
        from uta.testgen.ports.registry import task_ports_for

        ports = task_ports_for(task_db_path)
        if ports is None:
            return explicit
        return dict(changed_lines_from_task_context(ports.manager, int(task_id)) or {})
    except Exception:  # noqa: BLE001 - never fail a run over scoping metadata
        logger.warning(
            "could not read changed lines for task %s; verification will be "
            "scoped to whatever the caller supplied",
            task_id,
            exc_info=True,
        )
        return explicit


def _spec_context(state: Mapping[str, Any]) -> str:
    """The operator's spec context, widened with the task's own RDC context."""
    explicit = str(state.get("spec_context") or "")
    task_id = state.get("task_id")
    task_db_path = str(state.get("task_db_path") or "")
    if not task_id or not task_db_path:
        return explicit
    try:
        from uta.language.python.selection import (
            prompt_spec_context_from_task_context,
        )
        from uta.testgen.ports.registry import task_ports_for

        ports = task_ports_for(task_db_path)
        if ports is None:
            return explicit
        # `.manager` and not the ports object: the persistence adapter is a
        # narrow reader port with no `get_task`, so passing it here raised
        # AttributeError straight into the guard below -- a warning nobody
        # reads and no merged context.
        return prompt_spec_context_from_task_context(
            ports.manager, int(task_id), explicit=explicit
        )
    except Exception:  # noqa: BLE001 - prompt context is never worth failing a run
        logger.warning(
            "could not read RDC context for task %s; prompting with the "
            "operator-supplied spec context only",
            task_id,
            exc_info=True,
        )
        return explicit


def _base_ref(state: Mapping[str, Any]) -> str:
    explicit = str(state.get("base_ref") or "").strip()
    if explicit:
        return explicit
    task_id = state.get("task_id")
    task_db_path = str(state.get("task_db_path") or "")
    if task_id and task_db_path:
        try:
            from uta.testgen.ports.registry import task_ports_for

            ports = task_ports_for(task_db_path)
            task = ports.manager.get_task(int(task_id)) if ports is not None else None
            if task and str(task.get("base_ref") or "").strip():
                return str(task["base_ref"])
        except Exception:  # noqa: BLE001 - default is the established contract
            logger.warning("could not read base ref for task %s", task_id, exc_info=True)
    return "origin/master"


def prepare_python_cycle_state(state: Mapping[str, Any]) -> Dict[str, Any]:
    selections = list(state.get("current_target_batch") or [])
    if not selections and state.get("current_target"):
        selections = [state["current_target"]]
    if not selections:
        raise ValueError("Python cycle preparation requires one selected target")
    target = coerce_target(selections[0])
    repo = Path(str(state["repo_path"])).resolve()
    builder = PythonContextBuilder(repo)
    context_payload = builder.build_target_context(target)
    context_paths = builder.export_target_context(target)
    destination = select_python_test_destination(
        repo, target, context_payload=context_payload
    )
    maximums = {
        "python_repair_total": max(0, int(settings.python_repair_max_attempts or 0)),
        "fix_coverage": max(1, int(settings.coverage_repair_max_attempts or 1)),
        "fix_mutation": max(1, int(settings.mutation_repair_max_attempts or 1)),
        **dict(state.get("max_attempts_by_phase") or {}),
    }
    return {
        "repo_path": str(repo),
        "task_db_path": str(state.get("task_db_path") or ""),
        "language": "python",
        "quality_mode": str(state.get("quality_mode") or "class_batch"),
        "batch": [target.target_id],
        "target": target.as_selection(),
        "generated_test_path": destination.path,
        "allow_existing_non_uta": bool(destination.uses_existing_test),
        "existing_test_path": destination.path if destination.uses_existing_test else "",
        "existing_test_reasons": list(destination.existing_match.reasons) if destination.existing_match else [],
        "target_context_paths": context_paths,
        "context_payload": context_payload,
        "coverage_gate": float(state.get("coverage_gate") or settings.coverage_gate),
        "mutation_gate": float(state.get("mutation_gate") or settings.mutation_gate),
        "results": dict(state.get("results") or {}),
        "session_ids": list(state.get("session_ids") or []),
        "session_refs": list(state.get("session_refs") or []),
        "phase_timings": dict(state.get("phase_timings") or {}),
        "attempts_by_phase": dict(state.get("attempts_by_phase") or {}),
        "best_scores_by_phase": dict(state.get("best_scores_by_phase") or {}),
        "max_attempts_by_phase": maximums,
        "phase_results": dict(state.get("phase_results") or {}),
        "turn_history": list(state.get("turn_history") or []),
        "index_query_command": (
            "uta query-index --repo . --language python "
            f"--target {target.display_name} --json-output"
        ),
        "side_effect_hints": [
            str(item) for item in context_payload.get("side_effect_hints") or []
        ],
        "companion_files": [
            item.get("path") for item in context_payload.get("companion_files") or []
            if item.get("path")
        ],
        # An RDC-triggered task carries the behaviour the change should
        # produce; without it the prompts had only the implementation to infer
        # intent from, which is what spec context exists to prevent.
        "spec_context": _spec_context(state),
        # Empty here means "measure nothing", and nothing scores 100% -- so a
        # repair task whose changed lines never arrived prechecks as already
        # passing and skips generation. Fall back to what CI enforcement
        # recorded on the task.
        "changed_lines": _changed_lines(state),
        "base_ref": _base_ref(state),
    }


__all__ = ["prepare_python_cycle_state"]

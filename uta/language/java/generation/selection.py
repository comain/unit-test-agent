"""Which Java classes the next generation turn works on.

Batch selection is a policy decision driven by source complexity and module
locality, plus the target-identity aliases the workflow state carries. It is
separate from the Maven leaves it measures with, so the batching rules can be
read and tested without process execution.
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.language.java.workspace import candidate_source_path as _candidate_source_path
from uta.language.java.workspace import class_module as _class_module
from uta.shared.config import settings as uta_settings
from uta.shared.targets import TargetIdentity
from uta.testgen.graph.state import AgentState
from uta.testgen.progress import set_stage as _set_stage
from uta.testgen.workspace_guard import (
    verify_task_branch_and_preexisting_diff as _verify_task_branch_and_preexisting_diff,
)

logger = logging.getLogger("uta")


def _java_target_selection(class_fqn: str) -> Dict[str, Any]:
    return TargetIdentity.java_class(class_fqn).as_selection()


def _target_alias_update(batch: List[str]) -> Dict[str, Any]:
    target_batch = [_java_target_selection(fqn) for fqn in batch]
    return {
        "current_target_batch": target_batch,
        "current_target": target_batch[0] if target_batch else None,
    }



def _should_stop_after(state: Dict[str, Any], stage: str) -> bool:
    target = (state.get("stop_after_stage") or "").strip()
    return bool(target and target == stage)



def _source_complexity_summary(source_path: str, coverage_gate: int) -> Dict[str, Any]:
    summary = {
        "source_path": source_path,
        "line_count": 0,
        "public_method_count": 0,
        "strict_coverage": False,
    }
    try:
        text = Path(source_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return summary

    summary["line_count"] = len(text.splitlines())
    summary["public_method_count"] = len(
        re.findall(r"^\s*public\s+(?!class\b|interface\b|enum\b|@interface\b)[^{;\n]*\(", text, flags=re.MULTILINE)
    )
    summary["strict_coverage"] = bool(
        coverage_gate >= 70
        and (summary["line_count"] >= 250 or summary["public_method_count"] >= 8)
    )
    return summary



def _batch_complexity_profile(state: Dict[str, Any], class_fqn: str) -> Dict[str, Any]:
    meta = _source_complexity_summary(
        _candidate_source_path(state, class_fqn),
        int(state.get("coverage_gate") or uta_settings.coverage_gate),
    )
    line_count = int(meta.get("line_count") or 0)
    public_methods = int(meta.get("public_method_count") or 0)
    is_complex = (
        line_count <= 0
        or line_count >= int(uta_settings.smart_complex_line_threshold or 100)
        or public_methods >= int(uta_settings.smart_complex_public_method_threshold or 4)
    )
    return {
        **meta,
        "batch_kind": "complex" if is_complex else "simple",
        "is_complex": is_complex,
    }



def _select_smart_batch(state: Dict[str, Any], remaining: List[str], requested_cap: int) -> List[str]:
    if not remaining:
        return []
    smart_batch_context = (
        bool(state.get("production"))
        or state.get("quality_gate_backend") == "maven_enforcer"
        or state.get("quality_mode") == "ci_incremental"
    )
    if not smart_batch_context or not bool(uta_settings.smart_batching_enabled):
        return remaining[: max(1, requested_cap)]

    first = remaining[0]
    first_module = _class_module(state, first)
    first_profile = _batch_complexity_profile(state, first)
    if first_profile["is_complex"]:
        logger.info(
            "Smart batch selected single complex class: %s lines=%s public_methods=%s",
            first,
            first_profile.get("line_count"),
            first_profile.get("public_method_count"),
        )
        return [first]

    cap = requested_cap if requested_cap > 1 else int(uta_settings.smart_simple_batch_size or 3)
    cap = max(1, min(3, cap))
    batch = [first]
    profiles = {first: first_profile}
    for class_fqn in remaining[1:]:
        if len(batch) >= cap:
            break
        if _class_module(state, class_fqn) != first_module:
            break
        profile = _batch_complexity_profile(state, class_fqn)
        profiles[class_fqn] = profile
        if profile["is_complex"]:
            break
        batch.append(class_fqn)
    logger.info(
        "Smart batch selected %d simple class(es): %s profiles=%s",
        len(batch),
        batch,
        {
            class_fqn: {
                "lines": profile.get("line_count"),
                "public_methods": profile.get("public_method_count"),
                "kind": profile.get("batch_kind"),
            }
            for class_fqn, profile in profiles.items()
        },
    )
    return batch





def _plan_breadth_replan_reason(class_fqn: str, breadth: Any) -> Optional[str]:
    """Return a replan reason only for fatal breadth issues.

    OVER means the plan is noisy, but it can still be a usable generation plan
    when feasibility passes. Treating OVER as fatal caused valid plans to be
    discarded and replaced by long replanning turns.
    """
    verdict = getattr(getattr(breadth, "verdict", None), "value", getattr(breadth, "verdict", None))
    if verdict == "UNDER":
        return f"[{class_fqn}] {breadth.message}"
    return None




def select_next_class(state: AgentState) -> Dict[str, Any]:
    _set_stage(state, "select_batch", "choose next batch", class_fqns=[])
    candidates = state["candidates"]
    results = state["results"]
    batch_cap = state.get("classes_per_agent_run", 1)

    remaining = [fqn for fqn in candidates if fqn not in results]
    if not remaining:
        return {
            "finished": True,
            "current_batch": [],
            "current_class": None,
            "current_stage": "select_batch",
            **_target_alias_update([]),
        }
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if task_id and task_db_path:
        try:
            from uta.tasks.manager import TaskManager
            from uta.tasks.models import TERMINAL_CLASS_STATUSES

            rows = TaskManager(task_db_path).db.class_tasks_by_fqn(int(task_id), remaining)
            # Skip classes already in a terminal state in the DB (e.g. PASS from a prior
            # successful run that wasn't reflected in the in-memory results dict on resume).
            already_done = {fqn for fqn, row in rows.items() if row["status"] in TERMINAL_CLASS_STATUSES}
            if already_done:
                logger.info("select_batch: skipping %d already-terminal DB class(es): %s", len(already_done), already_done)
                remaining = [fqn for fqn in remaining if fqn not in already_done]
            if not remaining:
                return {
                    "finished": True,
                    "current_batch": [],
                    "current_class": None,
                    "current_stage": "select_batch",
                    **_target_alias_update([]),
                }
            candidate_index = {fqn: index for index, fqn in enumerate(candidates)}
            remaining = sorted(
                remaining,
                key=lambda fqn: (
                    int(rows[fqn]["priority"]) if fqn in rows else 100,
                    candidate_index.get(fqn, 10**9),
                ),
            )
        except Exception:
            logger.debug("Failed to apply production class priority ordering", exc_info=True)

    batch = _select_smart_batch(state, remaining, max(1, int(batch_cap or 1)))
    active_module = _class_module(state, batch[0]) if batch else state.get("module")
    _verify_task_branch_and_preexisting_diff(state, batch)
    logger.info(
        "Selected batch: requested=%d actual=%d classes=%s",
        batch_cap,
        len(batch),
        batch,
    )
    update = {
        "current_batch": batch,
        "current_class": batch[0],
        "finished": False,
        "current_stage": "select_batch",
        **_target_alias_update(batch),
    }
    if "module_filter" not in state:
        update["module_filter"] = state.get("module")
    if active_module:
        update["module"] = active_module
    return update


__all__ = [
    "_batch_complexity_profile",
    "_java_target_selection",
    "_plan_breadth_replan_reason",
    "_select_smart_batch",
    "_should_stop_after",
    "_source_complexity_summary",
    "_target_alias_update",
    "select_next_class",
]

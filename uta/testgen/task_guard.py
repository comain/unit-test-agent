"""Budget and stop enforcement around an LLM phase.

The half of the old `workspace_guard.py` that needs the task queue: check
whether a stop was requested, whether the budget is spent, and whether the
diff an LLM produced stayed inside what the workspace policy allows.

The policy itself -- which paths are allowed, how to read git status -- has no
use for a queue and lives in `workspace_policy.py`, so the deterministic
enforcement lane can use it without importing any of this.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from uta.testgen.ports.registry import task_ports_from_state
from uta.shared.workspace_policy import (
    TaskBudgetExceeded,
    TaskStopRequested,
    TaskUnsafeDiffError,
    _git_run,
    allowed_llm_path,
    allowed_preexisting_dirty_path,
    git_status_paths,
    git_status_snapshot,
    task_fqns_for_guard,
    workspace_policy_for,
)

logger = logging.getLogger("uta")


def llm_guard_before(
    state: Optional[Dict[str, Any]], batch: List[str], phase: str
) -> Optional[Dict[str, Any]]:
    if not state or not state.get("task_id") or not state.get("task_db_path"):
        return None
    repo_path = state.get("repo_path")
    if not repo_path:
        return None
    task_id = str(state["task_id"])
    ports = task_ports_from_state(state)
    if ports is not None:
        stop_reason = ports.stop_reason(task_id) if hasattr(ports, "stop_reason") else None
        if stop_reason:
            if hasattr(ports, "mark_stopped"):
                ports.mark_stopped(task_id, reason=stop_reason, stage=phase)
            raise TaskStopRequested(stop_reason)

        guarded_fqns = task_fqns_for_guard(state, batch)
        if hasattr(ports, "check_turn_budget_and_increment"):
            ports.check_turn_budget_and_increment(task_id, guarded_fqns, phase=phase)

        workspace_policy_for(state, batch).cleanup_runtime_residue(repo_path)
        if hasattr(ports, "add_event"):
            ports.add_event(
                task_id,
                "llm_progress",
                {
                    "message": f"Starting LLM phase {phase}",
                    "stage": phase,
                    "payload": {"batch": batch},
                },
            )
    return {
        "repo_path": repo_path,
        "before": git_status_snapshot(repo_path),
        "phase": phase,
        "batch": list(batch or []),
    }


def llm_guard_after(
    state: Optional[Dict[str, Any]], snapshot: Optional[Dict[str, Any]]
) -> None:
    if (
        not state
        or not snapshot
        or not state.get("task_id")
        or not state.get("task_db_path")
    ):
        return
    repo_path = snapshot["repo_path"]
    batch = list(snapshot.get("batch") or [])
    before = dict(snapshot.get("before") or {})
    policy = workspace_policy_for(state, batch)
    policy.cleanup_runtime_residue(repo_path)
    policy.cleanup_llm_generated_residue(repo_path, before)
    after = git_status_snapshot(repo_path)
    changed_paths = {
        path
        for path in set(before.keys()) | set(after.keys())
        if before.get(path) != after.get(path)
    }
    unsafe = [
        path
        for path in sorted(changed_paths)
        if not allowed_llm_path(path, state, batch)
    ]
    if not unsafe:
        return
    unsafe_message = "Unsafe LLM-authored paths: " + ", ".join(unsafe)
    ports = task_ports_from_state(state)
    if ports is not None:
        ports.record_unsafe_diff(
            str(state["task_id"]),
            task_fqns_for_guard(state, batch),
            stage=snapshot.get("phase") or "",
            message=unsafe_message,
            paths=unsafe,
        )
    raise TaskUnsafeDiffError(unsafe_message)


def verify_task_branch_and_preexisting_diff(
    state: Dict[str, Any], batch: List[str]
) -> None:
    if not state.get("task_id") or not state.get("task_db_path"):
        return
    repo_path = state.get("repo_path")
    expected_branch = state.get("branch_name")
    if not repo_path:
        return
    workspace_policy_for(state, batch).cleanup_runtime_residue(repo_path)
    branch_result = _git_run(
        repo_path,
        "branch",
        "--show-current",
        capture_output=True,
        check=False,
        text=True,
    )
    if branch_result.returncode == 0 and expected_branch:
        current_branch = branch_result.stdout.strip()
        if current_branch and current_branch != expected_branch:
            message = f"Production task branch mismatch: expected={expected_branch} current={current_branch}"
            ports = task_ports_from_state(state)
            if ports is not None:
                ports.mark_failed(str(state["task_id"]), message, stage="branch_safety")
            raise TaskUnsafeDiffError(message)

    dirty_paths = sorted(git_status_paths(repo_path))
    unsafe = [
        path
        for path in dirty_paths
        if not allowed_preexisting_dirty_path(path, state, batch)
    ]
    if unsafe:
        message = "Pre-existing unsafe dirty paths before class run: " + ", ".join(
            unsafe
        )
        ports = task_ports_from_state(state)
        if ports is not None:
            ports.record_unsafe_diff(
                str(state["task_id"]),
                task_fqns_for_guard(state, batch),
                stage="branch_safety",
                message=message,
                fail_task=True,
            )
        raise TaskUnsafeDiffError(message)

# Re-exported on purpose: these names are imported from this module
# elsewhere in the tree. Declaring them makes that a contract rather than
# an accident, and lets the linter tell a re-export from a dead import.
__all__ = [
    "TaskBudgetExceeded",
]

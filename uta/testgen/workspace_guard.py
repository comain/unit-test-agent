"""Workspace safety, kept importable under its original name."""

from __future__ import annotations

from uta.shared.workspace_policy import (
    TaskBudgetExceeded,
    TaskStopRequested,
    TaskUnsafeDiffError,
    WorkspacePolicy,
    allowed_llm_path,
    allowed_preexisting_dirty_path,
    extra_authored_test_paths,
    git_status_paths,
    git_status_snapshot,
    guard_language,
    looks_like_test_artifact_path,
    normalize_commit_path,
    task_fqns_for_guard,
    workspace_policies,
    workspace_policy_for,
)
from uta.testgen.task_guard import (
    llm_guard_after,
    llm_guard_before,
    verify_task_branch_and_preexisting_diff,
)

__all__ = [
    "TaskBudgetExceeded",
    "TaskStopRequested",
    "TaskUnsafeDiffError",
    "WorkspacePolicy",
    "allowed_llm_path",
    "allowed_preexisting_dirty_path",
    "extra_authored_test_paths",
    "git_status_paths",
    "git_status_snapshot",
    "guard_language",
    "llm_guard_after",
    "llm_guard_before",
    "looks_like_test_artifact_path",
    "normalize_commit_path",
    "task_fqns_for_guard",
    "verify_task_branch_and_preexisting_diff",
    "workspace_policies",
    "workspace_policy_for",
]

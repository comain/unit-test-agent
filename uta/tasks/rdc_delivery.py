"""RDC delivery task management and result synchronization."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from uta.shared.delivery import (
    PushConflictError,
    PushPolicyError,
    RdcDeliveryContext,
    RdcRepairPublisher,
    is_allowed_rdc_test_path,
    is_ignored_rdc_runtime_artifact,
    passed_result_targets_and_paths,
    repo_relative_path,
    result_targets_and_paths,
)
from uta.tasks.models import json_loads


def rdc_delivery_context_from_task(
    manager: Any,
    task_id: int,
    branch_name: str,
    target_ids: Iterable[str],
    *,
    rdc_context: Optional[Dict[str, Any]] = None,
    commit_paths: Optional[List[str]] = None,
) -> Optional[RdcDeliveryContext]:
    task = manager.get_task(task_id)
    if not task:
        return None
    context = rdc_context or json_loads(task.get("rdc_context_json") or "{}")
    if not context:
        return None
    pipeline = context.get("pipeline") or {}
    if not pipeline:
        return None
    jira = context.get("jira") or {}
    return RdcDeliveryContext(
        branch_name=branch_name,
        repo_task_id=task_id,
        rdc_task_id=str(pipeline.get("taskId") or ""),
        rdc_record_id=str(pipeline.get("recordId") or ""),
        jira_key=str(pipeline.get("jiraId") or jira.get("id") or ""),
        class_fqns=list(target_ids),
        commit_paths=commit_paths,
    )


def checkpoint_rdc_repair_results(
    *,
    repo: str | Path,
    manager: Any,
    task_id: int,
    branch_name: str,
    results: Dict[str, Any],
    target_ids: Optional[Iterable[str]] = None,
    commit_paths: Optional[List[str]] = None,
    rdc_context: Optional[Dict[str, Any]] = None,
    delivery_context: Optional[RdcDeliveryContext] = None,
    module: Optional[str] = None,
    phase_token_usage: Optional[Dict[str, Any]] = None,
) -> bool:
    selected_targets = list(target_ids or [])
    if not selected_targets:
        selected_targets, commit_paths = passed_result_targets_and_paths(repo, results)
    if not selected_targets:
        return False
    try:
        return commit_rdc_repair_results(
            repo=repo,
            manager=manager,
            task_id=task_id,
            branch_name=branch_name,
            results=results,
            target_ids=selected_targets,
            commit_paths=commit_paths,
            rdc_context=rdc_context,
            delivery_context=delivery_context,
            module=module,
            phase_token_usage=phase_token_usage,
        )
    except PushPolicyError as exc:
        if str(exc) == "RDC repair delivery found no test changes to commit":
            if record_existing_repair_commit(
                manager=manager,
                task_id=task_id,
                target_ids=selected_targets,
                results=results,
                module=module,
                phase_token_usage=phase_token_usage,
            ):
                return True
            manager.record_commit(
                task_id,
                class_fqns=selected_targets,
                commit_sha=None,
                remote_ref=None,
            )
            return False
        manager.record_push_failed(
            task_id,
            branch_name=branch_name,
            message=str(exc),
            class_fqns=selected_targets,
        )
        raise
    except PushConflictError as exc:
        manager.record_push_failed(
            task_id,
            branch_name=branch_name,
            message=str(exc),
            class_fqns=selected_targets,
        )
        raise


def commit_rdc_repair_results(
    *,
    repo: str | Path,
    manager: Any,
    task_id: int,
    branch_name: str,
    results: Dict[str, Any],
    target_ids: Optional[Iterable[str]] = None,
    commit_paths: Optional[List[str]] = None,
    rdc_context: Optional[Dict[str, Any]] = None,
    delivery_context: Optional[RdcDeliveryContext] = None,
    module: Optional[str] = None,
    phase_token_usage: Optional[Dict[str, Any]] = None,
) -> bool:
    selected_targets = list(target_ids or [])
    selected_paths = commit_paths
    if not selected_targets:
        selected_targets, selected_paths = passed_result_targets_and_paths(repo, results)
    if not selected_targets:
        return False

    if not branch_name:
        manager.record_push_failed(
            task_id,
            branch_name="",
            message="RDC repair delivery skipped: repo task has no branch_name",
            class_fqns=selected_targets,
        )
        return False

    if delivery_context:
        context = RdcDeliveryContext(
            branch_name=delivery_context.branch_name or branch_name,
            repo_task_id=delivery_context.repo_task_id if delivery_context.repo_task_id is not None else task_id,
            rdc_task_id=delivery_context.rdc_task_id,
            rdc_record_id=delivery_context.rdc_record_id,
            jira_key=delivery_context.jira_key,
            class_fqns=selected_targets,
            commit_paths=selected_paths,
        )
    else:
        context = rdc_delivery_context_from_task(
            manager,
            task_id,
            branch_name,
            selected_targets,
            rdc_context=rdc_context,
            commit_paths=selected_paths,
        )
    if not context:
        return False

    push_result = RdcRepairPublisher(repo).publish(context)
    manager.record_push_verified(
        task_id,
        branch_name=context.branch_name,
        local_head=push_result.commit_sha,
        remote_head=push_result.remote_ref,
    )
    manager.record_commit(
        task_id,
        class_fqns=selected_targets,
        commit_sha=push_result.commit_sha,
        pushed_at=push_result.pushed_at,
        remote_ref=push_result.remote_ref,
    )
    selected_results = {target_id: results[target_id] for target_id in selected_targets if target_id in results}
    if selected_results:
        manager.sync_results(
            task_id,
            selected_results,
            module=module,
            phase_token_usage=phase_token_usage,
            elapsed_seconds=None,
        )
    return True


def record_existing_repair_commit(
    *,
    manager: Any,
    task_id: int,
    target_ids: Iterable[str],
    results: Dict[str, Any],
    module: Optional[str] = None,
    phase_token_usage: Optional[Dict[str, Any]] = None,
) -> bool:
    task = manager.get_task(task_id)
    latest_commit = task.get("latest_commit") if task else None
    remote_ref = task.get("remote_ref") if task else None
    selected_targets = list(target_ids)
    manager.record_commit(
        task_id,
        class_fqns=selected_targets,
        commit_sha=latest_commit,
        pushed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        remote_ref=remote_ref,
    )
    selected_results = {target_id: results[target_id] for target_id in selected_targets if target_id in results}
    if selected_results:
        manager.sync_results(
            task_id,
            selected_results,
            module=module,
            phase_token_usage=phase_token_usage,
            elapsed_seconds=None,
        )
    return True


__all__ = [
    "PushConflictError",
    "PushPolicyError",
    "RdcDeliveryContext",
    "RdcRepairPublisher",
    "checkpoint_rdc_repair_results",
    "commit_rdc_repair_results",
    "is_allowed_rdc_test_path",
    "is_ignored_rdc_runtime_artifact",
    "passed_result_targets_and_paths",
    "rdc_delivery_context_from_task",
    "record_existing_repair_commit",
    "repo_relative_path",
    "result_targets_and_paths",
]

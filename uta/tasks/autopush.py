from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from uta.ci_plugin.auto_push import AutoPushContext, CiAutoPusher
from uta.tasks.models import json_loads


def repo_relative_path(repo: str | Path, path_value: Any) -> Optional[str]:
    if not path_value:
        return None
    repo_path = Path(repo).expanduser().resolve()
    path = Path(str(path_value)).expanduser()
    try:
        if path.is_absolute():
            path = path.resolve().relative_to(repo_path)
    except ValueError:
        return None
    normalized = path.as_posix().lstrip("/")
    return normalized or None


def passed_result_targets_and_paths(repo: str | Path, results: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    target_ids: List[str] = []
    commit_paths: List[str] = []
    for target_id, result in (results or {}).items():
        if str((result or {}).get("status") or "").upper() != "PASS":
            continue
        target_ids.append(str(target_id))
        rel_path = repo_relative_path(repo, (result or {}).get("test_file_path"))
        if rel_path:
            commit_paths.append(rel_path)
    conftest = Path(repo) / "tests" / "uta_generated" / "conftest.py"
    if target_ids and conftest.exists():
        commit_paths.append("tests/uta_generated/conftest.py")
    return target_ids, sorted(dict.fromkeys(commit_paths))


def ci_auto_push_context_from_task(
    manager: Any,
    task_id: int,
    branch_name: str,
    target_ids: Iterable[str],
    *,
    ci_context: Optional[Dict[str, Any]] = None,
    commit_paths: Optional[List[str]] = None,
) -> Optional[AutoPushContext]:
    task = manager.get_task(task_id)
    if not task:
        return None
    context = ci_context or json_loads(task.get("ci_context_json") or "{}")
    if not context:
        return None
    pipeline = context.get("pipeline") or {}
    if not pipeline:
        return None
    jira = context.get("jira") or {}
    return AutoPushContext(
        branch_name=branch_name,
        repo_task_id=task_id,
        ci_task_id=str(pipeline.get("taskId") or ""),
        ci_record_id=str(pipeline.get("recordId") or ""),
        jira_key=str(pipeline.get("jiraId") or jira.get("id") or ""),
        class_fqns=list(target_ids),
        commit_paths=commit_paths,
    )


def commit_ci_repair_results(
    *,
    repo: str | Path,
    manager: Any,
    task_id: int,
    branch_name: str,
    results: Dict[str, Any],
    target_ids: Optional[Iterable[str]] = None,
    commit_paths: Optional[List[str]] = None,
    ci_context: Optional[Dict[str, Any]] = None,
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
            message="CI repair auto-push skipped: repo task has no branch_name",
            class_fqns=selected_targets,
        )
        return False

    context = ci_auto_push_context_from_task(
        manager,
        task_id,
        branch_name,
        selected_targets,
        ci_context=ci_context,
        commit_paths=selected_paths,
    )
    if not context:
        return False

    push_result = CiAutoPusher(repo).commit_and_push(context)
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
    if not latest_commit:
        return False
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

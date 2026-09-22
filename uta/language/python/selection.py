"""Product-task selection and checkpoint recovery for Python batches."""

from __future__ import annotations

from uta.testgen.spec_context import MAX_SPEC_CONTEXT_BYTES

from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.testgen.batch import BatchGenerationRequest
from uta.shared.targets import TargetRef, target_identity_from_row
from uta.tasks.rdc_delivery import checkpoint_rdc_repair_results, repo_relative_path
from uta.tasks.models import TERMINAL_CLASS_STATUSES, json_loads


def prepare_python_batch_targets(
    request: BatchGenerationRequest,
) -> tuple[List[TargetRef], Dict[str, Dict[str, Any]], Optional[Any]]:
    """Apply production resume/checkpoint policy once before graph iteration."""
    repo = Path(request.repo_path).expanduser().resolve()
    targets = list(request.targets or [])
    results: Dict[str, Dict[str, Any]] = {}
    manager = _python_task_manager(request)
    if not manager or not request.task_id:
        return targets, results, manager
    targets = include_uncheckpointed_terminal_targets(
        manager, int(request.task_id), targets
    )
    targets = _checkpoint_existing_terminal_targets(
        manager=manager,
        request=request,
        repo=repo,
        targets=targets,
        results=results,
    )
    return skip_terminal_task_targets(manager, int(request.task_id), targets), results, manager


def _python_task_manager(request: BatchGenerationRequest) -> Optional[Any]:
    if not request.task_id or not request.task_db_path:
        return None
    from uta.tasks.manager import TaskManager

    return TaskManager(request.task_db_path)


def _checkpoint_terminal_target(
    *,
    manager: Any,
    request: BatchGenerationRequest,
    repo: Path,
    target: TargetRef,
    result: Dict[str, Any],
) -> bool:
    if not manager or not request.task_id:
        return False
    task = manager.get_task(int(request.task_id))
    if not task:
        return False
    commit_path = repo_relative_path(repo, result.get("test_file_path"))
    if not commit_path:
        return False
    return checkpoint_rdc_repair_results(
        repo=repo,
        manager=manager,
        task_id=int(request.task_id),
        branch_name=str(task.get("branch_name") or ""),
        results={target.target_id: result},
        target_ids=[target.target_id],
        commit_paths=[commit_path],
        module=task.get("module_filter") if "module_filter" in task.keys() else None,
        phase_token_usage=(
            result.get("phase_token_usage")
            if isinstance(result.get("phase_token_usage"), dict)
            else None
        ),
    )


def skip_terminal_task_targets(
    manager: Any, task_id: int, targets: List[TargetRef]
) -> List[TargetRef]:
    task = manager.get_task(task_id)
    context = json_loads(task.get("rdc_context_json") or "{}") if task else {}
    should_checkpoint = bool(
        (context.get("pipeline") or {}) if isinstance(context, dict) else False
    )
    runnable: List[TargetRef] = []
    for target in targets:
        row = manager.find_target_task(task_id, target)
        if (
            should_checkpoint
            and row
            and row.get("status") == "PASS"
            and (not row.get("commit_sha") or not row.get("pushed_at"))
        ):
            runnable.append(target)
        elif not row or row.get("status") not in TERMINAL_CLASS_STATUSES:
            runnable.append(target)
    return runnable


def include_uncheckpointed_terminal_targets(
    manager: Any, task_id: int, targets: List[TargetRef]
) -> List[TargetRef]:
    task = manager.get_task(task_id)
    context = json_loads(task.get("rdc_context_json") or "{}") if task else {}
    if not bool(
        (context.get("pipeline") or {}) if isinstance(context, dict) else False
    ):
        return targets
    seen = {(target.language, target.target_id) for target in targets}
    restored: List[TargetRef] = []
    for row in manager.list_class_tasks(task_id):
        if row.get("language") != "python":
            continue
        if row.get("status") not in TERMINAL_CLASS_STATUSES:
            continue
        if (row.get("commit_sha") and row.get("pushed_at")) or not row.get(
            "test_file_path"
        ):
            continue
        target = target_identity_from_row(row)
        key = (target.language, target.target_id)
        if key not in seen:
            seen.add(key)
            restored.append(target)
    return restored + list(targets)


def _checkpoint_existing_terminal_targets(
    *,
    manager: Any,
    request: BatchGenerationRequest,
    repo: Path,
    targets: List[TargetRef],
    results: Dict[str, Dict[str, Any]],
) -> List[TargetRef]:
    remaining: List[TargetRef] = []
    for target in targets:
        row = manager.find_target_task(int(request.task_id), target)
        if not _needs_existing_terminal_checkpoint(row):
            remaining.append(target)
            continue
        result = _result_payload_from_task_row(row)
        results[target.target_id] = result
        if not _checkpoint_terminal_target(
            manager=manager,
            request=request,
            repo=repo,
            target=target,
            result=result,
        ):
            remaining.append(target)
    return remaining


def _needs_existing_terminal_checkpoint(row: Optional[Dict[str, Any]]) -> bool:
    return bool(
        row
        and row.get("status") in TERMINAL_CLASS_STATUSES
        and (not row.get("commit_sha") or not row.get("pushed_at"))
        and row.get("test_file_path")
    )


def _result_payload_from_task_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": row.get("status") or "PASS",
        "language": row.get("language") or "python",
        "target_id": row.get("target_id") or row.get("class_fqn"),
        "display_name": row.get("display_name") or row.get("target_id") or row.get("class_fqn"),
        "source_path": row.get("source_path"),
        "target_granularity": row.get("target_granularity") or "file",
        "test_file_path": row.get("test_file_path"),
        "coverage": row.get("coverage"),
        "mutation_score": row.get("mutation_score"),
        "surviving_mutants": row.get("surviving_mutants"),
        "total_mutants": row.get("total_mutants"),
        "test_count": row.get("test_count"),
        "phase_token_usage": json_loads(row.get("phase_token_usage_json") or "{}"),
    }


__all__ = [
    "include_uncheckpointed_terminal_targets",
    "prepare_python_batch_targets",
    "skip_terminal_task_targets",
]


def prompt_spec_context_from_task_context(
    manager: Any,
    task_id: Optional[int],
    *,
    explicit: str = "",
) -> str:
    """Merge the operator's spec context with the RDC task's own description.

    An RDC-triggered task already carries the behaviour the change is meant to
    produce -- the requester's context and the issue description -- and the
    repair prompts were being written without it. The agent then had only the
    implementation to infer intent from, which is exactly the failure mode
    spec context exists to prevent.

    The explicit value leads, because an operator who supplied one meant it.
    Sections are de-duplicated so a description pasted into both places is not
    repeated, and the whole thing is bounded: this goes into a prompt.
    """
    sections: List[str] = []
    if str(explicit or "").strip():
        sections.append(str(explicit).strip())
    if manager and task_id:
        task = manager.get_task(task_id)
        context = json_loads(task.get("rdc_context_json") or "{}") if task else {}
        user = context.get("user") if isinstance(context.get("user"), dict) else {}
        issue = context.get("issue") if isinstance(context.get("issue"), dict) else {}
        user_context = str(user.get("context") or "").strip()
        issue_description = str(issue.get("description") or "").strip()
        if user_context and user_context not in sections:
            sections.append(user_context)
        if issue_description and issue_description not in sections:
            sections.append(issue_description)
    return "\n\n".join(sections)[:MAX_SPEC_CONTEXT_BYTES]


def changed_lines_from_task_context(
    manager: Any, task_id: Optional[int]
) -> Optional[Dict[str, List[int]]]:
    """The changed lines the CI enforcement recorded for this task.

    Python verification scopes coverage and mutation to these lines. With an
    empty map it measures nothing, and nothing scores as 100% -- so a repair
    task whose changed lines never arrived prechecks as already-passing and
    skips generation entirely.

    That is what happened on beta: enforcement correctly failed
    `dify_app_sync.py` at 9.09% over eighteen new lines, the repair task ran
    `precheck_existing_tests` -> `complete_generation` with zero LLM turns and
    zero tokens, and the session went green having written nothing. The lines
    were in the task's `rdc_context_json` the whole time.

    Ported from main's `uta/language/python/batch.py`, which this branch
    replaced with a facade; the helper went with it.
    """
    if not (manager and task_id):
        return None
    task = manager.get_task(int(task_id))
    if not task:
        return None
    context = json_loads(task.get("rdc_context_json") or "{}")
    enforcement = context.get("enforcement") if isinstance(context, dict) else None
    evidence = enforcement.get("evidence") if isinstance(enforcement, dict) else None
    changed = (
        (evidence.get("changedLines") or evidence.get("changed_lines"))
        if isinstance(evidence, dict)
        else None
    )
    if not isinstance(changed, dict):
        return None
    normalized: Dict[str, List[int]] = {}
    for path, lines in changed.items():
        if not path:
            continue
        normalized[str(path)] = sorted({int(line) for line in lines or [] if int(line) > 0})
    return normalized or None

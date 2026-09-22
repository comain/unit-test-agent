"""Application-level workflow retention and checkpoint pruning coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agent_core.workflow import (
    WorkflowRunIdentity,
    delete_checkpoint_lineage,
    open_checkpointer,
)

from uta.tasks.db import TaskDB
from uta.tasks.workflow_retention import (
    eligible_identities,
    eligible_superseded_legacy_runs,
    eligible_terminal_task_ids,
)
from uta.testgen.graph.application import (
    CYCLE_NAME,
    CYCLE_VERSION,
    workflow_state_root,
)
from uta.testgen.operations import OperationArtifactStore
from uta.testgen.prompts.artifacts import (
    PromptArtifactRetention,
    PromptArtifactScopeError,
    configured_prompt_artifact_root,
)
from uta.testgen.standalone_execution import prune_standalone_generation_executions


@dataclass(frozen=True)
class WorkflowPruneResult:
    lineages_deleted: int = 0
    operations_deleted: int = 0
    artifacts_deleted: int = 0
    prompt_artifacts_deleted: int = 0
    progress_events_deleted: int = 0


def prune_workflows(
    task_db_path: str | Path,
    *,
    older_than_days: int = 30,
    progress_older_than_days: Optional[int] = None,
    now: Optional[datetime] = None,
    checkpointer_factory: Optional[Callable[..., Any]] = None,
) -> WorkflowPruneResult:
    """Prune eligible lineages without knowing LangGraph's database schema."""
    days = int(older_than_days)
    if days < 0:
        raise ValueError("workflow retention days must be non-negative")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = (current - timedelta(days=days)).replace(microsecond=0).isoformat()
    progress_days = days if progress_older_than_days is None else int(progress_older_than_days)
    if progress_days < 0:
        raise ValueError("progress event retention days must be non-negative")
    progress_cutoff = (current - timedelta(days=progress_days)).replace(
        microsecond=0
    ).isoformat()

    db = TaskDB(task_db_path)
    db.init()
    progress_count = db.prune_progress_events(before=progress_cutoff)
    terminal_task_ids = eligible_terminal_task_ids(db, cutoff=cutoff)
    candidates = eligible_identities(
        db,
        cutoff=cutoff,
        terminal_task_ids=terminal_task_ids,
    )
    eligible_legacy_runs = eligible_superseded_legacy_runs(db, cutoff=cutoff)
    prompt_retention = _prompt_retention_outside_repositories(db)
    prompt_count = 0
    prompt_count += prune_standalone_generation_executions(
        cutoff_timestamp=(current - timedelta(days=days)).timestamp()
    )
    if prompt_retention is not None:
        prompt_count += prompt_retention.prune_standalone(
            cutoff_timestamp=(current - timedelta(days=days)).timestamp()
        )
    if not candidates:
        if prompt_retention is not None:
            for task_id, legacy_run_id in eligible_legacy_runs:
                prompt_count += prompt_retention.delete_managed_legacy(
                    task_id=task_id,
                    workflow_run_id=legacy_run_id,
                )
            for task_id in terminal_task_ids:
                prompt_count += prompt_retention.delete_terminal_task_legacy(
                    task_id=task_id
                )
        return WorkflowPruneResult(
            prompt_artifacts_deleted=prompt_count,
            progress_events_deleted=progress_count,
        )

    root = workflow_state_root(db.path)
    artifacts = OperationArtifactStore(root / "results")
    opener = checkpointer_factory or open_checkpointer
    checkpoint_path = root / "checkpoints.sqlite"
    lineage_count = operation_count = artifact_count = 0
    with db.connect() as conn:
        forbidden_roots = tuple(
            Path(str(row["repo_path"]))
            for row in conn.execute("SELECT DISTINCT repo_path FROM repo_tasks")
            if row["repo_path"]
        )
    with opener(checkpoint_path, forbidden_roots=forbidden_roots) as checkpointer:
        for repo_task_id, workflow_run_id, unit_id in candidates:
            identity = WorkflowRunIdentity(
                product="uta",
                task_id=str(repo_task_id),
                unit_id=unit_id,
                workflow_run_id=workflow_run_id,
                cycle=CYCLE_NAME,
                version=CYCLE_VERSION,
            )
            delete_checkpoint_lineage(checkpointer, identity)
            removed_artifacts = artifacts.delete_unit(workflow_run_id, unit_id)
            with db.transaction() as conn:
                removed_operations = conn.execute(
                    """
                    DELETE FROM workflow_operations
                    WHERE repo_task_id=? AND workflow_run_id=? AND unit_id=?
                    """,
                    (repo_task_id, workflow_run_id, unit_id),
                ).rowcount
            if prompt_retention is not None:
                prompt_count += prompt_retention.delete_managed_unit(
                    task_id=repo_task_id,
                    workflow_run_id=workflow_run_id,
                    unit_id=unit_id,
                )
            lineage_count += 1
            operation_count += max(int(removed_operations or 0), 0)
            artifact_count += removed_artifacts
        if prompt_retention is not None:
            for task_id, legacy_run_id in eligible_legacy_runs:
                prompt_count += prompt_retention.delete_managed_legacy(
                    task_id=task_id,
                    workflow_run_id=legacy_run_id,
                )
            for task_id in terminal_task_ids:
                prompt_count += prompt_retention.delete_terminal_task_legacy(
                    task_id=task_id
                )
    return WorkflowPruneResult(
        lineages_deleted=lineage_count,
        operations_deleted=operation_count,
        artifacts_deleted=artifact_count,
        prompt_artifacts_deleted=prompt_count,
        progress_events_deleted=progress_count,
    )


def _prompt_retention_outside_repositories(
    db: TaskDB,
) -> Optional[PromptArtifactRetention]:
    try:
        root = configured_prompt_artifact_root()
    except PromptArtifactScopeError:
        return None
    if ".uta_cache" in root.parts:
        return None
    with db.connect() as conn:
        rows = conn.execute("SELECT DISTINCT repo_path FROM repo_tasks")
        for row in rows:
            try:
                repository = Path(str(row["repo_path"])).expanduser().resolve()
            except (OSError, RuntimeError):
                return None
            if (
                root == repository
                or repository in root.parents
                or root in repository.parents
            ):
                return None
    return PromptArtifactRetention(root=root)


__all__ = [
    "WorkflowPruneResult",
    "prune_workflows",
]

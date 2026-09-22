"""Commands that delete or redo past work.

Two commands, both destructive in a way the rest of the family is not:
`clean-rerun-generation` throws away a task's generation history so it can be
run again, and `prune-workflows` removes durable workflow state older than a
cutoff. They are separated from the control commands because stopping a task
is reversible and deleting its record is not.
"""

from __future__ import annotations

import click


def register_retention_commands(tasks_group, *, console) -> None:
    """Attach the retention commands to the tasks group."""

    @tasks_group.command("clean-rerun-generation")
    @click.argument("task_id", type=int)
    @click.option("--reason", required=True, help="Required audit reason for replacing the workflow lineage")
    @click.option("--confirm-task-id", required=True, type=int, help="Repeat TASK_ID to confirm potentially repeated model work")
    @click.option("--force-rerun-failed", is_flag=True, help="Requeue failed child rows as well as unfinished rows")
    @click.option("--force-rerun-all", is_flag=True, help="Requeue every child row, including passing rows")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_clean_rerun_generation(
        task_id,
        reason,
        confirm_task_id,
        force_rerun_failed,
        force_rerun_all,
        task_db,
    ):
        """Replace a confirmed corrupt or deliberately abandoned lineage."""
        from uta.tasks.manager import TaskManager

        try:
            new_run_id = TaskManager(task_db).clean_rerun_generation(
                task_id,
                reason=reason,
                confirm_task_id=confirm_task_id,
                force_rerun_failed=force_rerun_failed,
                force_rerun_all=force_rerun_all,
            )
        except (KeyError, RuntimeError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        console.print(
            f"[green]Task {task_id} queued with new workflow run {new_run_id}[/green]"
        )

    @tasks_group.command("prune-workflows")
    @click.option("--older-than-days", default=30, show_default=True, type=click.IntRange(min=0))
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_prune_workflows(older_than_days, task_db):
        """Delete eligible generation lineages through agent-core."""
        from uta.tasks.db import default_db_path
        from uta.app.retention import prune_workflows

        result = prune_workflows(
            task_db or default_db_path(), older_than_days=older_than_days
        )
        console.print(
            "[green]Pruned "
            f"{result.lineages_deleted} workflow lineages, "
            f"{result.operations_deleted} operations, and "
            f"{result.artifacts_deleted} operation artifacts, "
            f"{result.prompt_artifacts_deleted} prompt artifacts; removed "
            f"{result.progress_events_deleted} progress events[/green]"
        )

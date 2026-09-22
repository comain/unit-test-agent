"""Commands that change what a task is doing right now.

Start it, run the daemon that starts many, stop it, resume it, cancel it,
unblock it, or move it up and down the queue -- at the repo, class or target
level. These share an audience and a blast radius: each one takes effect on
running work, which is why they are reviewed together and kept apart from the
read-only views.
"""

from __future__ import annotations

import subprocess
import sys

import click

from uta.app.task_daemon import run_task_daemon


def register_control_commands(
    tasks_group,
    *,
    console,
    task_config_snapshot,
) -> None:
    """Attach the execution-control commands to the tasks group."""
    _task_config_snapshot = task_config_snapshot

    @tasks_group.command("start")
    @click.argument("task_id", required=False, type=int)
    @click.option("--next", "start_next", is_flag=True, help="Acquire and execute the next queued task")
    @click.option("--include-failed", is_flag=True, help="Allow --next to reacquire FAILED repo tasks")
    @click.option("--execute", is_flag=True, help="Run the selected task immediately in this process")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_start(task_id, start_next, include_failed, execute, task_db):
        from uta.tasks.manager import TaskManager
        from uta.tasks.scheduler import TaskScheduler

        manager = TaskManager(task_db)
        selected_id = task_id
        if start_next:
            execute = True
            task = TaskScheduler(str(manager.db_path)).acquire_next(include_failed=include_failed)
            if not task:
                console.print("[yellow]No queued task available[/yellow]")
                return
            selected_id = int(task["id"])
        if selected_id is None:
            raise click.ClickException("Provide TASK_ID or --next")
        if not start_next:
            manager.start_task(selected_id)
        console.print(f"[green]Task {selected_id} {'acquired' if start_next else 'queued'}[/green]")
        if execute:
            cmd = [sys.executable, "-m", "uta.app.cli", "run", "--production", "--task-id", str(selected_id), "--task-db", str(manager.db_path)]
            raise SystemExit(subprocess.call(cmd))

    @tasks_group.command("daemon")
    @click.option("--interval", "--poll-interval", default=10.0, show_default=True, type=float, help="Polling interval in seconds")
    @click.option("--heartbeat-interval", default=15.0, show_default=True, type=float, help="Heartbeat interval in seconds")
    @click.option("--once", is_flag=True, help="Poll once and exit")
    @click.option("--allow-same-repo-concurrency", is_flag=True, help="Allow multiple active tasks for one repo")
    @click.option("--include-failed", is_flag=True, help="Allow daemon to retry FAILED repo tasks")
    @click.option("--max-parallel", default=None, type=int, help="Max concurrent repo tasks (default: UTA_MAX_PARALLEL_REPOS or 1)")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_daemon(interval, heartbeat_interval, once, allow_same_repo_concurrency, include_failed, max_parallel, task_db):
        run_task_daemon(
            interval=interval,
            heartbeat_interval=heartbeat_interval,
            once=once,
            allow_same_repo_concurrency=allow_same_repo_concurrency,
            include_failed=include_failed,
            max_parallel=max_parallel,
            task_db=task_db,
            task_config_snapshot=_task_config_snapshot,
        )

    @tasks_group.command("stop")
    @click.argument("task_id", type=int)
    @click.option("--reason", default=None)
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_stop(task_id, reason, task_db):
        from uta.tasks.manager import TaskManager

        TaskManager(task_db).stop_task(task_id, reason=reason)
        console.print(f"[yellow]Stop requested for task {task_id}[/yellow]")

    @tasks_group.command("resume")
    @click.argument("task_id", type=int)
    @click.option("--force-rerun-failed", is_flag=True, help="Requeue failed child rows as well as unfinished rows")
    @click.option("--force-rerun-all", is_flag=True, help="Requeue every child row, including passing rows")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_resume(task_id, force_rerun_failed, force_rerun_all, task_db):
        from uta.tasks.manager import TaskManager

        TaskManager(task_db).resume_task(
            task_id,
            force_rerun_failed=force_rerun_failed,
            force_rerun_all=force_rerun_all,
        )
        console.print(f"[green]Task {task_id} queued for resume[/green]")

    @tasks_group.command("cancel")
    @click.argument("task_id", type=int)
    @click.option("--reason", default=None)
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_cancel(task_id, reason, task_db):
        from uta.tasks.manager import TaskManager

        TaskManager(task_db).cancel_task(task_id, reason=reason)
        console.print(f"[yellow]Task {task_id} cancelled[/yellow]")

    @tasks_group.command("unblock")
    @click.argument("task_id", type=int)
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_unblock(task_id, task_db):
        """Reset a POISONED or BUDGET_EXCEEDED task to QUEUED."""
        from uta.tasks.manager import TaskManager

        TaskManager(task_db).unblock(task_id)
        console.print(f"[green]Task {task_id} unblocked and queued[/green]")

    @tasks_group.command("reprioritize")
    @click.argument("task_id", type=int)
    @click.argument("priority_arg", required=False, type=int)
    @click.option("--priority", "priority_opt", type=int, help="New priority; lower runs first")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_reprioritize(task_id, priority_arg, priority_opt, task_db):
        from uta.tasks.manager import TaskManager

        priority = priority_opt if priority_opt is not None else priority_arg
        if priority is None:
            raise click.ClickException("Provide PRIORITY or --priority")
        TaskManager(task_db).reprioritize_task(task_id, priority)
        console.print(f"[green]Task {task_id} priority={priority}[/green]")

    @tasks_group.command("reprioritize-class")
    @click.argument("class_task_id", type=int)
    @click.argument("priority_arg", required=False, type=int)
    @click.option("--priority", "priority_opt", type=int, help="New priority; lower runs first")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_reprioritize_class(class_task_id, priority_arg, priority_opt, task_db):
        from uta.tasks.manager import TaskManager

        priority = priority_opt if priority_opt is not None else priority_arg
        if priority is None:
            raise click.ClickException("Provide PRIORITY or --priority")
        TaskManager(task_db).reprioritize_class(class_task_id, priority)
        console.print(f"[green]Class task {class_task_id} priority={priority}[/green]")

    @tasks_group.command("reprioritize-target")
    @click.argument("target_task_id", type=int)
    @click.argument("priority_arg", required=False, type=int)
    @click.option("--priority", "priority_opt", type=int, help="New priority; lower runs first")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_reprioritize_target(target_task_id, priority_arg, priority_opt, task_db):
        from uta.tasks.manager import TaskManager

        priority = priority_opt if priority_opt is not None else priority_arg
        if priority is None:
            raise click.ClickException("Provide PRIORITY or --priority")
        TaskManager(task_db).reprioritize_class(target_task_id, priority)
        console.print(f"[green]Target task {target_task_id} priority={priority}[/green]")

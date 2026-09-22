"""Subprocess scheduler for the ``uta tasks daemon`` command."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import click
from rich.console import Console

from uta.shared.config import settings


#: `highlight=False` because rich's automatic highlighter emboldens anything
#: that looks like a number or a path, which turns "Created task 1" into
#: "Created task \x1b[1m1\x1b[0m" -- styling the CLI never asked for.
#:
#: `soft_wrap=True` because the default wraps at the console width, and a task
#: database path broken across two lines cannot be copied out of a terminal.
#: That is a usability bug the tests happened to catch.
console = Console(highlight=False, soft_wrap=True)
logger = logging.getLogger("uta")


def _prune_workflows_if_due(
    task_db_path: Path,
    *,
    last_run_at: Optional[float],
    now_monotonic: float,
    force: bool = False,
) -> float:
    """Run bounded workflow retention at startup and on its six-hour cadence."""
    interval = max(float(settings.workflow_retention_interval_seconds), 1.0)
    if not force and last_run_at is not None and now_monotonic - last_run_at < interval:
        return last_run_at
    try:
        from uta.app.retention import prune_workflows

        result = prune_workflows(
            task_db_path,
            older_than_days=settings.workflow_checkpoint_retention_days,
            progress_older_than_days=settings.workflow_progress_event_retention_days,
        )
        if result.lineages_deleted or result.prompt_artifacts_deleted:
            logger.info(
                "workflow retention pruned %d lineages, %d operations, "
                "%d operation artifacts, %d prompt artifacts",
                result.lineages_deleted,
                result.operations_deleted,
                result.artifacts_deleted,
                result.prompt_artifacts_deleted,
            )
    except Exception:
        # Maintenance cannot take down scheduling. The timestamp bounds log
        # volume; operators can run the same retention path explicitly.
        logger.exception("workflow retention pass failed")
    return now_monotonic


def _daemon_child_environment(
    task: Dict[str, Any],
    *,
    environ: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """The environment one task's child process should run under.

    Java tasks may pin a JDK; Python tasks may pin a managed test environment.
    The task's own config snapshot wins over current deployment configuration:
    the runtime a task was created against is the runtime it should still use,
    even if the mapping changes while it sits in the queue.
    """
    base_environment = dict(os.environ if environ is None else environ)
    language = str(task.get("language") or "").lower()
    if language == "python":
        from uta.language.python.environment_recipes import (
            PYTHON_ENVIRONMENT_RECIPE_SNAPSHOT_KEY,
            PythonEnvironmentRecipe,
            apply_python_environment_recipe,
        )

        snapshot = json.loads(task.get("config_snapshot_json") or "{}")
        raw_recipe = snapshot.get(PYTHON_ENVIRONMENT_RECIPE_SNAPSHOT_KEY)
        if isinstance(raw_recipe, dict):
            return apply_python_environment_recipe(
                PythonEnvironmentRecipe.from_dict(raw_recipe),
                environ=base_environment,
            )
        return base_environment
    if language != "java":
        return base_environment

    from uta.language.java.runtime import RepositoryJavaRuntimeResolver

    snapshot = json.loads(task.get("config_snapshot_json") or "{}")
    repository = str(task.get("repo_slug") or "")
    resolver = RepositoryJavaRuntimeResolver(
        settings.daemon_java_home,
        settings.repository_java_homes,
    )
    snapshotted_java_home = str(snapshot.get("java_home") or "").strip()
    return resolver.environment_for(
        repository,
        environ=base_environment,
        java_home=snapshotted_java_home or None,
    )


def _daemon_child_popen_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {}
    return {"start_new_session": True}


def _terminate_daemon_child(proc: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    if os.name != "nt":
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            proc.wait(timeout=grace_seconds)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            return
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _cleanup_daemon_pool(pool: Dict[int, Dict[str, Any]], manager: Any, *, reason: str) -> List[int]:
    requeued: List[int] = []
    for task_id, slot in list(pool.items()):
        proc = slot["proc"]
        if proc.poll() is not None:
            continue
        console.print(f"[yellow]Stopping task {task_id}: {reason}[/yellow]")
        _terminate_daemon_child(proc)
        requeued.extend(manager.requeue_daemon_shutdown_tasks([task_id], reason=reason))
    return requeued


def _ensure_daemon_java_home() -> Optional[str]:
    configured_java_home = (os.environ.get("UTA_DAEMON_JAVA_HOME") or settings.daemon_java_home or "").strip()
    inherited_java_home = (os.environ.get("JAVA_HOME") or "").strip()
    chosen_java_home = ""

    if configured_java_home:
        configured_java = Path(configured_java_home) / "bin" / "java"
        if configured_java.exists() and os.access(configured_java, os.X_OK):
            chosen_java_home = configured_java_home
        elif os.environ.get("UTA_DAEMON_JAVA_HOME"):
            raise click.ClickException(f"UTA_DAEMON_JAVA_HOME does not contain bin/java: {configured_java_home}")

    if not chosen_java_home and inherited_java_home:
        chosen_java_home = inherited_java_home

    if chosen_java_home:
        os.environ["JAVA_HOME"] = chosen_java_home
        java_bin = str(Path(chosen_java_home) / "bin")
        path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
        if not path_entries or path_entries[0] != java_bin:
            os.environ["PATH"] = os.pathsep.join([java_bin, *[entry for entry in path_entries if entry != java_bin]])
        return chosen_java_home
    return None


# Match both current and pre-``uta.app`` command paths during rolling upgrades.
_UTA_TASK_PROCESS_RE = re.compile(
    r"(?:uta/(?:app/)?cli\.py|uta\.(?:app\.)?cli)\s+run\b[^\n]*?--task-id(?:=|\s+)(\d+)"
)


def _task_ids_from_process_listing(process_listing: str) -> set[int]:
    return {int(match.group(1)) for match in _UTA_TASK_PROCESS_RE.finditer(process_listing or "")}


def _live_uta_task_ids() -> set[int]:
    if os.name == "nt":
        return set()
    try:
        result = subprocess.run(
            ["ps", "-eo", "args="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return _task_ids_from_process_listing(result.stdout) if result.returncode == 0 else set()


def run_task_daemon(
    *,
    interval: float,
    heartbeat_interval: float,
    once: bool,
    allow_same_repo_concurrency: bool,
    include_failed: bool,
    max_parallel: Optional[int],
    task_db: Optional[str],
    task_config_snapshot: Callable[[], Dict[str, Any]],
) -> None:
    """Schedule queued tasks and supervise their worker subprocesses."""
    from uta.tasks.manager import TaskManager
    from uta.tasks.run_error_classifier import classify_run_error
    from uta.tasks.scheduler import TaskScheduler

    daemon_java_home = _ensure_daemon_java_home()
    scheduler = TaskScheduler(task_db)
    manager = TaskManager(str(scheduler.db.path))
    max_slots = max_parallel if max_parallel is not None else int(settings.max_parallel_repos)
    console.print(f"[green]UTA task daemon using {scheduler.db.path} (max-parallel={max_slots})[/green]")
    console.print(f"[dim]Java enforcement: JAVA_HOME={daemon_java_home or 'not configured'}[/dim]")

    pool: Dict[int, Dict[str, Any]] = {}
    pending_retries: Dict[int, Dict[str, Any]] = {}
    last_idle_heartbeat_at = 0.0
    last_workflow_retention_at = _prune_workflows_if_due(
        scheduler.db.path,
        last_run_at=None,
        now_monotonic=time.monotonic(),
        force=True,
    )
    transient_backoff_seconds = [30, 60, 120]
    transient_max_retries = 3
    previous_signal_handlers: Dict[int, Any] = {}
    shutdown_requested = False

    def request_daemon_shutdown(signum, frame) -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            raise SystemExit(128 + int(signum))
        shutdown_requested = True
        console.print("[yellow]Shutdown requested; draining running tasks before daemon exit[/yellow]")

    if os.name != "nt":
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_daemon_shutdown)

    def launch(task_id: int, retry_count: int = 0) -> None:
        cmd = [
            sys.executable,
            "-m",
            "uta.app.cli",
            "run",
            "--production",
            "--task-id",
            str(task_id),
            "--task-db",
            str(scheduler.db.path),
        ]
        task = manager.get_task(task_id)
        try:
            child_environment = _daemon_child_environment(task)
        except RuntimeError as exc:
            # A misconfigured Java home is a configuration error, not a task
            # failure to retry: every attempt would fail the same way, so fail
            # it once and say why.
            manager.mark_failed(task_id, str(exc), stage="runtime_configuration")
            console.print(
                f"[red]Task {task_id} runtime configuration failed: {exc}[/red]"
            )
            return
        proc = subprocess.Popen(
            cmd, env=child_environment, **_daemon_child_popen_kwargs()
        )
        pool[task_id] = {"proc": proc, "last_heartbeat": 0.0, "retry_count": retry_count}

    def queue_retry(task_id: int, retry_count: int, *, backoff_seconds: float = 0.0) -> None:
        manager.db.update_repo_task(
            task_id,
            status="QUEUED",
            current_stage="queued_retry",
            current_detail=f"retry {retry_count} after {backoff_seconds:.0f}s backoff",
        )
        manager.db.add_event(
            task_id,
            None,
            "task_retry_queued",
            f"Retry {retry_count} scheduled after {backoff_seconds:.0f}s",
            stage="queued_retry",
        )
        pending_retries[task_id] = {
            "retry_after": time.monotonic() + backoff_seconds,
            "retry_count": retry_count,
        }

    def reap_finished() -> None:
        for task_id in list(pool):
            proc = pool[task_id]["proc"]
            return_code = proc.poll()
            if return_code is None:
                continue
            return_code = int(return_code)
            retry_count = int(pool[task_id].get("retry_count", 0))
            del pool[task_id]

            if return_code != 0:
                task_row = manager.db.get_repo_task(task_id)
                error_message = (task_row["last_error"] or "") if task_row else ""
                if not error_message:
                    error_message = f"uta run exited with status {return_code}"
                policy = classify_run_error(error_message)
                logger.info(
                    "[%s] Task %d rc=%d retries=%d: %s",
                    policy,
                    task_id,
                    return_code,
                    retry_count,
                    error_message[:120],
                )

                if policy == "budget_abort":
                    manager.mark_budget_exceeded(task_id, error_message)
                    console.print(f"[red]Task {task_id} BUDGET_EXCEEDED — not retrying[/red]")
                elif policy == "transient" and retry_count < transient_max_retries:
                    backoff = transient_backoff_seconds[min(retry_count, len(transient_backoff_seconds) - 1)]
                    queue_retry(task_id, retry_count + 1, backoff_seconds=backoff)
                    console.print(
                        f"[yellow]Task {task_id} transient error — retry "
                        f"{retry_count + 1}/{transient_max_retries} after {backoff}s[/yellow]"
                    )
                elif policy == "internal_retry_once" and retry_count < 1:
                    queue_retry(task_id, 1)
                    console.print(f"[yellow]Task {task_id} internal error — retry 1/1 (immediate)[/yellow]")
                else:
                    manager.mark_failed(task_id, error_message, stage="runner_exit")
                    console.print(
                        f"[dim]Task {task_id} failed (policy={policy}, retries={retry_count})[/dim]"
                    )
            else:
                console.print(f"[dim]Task {task_id} finished ok[/dim]")

            scheduler.heartbeat(
                repo_task_id=task_id,
                status="IDLE",
                message=f"task exited {return_code}",
                config_snapshot=task_config_snapshot(),
            )

    def promote_ready_retries() -> None:
        now = time.monotonic()
        for task_id in list(pending_retries):
            entry = pending_retries[task_id]
            if now >= entry["retry_after"] and len(pool) < max_slots:
                del pending_retries[task_id]
                console.print(
                    f"[yellow]Launching retry for task {task_id} "
                    f"(attempt {entry['retry_count']})[/yellow]"
                )
                launch(task_id, retry_count=entry["retry_count"])

    def batch_cap_reached() -> bool:
        cap = settings.batch_cap_usd
        if cap is None:
            return False
        total = scheduler.db.total_provider_cost_usd()
        if total >= cap:
            logger.warning(
                "[BATCH CAP REACHED] spent $%.4f >= cap $%.4f — pausing dequeue",
                total,
                cap,
            )
            return True
        return False

    try:
        while True:
            reap_finished()
            now = time.monotonic()
            last_workflow_retention_at = _prune_workflows_if_due(
                scheduler.db.path,
                last_run_at=last_workflow_retention_at,
                now_monotonic=now,
            )

            if shutdown_requested and not pool:
                console.print("[yellow]No running tasks remain; daemon exit is now safe[/yellow]")
                return

            if not shutdown_requested:
                promote_ready_retries()
            live_task_ids = _live_uta_task_ids()
            recovered = manager.requeue_orphaned_running_tasks(
                active_task_ids=set(pool) | live_task_ids,
                stale_after_seconds=settings.task_runner_stale_heartbeat_seconds,
            )
            for task_id in recovered:
                console.print(
                    f"[yellow]Requeued orphaned RUNNING task {task_id} "
                    "after stale runner heartbeat[/yellow]"
                )

            if not shutdown_requested and not batch_cap_reached():
                while len(pool) < max_slots:
                    task = scheduler.acquire_next(
                        allow_same_repo_concurrency=allow_same_repo_concurrency,
                        include_failed=include_failed,
                        record_idle_heartbeat=not bool(pool),
                        exclude_task_ids=set(pool) | set(pending_retries) | live_task_ids,
                    )
                    if not task:
                        break
                    task_id = int(task["id"])
                    console.print(
                        f"[blue]Starting task {task_id} "
                        f"(slot {len(pool) + 1}/{max_slots})[/blue]"
                    )
                    launch(task_id)

            now = time.monotonic()
            for task_id, slot in pool.items():
                if now - slot["last_heartbeat"] >= max(heartbeat_interval, 1.0):
                    scheduler.heartbeat(
                        repo_task_id=task_id,
                        status="RUNNING",
                        message="task subprocess running",
                        config_snapshot=task_config_snapshot(),
                    )
                    slot["last_heartbeat"] = now

            if not pool and not pending_retries:
                if once:
                    console.print("[yellow]No queued task available[/yellow]")
                    return
                if now - last_idle_heartbeat_at >= max(heartbeat_interval, 1.0):
                    scheduler.heartbeat(
                        repo_task_id=None,
                        status="IDLE",
                        message="waiting for task",
                        config_snapshot=task_config_snapshot(),
                    )
                    last_idle_heartbeat_at = now

            time.sleep(min(max(interval, 1.0), max(heartbeat_interval, 1.0)))

            if once and not pool and not pending_retries:
                return
    finally:
        _cleanup_daemon_pool(pool, manager, reason="daemon exiting")
        for signum, previous in previous_signal_handlers.items():
            signal.signal(signum, previous)


__all__ = [
    "_cleanup_daemon_pool",
    "_daemon_child_popen_kwargs",
    "_ensure_daemon_java_home",
    "_live_uta_task_ids",
    "_prune_workflows_if_due",
    "_task_ids_from_process_listing",
    "_terminate_daemon_child",
    "run_task_daemon",
]

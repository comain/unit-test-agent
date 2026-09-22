"""Claiming work, and the controls that interrupt it.

`acquire_next_task` claims in one transaction so two runners cannot take the
same task.

A mixin rather than a collaborator: `TaskDB` owns the one connection and the
one transaction, and these read and write through it. Handing each its own
connection is exactly how an atomic transition becomes several.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, List, Optional

from uta.tasks.models import now_iso


class SchedulerRepositoryMixin:
    """The scheduler repository half of `TaskDB`."""

    def acquire_next_task(
        self,
        *,
        allow_same_repo_concurrency: bool = False,
        include_failed: bool = False,
        exclude_task_ids: Optional[Iterable[int]] = None,
    ) -> Optional[sqlite3.Row]:
        from uta.tasks.generation_engine import require_durable_generation_task

        now = now_iso()
        statuses = "'CREATED', 'QUEUED'"
        if include_failed:
            statuses += ", 'FAILED'"
        excluded = sorted({int(task_id) for task_id in (exclude_task_ids or [])})
        exclusion_sql = ""
        if excluded:
            exclusion_sql = f" AND id NOT IN ({','.join('?' for _ in excluded)})"
        with self.transaction() as conn:
            rows = list(
                conn.execute(
                    f"""
                SELECT * FROM repo_tasks
                WHERE status IN ({statuses})
                {exclusion_sql}
                ORDER BY priority ASC, created_at ASC
                LIMIT 20
                """,
                    excluded,
                )
            )
            for row in rows:
                require_durable_generation_task(row, action="be acquired")
                if not allow_same_repo_concurrency:
                    active = conn.execute(
                        """
                        SELECT 1 FROM repo_tasks
                        WHERE repo_path=? AND status IN ('RUNNING', 'STOP_REQUESTED') AND id<>?
                        LIMIT 1
                        """,
                        (row["repo_path"], row["id"]),
                    ).fetchone()
                    if active:
                        continue
                conn.execute(
                    """
                    UPDATE repo_tasks
                    SET status='RUNNING', started_at=COALESCE(started_at, ?), updated_at=?, current_stage='acquired'
                    WHERE id=?
                    """,
                    (now, now, row["id"]),
                )
                return conn.execute(
                    "SELECT * FROM repo_tasks WHERE id=?", (row["id"],)
                ).fetchone()
        return None

    def upsert_heartbeat(
        self,
        runner_id: str,
        *,
        repo_task_id: Optional[int],
        pid: int,
        hostname: str,
        status: str,
        message: Optional[str] = None,
        loaded_config_hash: Optional[str] = None,
    ) -> None:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runner_heartbeats(runner_id, repo_task_id, current_repo_task_id, pid, hostname, status, message, started_at, heartbeat_at, loaded_config_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(runner_id) DO UPDATE SET
                    repo_task_id=excluded.repo_task_id,
                    current_repo_task_id=excluded.current_repo_task_id,
                    pid=excluded.pid,
                    hostname=excluded.hostname,
                    status=excluded.status,
                    message=excluded.message,
                    heartbeat_at=excluded.heartbeat_at,
                    loaded_config_hash=excluded.loaded_config_hash,
                    updated_at=excluded.updated_at
                """,
                (
                    runner_id,
                    repo_task_id,
                    repo_task_id,
                    pid,
                    hostname,
                    status,
                    message,
                    now,
                    now,
                    loaded_config_hash,
                    now,
                    now,
                ),
            )

    def latest_heartbeat(self) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM runner_heartbeats ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()

    def request_control(
        self,
        repo_task_id: int,
        action: str,
        *,
        reason: Optional[str] = None,
        class_task_id: Optional[int] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_control(repo_task_id, class_task_id, action, requested_action, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (repo_task_id, class_task_id, action, action, reason, now_iso()),
            )

    def latest_control(self, repo_task_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM task_control
                WHERE repo_task_id=? AND acknowledged_at IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (repo_task_id,),
            ).fetchone()

    def acknowledge_controls(
        self, repo_task_id: int, *, action: Optional[str] = None
    ) -> None:
        now = now_iso()
        clauses = ["repo_task_id=?", "acknowledged_at IS NULL"]
        params: List[Any] = [repo_task_id]
        if action:
            clauses.append("action=?")
            params.append(action)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE task_control SET acknowledged_at=?, handled_at=? WHERE {' AND '.join(clauses)}",
                (now, now, *params),
            )

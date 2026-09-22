"""Events and progress: what happened, in order.

`finish_task_with_terminal_event` writes the terminal state and its event in
one transaction on purpose -- a task that is finished but has no event saying
so is invisible to every reader.

A mixin rather than a collaborator: `TaskDB` owns the one connection and the
one transaction, and these read and write through it. Handing each its own
connection is exactly how an atomic transition becomes several.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Union

from uta.tasks.models import json_dumps, now_iso


class EventRepositoryMixin:
    """The event repository half of `TaskDB`."""

    def add_event(
        self,
        repo_task_id: Optional[int],
        class_task_id: Optional[int],
        event_type: str,
        message: str,
        *,
        stage: Optional[str] = None,
        severity: str = "INFO",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_events(repo_task_id, class_task_id, event_type, severity, stage, message, payload_json, ts, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_task_id,
                    class_task_id,
                    event_type,
                    severity,
                    stage,
                    message,
                    json_dumps(payload),
                    now,
                    now,
                ),
            )

    def append_progress_event_batch(
        self,
        repo_task_id: int,
        events: Iterable[Dict[str, Any]],
        *,
        max_events: int,
        max_serialized_bytes: int,
    ) -> Dict[str, Any]:
        from uta.tasks.progress_store import append_progress_event_batch

        return append_progress_event_batch(
            self,
            repo_task_id,
            events,
            max_events=max_events,
            max_serialized_bytes=max_serialized_bytes,
        )

    def events_since(
        self, repo_task_id: int, *, after_id: int = 0, limit: int = 500
    ) -> List[sqlite3.Row]:
        from uta.tasks.progress_store import events_since

        return events_since(self, repo_task_id, after_id=after_id, limit=limit)

    def prune_progress_events(self, *, before: str) -> int:
        from uta.tasks.progress_store import prune_progress_events

        return prune_progress_events(self, before=before)

    def latest_events(self, repo_task_id: int, *, limit: Optional[int] = 20) -> List[sqlite3.Row]:
        """Newest events first. `limit=None` returns the task's whole history.

        SQLite treats a negative LIMIT as unbounded, which is how `None` is
        expressed to the query below.
        """
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                SELECT * FROM task_events
                WHERE repo_task_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                    (repo_task_id, -1 if limit is None else int(limit)),
                )
            )

    def finish_task_with_terminal_event(
        self,
        repo_task_id: int,
        *,
        status: str,
        message: str,
        stage: str,
        severity: str = "INFO",
        payload: Optional[Dict[str, Any]] = None,
        task_fields: Optional[Dict[str, Any]] = None,
        summary_event_type: Optional[Union[str, Iterable[str]]] = None,
    ) -> int:
        from uta.tasks.progress_store import finish_task_with_terminal_event

        return finish_task_with_terminal_event(
            self,
            repo_task_id,
            status=status,
            message=message,
            stage=stage,
            severity=severity,
            payload=payload,
            task_fields=task_fields,
            summary_event_type=summary_event_type,
        )

    def workflow_disposition_counts(self, repo_task_id: int) -> Dict[str, int]:
        event_types = (
            "workflow_started",
            "workflow_resumed",
            "workflow_reused_completed",
        )
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_type, COUNT(*) AS n
                FROM task_events
                WHERE repo_task_id=? AND event_type IN (?, ?, ?)
                GROUP BY event_type
                """,
                (int(repo_task_id), *event_types),
            ).fetchall()
        observed = {str(row["event_type"]): int(row["n"] or 0) for row in rows}
        return {
            event_type.removeprefix("workflow_"): observed.get(event_type, 0)
            for event_type in event_types
        }

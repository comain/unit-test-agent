"""Transactional persistence for bounded task progress and terminal cursors."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional, Union

from uta.tasks.models import TERMINAL_REPO_STATUSES, json_dumps, now_iso

if TYPE_CHECKING:
    from uta.tasks.db import TaskDB


def append_progress_event_batch(
    db: "TaskDB",
    repo_task_id: int,
    events: Iterable[Dict[str, Any]],
    *,
    max_events: int,
    max_serialized_bytes: int,
) -> Dict[str, Any]:
    """Atomically admit a prefix of progress under the persisted task cap."""
    prepared = []
    marker_requested = False
    for event in events:
        item = dict(event)
        if item.get("event_type") == "progress_truncated":
            marker_requested = True
            continue
        payload_json = json_dumps(item.get("payload"))
        prepared.append(
            {
                **item,
                "payload_json": payload_json,
                "serialized_bytes": len(payload_json.encode("utf-8")),
            }
        )

    max_rows = max(int(max_events), 0)
    max_bytes = max(int(max_serialized_bytes), 0)
    now = now_iso()
    with db.transaction() as conn:
        task = conn.execute(
            "SELECT progress_event_count, progress_event_bytes, progress_truncated "
            "FROM repo_tasks WHERE id=?",
            (int(repo_task_id),),
        ).fetchone()
        if task is None:
            raise KeyError(f"Task {repo_task_id} not found")

        used_rows = int(task["progress_event_count"] or 0)
        used_bytes = int(task["progress_event_bytes"] or 0)
        admitted = []
        admitted_bytes = 0
        for item in prepared:
            size = int(item["serialized_bytes"])
            if used_rows + len(admitted) + 1 > max_rows:
                break
            if used_bytes + admitted_bytes + size > max_bytes:
                break
            admitted.append(item)
            admitted_bytes += size

        dropped = len(admitted) < len(prepared)
        for item in admitted:
            conn.execute(
                """
                INSERT INTO task_events(
                    repo_task_id, class_task_id, event_type, severity,
                    stage, message, payload_json, ts, created_at
                ) VALUES (?, NULL, 'agent_progress', ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(repo_task_id),
                    str(item.get("severity") or "INFO"),
                    item.get("stage"),
                    str(item.get("message") or "Agent progress"),
                    item["payload_json"],
                    now,
                    now,
                ),
            )

        truncated = bool(marker_requested or dropped)
        won_marker = truncated and not bool(task["progress_truncated"])
        if won_marker:
            conn.execute(
                """
                INSERT INTO task_events(
                    repo_task_id, class_task_id, event_type, severity,
                    stage, message, payload_json, ts, created_at
                ) VALUES (?, NULL, 'progress_truncated', 'WARNING', NULL, ?, ?, ?, ?)
                """,
                (
                    int(repo_task_id),
                    "Some agent progress updates were omitted",
                    json_dumps(
                        {
                            "maxEvents": max_rows,
                            "maxSerializedBytes": max_bytes,
                        }
                    ),
                    now,
                    now,
                ),
            )

        conn.execute(
            """
            UPDATE repo_tasks
            SET progress_event_count=progress_event_count+?,
                progress_event_bytes=progress_event_bytes+?,
                progress_truncated=CASE WHEN ? THEN 1 ELSE progress_truncated END,
                updated_at=?
            WHERE id=?
            """,
            (
                len(admitted),
                admitted_bytes,
                1 if won_marker else 0,
                now,
                int(repo_task_id),
            ),
        )
    return {
        "admitted_events": len(admitted),
        "admitted_serialized_bytes": admitted_bytes,
        "truncated": truncated,
    }


def events_since(
    db: "TaskDB", repo_task_id: int, *, after_id: int = 0, limit: int = 500
):
    """Read the product event cursor oldest-first, strictly after ``id``."""
    cursor = max(int(after_id), 0)
    bounded_limit = min(max(int(limit), 1), 5000)
    with db.connect() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM task_events
                WHERE repo_task_id=? AND id>?
                ORDER BY id ASC LIMIT ?
                """,
                (int(repo_task_id), cursor, bounded_limit),
            )
        )


def finish_task_with_terminal_event(
    db: "TaskDB",
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
    """Commit terminal product state and its closing stream cursor together."""
    terminal_status = str(status).upper()
    if terminal_status not in TERMINAL_REPO_STATUSES:
        raise ValueError(f"non-terminal task status: {status!r}")
    now = now_iso()
    updates = dict(task_fields or {})
    updates.update(
        {
            "status": terminal_status,
            "current_stage": stage,
            "current_detail": message,
            "finished_at": updates.get("finished_at") or now,
            "updated_at": now,
        }
    )
    assignments = ", ".join(f"{key}=?" for key in updates)
    with db.transaction() as conn:
        prior = conn.execute(
            "SELECT status FROM repo_tasks WHERE id=?", (int(repo_task_id),)
        ).fetchone()
        if prior is None:
            raise KeyError(f"Task {repo_task_id} not found")
        prior_terminal = None
        if str(prior["status"] or "").upper() == terminal_status:
            prior_terminal = conn.execute(
                """
                SELECT id FROM task_events
                WHERE repo_task_id=? AND event_type='task_terminal'
                ORDER BY id DESC LIMIT 1
                """,
                (int(repo_task_id),),
            ).fetchone()
        cursor = conn.execute(
            f"UPDATE repo_tasks SET {assignments} WHERE id=?",
            (*updates.values(), int(repo_task_id)),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"Task {repo_task_id} not found")
        if prior_terminal is not None:
            return int(prior_terminal["id"])
        if isinstance(summary_event_type, str):
            summary_types = (summary_event_type,)
        else:
            summary_types = tuple(summary_event_type or ())
        for event_type in summary_types:
            conn.execute(
                """
                INSERT INTO task_events(
                    repo_task_id, class_task_id, event_type, severity,
                    stage, message, payload_json, ts, created_at
                ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(repo_task_id),
                    str(event_type),
                    severity,
                    stage,
                    message,
                    json_dumps(payload),
                    now,
                    now,
                ),
            )
        terminal = conn.execute(
            """
            INSERT INTO task_events(
                repo_task_id, class_task_id, event_type, severity,
                stage, message, payload_json, ts, created_at
            ) VALUES (?, NULL, 'task_terminal', ?, ?, ?, ?, ?, ?)
            """,
            (
                int(repo_task_id),
                severity,
                stage,
                message,
                json_dumps({**(payload or {}), "status": terminal_status}),
                now,
                now,
            ),
        )
        return int(terminal.lastrowid)


def prune_progress_events(db: "TaskDB", *, before: str) -> int:
    """Delete only old agent detail; summaries and terminal cursors survive."""
    with db.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM task_events WHERE event_type='agent_progress' "
            "AND created_at<=?",
            (str(before),),
        )
        return max(int(cursor.rowcount or 0), 0)


__all__ = [
    "append_progress_event_batch",
    "events_since",
    "finish_task_with_terminal_event",
    "prune_progress_events",
]

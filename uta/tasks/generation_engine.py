"""Durable generation-engine snapshot classification and cutover audit."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from uta.tasks.models import (
    CLASS_TASK_STATUSES,
    REPO_TASK_STATUSES,
    TERMINAL_CLASS_STATUSES,
    TERMINAL_REPO_STATUSES,
)


GENERATION_ENGINE_VERSION_KEY = "generation_engine_version"
DURABLE_GENERATION_ENGINE_VERSION = 2
GENERATION_CYCLE_COMPATIBILITY_KEY = "generation_cycle_v2_enabled"


class LegacyGenerationTaskError(RuntimeError):
    """A persisted task cannot cross the durable-only execution boundary."""


def durable_generation_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a new-task snapshot that rolls back safely to the v2-capable build."""
    supplied = dict(snapshot or {})
    if (
        GENERATION_CYCLE_COMPATIBILITY_KEY in supplied
        and supplied[GENERATION_CYCLE_COMPATIBILITY_KEY] is not True
    ):
        raise ValueError("new task generation engine compatibility must be true")
    if GENERATION_ENGINE_VERSION_KEY in supplied:
        version = supplied[GENERATION_ENGINE_VERSION_KEY]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != DURABLE_GENERATION_ENGINE_VERSION
        ):
            raise ValueError("new task generation engine version must be integer 2")
    return {
        **supplied,
        GENERATION_CYCLE_COMPATIBILITY_KEY: True,
        GENERATION_ENGINE_VERSION_KEY: DURABLE_GENERATION_ENGINE_VERSION,
    }


def classify_generation_snapshot(raw_snapshot: Any) -> str | None:
    """Return a stable blocker reason, or ``None`` for engine version 2."""
    try:
        snapshot = json.loads(raw_snapshot)
    except (TypeError, json.JSONDecodeError):
        return "malformed_config_snapshot_json"
    if not isinstance(snapshot, dict):
        return "config_snapshot_not_object"
    if GENERATION_ENGINE_VERSION_KEY not in snapshot:
        return "missing_generation_engine_version"
    version = snapshot[GENERATION_ENGINE_VERSION_KEY]
    if isinstance(version, bool) or not isinstance(version, int):
        return "invalid_generation_engine_version_type"
    if version != DURABLE_GENERATION_ENGINE_VERSION:
        return "unsupported_generation_engine_version"
    return None


def require_durable_generation_task(task: Mapping[str, Any], *, action: str) -> None:
    reason = classify_generation_snapshot(task["config_snapshot_json"])
    if reason is None:
        return
    task_id = int(task["id"])
    raise LegacyGenerationTaskError(
        f"Task {task_id} cannot {action}: its persisted generation engine is "
        f"legacy or invalid ({reason}). Finish or cancel the old task, then "
        "submit a new task after the durable-only build starts."
    )


def audit_generation_cutover(db_path: str | Path) -> dict[str, Any]:
    """Inspect the product DB without initializing or mutating it."""
    started = time.perf_counter()
    path = Path(db_path).expanduser().resolve()
    uri = f"{path.as_uri()}?mode=ro"
    terminal_class_statuses = sorted(TERMINAL_CLASS_STATUSES)
    known_class_statuses = sorted(CLASS_TASK_STATUSES)
    terminal_placeholders = ",".join("?" for _ in terminal_class_statuses)
    known_placeholders = ",".join("?" for _ in known_class_statuses)
    query = f"""
        SELECT r.id, r.status, r.config_snapshot_json,
               COALESCE(classes.active_class_count, 0) AS active_class_count,
               COALESCE(classes.unknown_class_count, 0) AS unknown_class_count
        FROM repo_tasks AS r
        LEFT JOIN (
            SELECT repo_task_id,
                   SUM(CASE WHEN status IS NULL OR status NOT IN ({terminal_placeholders})
                            THEN 1 ELSE 0 END) AS active_class_count,
                   SUM(CASE WHEN status IS NULL OR status NOT IN ({known_placeholders})
                            THEN 1 ELSE 0 END) AS unknown_class_count
            FROM class_tasks
            GROUP BY repo_task_id
        ) AS classes ON classes.repo_task_id=r.id
        ORDER BY r.id ASC
    """
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(query, [*terminal_class_statuses, *known_class_statuses])
        )
    finally:
        connection.close()

    blockers: list[dict[str, Any]] = []
    non_terminal = {
        "CREATED",
        "QUEUED",
        "RUNNING",
        "STOP_REQUESTED",
        "STOPPED",
    }
    for row in rows:
        status = str(row["status"])
        reason = None
        if status not in REPO_TASK_STATUSES:
            reason = "unknown_repo_task_status"
        elif int(row["unknown_class_count"] or 0):
            reason = "unknown_class_task_status"
        elif status in non_terminal:
            reason = classify_generation_snapshot(row["config_snapshot_json"])
        elif status in TERMINAL_REPO_STATUSES and int(row["active_class_count"] or 0):
            reason = "active_class_rows_under_terminal_task"
        if reason is not None:
            blockers.append(
                {"task_id": int(row["id"]), "status": status, "reason": reason}
            )
    return {
        "scanned_task_count": len(rows),
        "blocker_count": len(blockers),
        "blockers": blockers,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }


__all__ = [
    "DURABLE_GENERATION_ENGINE_VERSION",
    "GENERATION_CYCLE_COMPATIBILITY_KEY",
    "GENERATION_ENGINE_VERSION_KEY",
    "LegacyGenerationTaskError",
    "audit_generation_cutover",
    "classify_generation_snapshot",
    "durable_generation_snapshot",
    "require_durable_generation_task",
]

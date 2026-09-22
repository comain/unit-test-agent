"""Task-level eligibility queries for workflow retention."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from uta.tasks.db import TaskDB
from uta.tasks.models import TERMINAL_REPO_STATUSES
from uta.tasks.prompt_scope_audit import is_managed_legacy_run_id


def eligible_terminal_task_ids(db: TaskDB, *, cutoff: str) -> Tuple[int, ...]:
    terminal_statuses = sorted({*TERMINAL_REPO_STATUSES, "STOPPED"})
    placeholders = ",".join("?" for _ in terminal_statuses)
    with db.connect() as conn:
        return tuple(
            int(row["id"])
            for row in conn.execute(
                f"""
                SELECT id FROM repo_tasks
                WHERE status IN ({placeholders})
                  AND COALESCE(finished_at, updated_at) <= ?
                ORDER BY id
                """,
                (*terminal_statuses, cutoff),
            )
        )


def eligible_superseded_legacy_runs(
    db: TaskDB, *, cutoff: str
) -> Tuple[Tuple[int, str], ...]:
    """Read typed product-owned scope rows eligible after supersession."""
    with db.connect() as conn:
        eligible: Dict[Tuple[int, str], None] = {}
        rows = conn.execute(
            """
            SELECT repo_task_id, legacy_run_id
            FROM legacy_prompt_scopes
            WHERE superseded_at IS NOT NULL AND superseded_at <= ?
            ORDER BY repo_task_id, legacy_run_id
            """,
            (cutoff,),
        )
        for row in rows:
            run_id = row["legacy_run_id"]
            if is_managed_legacy_run_id(run_id):
                eligible[(int(row["repo_task_id"]), str(run_id))] = None
    return tuple(sorted(eligible))


def eligible_identities(
    db: TaskDB,
    *,
    cutoff: str,
    terminal_task_ids: Tuple[int, ...],
) -> Tuple[Tuple[int, str, str], ...]:
    found: Dict[Tuple[int, str, str], None] = {}
    with db.connect() as conn:
        for task_id in terminal_task_ids:
            for row in conn.execute(
                """
                SELECT DISTINCT workflow_run_id, unit_id
                FROM workflow_operations WHERE repo_task_id=?
                """,
                (task_id,),
            ):
                found[(task_id, str(row["workflow_run_id"]), str(row["unit_id"]))] = None
            for row in conn.execute(
                "SELECT DISTINCT batch_key FROM class_tasks "
                "WHERE repo_task_id=? AND batch_key IS NOT NULL",
                (task_id,),
            ):
                parsed = _parse_batch_key(row["batch_key"])
                if parsed:
                    found[(task_id, *parsed)] = None

        superseded = list(
            conn.execute(
                """
                SELECT repo_task_id, old_workflow_run_id, old_unit_ids_json
                FROM workflow_run_supersessions
                WHERE prior_identity_kind='known' AND requested_at <= ?
                """,
                (cutoff,),
            )
        )
        for row in superseded:
            try:
                unit_ids = json.loads(row["old_unit_ids_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(unit_ids, list):
                continue
            for unit_id in unit_ids:
                found[
                    (
                        int(row["repo_task_id"]),
                        str(row["old_workflow_run_id"]),
                        str(unit_id),
                    )
                ] = None
    return tuple(sorted(found))


def _parse_batch_key(value: Any) -> Optional[Tuple[str, str]]:
    parts = str(value or "").split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return str(parts[0]), str(parts[1])


__all__ = [
    "eligible_identities",
    "eligible_superseded_legacy_runs",
    "eligible_terminal_task_ids",
]

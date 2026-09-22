"""Atomic, audited replacement of generation workflow identities.

This is deliberately separate from the general task manager: clean rerun is a
rare recovery transaction with its own eligibility, identity, audit, and
requeue rules. ``TaskManager.clean_rerun_generation`` remains the sole public
entrypoint and delegates here after collecting its configuration default.
"""

from __future__ import annotations

import getpass
import hashlib
import re
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from uta.tasks.db import TaskDB
from uta.tasks.models import (
    TERMINAL_CLASS_STATUSES,
    TERMINAL_REPO_STATUSES,
    json_dumps,
    json_loads,
    now_iso,
)
from uta.tasks.prompt_scope_audit import parse_legacy_scope_opened_payload
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ORDINARY_REQUEUE = {"CREATED", "PENDING", "QUEUED", "RUNNING", "STOPPED"}
_NON_FAILURE = {"PASS", "CREATED", "PENDING", "QUEUED", "RUNNING", "STOPPED", "GENERATED"}


def clean_rerun_generation(
    db: TaskDB,
    task_id: int,
    *,
    reason: str,
    confirm_task_id: int,
    force_rerun_failed: bool = False,
    force_rerun_all: bool = False,
    requested_by: Optional[str] = None,
    default_batch_size: int = 1,
) -> str:
    clean_reason, actor = _validated_request(
        task_id=task_id,
        reason=reason,
        confirm_task_id=confirm_task_id,
        requested_by=requested_by,
    )
    now = now_iso()
    new_run_id = uuid.uuid4().hex
    policy = "all" if force_rerun_all else "failed" if force_rerun_failed else "ordinary"

    with db.transaction() as conn:
        task = conn.execute(
            "SELECT * FROM repo_tasks WHERE id=?", (int(task_id),)
        ).fetchone()
        _require_eligible(conn, task, task_id=task_id)
        rows = list(
            conn.execute(
                "SELECT * FROM class_tasks WHERE repo_task_id=? "
                "ORDER BY priority ASC, id ASC",
                (int(task_id),),
            )
        )
        if not rows:
            raise RuntimeError("clean rerun requires at least one generation target")

        old_legacy_scopes = _prior_legacy_scopes(conn, task_id=task_id)
        known, missing_class_ids, old_key_counts = _prior_identities(rows)
        config = json_loads(task["config_snapshot_json"])
        batch_size = _batch_size(
            config=config,
            old_key_counts=old_key_counts,
            default_batch_size=default_batch_size,
        )
        new_unit_by_class, new_unit_ids = _partition(rows, batch_size=batch_size)
        requeue_statuses = _requeue_statuses(
            force_rerun_failed=force_rerun_failed,
            force_rerun_all=force_rerun_all,
        )
        statuses = _replace_keys_and_requeue(
            conn,
            rows,
            new_run_id=new_run_id,
            new_unit_by_class=new_unit_by_class,
            requeue_statuses=requeue_statuses,
            now=now,
        )
        _insert_supersessions(
            conn,
            task_id=task_id,
            known=known,
            missing_class_ids=missing_class_ids,
            new_run_id=new_run_id,
            new_unit_by_class=new_unit_by_class,
            policy=policy,
            reason=clean_reason,
            actor=actor,
            now=now,
        )
        _supersede_legacy_scopes(
            conn,
            task_id=task_id,
            new_run_id=new_run_id,
            now=now,
        )
        _queue_task(conn, task_id=task_id, statuses=statuses, now=now)
        _insert_event(
            conn,
            task_id=task_id,
            new_run_id=new_run_id,
            new_unit_ids=new_unit_ids,
            old_run_ids=list(known),
            old_legacy_scopes=old_legacy_scopes,
            missing_class_ids=missing_class_ids,
            policy=policy,
            reason=clean_reason,
            actor=actor,
            now=now,
        )
    return new_run_id


def _prior_legacy_scopes(conn, *, task_id: int) -> List[Dict[str, Any]]:
    linked: List[Dict[str, Any]] = []
    rows = conn.execute(
        "SELECT legacy_run_id, language FROM legacy_prompt_scopes "
        "WHERE repo_task_id=? AND superseded_at IS NULL "
        "ORDER BY opened_at, legacy_run_id",
        (int(task_id),),
    )
    for row in rows:
        payload = {
            "schema_version": 1,
            "legacy_run_id": row["legacy_run_id"],
            "language": row["language"],
        }
        parsed = parse_legacy_scope_opened_payload(payload)
        if parsed is not None:
            run_id, language = parsed
            linked.append({"run_id": run_id, "language": language})
    return linked


def _supersede_legacy_scopes(
    conn, *, task_id: int, new_run_id: str, now: str
) -> None:
    conn.execute(
        """
        UPDATE legacy_prompt_scopes
        SET superseded_at=?, superseded_by_workflow_run_id=?
        WHERE repo_task_id=? AND superseded_at IS NULL
        """,
        (now, new_run_id, int(task_id)),
    )


def _validated_request(
    *, task_id: int, reason: str, confirm_task_id: int, requested_by: Optional[str]
) -> Tuple[str, str]:
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise ValueError("a non-empty clean-rerun reason is required")
    if int(confirm_task_id) != int(task_id):
        raise ValueError("clean-rerun task confirmation does not match")
    actor = str(requested_by or getpass.getuser() or "unknown").strip() or "unknown"
    return clean_reason, actor


def _require_eligible(conn, task, *, task_id: int) -> None:
    if task is None:
        raise KeyError(f"Task {task_id} not found")
    from uta.tasks.generation_engine import require_durable_generation_task

    require_durable_generation_task(task, action="be clean-rerun")
    if task["status"] != "STOPPED" and task["status"] not in TERMINAL_REPO_STATUSES:
        raise RuntimeError("clean rerun requires a STOPPED or terminal task")
    active = conn.execute(
        """
        SELECT runner_id FROM runner_heartbeats
        WHERE status='RUNNING' AND current_repo_task_id=? LIMIT 1
        """,
        (int(task_id),),
    ).fetchone()
    if active is not None:
        raise RuntimeError(f"clean rerun refused: active runner lease {active['runner_id']}")


def _prior_identities(rows: Sequence[Mapping[str, Any]]):
    known: Dict[str, Dict[str, Any]] = {}
    missing: List[int] = []
    counts: Dict[str, int] = {}
    for row in rows:
        class_id = int(row["id"])
        key = row["batch_key"]
        if key is None or not str(key).strip():
            missing.append(class_id)
            continue
        parts = str(key).split("/")
        if len(parts) != 2 or not all(_COMPONENT.fullmatch(part) for part in parts):
            raise RuntimeError(
                f"malformed generation batch key for class task {class_id}: {key!r}"
            )
        run_id, unit_id = parts
        group = known.setdefault(run_id, {"unit_ids": [], "class_ids": []})
        if unit_id not in group["unit_ids"]:
            group["unit_ids"].append(unit_id)
        group["class_ids"].append(class_id)
        counts[str(key)] = counts.get(str(key), 0) + 1
    return known, missing, counts


def _batch_size(
    *, config: Mapping[str, Any], old_key_counts: Mapping[str, int], default_batch_size: int
) -> int:
    configured = config.get("classes_per_agent_run") or config.get("classes_per_run")
    inferred = max(old_key_counts.values(), default=0)
    return max(int(configured or inferred or default_batch_size), 1)


def _partition(rows: Sequence[Mapping[str, Any]], *, batch_size: int):
    by_class: Dict[int, str] = {}
    unit_ids: List[str] = []
    for index, start in enumerate(range(0, len(rows), batch_size), start=1):
        members = rows[start : start + batch_size]
        target_ids = [str(row["target_id"] or row["class_fqn"]) for row in members]
        digest = hashlib.sha256("\x00".join(target_ids).encode("utf-8")).hexdigest()[:12]
        unit_id = f"unit-{index:04d}-{digest}"
        unit_ids.append(unit_id)
        for row in members:
            by_class[int(row["id"])] = unit_id
    return by_class, unit_ids


def _requeue_statuses(*, force_rerun_failed: bool, force_rerun_all: bool) -> set[str]:
    statuses = set(_ORDINARY_REQUEUE)
    if force_rerun_failed:
        statuses.update(TERMINAL_CLASS_STATUSES - {"PASS", "CANCELLED"})
    if force_rerun_all:
        statuses.update(TERMINAL_CLASS_STATUSES)
    return statuses


def _replace_keys_and_requeue(
    conn,
    rows,
    *,
    new_run_id: str,
    new_unit_by_class: Mapping[int, str],
    requeue_statuses: set[str],
    now: str,
) -> List[str]:
    statuses: List[str] = []
    for row in rows:
        class_id = int(row["id"])
        batch_key = f"{new_run_id}/{new_unit_by_class[class_id]}"
        if row["status"] in requeue_statuses:
            statuses.append("QUEUED")
            conn.execute(
                """
                UPDATE class_tasks
                SET batch_key=?, status='QUEUED', stage='queued',
                    current_stage='queued', current_detail='clean generation rerun',
                    finished_at=NULL, last_error=NULL, error=NULL, updated_at=?
                WHERE id=?
                """,
                (batch_key, now, class_id),
            )
        else:
            statuses.append(str(row["status"]))
            conn.execute(
                "UPDATE class_tasks SET batch_key=?, updated_at=? WHERE id=?",
                (batch_key, now, class_id),
            )
    return statuses


def _insert_supersessions(
    conn,
    *,
    task_id: int,
    known: Mapping[str, Mapping[str, Any]],
    missing_class_ids: Sequence[int],
    new_run_id: str,
    new_unit_by_class: Mapping[int, str],
    policy: str,
    reason: str,
    actor: str,
    now: str,
) -> None:
    def new_units_for(class_ids: Iterable[int]) -> List[str]:
        return list(dict.fromkeys(new_unit_by_class[int(item)] for item in class_ids))

    for old_run_id, group in known.items():
        conn.execute(
            """
            INSERT INTO workflow_run_supersessions(
                supersession_id, repo_task_id, prior_identity_kind,
                old_workflow_run_id, new_workflow_run_id, old_unit_ids_json,
                new_unit_ids_json, affected_class_ids_json, requeue_policy,
                reason, requested_by, requested_at
            ) VALUES (?, ?, 'known', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex, int(task_id), old_run_id, new_run_id,
                json_dumps(group["unit_ids"]),
                json_dumps(new_units_for(group["class_ids"])),
                json_dumps(group["class_ids"]), policy, reason, actor, now,
            ),
        )
    if missing_class_ids:
        conn.execute(
            """
            INSERT INTO workflow_run_supersessions(
                supersession_id, repo_task_id, prior_identity_kind,
                old_workflow_run_id, new_workflow_run_id, old_unit_ids_json,
                new_unit_ids_json, affected_class_ids_json, requeue_policy,
                reason, requested_by, requested_at
            ) VALUES (?, ?, 'missing', NULL, ?, '[]', ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex, int(task_id), new_run_id,
                json_dumps(new_units_for(missing_class_ids)),
                json_dumps(list(missing_class_ids)), policy, reason, actor, now,
            ),
        )


def _queue_task(conn, *, task_id: int, statuses: Sequence[str], now: str) -> None:
    completed = sum(status in TERMINAL_CLASS_STATUSES for status in statuses)
    passed = sum(status == "PASS" for status in statuses)
    failed = sum(status not in _NON_FAILURE for status in statuses)
    conn.execute(
        """
        UPDATE repo_tasks
        SET status='QUEUED', stop_requested_at=NULL, finished_at=NULL,
            resume_count=COALESCE(resume_count, 0)+1,
            current_stage='queued', current_detail='clean generation rerun requested',
            last_error=NULL, error=NULL, completed_classes=?,
            passed_classes=?, failed_classes=?, updated_at=?
        WHERE id=?
        """,
        (completed, passed, failed, now, int(task_id)),
    )
    conn.execute(
        """
        UPDATE task_control SET acknowledged_at=?, handled_at=?
        WHERE repo_task_id=? AND acknowledged_at IS NULL
        """,
        (now, now, int(task_id)),
    )


def _insert_event(
    conn,
    *,
    task_id: int,
    new_run_id: str,
    new_unit_ids: Sequence[str],
    old_run_ids: Sequence[str],
    old_legacy_scopes: Sequence[Mapping[str, Any]],
    missing_class_ids: Sequence[int],
    policy: str,
    reason: str,
    actor: str,
    now: str,
) -> None:
    payload = {
        "new_workflow_run_id": new_run_id,
        "new_unit_ids": list(new_unit_ids),
        "old_workflow_run_ids": list(old_run_ids),
        "old_legacy_scopes": [dict(item) for item in old_legacy_scopes],
        "missing_class_ids": list(missing_class_ids),
        "requeue_policy": policy,
        "reason": reason,
        "requested_by": actor,
    }
    conn.execute(
        """
        INSERT INTO task_events(
            repo_task_id, class_task_id, event_type, severity, stage,
            message, payload_json, ts, created_at
        ) VALUES (?, NULL, ?, 'WARNING', 'queued', ?, ?, ?, ?)
        """,
        (
            int(task_id), "generation_cycle_clean_rerun_requested",
            "Clean generation rerun requested", json_dumps(payload), now, now,
        ),
    )


__all__ = ["clean_rerun_generation"]

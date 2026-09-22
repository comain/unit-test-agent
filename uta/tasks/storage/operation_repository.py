"""The workflow operation ledger's rows.

`start_workflow_operation` writes the claim *before* the work runs, which is
what makes a crash recoverable: the row names replayable input, and the
operation id makes the replay idempotent.

A mixin rather than a collaborator: `TaskDB` owns the one connection and the
one transaction, and these read and write through it. Handing each its own
connection is exactly how an atomic transition becomes several.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from uta.tasks.models import json_dumps, now_iso
from uta.tasks.storage.base import WorkflowOperationConflict


class OperationRepositoryMixin:
    """The operation repository half of `TaskDB`."""

    def get_workflow_operation(self, operation_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM workflow_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()

    def list_workflow_operations(self, repo_task_id: int) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM workflow_operations
                    WHERE repo_task_id=?
                    ORDER BY started_at ASC, operation_id ASC
                    """,
                    (int(repo_task_id),),
                )
            )

    def record_workflow_operation_cost(
        self,
        operation_id: str,
        *,
        paid_attempt_ordinal: int,
        provider_cost_usd: Optional[float],
        usage: Optional[Dict[str, Any]] = None,
    ) -> sqlite3.Row:
        """Insert one immutable paid-attempt accounting fact."""
        provenance = "recorded" if provider_cost_usd is not None else "unavailable"
        cost = float(provider_cost_usd) if provider_cost_usd is not None else None
        usage_json = json_dumps(usage or {})
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM workflow_operation_accounting "
                "WHERE operation_id=? AND paid_attempt_ordinal=?",
                (operation_id, int(paid_attempt_ordinal)),
            ).fetchone()
            if existing is not None:
                if (
                    existing["provider_cost_usd"] == cost
                    and existing["cost_provenance"] == provenance
                    and existing["usage_json"] == usage_json
                ):
                    return existing
                raise WorkflowOperationConflict(
                    f"paid attempt {operation_id}/{paid_attempt_ordinal} already recorded"
                )
            conn.execute(
                """
                INSERT INTO workflow_operation_accounting(
                    operation_id, paid_attempt_ordinal, provider_cost_usd,
                    cost_provenance, usage_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    int(paid_attempt_ordinal),
                    cost,
                    provenance,
                    usage_json,
                    now_iso(),
                ),
            )
            return conn.execute(
                "SELECT * FROM workflow_operation_accounting "
                "WHERE operation_id=? AND paid_attempt_ordinal=?",
                (operation_id, int(paid_attempt_ordinal)),
            ).fetchone()

    def workflow_operation_cost_total(self, repo_task_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS attempts,
                       SUM(CASE WHEN a.cost_provenance='unavailable' THEN 1 ELSE 0 END)
                           AS unavailable_attempts,
                       SUM(a.provider_cost_usd) AS provider_cost_usd
                FROM workflow_operation_accounting AS a
                JOIN workflow_operations AS o ON o.operation_id=a.operation_id
                WHERE o.repo_task_id=?
                """,
                (int(repo_task_id),),
            ).fetchone()
        unavailable = (
            not row
            or int(row["attempts"] or 0) == 0
            or int(row["unavailable_attempts"] or 0) > 0
        )
        return {
            "provider_cost_usd": None
            if unavailable
            else float(row["provider_cost_usd"] or 0.0),
            "cost_provenance": "unavailable" if unavailable else "recorded",
        }

    def latest_workflow_operation(
        self,
        *,
        repo_task_id: int,
        workflow_run_id: str,
        unit_id: str,
        phase: str,
        operation_step: str,
        attempt: int,
    ) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM workflow_operations
                WHERE repo_task_id=? AND workflow_run_id=? AND unit_id=?
                  AND phase=? AND operation_step=? AND attempt=?
                ORDER BY execution_ordinal DESC
                LIMIT 1
                """,
                (
                    repo_task_id,
                    workflow_run_id,
                    unit_id,
                    phase,
                    operation_step,
                    int(attempt),
                ),
            ).fetchone()

    def start_workflow_operation(
        self,
        *,
        operation_id: str,
        repo_task_id: int,
        workflow_run_id: str,
        unit_id: str,
        phase: str,
        operation_step: str,
        attempt: int,
        execution_ordinal: int,
        input_fingerprint: str,
        prerequisite_operation_ids: Iterable[str],
        schema_version: int,
        session_id: Optional[str] = None,
    ) -> sqlite3.Row:
        """Start one operation, accepting only an identical existing row."""
        prerequisites_json = json_dumps(list(prerequisite_operation_ids))
        immutable = (
            repo_task_id,
            workflow_run_id,
            unit_id,
            phase,
            operation_step,
            int(attempt),
            int(execution_ordinal),
            input_fingerprint,
            prerequisites_json,
            int(schema_version),
            session_id,
        )
        now = now_iso()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM workflow_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                stored = (
                    existing["repo_task_id"],
                    existing["workflow_run_id"],
                    existing["unit_id"],
                    existing["phase"],
                    existing["operation_step"],
                    existing["attempt"],
                    existing["execution_ordinal"],
                    existing["input_fingerprint"],
                    existing["prerequisite_operation_ids_json"],
                    existing["schema_version"],
                    existing["session_id"],
                )
                if stored != immutable:
                    raise WorkflowOperationConflict(
                        f"workflow operation {operation_id} conflicts with stored identity"
                    )
                return existing

            conn.execute(
                """
                INSERT INTO workflow_operations(
                    operation_id, repo_task_id, workflow_run_id, unit_id, phase,
                    operation_step, attempt, execution_ordinal, status,
                    input_fingerprint, prerequisite_operation_ids_json,
                    schema_version, session_id, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'STARTED', ?, ?, ?, ?, ?, ?)
                """,
                (operation_id, *immutable, now, now),
            )
            return conn.execute(
                "SELECT * FROM workflow_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()

    def complete_workflow_operation(
        self,
        operation_id: str,
        *,
        resulting_workspace_fingerprint: str,
        result_artifact_path: str,
        result_artifact_sha256: str,
        output_fingerprints: Dict[str, str],
        session_id: Optional[str] = None,
    ) -> sqlite3.Row:
        """Commit a durable result, or accept an identical prior completion."""
        output_json = json_dumps(output_fingerprints)
        completed_values = (
            resulting_workspace_fingerprint,
            result_artifact_path,
            result_artifact_sha256,
            output_json,
        )
        now = now_iso()
        with self.transaction() as conn:
            row = self._require_workflow_operation(conn, operation_id)
            if row["status"] == "COMPLETED":
                stored = (
                    row["resulting_workspace_fingerprint"],
                    row["result_artifact_path"],
                    row["result_artifact_sha256"],
                    row["output_fingerprints_json"],
                )
                if stored == completed_values:
                    if session_id is not None and row["session_id"] != session_id:
                        raise WorkflowOperationConflict(
                            f"workflow operation {operation_id} is already completed with a different session"
                        )
                    return row
                raise WorkflowOperationConflict(
                    f"workflow operation {operation_id} is already completed with different evidence"
                )
            if row["status"] != "STARTED":
                raise WorkflowOperationConflict(
                    f"workflow operation {operation_id} cannot complete from {row['status']}"
                )
            conn.execute(
                """
                UPDATE workflow_operations
                SET status='COMPLETED', resulting_workspace_fingerprint=?,
                    result_artifact_path=?, result_artifact_sha256=?,
                    output_fingerprints_json=?, session_id=COALESCE(?, session_id),
                    completed_at=?, updated_at=?
                WHERE operation_id=? AND status='STARTED'
                """,
                (*completed_values, session_id, now, now, operation_id),
            )
            return self._require_workflow_operation(conn, operation_id)

    def fail_workflow_operation(
        self, operation_id: str, *, error_kind: str, error: str
    ) -> sqlite3.Row:
        return self._finish_workflow_operation(
            operation_id, status="FAILED", error_kind=error_kind, error=error
        )

    def cancel_workflow_operation(
        self,
        operation_id: str,
        *,
        error_kind: str,
        error: str,
        resulting_workspace_fingerprint: Optional[str] = None,
        result_artifact_path: Optional[str] = None,
        result_artifact_sha256: Optional[str] = None,
        output_fingerprints: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,
    ) -> sqlite3.Row:
        return self._finish_workflow_operation(
            operation_id,
            status="CANCELLED",
            error_kind=error_kind,
            error=error,
            resulting_workspace_fingerprint=resulting_workspace_fingerprint,
            result_artifact_path=result_artifact_path,
            result_artifact_sha256=result_artifact_sha256,
            output_fingerprints=output_fingerprints,
            session_id=session_id,
        )

    def _finish_workflow_operation(
        self,
        operation_id: str,
        *,
        status: str,
        error_kind: str,
        error: str,
        resulting_workspace_fingerprint: Optional[str] = None,
        result_artifact_path: Optional[str] = None,
        result_artifact_sha256: Optional[str] = None,
        output_fingerprints: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,
    ) -> sqlite3.Row:
        output_json = (
            json_dumps(output_fingerprints) if output_fingerprints is not None else None
        )
        now = now_iso()
        with self.transaction() as conn:
            row = self._require_workflow_operation(conn, operation_id)
            if row["status"] == status:
                expected = (
                    error_kind,
                    error,
                    resulting_workspace_fingerprint,
                    result_artifact_path,
                    result_artifact_sha256,
                    output_json,
                    session_id,
                )
                stored = (
                    row["error_kind"],
                    row["error"],
                    row["resulting_workspace_fingerprint"],
                    row["result_artifact_path"],
                    row["result_artifact_sha256"],
                    row["output_fingerprints_json"]
                    if output_json is not None
                    else None,
                    row["session_id"] if session_id is not None else None,
                )
                if stored == expected:
                    return row
                raise WorkflowOperationConflict(
                    f"workflow operation {operation_id} has different {status.lower()} evidence"
                )
            if row["status"] != "STARTED":
                raise WorkflowOperationConflict(
                    f"workflow operation {operation_id} cannot become {status} from {row['status']}"
                )
            conn.execute(
                """
                UPDATE workflow_operations
                SET status=?, error_kind=?, error=?,
                    resulting_workspace_fingerprint=COALESCE(?, resulting_workspace_fingerprint),
                    result_artifact_path=COALESCE(?, result_artifact_path),
                    result_artifact_sha256=COALESCE(?, result_artifact_sha256),
                    output_fingerprints_json=COALESCE(?, output_fingerprints_json),
                    session_id=COALESCE(?, session_id),
                    completed_at=?, updated_at=?
                WHERE operation_id=? AND status='STARTED'
                """,
                (
                    status,
                    error_kind,
                    error,
                    resulting_workspace_fingerprint,
                    result_artifact_path,
                    result_artifact_sha256,
                    output_json,
                    session_id,
                    now,
                    now,
                    operation_id,
                ),
            )
            return self._require_workflow_operation(conn, operation_id)

    def retry_workflow_operation(
        self,
        old_operation_id: str,
        *,
        operation_id: str,
        repo_task_id: int,
        workflow_run_id: str,
        unit_id: str,
        phase: str,
        operation_step: str,
        attempt: int,
        execution_ordinal: int,
        input_fingerprint: str,
        prerequisite_operation_ids: Iterable[str],
        schema_version: int,
        error_kind: str = "crash_no_observable_effect",
    ) -> sqlite3.Row:
        """Atomically close an interrupted execution and start its successor."""
        prerequisites_json = json_dumps(list(prerequisite_operation_ids))
        now = now_iso()
        with self.transaction() as conn:
            old = self._require_workflow_operation(conn, old_operation_id)
            if old["superseded_by_operation_id"]:
                if old["superseded_by_operation_id"] != operation_id:
                    raise WorkflowOperationConflict(
                        f"workflow operation {old_operation_id} already has another successor"
                    )
                return self._require_workflow_operation(conn, operation_id)
            if old["status"] not in {"STARTED", "FAILED", "CANCELLED"}:
                raise WorkflowOperationConflict(
                    f"workflow operation {old_operation_id} cannot be retried from {old['status']}"
                )
            if old["status"] == "STARTED":
                conn.execute(
                    """
                    UPDATE workflow_operations
                    SET status='FAILED', error_kind=?, error=?, completed_at=?, updated_at=?
                    WHERE operation_id=? AND status='STARTED'
                    """,
                    (error_kind, error_kind, now, now, old_operation_id),
                )
            conn.execute(
                """
                INSERT INTO workflow_operations(
                    operation_id, repo_task_id, workflow_run_id, unit_id, phase,
                    operation_step, attempt, execution_ordinal, status,
                    input_fingerprint, prerequisite_operation_ids_json,
                    schema_version, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'STARTED', ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    repo_task_id,
                    workflow_run_id,
                    unit_id,
                    phase,
                    operation_step,
                    int(attempt),
                    int(execution_ordinal),
                    input_fingerprint,
                    prerequisites_json,
                    int(schema_version),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE workflow_operations
                SET superseded_by_operation_id=?, updated_at=?
                WHERE operation_id=?
                """,
                (operation_id, now, old_operation_id),
            )
            return self._require_workflow_operation(conn, operation_id)

    @staticmethod
    def _require_workflow_operation(
        conn: sqlite3.Connection, operation_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM workflow_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"workflow operation {operation_id} not found")
        return row

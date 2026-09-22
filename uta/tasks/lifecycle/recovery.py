from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from uta.shared.config import settings
from uta.tasks.domain_support import (
    normalize_repair_repo_identity as _normalize_repair_repo_identity,
    repair_selection_target_ids as _repair_selection_target_ids,
)
from uta.tasks.models import (
    TERMINAL_CLASS_STATUSES,
    json_loads,
)


class TaskLifecycleRecoveryMixin:
    """Recovery, requeuing, deduplication, and preemption mixin."""

    def find_active_duplicate_repair_task(self, task_id: int) -> Optional[int]:
        task = self.db.get_repo_task(task_id)
        if not task:
            raise KeyError(f"Task {task_id} not found")
        identity = self._repair_task_identity_parts(dict(task))
        if not identity:
            return None
        base_identity, target_ids = identity
        with self.db.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT * FROM repo_tasks
                    WHERE id<>?
                      AND status IN ('CREATED', 'QUEUED', 'RUNNING', 'STOP_REQUESTED')
                      AND language=?
                      AND COALESCE(branch_name, '')=COALESCE(?, '')
                      AND COALESCE(base_ref, '')=COALESCE(?, '')
                    ORDER BY created_at ASC, id ASC
                    """,
                    (task_id, task["language"], task["branch_name"], task["base_ref"]),
                )
            )
        for row in rows:
            other_identity = self._repair_task_identity_parts(dict(row))
            if not other_identity:
                continue
            other_base_identity, other_target_ids = other_identity
            if other_base_identity == base_identity and set(
                other_target_ids
            ).issuperset(target_ids):
                return int(row["id"])
        return None

    def find_active_duplicate_repair_task_for_targets(
        self,
        *,
        repo_identity: str,
        branch_name: Optional[str],
        base_ref: Optional[str],
        language: str,
        quality_gate_backend: str,
        target_ids: Iterable[str],
        module: Optional[str] = None,
    ) -> Optional[int]:
        normalized_repo_identity = _normalize_repair_repo_identity(repo_identity)
        normalized_target_ids = tuple(
            sorted({str(item).strip() for item in target_ids if str(item).strip()})
        )
        if not normalized_repo_identity or not normalized_target_ids:
            return None
        base_identity = self._repair_base_identity(
            repo_identity=normalized_repo_identity,
            branch_name=branch_name,
            base_ref=base_ref,
            language=language,
            quality_gate_backend=quality_gate_backend,
            module=module,
        )
        with self.db.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT * FROM repo_tasks
                    WHERE status IN ('CREATED', 'QUEUED', 'RUNNING', 'STOP_REQUESTED')
                      AND language=?
                      AND COALESCE(branch_name, '')=COALESCE(?, '')
                      AND COALESCE(base_ref, '')=COALESCE(?, '')
                    ORDER BY created_at ASC, id ASC
                    """,
                    (language or "java", branch_name, base_ref),
                )
            )
        requested = set(normalized_target_ids)
        for row in rows:
            other_identity = self._repair_task_identity_parts(dict(row))
            if not other_identity:
                continue
            other_base_identity, other_target_ids = other_identity
            if other_base_identity == base_identity and set(
                other_target_ids
            ).issuperset(requested):
                return int(row["id"])
        return None

    @staticmethod
    def _repair_task_identity(task: Dict[str, Any]) -> Optional[tuple[str, ...]]:
        parts = TaskLifecycleRecoveryMixin._repair_task_identity_parts(task)
        if not parts:
            return None
        base_identity, target_ids = parts
        return (*base_identity, *target_ids)

    @staticmethod
    def _repair_task_identity_parts(
        task: Dict[str, Any],
    ) -> Optional[tuple[tuple[str, ...], tuple[str, ...]]]:
        selection = json_loads(task.get("selection_json") or "{}")
        if selection.get("quality_mode") != "ci_incremental":
            return None
        context = json_loads(task.get("rdc_context_json") or "{}")
        pipeline = (
            context.get("pipeline") if isinstance(context.get("pipeline"), dict) else {}
        )
        repo_identity = _normalize_repair_repo_identity(
            str(pipeline.get("gitUrl") or "").strip()
            or str(pipeline.get("git_url") or "").strip()
            or str(task.get("repo_slug") or "").strip()
            or str(pipeline.get("appName") or "").strip()
        )
        target_ids = _repair_selection_target_ids(selection)
        if not repo_identity or not target_ids:
            return None
        base_identity = TaskLifecycleRecoveryMixin._repair_base_identity(
            repo_identity=repo_identity,
            branch_name=task.get("branch_name"),
            base_ref=task.get("base_ref"),
            language=str(task.get("language") or "java"),
            quality_gate_backend=str(selection.get("quality_gate_backend") or ""),
            module=task.get("module_filter"),
        )
        return base_identity, target_ids

    @staticmethod
    def _repair_base_identity(
        *,
        repo_identity: str,
        branch_name: Optional[str],
        base_ref: Optional[str],
        language: str,
        quality_gate_backend: str,
        module: Optional[str],
    ) -> tuple[str, ...]:
        return (
            "ci-incremental-repair",
            _normalize_repair_repo_identity(repo_identity),
            str(branch_name or ""),
            str(base_ref or ""),
            str(language or "java"),
            str(quality_gate_backend or ""),
            str(module or ""),
        )

    def resume_task(
        self,
        task_id: int,
        *,
        force_rerun_failed: bool = False,
        force_rerun_all: bool = False,
    ) -> None:
        row = self.db.get_repo_task(task_id)
        if not row:
            raise KeyError(f"Task {task_id} not found")
        from uta.tasks.generation_engine import require_durable_generation_task

        require_durable_generation_task(row, action="be resumed")
        if row["status"] == "STOP_REQUESTED":
            reason = row["current_detail"] or "stop requested"
            self.db.add_event(
                task_id,
                None,
                "task_resume_blocked",
                f"Cannot resume task while stop is still pending: {reason}",
                stage="queued",
                severity="WARNING",
            )
            raise RuntimeError(
                f"Task {task_id} stop is still pending; wait for STOPPED before resuming"
            )
        self.db.update_repo_task(
            task_id,
            status="QUEUED",
            stop_requested_at=None,
            finished_at=None,
            resume_count=int(row["resume_count"] or 0) + 1,
            current_stage="queued",
            current_detail="resume requested",
            last_error=None,
            error=None,
        )
        self.db.acknowledge_controls(task_id)
        resumable = {"CREATED", "PENDING", "QUEUED", "RUNNING", "STOPPED"}
        if force_rerun_failed:
            resumable.update(TERMINAL_CLASS_STATUSES - {"PASS", "CANCELLED"})
        if force_rerun_all:
            resumable.update(TERMINAL_CLASS_STATUSES)
        for row in self.db.list_class_tasks(task_id):
            if row["status"] in resumable:
                self.db.update_class_task(
                    row["id"],
                    status="QUEUED",
                    stage="queued",
                    current_stage="queued",
                    finished_at=None,
                    last_error=None,
                    error=None,
                )
        self._refresh_repo_counts(task_id)
        self.db.add_event(
            task_id,
            None,
            "task_resumed",
            "Task resumed and queued",
            stage="queued",
            payload={
                "force_rerun_failed": force_rerun_failed,
                "force_rerun_all": force_rerun_all,
            },
        )

    def clean_rerun_generation(
        self,
        task_id: int,
        *,
        reason: str,
        confirm_task_id: int,
        force_rerun_failed: bool = False,
        force_rerun_all: bool = False,
        requested_by: Optional[str] = None,
    ) -> str:
        """Replace a corrupt lineage through the audited recovery transaction."""
        row = self.db.get_repo_task(task_id)
        if row is None:
            raise KeyError(f"Task {task_id} not found")
        from uta.tasks.generation_engine import require_durable_generation_task

        require_durable_generation_task(row, action="be clean-rerun")
        from uta.tasks.workflow_rerun import clean_rerun_generation

        return clean_rerun_generation(
            self.db,
            task_id,
            reason=reason,
            confirm_task_id=confirm_task_id,
            force_rerun_failed=force_rerun_failed,
            force_rerun_all=force_rerun_all,
            requested_by=requested_by,
            default_batch_size=settings.classes_per_agent_run,
        )

    def requeue_orphaned_running_tasks(
        self,
        *,
        active_task_ids: Iterable[int],
        stale_after_seconds: float,
        now: Optional[datetime] = None,
    ) -> List[int]:
        active = {int(task_id) for task_id in active_task_ids}
        threshold = max(1.0, float(stale_after_seconds))
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        recovered: List[int] = []
        with self.db.connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT * FROM repo_tasks WHERE status='RUNNING' ORDER BY id ASC"
                )
            )
        for task in rows:
            task_id = int(task["id"])
            if task_id in active:
                continue
            heartbeat = self._latest_running_heartbeat(task_id)
            if heartbeat and not self._is_stale_heartbeat(heartbeat, threshold, now_dt):
                continue
            detail = "requeued after stale runner heartbeat"
            self.db.update_repo_task(
                task_id,
                status="QUEUED",
                current_stage="queued",
                current_detail=detail,
                finished_at=None,
                stop_requested_at=None,
                last_error=None,
                error=None,
            )
            for class_row in self.db.list_class_tasks(task_id):
                if class_row["status"] == "RUNNING":
                    self.db.update_class_task(
                        class_row["id"],
                        status="QUEUED",
                        stage="queued",
                        current_stage="queued",
                        current_detail=detail,
                        finished_at=None,
                        last_error=None,
                        error=None,
                    )
            self.db.add_event(
                task_id,
                None,
                "orphaned_task_requeued",
                detail,
                stage="queued",
                severity="WARNING",
                payload={
                    "stale_after_seconds": threshold,
                    "heartbeat_runner_id": heartbeat["runner_id"]
                    if heartbeat
                    else None,
                    "heartbeat_at": (
                        heartbeat["heartbeat_at"] or heartbeat["updated_at"]
                    )
                    if heartbeat
                    else None,
                },
            )
            self._refresh_repo_counts(task_id)
            recovered.append(task_id)
        return recovered

    def requeue_daemon_shutdown_tasks(
        self, active_task_ids: Iterable[int], *, reason: str
    ) -> List[int]:
        """Requeue tasks whose child processes were killed during daemon shutdown."""
        recovered: List[int] = []
        detail = f"requeued after daemon shutdown: {reason}"
        for task_id in sorted({int(value) for value in active_task_ids}):
            task = self.db.get_repo_task(task_id)
            if not task or task["status"] != "RUNNING":
                continue
            self.db.update_repo_task(
                task_id,
                status="QUEUED",
                current_stage="queued",
                current_detail=detail,
                finished_at=None,
                stop_requested_at=None,
                last_error=None,
                error=None,
            )
            for class_row in self.db.list_class_tasks(task_id):
                if class_row["status"] == "RUNNING":
                    self.db.update_class_task(
                        class_row["id"],
                        status="QUEUED",
                        stage="queued",
                        current_stage="queued",
                        current_detail=detail,
                        finished_at=None,
                        last_error=None,
                        error=None,
                    )
            self.db.add_event(
                task_id,
                None,
                "daemon_shutdown_requeued",
                detail,
                stage="queued",
                severity="WARNING",
                payload={"reason": reason},
            )
            self._refresh_repo_counts(task_id)
            recovered.append(task_id)
        return recovered

    def _latest_running_heartbeat(self, task_id: int) -> Optional[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM runner_heartbeats
                WHERE current_repo_task_id=? AND status='RUNNING'
                ORDER BY COALESCE(heartbeat_at, updated_at, created_at) DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()

    @staticmethod
    def _is_stale_heartbeat(
        heartbeat: sqlite3.Row, threshold_seconds: float, now: datetime
    ) -> bool:
        raw = (
            heartbeat["heartbeat_at"]
            or heartbeat["updated_at"]
            or heartbeat["created_at"]
        )
        try:
            heartbeat_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return True
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        return (now - heartbeat_at).total_seconds() >= threshold_seconds

    def reprioritize_task(self, task_id: int, priority: int) -> None:
        self.db.update_repo_task(task_id, priority=int(priority))
        self.db.add_event(
            task_id, None, "task_reprioritized", f"Priority set to {priority}"
        )

    def reprioritize_class(self, class_task_id: int, priority: int) -> None:
        row = self.db.get_class_task(class_task_id)
        if not row:
            raise KeyError(f"Class task {class_task_id} not found")
        self.db.update_class_task(class_task_id, priority=int(priority))
        self.db.add_event(
            row["repo_task_id"],
            class_task_id,
            "class_reprioritized",
            f"Class priority set to {priority}",
        )

    def preempt_running_same_repo_for_urgent(self, urgent_task_id: int) -> List[int]:
        urgent = self.db.get_repo_task(urgent_task_id)
        if not urgent:
            raise KeyError(f"Task {urgent_task_id} not found")
        with self.db.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT id FROM repo_tasks
                    WHERE repo_path=? AND status='RUNNING' AND id<>? AND priority>=100
                    ORDER BY started_at ASC, id ASC
                    """,
                    (urgent["repo_path"], urgent_task_id),
                )
            )
        preempted_ids: List[int] = []
        for row in rows:
            preempted_id = int(row["id"])
            reason = f"preempted by urgent RDC repair task {urgent_task_id}"
            self.stop_task(preempted_id, reason=reason)
            payload = {
                "preempted_by_task_id": urgent_task_id,
                "preempted_task_id": preempted_id,
            }
            self.db.add_event(
                preempted_id,
                None,
                "task_preempted",
                reason,
                stage="preempted",
                severity="WARNING",
                payload=payload,
            )
            self.db.add_event(
                urgent_task_id,
                None,
                "task_preempting",
                f"Requested stop for batch task {preempted_id}",
                stage="preempted",
                payload=payload,
            )
            preempted_ids.append(preempted_id)
        return preempted_ids

    def resume_preempted_tasks(self, urgent_task_id: int) -> List[int]:
        with self.db.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT repo_task_id, payload_json FROM task_events
                    WHERE event_type='task_preempted'
                    ORDER BY id ASC
                    """
                )
            )
        resumed: List[int] = []
        for row in rows:
            payload = json_loads(row["payload_json"])
            if int(payload.get("preempted_by_task_id") or 0) != int(urgent_task_id):
                continue
            preempted_id = int(row["repo_task_id"])
            task = self.db.get_repo_task(preempted_id)
            if not task or task["status"] != "STOPPED":
                continue
            self.resume_task(preempted_id)
            self.db.add_event(
                urgent_task_id,
                None,
                "preempted_task_resumed",
                f"Resumed preempted batch task {preempted_id}",
                stage="finished",
                payload={"preempted_task_id": preempted_id},
            )
            resumed.append(preempted_id)
        return resumed

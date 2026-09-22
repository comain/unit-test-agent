from __future__ import annotations

from typing import Optional

from uta.tasks.models import now_iso


class TaskLifecycleStateMixin:
    """State transition methods for tasks."""

    def start_task(self, task_id: int) -> None:
        self.db.update_repo_task(
            task_id,
            status="QUEUED",
            current_stage="queued",
            started_at=None,
            finished_at=None,
            last_error=None,
            error=None,
        )
        self.db.add_event(
            task_id,
            None,
            "task_queued",
            "Task queued for daemon execution",
            stage="queued",
        )

    def mark_running(
        self, task_id: int, *, stage: str = "startup", detail: Optional[str] = None
    ) -> None:
        row = self.db.get_repo_task(task_id)
        started_at = row["started_at"] if row and row["started_at"] else now_iso()
        self.db.update_repo_task(
            task_id,
            status="RUNNING",
            current_stage=stage,
            current_detail=detail,
            started_at=started_at,
            finished_at=None,
            last_error=None,
            error=None,
        )
        self.db.add_event(
            task_id, None, "task_running", detail or "Task running", stage=stage
        )

    def stop_task(self, task_id: int, *, reason: Optional[str] = None) -> None:
        self.db.request_control(task_id, "stop", reason=reason)
        self.db.update_repo_task(
            task_id,
            status="STOP_REQUESTED",
            stop_requested_at=now_iso(),
            current_detail=reason,
        )
        self.db.add_event(
            task_id,
            None,
            "task_stopped",
            reason or "Stop requested",
            severity="WARNING",
        )

    def mark_stopped(
        self, task_id: int, *, reason: Optional[str] = None, stage: str = "stopped"
    ) -> None:
        self.db.acknowledge_controls(task_id, action="stop")
        self.db.update_repo_task(
            task_id,
            status="STOPPED",
            current_stage=stage,
            current_detail=reason,
            finished_at=now_iso(),
            last_error=reason,
            error=reason,
        )
        self.db.add_event(
            task_id,
            None,
            "task_stopped",
            reason or "Task stopped cooperatively",
            stage=stage,
            severity="WARNING",
        )

    def check_stop_requested(self, task_id: int) -> Optional[str]:
        task = self.db.get_repo_task(task_id)
        if task and task["status"] == "STOP_REQUESTED":
            return task["current_detail"] or "stop requested"
        control = self.db.latest_control(task_id)
        if control and control["action"] in {"stop", "cancel"}:
            return control["reason"] or f"{control['action']} requested"
        return None

    def cancel_task(self, task_id: int, *, reason: Optional[str] = None) -> None:
        self.db.request_control(task_id, "cancel", reason=reason)
        for row in self.db.list_class_tasks(task_id):
            if row["status"] in {"CREATED", "PENDING", "QUEUED", "RUNNING", "STOPPED"}:
                self.db.update_class_task(
                    row["id"],
                    status="CANCELLED",
                    stage="cancelled",
                    current_stage="cancelled",
                    finished_at=now_iso(),
                    last_error=reason,
                    error=reason,
                )
        self.db.finish_task_with_terminal_event(
            task_id,
            status="CANCELLED",
            message=reason or "Task cancelled",
            stage="cancelled",
            severity="WARNING",
            summary_event_type="task_cancelled",
        )

    def mark_failed(
        self, repo_task_id: int, message: str, *, stage: str = "failed"
    ) -> None:
        from uta.shared.config import settings

        threshold = int(settings.quarantine_threshold)
        poison = (
            threshold > 0 and self.db.count_task_failures(repo_task_id) + 1 >= threshold
        )
        terminal_status = "POISONED" if poison else "FAILED"
        terminal_message = (
            f"Auto-quarantined after {threshold} failures. Last: {message}"
            if poison
            else message
        )
        self.db.finish_task_with_terminal_event(
            repo_task_id,
            status=terminal_status,
            message=terminal_message,
            stage="quarantined" if poison else stage,
            severity="ERROR",
            task_fields={"last_error": terminal_message, "error": terminal_message},
            summary_event_type=("task_failed", "task_poisoned")
            if poison
            else "task_failed",
        )
        self.resume_preempted_tasks(repo_task_id)

    def mark_completed(
        self, repo_task_id: int, *, message: str = "Task completed"
    ) -> None:
        self.db.finish_task_with_terminal_event(
            repo_task_id,
            status="COMPLETED",
            message=message,
            stage="finished",
            summary_event_type="task_completed",
        )
        self.resume_preempted_tasks(repo_task_id)

    def mark_poisoned(self, repo_task_id: int, message: str) -> None:
        self.db.finish_task_with_terminal_event(
            repo_task_id,
            status="POISONED",
            message=message,
            stage="quarantined",
            severity="ERROR",
            task_fields={"last_error": message, "error": message},
            summary_event_type="task_poisoned",
        )

    def mark_budget_exceeded(
        self, repo_task_id: int, message: str = "Budget cap exceeded"
    ) -> None:
        self.db.finish_task_with_terminal_event(
            repo_task_id,
            status="BUDGET_EXCEEDED",
            message=message,
            stage="budget_exceeded",
            severity="ERROR",
            task_fields={"last_error": message, "error": message},
            summary_event_type="task_budget_exceeded",
        )
        self.resume_preempted_tasks(repo_task_id)

    def unblock(self, repo_task_id: int) -> None:
        """Reset a POISONED or BUDGET_EXCEEDED task to QUEUED so the daemon can pick it up."""
        task = self.db.get_repo_task(repo_task_id)
        if task is None:
            raise ValueError(f"Task {repo_task_id} not found")
        from uta.tasks.generation_engine import require_durable_generation_task

        require_durable_generation_task(task, action="be unblocked")
        status = task["status"]
        if status not in ("POISONED", "BUDGET_EXCEEDED", "FAILED"):
            raise ValueError(
                f"Task {repo_task_id} has status {status!r}; only POISONED/BUDGET_EXCEEDED/FAILED can be unblocked"
            )
        self.db.update_repo_task(
            repo_task_id,
            status="QUEUED",
            current_stage="unblocked",
            current_detail=f"Unblocked from {status}",
            finished_at=None,
            error=None,
        )
        self.db.acknowledge_controls(repo_task_id)
        self.db.add_event(
            repo_task_id,
            None,
            "task_unblocked",
            f"Unblocked from {status}",
            stage="unblocked",
        )

from __future__ import annotations

from typing import Iterable, Optional

from uta.tasks.models import now_iso


class TaskGitAccountingMixin:
    """Git commit and push outcome accounting."""

    def record_commit(
        self,
        repo_task_id: int,
        *,
        class_fqns: Iterable[str],
        commit_sha: Optional[str],
        pushed_at: Optional[str] = None,
        remote_ref: Optional[str] = None,
    ) -> None:
        for class_fqn in class_fqns:
            row = self.db.find_class_task(repo_task_id, class_fqn)
            if row:
                self.db.update_class_task(
                    row["id"], commit_sha=commit_sha, pushed_at=pushed_at
                )
        self.db.update_repo_task(
            repo_task_id, latest_commit=commit_sha, remote_ref=remote_ref
        )
        self.db.add_event(
            repo_task_id,
            None,
            "commit_created" if commit_sha else "commit_skipped",
            f"Commit {commit_sha}" if commit_sha else "No commit sha recorded",
            payload={"class_fqns": list(class_fqns), "remote_ref": remote_ref},
        )

    def record_push_verified(
        self,
        repo_task_id: int,
        *,
        branch_name: str,
        local_head: str,
        remote_head: Optional[str],
    ) -> None:
        ok = bool(local_head and remote_head and local_head == remote_head)
        updates = {
            "remote_ref": remote_head,
            "latest_commit": local_head,
            "error": None
            if ok
            else f"Remote ref mismatch for {branch_name}: local={local_head} remote={remote_head}",
        }
        task = self.db.get_repo_task(repo_task_id)
        push_failed_rows = (
            [
                row
                for row in self.db.list_class_tasks(repo_task_id)
                if row["status"] == "PUSH_FAILED"
            ]
            if ok
            else []
        )
        if ok and task and task["status"] == "FAILED" and push_failed_rows:
            updates.update(
                {
                    "status": "COMPLETED",
                    "current_stage": "finished",
                    "current_detail": f"Push verified for {branch_name}",
                    "last_error": None,
                    "finished_at": now_iso(),
                }
            )
            pushed_at = now_iso()
            for row in push_failed_rows:
                self.db.update_class_task(
                    row["id"],
                    status="PASS",
                    current_stage="finished",
                    current_detail="PUSH_VERIFIED",
                    error=None,
                    last_error=None,
                    commit_sha=local_head,
                    pushed_at=pushed_at,
                )
        elif not ok:
            updates["status"] = "FAILED"
        self.db.update_repo_task(repo_task_id, **updates)
        self.db.add_event(
            repo_task_id,
            None,
            "push_verified" if ok else "push_failed",
            f"{branch_name}: local={local_head} remote={remote_head}",
            severity="INFO" if ok else "ERROR",
        )

    def record_push_failed(
        self,
        repo_task_id: int,
        *,
        branch_name: str,
        message: str,
        class_fqns: Optional[Iterable[str]] = None,
    ) -> None:
        for class_fqn in class_fqns or []:
            row = self.db.find_class_task(repo_task_id, class_fqn)
            if row:
                self.db.update_class_task(
                    row["id"], status="PUSH_FAILED", error=message, last_error=message
                )
        self.db.update_repo_task(
            repo_task_id,
            status="FAILED",
            current_stage="push_failed",
            current_detail=message,
            error=message,
            last_error=message,
        )
        self.db.add_event(
            repo_task_id,
            None,
            "push_failed",
            f"{branch_name}: {message}",
            stage="push",
            severity="ERROR",
            payload={"class_fqns": list(class_fqns or [])},
        )

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from uta.tasks.models import (
    TERMINAL_CLASS_STATUSES,
    now_iso,
)
from uta.shared.targets import (
    TargetIdentity,
    TargetRef,
    event_payload_for_targets,
    result_key,
)


class TaskLifecycleStageMixin:
    """Stage and target event tracking mixin for task lifecycle."""

    def record_stage(
        self,
        repo_task_id: int,
        stage: str,
        *,
        detail: Optional[str] = None,
        class_fqns: Optional[Iterable[str]] = None,
    ) -> None:
        self.db.update_repo_task(
            repo_task_id, current_stage=stage, current_detail=detail
        )
        updated_classes = []
        for class_fqn in dict.fromkeys(class_fqns or []):
            row = self.db.find_class_task(repo_task_id, class_fqn)
            if not row:
                continue
            status = row["status"]
            if status in TERMINAL_CLASS_STATUSES:
                continue
            updates = {"current_stage": stage, "current_detail": detail}
            updates["stage"] = stage
            updates["status"] = "RUNNING"
            if not row["started_at"]:
                updates["started_at"] = now_iso()
            self.db.update_class_task(row["id"], **updates)
            updated_classes.append(row["id"])
        self.db.add_event(
            repo_task_id,
            updated_classes[0] if len(updated_classes) == 1 else None,
            "stage_started",
            detail or stage,
            stage=stage,
            payload=self._target_event_payload(repo_task_id, class_fqns or []),
        )

    def record_stage_for_targets(
        self,
        repo_task_id: int,
        stage: str,
        *,
        detail: Optional[str] = None,
        targets: Optional[Iterable[TargetRef]] = None,
    ) -> None:
        target_keys = [result_key(target) for target in targets or []]
        self.record_stage(repo_task_id, stage, detail=detail, class_fqns=target_keys)

    def record_stage_completed(
        self,
        repo_task_id: int,
        stage: str,
        *,
        detail: Optional[str] = None,
        class_fqns: Optional[Iterable[str]] = None,
        class_task_id: Optional[int] = None,
    ) -> None:
        if class_task_id is None:
            fqns = list(dict.fromkeys(class_fqns or []))
            if len(fqns) == 1:
                row = self.db.find_class_task(repo_task_id, fqns[0])
                class_task_id = row["id"] if row else None
        self.db.add_event(
            repo_task_id,
            class_task_id,
            "stage_completed",
            detail or stage,
            stage=stage,
            payload=self._target_event_payload(repo_task_id, class_fqns or []),
        )

    def record_stage_completed_for_targets(
        self,
        repo_task_id: int,
        stage: str,
        *,
        detail: Optional[str] = None,
        targets: Optional[Iterable[TargetRef]] = None,
        class_task_id: Optional[int] = None,
    ) -> None:
        target_keys = [result_key(target) for target in targets or []]
        self.record_stage_completed(
            repo_task_id,
            stage,
            detail=detail,
            class_fqns=target_keys,
            class_task_id=class_task_id,
        )

    def _target_event_payload(
        self, repo_task_id: int, storage_keys: Iterable[str]
    ) -> Dict[str, Any]:
        keys = list(dict.fromkeys(storage_keys or []))
        if not keys:
            return {"class_fqns": []}
        rows = [self.db.find_class_task(repo_task_id, key) for key in keys]
        rows = [row for row in rows if row]
        if not rows:
            return {"class_fqns": keys}
        targets = [
            TargetIdentity(
                language=row["language"] if "language" in row.keys() else "java",
                target_id=row["target_id"]
                if "target_id" in row.keys() and row["target_id"]
                else row["class_fqn"],
                display_name=row["display_name"]
                if "display_name" in row.keys() and row["display_name"]
                else row["class_fqn"],
                source_path=row["source_path"] if "source_path" in row.keys() else None,
                symbol=row["symbol"] if "symbol" in row.keys() else row["class_fqn"],
                granularity=row["target_granularity"]
                if "target_granularity" in row.keys() and row["target_granularity"]
                else "class",
            )
            for row in rows
        ]
        payload = event_payload_for_targets(targets)
        payload["class_fqns"] = keys
        return payload

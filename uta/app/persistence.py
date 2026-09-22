"""Task storage, presented to the generation workflow as its own narrow ports.

The workflow declares what it needs (`uta/testgen/ports/persistence.py`); this
supplies it. Both dependencies point inward from here, which is what lets
`uta.testgen` import no persistence implementation and `uta.tasks` import no
workflow.

The adapter is deliberately thin and deliberately translating. Testgen speaks in
string task ids and mappings; task storage speaks in integer primary keys and
keyword-only arguments. Doing that conversion here rather than in a phase is the
difference between a port and an alias.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from uta.testgen.ports.persistence import TaskSnapshot


class TaskPersistenceAdapter:
    """One object implementing every task port the workflow declares.

    One object rather than four because a single deployment has a single task
    database, and handing a phase four objects backed by the same connection
    would be ceremony. The *ports* stay separate, which is what a phase's
    signature names, so nothing can quietly reach past what it asked for.
    """

    def __init__(self, task_db_path: str | Path) -> None:
        self._path = str(task_db_path)
        self._manager: Any = None

    # -- lazily, because building a manager opens a database ------------------

    @property
    def manager(self) -> Any:
        if self._manager is None:
            from uta.tasks.manager import TaskManager

            self._manager = TaskManager(self._path)
        return self._manager

    @property
    def db(self) -> Any:
        return self.manager.db

    # -- TaskReaderPort -------------------------------------------------------

    def get_task_snapshot(self, task_id: str) -> Optional[TaskSnapshot]:
        row = self.manager.db.get_repo_task(int(task_id))
        if row is None:
            return None
        config_snapshot = str(row["config_snapshot_json"]) if "config_snapshot_json" in row.keys() and row["config_snapshot_json"] is not None else None
        return TaskSnapshot(
            task_id=str(row["id"]),
            status=str(row["status"] or ""),
            language=str((row["language"] if "language" in row.keys() else "") or ""),
            repo_path=Path(str(row["repo_path"] or "")),
            created_at=str((row["created_at"] if "created_at" in row.keys() else "") or ""),
            updated_at=str((row["updated_at"] if "updated_at" in row.keys() else "") or ""),
            config_snapshot_json=config_snapshot,
        )

    def list_class_task_ids(self, task_id: str) -> Sequence[str]:
        return [str(row["id"]) for row in self.manager.list_class_tasks(int(task_id))]

    def token_totals(self, task_id: str) -> Mapping[str, int]:
        return dict(self.manager._token_totals(int(task_id)) or {})

    def ensure_stable_batches(
        self,
        repo_task_id: int,
        ordered_target_ids: Sequence[str],
        batch_size: int,
        workflow_run_id: Optional[str] = None,
    ) -> Sequence[Any]:
        from uta.testgen.batches import ensure_stable_generation_batches

        requested = tuple(str(target_id) for target_id in ordered_target_ids)
        rows = self.manager.list_class_tasks(int(repo_task_id))
        persisted = tuple(str(row["target_id"] or row["class_fqn"]) for row in rows)
        requested_set = set(requested)
        # A resume selects only unfinished targets, but batch identity belongs
        # to the task's original full order. Expand only an order-preserving
        # subset; reordered or unknown inputs still reach the strict validator
        # below and fail instead of silently regrouping paid work.
        effective = (
            persisted
            if requested and tuple(item for item in persisted if item in requested_set) == requested
            else requested
        )
        return ensure_stable_generation_batches(
            self.manager.db,
            repo_task_id=repo_task_id,
            ordered_target_ids=effective,
            batch_size=batch_size,
            workflow_run_id=workflow_run_id,
        )

    def first_non_terminal_batch(self, batches: Sequence[Any]) -> Optional[Any]:
        from uta.testgen.batches import first_non_terminal_batch

        return first_non_terminal_batch(self.manager.db, batches)

    # -- WorkflowExecutionPort ------------------------------------------------

    def workflow_state_root(self) -> Path:
        return Path(self._path).resolve().parent / "workflow-state"

    def create_progress_batcher(
        self, repo_task_id: int, workflow_run_id: str, unit_id: str
    ) -> Any:
        from agent_core.runtime import ProgressBatcher
        from uta.testgen.progress import UtaProgressEventStore

        store = UtaProgressEventStore(
            self.manager.db,
            repo_task_id=repo_task_id,
            workflow_run_id=workflow_run_id,
            unit_id=unit_id,
        )
        return ProgressBatcher(store.append_batch, budget=store.progress_budget())

    def create_operation_ledger(
        self,
        artifact_store_path: Path,
        *,
        fingerprint: Any,
        allowed_edit: Any,
        output_fingerprints: Any,
        max_crash_replays_per_operation: int = 1,
    ) -> Any:
        from uta.testgen.operations import OperationArtifactStore, WorkflowOperationLedger

        return WorkflowOperationLedger(
            self.manager.db,
            OperationArtifactStore(artifact_store_path),
            fingerprint=fingerprint,
            allowed_edit=allowed_edit,
            output_fingerprints=output_fingerprints,
            max_crash_replays_per_operation=max_crash_replays_per_operation,
        )

    def create_synthetic_task(self, request: Any) -> int:
        from uta.shared.targets import legacy_class_fqn_for_storage

        self.manager.db.init()
        targets = list(getattr(request, "targets", []) or [])
        task_id = self.manager.db.create_repo_task(
            {
                "repo_path": str(Path(request.repo_path).resolve()),
                "repo_slug": f"standalone-{request.language}",
                "language": request.language,
                "module_filter": getattr(request, "module", None),
                "selection": {"targets": [target.as_selection() for target in targets]},
                "coverage_gate": getattr(request, "coverage_gate", 0.0),
                "mutation_gate": getattr(request, "mutation_gate", 0.0),
                "total_classes": len(targets),
            }
        )
        for target in targets:
            self.manager.db.create_class_task(
                task_id,
                legacy_class_fqn_for_storage(target),
                module=getattr(request, "module", None),
                priority=100,
                language=target.language,
                target_id=target.target_id,
                source_path=target.source_path,
                symbol=target.symbol,
                target_granularity=target.granularity,
                display_name=target.display_name,
            )
        return task_id

    # -- TaskControlPort ------------------------------------------------------

    def is_stop_requested(self, task_id: str) -> bool:
        return bool(self.manager.check_stop_requested(int(task_id)))

    def stop_reason(self, task_id: str) -> Optional[str]:
        return self.manager.check_stop_requested(int(task_id))

    def mark_stopped(self, task_id: str, reason: str = "", *, stage: str = "") -> None:
        kwargs = {"reason": reason or None}
        if stage:
            kwargs["stage"] = stage
        self.manager.mark_stopped(int(task_id), **kwargs)

    def mark_failed(self, task_id: str, reason: str = "", *, stage: str = "") -> None:
        kwargs = {"stage": stage} if stage else {}
        self.manager.mark_failed(int(task_id), reason, **kwargs)

    # -- TaskSafetyPort -------------------------------------------------------

    def record_unsafe_diff(
        self,
        task_id: str,
        class_fqns: Sequence[str],
        *,
        stage: str,
        message: str,
        paths: Sequence[str] = (),
        fail_task: bool = False,
    ) -> None:
        import time

        task = int(task_id)
        rows = self.manager.db.find_class_tasks(task, list(class_fqns)).values()
        finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for row in rows:
            self.manager.db.update_class_task(
                row["id"],
                status="UNSAFE_DIFF",
                current_stage=stage,
                stage=stage,
                error=message,
                last_error=message,
                finished_at=finished,
            )
        if fail_task:
            self.manager.mark_failed(task, message, stage=stage)
        self.manager.db.add_event(
            task,
            None,
            "unsafe_diff",
            message,
            stage=stage,
            severity="ERROR",
            payload={"paths": list(paths)} if paths else None,
        )

    def check_turn_budget_and_increment(
        self,
        task_id: str,
        class_fqns: Sequence[str],
        *,
        phase: str,
    ) -> None:
        from uta.tasks.models import json_loads
        from uta.testgen.workspace_guard import TaskBudgetExceeded

        task_int = int(task_id)
        task = self.manager.db.get_repo_task(task_int)
        turn_limit = None
        try:
            budget_snapshot = json_loads(task["budget_config_snapshot_json"] if task else None)
            config_snapshot = json_loads(task["config_snapshot_json"] if task else None)
            turn_limit = (
                budget_snapshot.get("max_llm_turns_per_class")
                or config_snapshot.get("max_llm_turns_per_class")
                or os.environ.get("UTA_MAX_LLM_TURNS_PER_CLASS")
            )
            turn_limit = int(turn_limit) if turn_limit is not None else None
        except Exception:
            turn_limit = None

        class_rows = self.manager.db.find_class_tasks(task_int, list(class_fqns))
        for class_fqn in class_fqns:
            row = class_rows.get(class_fqn)
            if not row:
                continue
            next_turn_count = int(row["llm_turn_count"] or 0) + 1
            if turn_limit and next_turn_count > turn_limit:
                message = f"LLM turn hard cap exceeded for {class_fqn}: {next_turn_count}>{turn_limit}"
                self.manager.db.update_class_task(
                    row["id"], status="BUDGET_EXCEEDED", error=message, last_error=message
                )
                self.manager.mark_failed(task_int, message, stage="budget_blocked")
                self.manager.db.add_event(
                    task_int,
                    row["id"],
                    "budget_blocked",
                    message,
                    stage=phase,
                    severity="ERROR",
                )
                raise TaskBudgetExceeded(message)
            self.manager.db.update_class_task(row["id"], llm_turn_count=next_turn_count)

    # -- TaskEventPort --------------------------------------------------------

    def add_event(self, task_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        """Translate the workflow's flat payload into the row storage expects.

        `TaskDB.add_event` takes a class-task id, a message and three keyword
        fields; the workflow knows none of that shape and should not have to.
        """
        fields = dict(payload)
        self.manager.db.add_event(
            int(task_id),
            fields.pop("class_task_id", None),
            event_type,
            str(fields.pop("message", "")),
            stage=fields.pop("stage", None),
            severity=str(fields.pop("severity", "INFO")),
            payload=fields.pop("payload", None) or (fields or None),
        )

    def record_stage(self, task_id: str, stage: str, payload: Mapping[str, Any]) -> None:
        self.manager.record_stage(int(task_id), stage, **dict(payload))

    def record_stage_completed(self, task_id: str, stage: str, payload: Mapping[str, Any]) -> None:
        self.manager.record_stage_completed(int(task_id), stage, **dict(payload))

    # -- DeliveryOutcomePort --------------------------------------------------

    def record_commit(self, task_id: str, commit_sha: str, **kwargs: Any) -> None:
        payload = kwargs.pop("payload", None)
        if isinstance(payload, Mapping):
            kwargs.update(dict(payload))
        self.manager.record_commit(int(task_id), commit_sha=commit_sha, **kwargs)

    def record_push_verified(self, task_id: str, **kwargs: Any) -> None:
        payload = kwargs.pop("payload", None)
        if isinstance(payload, Mapping):
            kwargs.update(dict(payload))
        self.manager.record_push_verified(int(task_id), **kwargs)

    def record_push_failed(self, task_id: str, reason: str = "", **kwargs: Any) -> None:
        payload = kwargs.pop("payload", None)
        if isinstance(payload, Mapping):
            kwargs.update(dict(payload))
        if "reason" not in kwargs and reason:
            kwargs["reason"] = reason
        self.manager.record_push_failed(int(task_id), **kwargs)

    def get_task_branch(self, task_id: str) -> Optional[str]:
        task = self.manager.get_task(int(task_id))
        return str((task or {}).get("branch_name") or "").strip() or None

    def get_rdc_delivery_context(
        self, task_id: str, branch_name: str, batch: Sequence[str]
    ) -> Optional[Dict[str, Any]]:
        from uta.tasks.rdc_delivery import rdc_delivery_context_from_task

        return rdc_delivery_context_from_task(self.manager, int(task_id), branch_name, list(batch))

    def sync_results(self, task_id: str, results: Mapping[str, Any], **kwargs: Any) -> None:
        self.manager.sync_results(int(task_id), dict(results), **kwargs)

    def rollup_tokens_and_costs(
        self, task_id: str, session_tokens: Mapping[str, Any], phase_tokens: Mapping[str, Any]
    ) -> None:
        _mgr = self.manager
        _totals = _mgr._token_totals(session_tokens, phase_tokens)
        _cost = _mgr.db.workflow_operation_cost_total(int(task_id))
        _provider_cost = _cost["provider_cost_usd"]
        _mgr.db.update_repo_task(
            int(task_id),
            input_tokens=_totals["input"],
            output_tokens=_totals["output"],
            cache_read_tokens=_totals["cache_read"],
            cache_write_tokens=_totals["cache_write"],
            reasoning_tokens=_totals["reasoning"],
            total_tokens=sum(_totals.values()),
            actual_input_tokens=_totals["input"],
            actual_output_tokens=_totals["output"],
            actual_cache_read_tokens=_totals["cache_read"],
            actual_cache_write_tokens=_totals["cache_write"],
            actual_cost=_provider_cost,
            provider_cost_usd=_provider_cost,
        )


def task_persistence_for(task_db_path: str | Path) -> TaskPersistenceAdapter:
    """The adapter a workflow run should be given."""
    return TaskPersistenceAdapter(task_db_path)


def register_task_persistence() -> None:
    """Tell testgen how to build task ports.

    Called once during application composition. Testgen cannot import this
    module -- that would put the composition root back inside the workflow --
    so the direction is inverted here, where knowing about both sides is the job.
    """
    from uta.testgen.ports.registry import set_task_persistence_provider

    set_task_persistence_provider(task_persistence_for)


__all__ = [
    "TaskPersistenceAdapter",
    "register_task_persistence",
    "task_persistence_for",
]

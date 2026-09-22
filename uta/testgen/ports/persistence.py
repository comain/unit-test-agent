"""What the generation workflow needs from task storage, and nothing more.

These are *consumer-side* ports: testgen declares them because testgen is what
calls them, and the application implements them with real task storage. The
dependency therefore points from the app down into both sides, and testgen
imports no persistence implementation at all.

They are split by responsibility on purpose. One interface carrying every method
the workflow happens to call is the manager again with a Protocol on top -- the
workflow could still reach anything, and no reader could tell from a signature
what a phase actually touches. A test enforces the split by capping how wide any
one port may get.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class TaskSnapshot:
    """An immutable read of a task, handed to the workflow instead of a row.

    A snapshot rather than a live handle: a phase that holds a database object
    can read anything at any time, and the whole point here is that it cannot.
    """

    task_id: str
    status: str
    language: str
    repo_path: Path
    created_at: str = ""
    updated_at: str = ""
    config_snapshot_json: Optional[str] = None
    config: Mapping[str, Any] = field(default_factory=dict)
    results: Mapping[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            val = getattr(self, key)
            return val if val is not None else default
        if key == "id":
            return self.task_id
        return self.config.get(key, default)

    def __getitem__(self, key: str) -> Any:
        val = self.get(key)
        if val is None and key not in ("config_snapshot_json",):
            raise KeyError(key)
        return val


@runtime_checkable
class TaskReaderPort(Protocol):
    """Reads that inform a decision, never a handle to read more with."""

    def get_task_snapshot(self, task_id: str) -> Optional[TaskSnapshot]: ...

    def list_class_task_ids(self, task_id: str) -> Sequence[str]: ...

    def token_totals(self, task_id: str) -> Mapping[str, int]: ...

    def ensure_stable_batches(
        self,
        repo_task_id: int,
        ordered_target_ids: Sequence[str],
        batch_size: int,
        workflow_run_id: Optional[str] = None,
    ) -> Sequence[Any]: ...

    def first_non_terminal_batch(self, batches: Sequence[Any]) -> Optional[Any]: ...


@runtime_checkable
class WorkflowExecutionPort(Protocol):
    """Workflow state directories, operation ledgers, and progress tracking."""

    def workflow_state_root(self) -> Path: ...

    def create_progress_batcher(
        self, repo_task_id: int, workflow_run_id: str, unit_id: str
    ) -> Any: ...

    def create_operation_ledger(
        self,
        artifact_store_path: Path,
        *,
        fingerprint: Any,
        allowed_edit: Any,
        output_fingerprints: Any,
        max_crash_replays_per_operation: int = 1,
    ) -> Any: ...

    def create_synthetic_task(self, request: Any) -> int: ...


@runtime_checkable
class TaskControlPort(Protocol):
    """Whether to keep going, and how a run ended.

    Cancellation is a question the workflow asks between phases; stopping and
    failing are how it answers for itself. Nothing here writes evidence.
    """

    def is_stop_requested(self, task_id: str) -> bool: ...

    def mark_stopped(self, task_id: str, reason: str = "", *, stage: str = "") -> None: ...

    def mark_failed(self, task_id: str, reason: str = "", *, stage: str = "") -> None: ...

    def stop_reason(self, task_id: str) -> Optional[str]:
        """Why a stop was requested, for the message a caller raises."""
        ...


@runtime_checkable
class TaskEventPort(Protocol):
    """Progress and stage transitions, as they happen."""

    def add_event(self, task_id: str, event_type: str, payload: Mapping[str, Any]) -> None: ...

    def record_stage(self, task_id: str, stage: str, payload: Mapping[str, Any]) -> None: ...

    def record_stage_completed(self, task_id: str, stage: str, payload: Mapping[str, Any]) -> None: ...

    def rollup_tokens_and_costs(
        self, task_id: str, session_tokens: Mapping[str, Any], phase_tokens: Mapping[str, Any]
    ) -> None: ...


@runtime_checkable
class TaskSafetyPort(Protocol):
    """Recording that a run touched something it should not have."""

    def record_unsafe_diff(
        self,
        task_id: str,
        class_fqns: Sequence[str],
        *,
        stage: str,
        message: str,
        paths: Sequence[str] = (),
        fail_task: bool = False,
    ) -> None: ...

    def check_turn_budget_and_increment(
        self,
        task_id: str,
        class_fqns: Sequence[str],
        *,
        phase: str,
    ) -> None: ...


@runtime_checkable
class DeliveryOutcomePort(Protocol):
    """What happened when generated tests were published."""

    def record_commit(self, task_id: str, commit_sha: str, **kwargs: Any) -> None: ...

    def record_push_verified(self, task_id: str, **kwargs: Any) -> None: ...

    def record_push_failed(self, task_id: str, reason: str = "", **kwargs: Any) -> None: ...

    def get_task_branch(self, task_id: str) -> Optional[str]: ...

    def get_rdc_delivery_context(
        self, task_id: str, branch_name: str, batch: Sequence[str]
    ) -> Optional[Dict[str, Any]]: ...

    def sync_results(self, task_id: str, results: Mapping[str, Any], **kwargs: Any) -> None: ...


__all__ = [
    "DeliveryOutcomePort",
    "TaskSafetyPort",
    "TaskControlPort",
    "TaskEventPort",
    "TaskReaderPort",
    "TaskSnapshot",
    "WorkflowExecutionPort",
]

"""Workflow progress state shared by language and delivery nodes."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from agent_core.runtime import (
    AgentProgressEvent,
    AppendBatchResult,
    ProgressBudget,
)

from uta.testgen.ports.registry import task_ports_from_state
from uta.testgen.targets import active_target_ids
from uta.testgen.workspace_guard import TaskStopRequested

logger = logging.getLogger("uta")

DEFAULT_TASK_PROGRESS_EVENTS = 20_000
DEFAULT_TASK_PROGRESS_BYTES = 20 * 1024 * 1024


class UtaProgressEventStore:
    """Adapt agent-core's safe projection to UTA's transactional event log."""

    def __init__(
        self,
        db: Any,
        *,
        repo_task_id: int,
        workflow_run_id: str,
        unit_id: str,
        attempt: int = 0,
        max_attempts: int = 0,
        max_events: int = DEFAULT_TASK_PROGRESS_EVENTS,
        max_serialized_bytes: int = DEFAULT_TASK_PROGRESS_BYTES,
    ) -> None:
        self.db = db
        self.repo_task_id = int(repo_task_id)
        self.workflow_run_id = str(workflow_run_id)
        self.unit_id = str(unit_id)
        self.attempt = int(attempt)
        self.max_attempts = int(max_attempts)
        self.max_events = int(max_events)
        self.max_serialized_bytes = int(max_serialized_bytes)

    def append_batch(
        self, *, session_id: str, events: List[AgentProgressEvent]
    ) -> AppendBatchResult:
        records = [self._record(session_id, event) for event in events]
        admitted = self.db.append_progress_event_batch(
            self.repo_task_id,
            records,
            max_events=self.max_events,
            max_serialized_bytes=self.max_serialized_bytes,
        )
        return AppendBatchResult(**admitted)

    def progress_budget(self) -> ProgressBudget:
        """Seed agent-core's process-local budget from durable task usage."""
        task = self.db.get_repo_task(self.repo_task_id)
        if task is None:
            raise KeyError(f"Task {self.repo_task_id} not found")
        return ProgressBudget(
            max_events=self.max_events,
            max_serialized_bytes=self.max_serialized_bytes,
            used_events=int(task["progress_event_count"] or 0),
            used_serialized_bytes=int(task["progress_event_bytes"] or 0),
        )

    def _record(self, session_id: str, event: AgentProgressEvent) -> Dict[str, Any]:
        kind = str(event.kind or "")
        payload = {
            "workflowRunId": self.workflow_run_id,
            "unitId": self.unit_id,
            "attempt": self.attempt,
            "maxAttempts": self.max_attempts,
            "sessionId": str(session_id or event.session_id or ""),
            "sequence": int(event.sequence),
            "kind": kind,
            "status": event.status,
            "detail": event.detail,
            "tool": event.tool,
        }
        return {
            "event_type": (
                "progress_truncated"
                if kind == "progress_truncated"
                else "agent_progress"
            ),
            "severity": (
                "ERROR"
                if kind == "error"
                else "WARNING"
                if kind in {"rate_limit", "model_fallback"}
                else "INFO"
            ),
            "stage": str(event.phase or "") or None,
            "message": str(event.summary or "Agent progress"),
            # Empty strings are dropped too, not just None: an error event
            # arriving before a session exists was writing `sessionId: ""`,
            # which reads as evidence that there is no session rather than as
            # "not known yet". The turn's own outcome event carries the
            # identifiers once the turn ends.
            "payload": {
                key: value
                for key, value in payload.items()
                if value is not None and value != ""
            },
        }


class UtaTaskEventStreamStore:
    """Present UTA's existing event table to agent-core's SSE streamer."""

    def __init__(self, db: Any):
        self.db = db

    def events_since(
        self, *, task_ref: str, after_id: int = 0, limit: int = 500
    ):
        try:
            task_id = int(task_ref)
        except (TypeError, ValueError) as exc:
            raise ValueError("task_ref must be a numeric UTA repo task id") from exc
        if task_id <= 0:
            raise ValueError("task_ref must be a positive UTA repo task id")
        if int(after_id) < 0:
            raise ValueError("after_id must be nonnegative")
        return self.db.events_since(task_id, after_id=int(after_id), limit=int(limit))


def merge_phase_timings(state: Dict[str, Any], **updates: float) -> Dict[str, float]:
    """Accumulate node timing updates without mutating graph state."""
    timings = dict(state.get("phase_timings", {}) or {})
    for key, value in updates.items():
        timings[key] = timings.get(key, 0.0) + float(value)
    return timings


def set_stage(
    state: Dict[str, Any],
    stage: str,
    detail: Optional[str] = None,
    *,
    class_fqns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Record a stage transition and honor a production stop request."""
    message = f"[stage] {stage}"
    if detail:
        message += f" — {detail}"
    logger.info(message)
    task_id = state.get("task_id")
    if task_id:
        try:
            ports = task_ports_from_state(state)
            if ports is not None:
                stop_reason = ports.stop_reason(str(task_id)) if hasattr(ports, "stop_reason") else None
                if stop_reason:
                    if hasattr(ports, "mark_stopped"):
                        ports.mark_stopped(str(task_id), reason=stop_reason, stage=stage)
                    raise TaskStopRequested(stop_reason)
                stage_class_fqns: List[str] = []
                if class_fqns is None:
                    stage_class_fqns.extend(active_target_ids(state))
                else:
                    stage_class_fqns.extend(str(item) for item in class_fqns if item)
                prev_stage = state.get("current_stage")
                if prev_stage and prev_stage != stage and hasattr(ports, "record_stage_completed"):
                    ports.record_stage_completed(
                        str(task_id),
                        prev_stage,
                        {"class_fqns": list(dict.fromkeys(stage_class_fqns))},
                    )
                if hasattr(ports, "record_stage"):
                    ports.record_stage(
                        str(task_id),
                        stage,
                        {"detail": detail, "class_fqns": list(dict.fromkeys(stage_class_fqns))},
                    )
        except TaskStopRequested:
            raise
        except Exception:
            logger.debug("Failed to record production task stage", exc_info=True)
    return {"current_stage": stage}

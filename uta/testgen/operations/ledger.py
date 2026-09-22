"""Recording an operation outcome, and composing the halves that do it.

Write order is load-bearing and is not this split's to change: the ledger
claims an operation *before* the work, so a crash leaves a replayable
`running` row rather than finished work nothing knows about, and the
artifact is written before the row completes.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from agent_core.workflow import AgentTurnResult

from uta.testgen.operations.artifact_store import OperationArtifactStore
from uta.testgen.operations.models import (
    OperationResultEnvelope,
)
from uta.testgen.operations.reconciliation import OperationReconciliation


class WorkflowOperationLedger(OperationReconciliation):
    """Classify, persist and restore product-owned generation operations."""

    def __init__(
        self,
        db: Any,
        artifacts: OperationArtifactStore,
        *,
        fingerprint: Callable[[Mapping[str, Any], str, str], str],
        allowed_edit: Callable[[Mapping[str, Any], Mapping[str, Any]], bool],
        output_fingerprints: Callable[[Mapping[str, Any], str, str], Mapping[str, str]],
        max_crash_replays_per_operation: int = 1,
    ) -> None:
        self.db = db
        self.artifacts = artifacts
        self._fingerprint = fingerprint
        self._allowed_edit = allowed_edit
        self._output_fingerprints = output_fingerprints
        self._max_crash_replays = max(int(max_crash_replays_per_operation), 0)

    @property
    def cost_gate(self) -> Any:
        from uta.testgen.operations.cost_gate import WorkflowCostGate

        return WorkflowCostGate(self.db)


    def on_result(
        self,
        state: Mapping[str, Any],
        config: Mapping[str, Any],
        result: AgentTurnResult,
    ) -> None:
        """The neutral agent-core durability port for a normalized turn."""
        phase = str(config.get("label") or state.get("current_phase") or "")
        row = self._row_from_state(state, phase=phase, step="turn")
        resulting = self._fingerprint(state, phase, "turn")
        outputs = dict(self._output_fingerprints(state, phase, "turn"))
        envelope = OperationResultEnvelope(
            identity=self.identity_for_row(row),
            result_kind="turn",
            result=result.as_dict(),
            resulting_workspace_fingerprint=resulting,
            output_fingerprints=outputs,
            prerequisite_operation_ids=self._row_prerequisites(row),
        )
        stored = self.artifacts.write(envelope)
        if result.status == "cancelled":
            self.db.cancel_workflow_operation(
                row["operation_id"],
                error_kind="cancelled",
                error="cancelled",
                resulting_workspace_fingerprint=resulting,
                result_artifact_path=stored.relative_path,
                result_artifact_sha256=stored.sha256,
                output_fingerprints=outputs,
                session_id=_last_session_locator(result),
            )
            return
        self.db.complete_workflow_operation(
            row["operation_id"],
            resulting_workspace_fingerprint=resulting,
            result_artifact_path=stored.relative_path,
            result_artifact_sha256=stored.sha256,
            output_fingerprints=outputs,
            session_id=_last_session_locator(result),
        )

    def reject_result(
        self,
        state: Mapping[str, Any],
        config: Mapping[str, Any],
        result: AgentTurnResult,
        *,
        reason: str,
    ) -> None:
        """Keep rejection terminal without making its output reusable."""
        phase = str(config.get("label") or state.get("current_phase") or "")
        row = self._row_from_state(state, phase=phase, step="turn")
        self.db.fail_workflow_operation(
            str(row["operation_id"]), error_kind="guard_rejected", error=str(reason)
        )


    def on_cost(
        self,
        *,
        operation_id: str,
        paid_attempt_ordinal: int,
        provider_cost_usd: Optional[float],
    ) -> None:
        """Persist the only authoritative currency fact for a paid attempt."""
        self.db.record_workflow_operation_cost(
            operation_id,
            paid_attempt_ordinal=paid_attempt_ordinal,
            provider_cost_usd=provider_cost_usd,
        )


    def record_result(
        self,
        *,
        phase: str,
        step: str,
        state: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Persist an interpreted or deterministic phase before node return."""
        row = self._row_from_state(state, phase=phase, step=step)
        resulting = self._fingerprint(state, phase, step)
        outputs = dict(self._output_fingerprints(state, phase, step))
        envelope = OperationResultEnvelope(
            identity=self.identity_for_row(row),
            result_kind="phase",
            result=dict(result),
            resulting_workspace_fingerprint=resulting,
            output_fingerprints=outputs,
            prerequisite_operation_ids=self._row_prerequisites(row),
        )
        stored = self.artifacts.write(envelope)
        self.db.complete_workflow_operation(
            row["operation_id"],
            resulting_workspace_fingerprint=resulting,
            result_artifact_path=stored.relative_path,
            result_artifact_sha256=stored.sha256,
            output_fingerprints=outputs,
        )
        return {
            **dict(result),
            "operation_id": row["operation_id"],
            "operation_step": step,
            "execution_ordinal": row["execution_ordinal"],
        }


    def _complete_from_envelope(
        self,
        row: Mapping[str, Any],
        envelope: OperationResultEnvelope,
        relative_path: str,
        sha256: str,
    ) -> None:
        session_id = _last_session_locator_mapping(envelope.result)
        self.db.complete_workflow_operation(
            str(row["operation_id"]),
            resulting_workspace_fingerprint=envelope.resulting_workspace_fingerprint,
            result_artifact_path=relative_path,
            result_artifact_sha256=sha256,
            output_fingerprints=dict(envelope.output_fingerprints),
            session_id=session_id,
        )


    def _cancel_from_envelope(
        self,
        row: Mapping[str, Any],
        envelope: OperationResultEnvelope,
        relative_path: str,
        sha256: str,
    ) -> None:
        session_id = _last_session_locator_mapping(envelope.result)
        self.db.cancel_workflow_operation(
            str(row["operation_id"]),
            error_kind="cancelled",
            error="cancelled",
            resulting_workspace_fingerprint=envelope.resulting_workspace_fingerprint,
            result_artifact_path=relative_path,
            result_artifact_sha256=sha256,
            output_fingerprints=dict(envelope.output_fingerprints),
            session_id=session_id,
        )


__all__ = ["WorkflowOperationLedger"]


def _last_session_locator(result: AgentTurnResult) -> Optional[str]:
    return result.session_refs[-1].locator if result.session_refs else None


def _last_session_locator_mapping(result: Mapping[str, Any]) -> Optional[str]:
    refs = list(result.get("session_refs") or ())
    if refs and isinstance(refs[-1], Mapping) and refs[-1].get("locator"):
        return str(refs[-1]["locator"])
    legacy = result.get("session_id")
    return str(legacy) if legacy else None

"""Deciding what actually happened, when a run stopped without saying.

Separated from recording because the two answer different questions and
fail differently. Recording asks "write this down"; reconciliation asks
"is what is written down true?", and it re-reads the ledger and the
artifact chain to answer even for a checkpoint that claims completion --
a terminal snapshot proves where execution stopped, not that the product
write survived.

A mixin rather than a collaborator: every method here reads the same `db`
and `artifacts` the recording half writes, and handing it its own copies
would mean two objects disagreeing about one row.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Tuple


from uta.testgen.operations.models import (
    ENVELOPE_SCHEMA_VERSION,
    ArtifactValidationError,
    OperationIdentity,
    OperationResultEnvelope,
)


def _last_session_locator(result: Mapping[str, Any]) -> Optional[str]:
    refs = list(result.get("session_refs") or ())
    if refs and isinstance(refs[-1], Mapping) and refs[-1].get("locator"):
        return str(refs[-1]["locator"])
    legacy = result.get("session_id")
    return str(legacy) if legacy else None


class OperationReconciliation:
    """The reconciliation half of `WorkflowOperationLedger`."""

    def classify(
        self, *, phase: str, step: str, state: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Choose one of the seven explicit crash-reconciliation outcomes."""
        parts = self._state_parts(state, phase=phase, step=step)
        # Only the phase's *first* side-effecting step samples the outputs.
        # `interpret` has its own reconciliation node that runs after the turn
        # has already written, so letting it recompute would overwrite the
        # pre-turn record with the post-turn one -- and interpretation would
        # then compare the produced file against itself and conclude nothing
        # was produced.
        outputs_before = (
            {} if step == "interpret" else self._output_fingerprints(state, phase, step)
        )
        row = self.db.latest_workflow_operation(**parts["lookup"])
        current = self._fingerprint(state, phase, step)
        if row is None:
            identity = OperationIdentity(
                workflow_run_id=parts["workflow_run_id"],
                unit_id=parts["unit_id"],
                phase=phase,
                operation_step=step,
                logical_attempt=parts["attempt"],
                execution_ordinal=0,
                input_fingerprint=current,
            )
            self.db.start_workflow_operation(
                operation_id=identity.operation_id,
                repo_task_id=parts["repo_task_id"],
                workflow_run_id=identity.workflow_run_id,
                unit_id=identity.unit_id,
                phase=identity.phase,
                operation_step=identity.operation_step,
                attempt=identity.logical_attempt,
                execution_ordinal=identity.execution_ordinal,
                input_fingerprint=identity.input_fingerprint,
                prerequisite_operation_ids=parts["prerequisites"],
                schema_version=ENVELOPE_SCHEMA_VERSION,
            )
            return self._decision("run", identity, outputs_before=outputs_before)

        identity = self.identity_for_row(row)
        try:
            artifact = self._artifact_for_row(row)
            self._validate_prerequisites(row, state=state)
        except ArtifactValidationError:
            return self._decision(
                "fail_indeterminate", identity, outputs_before=outputs_before
            )

        status = str(row["status"])
        if status == "COMPLETED":
            if artifact is None:
                return self._decision(
                    "fail_indeterminate", identity, outputs_before=outputs_before
                )
            envelope, _, _ = artifact
            outputs_match = self._current_outputs_match(
                state=state, row=row, envelope=envelope
            )
            outcome = (
                "reuse_result"
                if (
                    current == envelope.resulting_workspace_fingerprint
                    and outputs_match
                )
                else "fail_unsafe"
            )
            return self._decision(outcome, identity, outputs_before=outputs_before)

        if status == "STARTED" and artifact is not None:
            envelope, relative_path, sha256 = artifact
            if current != envelope.resulting_workspace_fingerprint:
                return self._decision(
                    "fail_indeterminate", identity, outputs_before=outputs_before
                )
            if not self._current_outputs_match(state=state, row=row, envelope=envelope):
                return self._decision(
                    "fail_indeterminate", identity, outputs_before=outputs_before
                )
            if (
                envelope.result_kind == "turn"
                and envelope.result.get("status") == "cancelled"
            ):
                self._cancel_from_envelope(row, envelope, relative_path, sha256)
                row = self.db.get_workflow_operation(identity.operation_id)
            else:
                self._complete_from_envelope(row, envelope, relative_path, sha256)
                return self._decision(
                    "adopt_result", identity, outputs_before=outputs_before
                )

        if current != row["input_fingerprint"]:
            outcome = (
                "verify_existing_edit"
                if self._allowed_edit(state, row)
                else "fail_unsafe"
            )
            return self._decision(outcome, identity, outputs_before=outputs_before)

        if identity.execution_ordinal >= self._max_crash_replays:
            return self._decision(
                "fail_indeterminate", identity, outputs_before=outputs_before
            )

        successor = OperationIdentity(
            workflow_run_id=identity.workflow_run_id,
            unit_id=identity.unit_id,
            phase=identity.phase,
            operation_step=identity.operation_step,
            logical_attempt=identity.logical_attempt,
            execution_ordinal=identity.execution_ordinal + 1,
            input_fingerprint=current,
        )
        self.db.retry_workflow_operation(
            identity.operation_id,
            operation_id=successor.operation_id,
            repo_task_id=parts["repo_task_id"],
            workflow_run_id=successor.workflow_run_id,
            unit_id=successor.unit_id,
            phase=successor.phase,
            operation_step=successor.operation_step,
            attempt=successor.logical_attempt,
            execution_ordinal=successor.execution_ordinal,
            input_fingerprint=successor.input_fingerprint,
            prerequisite_operation_ids=parts["prerequisites"],
            schema_version=ENVELOPE_SCHEMA_VERSION,
        )
        return self._decision(
            "retry_no_effect", successor, outputs_before=outputs_before
        )


    def rehydrate(
        self, *, phase: str, step: str, state: Mapping[str, Any]
    ) -> Dict[str, Any]:
        row = self._row_from_state(state, phase=phase, step=step)
        artifact = self._artifact_for_row(row)
        if artifact is None:
            return {"rehydration": "unavailable"}
        envelope, _, _ = artifact
        if envelope.result_kind == "turn":
            result = dict(envelope.result)
            return {
                "rehydration": "ready",
                "turn_result": result,
                "turn_status": result.get("status", "failed"),
                "turn_text": result.get("text", ""),
                "turn_session_id": _last_session_locator(result),
                "turn_session_refs": list(result.get("session_refs") or []),
                "turn_usage": dict(result.get("usage") or {}),
                "turn_diagnostics": dict(result.get("diagnostics") or {}),
                "turn_retrospective": dict(result.get("retrospective") or {}),
                "turn_patch_count": int(result.get("patch_count") or 0),
                "turn_recovered": bool(result.get("recovered")),
                "turn_elapsed_seconds": float(result.get("elapsed_seconds") or 0),
                "turn_raw_log_path": result.get("raw_log_path"),
            }
        result = dict(envelope.result)
        if "phase_outcome" not in result and "outcome" in result:
            result["phase_outcome"] = result["outcome"]
        phase_results = dict(state.get("phase_results") or {})
        phase_results[phase] = dict(result)
        return {**result, "phase_results": phase_results, "rehydration": "ready"}


    def validate_terminal(self, state: Mapping[str, Any]) -> OperationResultEnvelope:
        """Validate product truth before an outer workflow commits a unit.

        A terminal LangGraph snapshot only proves where graph execution
        stopped. It does not prove the final product write survived. This
        check deliberately re-reads the ledger and artifact chain even for a
        ``reused_completed`` checkpoint.
        """
        row = self._row_from_state(
            state, phase="complete_generation", step="deterministic"
        )
        if row["status"] != "COMPLETED":
            raise ArtifactValidationError(
                "terminal generation operation is not completed"
            )
        artifact = self._artifact_for_row(row)
        if artifact is None:
            raise ArtifactValidationError(
                "terminal generation operation has no result artifact"
            )
        envelope, _, _ = artifact
        self._validate_prerequisites(row, state=state)
        current = self._fingerprint(state, "complete_generation", "deterministic")
        if current != envelope.resulting_workspace_fingerprint:
            raise ArtifactValidationError(
                "terminal generation workspace no longer matches its result"
            )
        if not self._current_outputs_match(state=state, row=row, envelope=envelope):
            raise ArtifactValidationError(
                "terminal generation outputs no longer match their result"
            )
        return envelope


    @staticmethod
    def identity_for_row(row: Mapping[str, Any]) -> OperationIdentity:
        return OperationIdentity(
            workflow_run_id=str(row["workflow_run_id"]),
            unit_id=str(row["unit_id"]),
            phase=str(row["phase"]),
            operation_step=str(row["operation_step"]),
            logical_attempt=int(row["attempt"]),
            execution_ordinal=int(row["execution_ordinal"]),
            input_fingerprint=str(row["input_fingerprint"]),
        )


    def _state_parts(
        self, state: Mapping[str, Any], *, phase: str, step: str
    ) -> Dict[str, Any]:
        try:
            repo_task_id = int(state["task_id"])
            workflow_run_id = str(state["workflow_run_id"])
            unit_id = str(state["unit_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactValidationError(
                "workflow operation state needs task_id, workflow_run_id and unit_id"
            ) from exc
        attempts = state.get("attempts_by_phase") or state.get("attempts") or {}
        attempt = int(attempts.get(phase, 0))
        prerequisites = [
            str(item) for item in state.get("prerequisite_operation_ids") or ()
        ]
        previous_id = state.get("operation_id")
        previous = (
            self.db.get_workflow_operation(str(previous_id)) if previous_id else None
        )
        if previous is not None:
            same_operation_slot = (
                str(previous["phase"]) == phase
                and str(previous["operation_step"]) == step
                and int(previous["attempt"]) == attempt
            )
            if not same_operation_slot and previous["status"] == "COMPLETED":
                prerequisites.append(str(previous["operation_id"]))
        prerequisites = tuple(dict.fromkeys(prerequisites))
        return {
            "repo_task_id": repo_task_id,
            "workflow_run_id": workflow_run_id,
            "unit_id": unit_id,
            "attempt": attempt,
            "prerequisites": prerequisites,
            "lookup": {
                "repo_task_id": repo_task_id,
                "workflow_run_id": workflow_run_id,
                "unit_id": unit_id,
                "phase": phase,
                "operation_step": step,
                "attempt": attempt,
            },
        }


    def _row_from_state(
        self, state: Mapping[str, Any], *, phase: str, step: str
    ) -> Mapping[str, Any]:
        operation_id = state.get("operation_id")
        row = (
            self.db.get_workflow_operation(str(operation_id)) if operation_id else None
        )
        if row is None:
            parts = self._state_parts(state, phase=phase, step=step)
            row = self.db.latest_workflow_operation(**parts["lookup"])
        if row is None or row["phase"] != phase or row["operation_step"] != step:
            raise ArtifactValidationError(
                f"no active {phase}/{step} workflow operation"
            )
        return row


    def _artifact_for_row(
        self, row: Mapping[str, Any]
    ) -> Optional[Tuple[OperationResultEnvelope, str, str]]:
        identity = self.identity_for_row(row)
        if row["result_artifact_path"] and row["result_artifact_sha256"]:
            envelope = self.artifacts.read(
                str(row["result_artifact_path"]),
                expected_sha256=str(row["result_artifact_sha256"]),
            )
            sha256 = str(row["result_artifact_sha256"])
            relative = str(row["result_artifact_path"])
        else:
            orphan = self.artifacts.read_for_identity(identity)
            if orphan is None:
                return None
            envelope, sha256 = orphan
            relative = str(
                self.artifacts.path_for(identity).relative_to(self.artifacts.root)
            )
        if envelope.identity != identity:
            raise ArtifactValidationError(
                "operation artifact identity conflicts with ledger"
            )
        if envelope.prerequisite_operation_ids != self._row_prerequisites(row):
            raise ArtifactValidationError(
                "operation artifact prerequisites conflict with ledger"
            )
        expected_kind = "turn" if row["operation_step"] == "turn" else "phase"
        if envelope.result_kind != expected_kind:
            raise ArtifactValidationError(
                "operation artifact result kind conflicts with ledger"
            )
        return envelope, relative, sha256


    def _validate_prerequisites(
        self,
        row: Mapping[str, Any],
        *,
        state: Mapping[str, Any],
        seen: Optional[set[str]] = None,
    ) -> None:
        """Require the complete, valid artifact chain before any reuse.

        Prerequisite IDs live in JSON rather than a foreign-key table because
        the graph can have more than one predecessor. Validation therefore has
        to be explicit: a syntactically valid child artifact must not outlive
        or cross-link to stale evidence from another task, run or unit.
        """
        operation_id = str(row["operation_id"])
        ancestors = set(seen or ())
        if operation_id in ancestors:
            raise ArtifactValidationError(
                "workflow operation prerequisites contain a cycle"
            )
        ancestors.add(operation_id)

        for prerequisite_id in self._row_prerequisites(row):
            if prerequisite_id in ancestors:
                raise ArtifactValidationError(
                    "workflow operation prerequisites contain a cycle"
                )
            prerequisite = self.db.get_workflow_operation(prerequisite_id)
            if prerequisite is None or prerequisite["status"] != "COMPLETED":
                raise ArtifactValidationError(
                    f"workflow prerequisite is not complete: {prerequisite_id}"
                )
            for field in ("repo_task_id", "workflow_run_id", "unit_id"):
                if prerequisite[field] != row[field]:
                    raise ArtifactValidationError(
                        f"workflow prerequisite crosses {field}: {prerequisite_id}"
                    )
            artifact = self._artifact_for_row(prerequisite)
            if artifact is None:
                raise ArtifactValidationError(
                    f"workflow prerequisite has no result: {prerequisite_id}"
                )
            # Deliberately *not* comparing the prerequisite's recorded outputs
            # against the current workspace. A prerequisite ran earlier by
            # definition, and later phases are supposed to move what it
            # observed: `precheck_existing_tests` records the target's test
            # file while it does not yet exist, and `generate_tests` then
            # writes it. Re-checking that here rejected runs that had worked
            # perfectly, and quarantined the task.
            #
            # Chain integrity is still enforced above -- the artifact exists,
            # its hash matches, it is COMPLETED, and it belongs to this
            # task/run/unit -- and every operation's own outputs are still
            # validated when it is the row under validation.
            self._validate_prerequisites(
                prerequisite,
                state=state,
                seen=ancestors,
            )


    def _current_outputs_match(
        self,
        *,
        state: Mapping[str, Any],
        row: Mapping[str, Any],
        envelope: OperationResultEnvelope,
    ) -> bool:
        current = self._output_fingerprints(
            state,
            str(row["phase"]),
            str(row["operation_step"]),
        )
        return dict(current) == dict(envelope.output_fingerprints)


    @staticmethod
    def _row_prerequisites(row: Mapping[str, Any]) -> Tuple[str, ...]:
        try:
            value = json.loads(str(row["prerequisite_operation_ids_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError(
                "ledger prerequisites are not valid JSON"
            ) from exc
        if not isinstance(value, list):
            raise ArtifactValidationError("ledger prerequisites must be a list")
        return tuple(str(item) for item in value)


    @staticmethod
    def _decision(
        outcome: str,
        identity: OperationIdentity,
        *,
        outputs_before: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        return {
            "outcome": outcome,
            # What the phase's declared outputs looked like *before* it ran.
            # Every backend already declares `output_paths`, so this is the
            # language-neutral way to answer "did this phase produce anything?"
            # -- sampling after the fact cannot distinguish the agent's own new
            # file from one that was always there.
            **(
                {"output_fingerprints_before": dict(outputs_before)}
                if outputs_before
                else {}
            ),
            "operation_id": identity.operation_id,
            "operation_step": identity.operation_step,
            "operation_input_fingerprint": identity.input_fingerprint,
            "logical_attempt": identity.logical_attempt,
            "execution_ordinal": identity.execution_ordinal,
        }


__all__ = ["OperationReconciliation"]

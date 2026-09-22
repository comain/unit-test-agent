"""What a finished turn is worth telling an operator.

The public progress stream cannot answer "why did that fail". Agent-core
projects every provider update through a fixed table -- an error becomes
"Agent needs attention" and nothing else -- because `TurnProgress.message` is
trusted-log material that may quote model text, a command, or a credential.
That projection is right, and copying the provider's message past it would
defeat it. But it leaves an operator with an ERROR row that names no phase
outcome, no session, and no log, which is the row production actually showed
for a failed `fix_mutation` turn.

So the precise account comes from the other side: what UTA and agent-core
already know about their own run. `AgentTurnResult.diagnostics` is itself a
vetted classification -- `turn_type`, `node_status`, `fallback_reason` (a bare
token or the literal "unclassified"), `model_attempts` -- and the result also
carries the session refs and the raw log path. None of that is provider prose,
and together it names the failure and points at the evidence.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from uta.testgen.ports.registry import task_ports_from_state
from uta.testgen.turn_accounting import session_refs

#: Diagnostics agent-core has already vetted for a person to read. Anything
#: outside this set stays in the turn artifact: the whole point of this event
#: is that it cannot become a channel for provider text.
VETTED_DIAGNOSTICS = (
    "turn_type",
    "node_status",
    "fallback_eligible",
    "fallback_reason",
    "model_attempts",
    "error",
)


def turn_outcome_event(phase: str, turn: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The event a finished turn deserves, or None when it went cleanly.

    A turn that completed first try says nothing an operator needs; one that
    failed, or only got there after falling back to another provider, says
    quite a lot.
    """
    status = str(turn.get("status") or "unknown")
    diagnostics = {
        key: value
        for key, value in dict(turn.get("diagnostics") or {}).items()
        if key in VETTED_DIAGNOSTICS
    }
    recovered = bool(turn.get("recovered"))
    fallback = str(diagnostics.get("fallback_reason") or "")
    model_attempts = diagnostics.get("model_attempts")
    attempt_fallback = ""
    if isinstance(model_attempts, list):
        for attempt in model_attempts:
            if isinstance(attempt, Mapping) and attempt.get("fallback_reason"):
                attempt_fallback = str(attempt["fallback_reason"])
                break
    if status == "completed" and not recovered and not fallback and not attempt_fallback:
        return None

    # The phase is already the event's stage, so the message spends its words
    # on what the stage cannot say.
    reason = fallback or attempt_fallback or str(diagnostics.get("turn_type") or "") or status
    if status == "completed":
        message = f"Turn completed after recovery ({reason})"
        severity = "WARNING"
    else:
        message = f"Turn {status} ({reason})"
        severity = "ERROR"
    payload = {
        "phase": phase,
        "status": status,
        "recovered": recovered,
        "attempts": int(turn.get("attempts") or 0),
        "patchCount": int(turn.get("patch_count") or 0),
        "elapsedSeconds": float(turn.get("elapsed_seconds") or 0.0),
        # Where the untruncated account lives. The raw log holds provider text,
        # so it is named here and never inlined.
        "rawLogPath": turn.get("raw_log_path") or "",
        "sessionRefs": session_refs(turn),
        **diagnostics,
    }
    return {
        "message": message,
        "stage": phase,
        "severity": severity,
        "payload": {key: value for key, value in payload.items() if value not in (None, "", [], {})},
    }


def record_turn_outcome(
    state: Mapping[str, Any], *, phase: str, turn: Optional[Mapping[str, Any]]
) -> None:
    """Write the turn's own account of itself to the task event log."""
    if not isinstance(turn, Mapping) or not state.get("task_id"):
        return
    event = turn_outcome_event(phase, turn)
    if event is None:
        return
    ports = task_ports_from_state(state)
    if ports is None or not hasattr(ports, "add_event"):
        return
    ports.add_event(str(state["task_id"]), "turn_outcome", event)


__all__ = ["VETTED_DIAGNOSTICS", "record_turn_outcome", "turn_outcome_event"]

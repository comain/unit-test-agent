"""Typed agent-turn composition at the UTA workflow boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from agent_core.harness import (
    AgentTurnContext,
    HarnessBinding,
    TurnCost,
    open_harness_session,
)
from agent_core.runtime import SinkProgressPort


@dataclass
class TurnInvocationScope:
    """Invocation data needed by product ports but never checkpointed."""

    state: dict[str, Any] = field(default_factory=dict)

    def bind(self, state: Mapping[str, Any]) -> None:
        self.state = dict(state)

    def for_request(self, request: Any) -> dict[str, Any]:
        return {
            **self.state,
            "operation_id": request.operation_id,
            "current_phase": request.name,
        }


class CallableCancellation:
    def __init__(self, check: Callable[[], bool]) -> None:
        self._check = check

    def is_cancelled(self) -> bool:
        return bool(self._check())


class HarnessSessions:
    def open_session(self, *, harness: Any, repo_path: Path, model_id: Optional[str], effort_strategy: str = "default"):
        options = {"model_id": model_id} if model_id is not None else {}
        if effort_strategy != "default":
            options["effort_strategy"] = effort_strategy
        return open_harness_session(harness, repo_path=repo_path, **options)


class UtaTurnGuard:
    def __init__(self, scope: TurnInvocationScope, before: Any, after: Any) -> None:
        self._scope = scope
        self._before = before
        self._after = after

    def before(self, request: Any) -> Any:
        if not callable(self._before):
            return None
        return self._before(
            self._scope.for_request(request), {"label": request.name}
        )

    def after(self, request: Any, token: Any) -> None:
        if callable(self._after):
            self._after(
                self._scope.for_request(request), {"label": request.name}, token
            )


class UtaTurnCost:
    def __init__(self, ledger: Any, gate: Any) -> None:
        self._ledger = ledger
        self._gate = gate

    def before_paid_attempt(
        self, *, operation_id: str, paid_attempt_ordinal: int
    ) -> None:
        if self._gate is not None:
            self._gate.allow_attempt(
                operation_id=operation_id,
                paid_attempt_ordinal=paid_attempt_ordinal,
            )

    def after_paid_attempt(
        self,
        *,
        operation_id: str,
        paid_attempt_ordinal: int,
        cost: TurnCost,
    ) -> None:
        self._ledger.on_cost(
            operation_id=operation_id,
            paid_attempt_ordinal=paid_attempt_ordinal,
            provider_cost_usd=cost.provider_cost_usd,
        )


class UtaTurnResults:
    def __init__(self, ledger: Any, scope: TurnInvocationScope) -> None:
        self._ledger = ledger
        self._scope = scope

    def commit(self, request: Any, execution: Any) -> None:
        self._ledger.on_result(
            self._scope.for_request(request),
            {"label": request.name},
            execution.result,
        )

    def reject(self, request: Any, execution: Any, *, reason: str) -> None:
        self._ledger.reject_result(
            self._scope.for_request(request),
            {"label": request.name},
            execution.result,
            reason=reason,
        )


def compose_turn_context(
    *,
    harness_name: str,
    runner: Any,
    ledger: Any,
    scope: TurnInvocationScope,
    context: Mapping[str, Any],
) -> AgentTurnContext:
    """Validate and assemble the live ports shared by Java and Python."""
    if not harness_name:
        raise ValueError("generation workflow requires a configured harness name")
    cancellation = context.get("is_cancelled")
    progress_sink = context.get("progress_sink")
    return AgentTurnContext(
        binding=HarnessBinding(name=harness_name, harness=runner),
        cost=UtaTurnCost(
            ledger, context.get("cost_gate") or getattr(ledger, "cost_gate", None)
        ),
        cancellation=(
            CallableCancellation(cancellation) if callable(cancellation) else None
        ),
        sessions=HarnessSessions(),
        guard=UtaTurnGuard(
            scope, context.get("before_turn"), context.get("after_turn")
        ),
        progress=SinkProgressPort(progress_sink),
        results=UtaTurnResults(ledger, scope),
    )


__all__ = ["TurnInvocationScope", "compose_turn_context"]

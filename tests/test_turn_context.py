from pathlib import Path
from types import SimpleNamespace

from agent_core.harness import AgentTurnRequest, TurnCost

from uta.testgen.graph.turn_context import (
    TurnInvocationScope,
    UtaTurnCost,
    UtaTurnResults,
)


class Ledger:
    def __init__(self):
        self.costs = []
        self.results = []
        self.rejections = []

    def on_cost(self, **kwargs):
        self.costs.append(kwargs)

    def on_result(self, state, config, result):
        self.results.append((state, config, result))

    def reject_result(self, state, config, result, *, reason):
        self.rejections.append((state, config, result, reason))


class Gate:
    def __init__(self):
        self.attempts = []

    def allow_attempt(self, **kwargs):
        self.attempts.append(kwargs)


def request():
    return AgentTurnRequest(
        operation_id="operation-1",
        name="generate_tests",
        prompt=lambda _attempt, _error: Path("prompt.md"),
        repo_path=Path("."),
    )


def test_turn_cost_adapter_preserves_known_zero_and_unavailable_attempts():
    ledger = Ledger()
    gate = Gate()
    adapter = UtaTurnCost(ledger, gate)

    for ordinal, amount in enumerate((1.25, 0.0, None)):
        adapter.before_paid_attempt(
            operation_id="operation-1", paid_attempt_ordinal=ordinal
        )
        adapter.after_paid_attempt(
            operation_id="operation-1",
            paid_attempt_ordinal=ordinal,
            cost=TurnCost(provider_cost_usd=amount),
        )

    assert [item["paid_attempt_ordinal"] for item in gate.attempts] == [0, 1, 2]
    assert [item["provider_cost_usd"] for item in ledger.costs] == [1.25, 0.0, None]


def test_result_adapter_binds_operation_identity_for_accept_and_guard_rejection():
    ledger = Ledger()
    scope = TurnInvocationScope({"repo_task_id": 7})
    adapter = UtaTurnResults(ledger, scope)
    execution = SimpleNamespace(result=SimpleNamespace(status="completed"))

    adapter.commit(request(), execution)
    adapter.reject(request(), execution, reason="workspace_guard_rejected")

    accepted_state, accepted_config, _ = ledger.results[0]
    rejected_state, rejected_config, _, reason = ledger.rejections[0]
    assert accepted_state["operation_id"] == "operation-1"
    assert rejected_state["operation_id"] == "operation-1"
    assert accepted_config == rejected_config == {"label": "generate_tests"}
    assert reason == "workspace_guard_rejected"

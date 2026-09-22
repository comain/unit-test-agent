"""Building and running the generation cycle as a real graph.

B2 proved the topology is well-formed on paper. This proves it executes: that
every node name and selector the YAML mentions actually resolves, that a run
walks from precheck to completion, and — the part worth the most — that a
resumed run does *not* re-enter the model.

Everything the cycle needs from the outside is a port supplied through
`context`: the language backend, the operation ledger, and the harness. That
is what lets this file drive a full cycle with a scripted backend instead of
Maven, PIT and a live provider.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from uta.testgen.graph.cycle import (
    GenerationPaused,
    build_cycle_workflow,
    cycle_registry,
    generation_prompt,
    generation_interpret,
    generation_operation,
    reconcile_generation_operation,
    resolved_registry,
)
from uta.testgen.graph.generation_cycle import generation_cycle_spec
from uta.testgen.prompts import PromptArtifactScope, open_prompt_artifact_scope
from uta.testgen.prompts.artifacts import PromptArtifactScopeError


# -- scripted ports ----------------------------------------------------------

class ScriptedLedger:
    """Classifies every reconciliation. Defaults to "just run it"."""

    def __init__(self, outcomes=None, rehydrated=None):
        self.outcomes = dict(outcomes or {})
        self.rehydrated = rehydrated or {}
        self.classified: list = []

    def classify(self, *, phase, step, state):
        self.classified.append((phase, step))
        outcome = self.outcomes.get((phase, step), self.outcomes.get(phase, "run"))
        return {
            "outcome": outcome,
            "operation_id": f"{phase}-{step}-operation",
            "logical_attempt": 0,
            "execution_ordinal": 0,
        }

    def rehydrate(self, *, phase, step, state):
        return dict(self.rehydrated.get(phase, {"rehydration": "ready"}))


class ScriptedBackend:
    """A language backend that answers instantly and records what it was asked."""

    language = "scripted"

    def __init__(self, outcomes=None):
        self.outcomes = dict(outcomes or {})
        self.prompted: list = []
        self.interpreted: list = []
        self.ran: list = []

    #: Each phase speaks its own verdict vocabulary. `precheck_existing_tests`
    #: says `proceed`, not `passed` -- a fixture that answers "passed"
    #: everywhere falls through every branch default and completes the cycle
    #: without ever reaching the model, while still looking like a clean run.
    DEFAULT_OUTCOME = {"precheck_existing_tests": "proceed"}

    def _outcome(self, phase, default=None):
        default = default or self.DEFAULT_OUTCOME.get(phase, "passed")
        value = self.outcomes.get(phase, default)
        if isinstance(value, list):
            return value.pop(0) if value else default
        return value

    # deterministic phases
    def run_phase(self, phase, state):
        self.ran.append(phase)
        return {"phase_outcome": self._outcome(phase)}

    # model phases
    def render_prompt(self, phase, state):
        self.prompted.append(phase)
        return f"do the {phase} work"

    def interpret(self, phase, state, turn):
        self.interpreted.append(phase)
        return {"phase_outcome": self._outcome(phase)}


class ScriptedTurn:
    def __init__(self, type="completed"):
        self.type = type
        self.result = "done"
        self.session_id = "sess-1"


class ScriptedRunner:
    def __init__(self, turn=None):
        self.turn = turn or ScriptedTurn()
        self.calls = 0

    def run_turn(self, **kwargs):
        self.calls += 1
        return self.turn

    # a phase session is a runner with a lifecycle
    session_id = "sess-1"

    def snapshot(self):
        from agent_core.harness.sessions import SessionSnapshot

        return SessionSnapshot(session_id="sess-1")

    def close(self):
        pass


def context_for(backend=None, ledger=None, runner=None, **extra):
    from agent_core.harness import AgentTurnContext, HarnessBinding

    runner = runner or ScriptedRunner()
    class Cost:
        def before_paid_attempt(self, **kwargs):
            pass

        def after_paid_attempt(self, **kwargs):
            pass

    class Sessions:
        def open_session(self, **kwargs):
            return runner

    context = {
        "backend": backend or ScriptedBackend(),
        "ledger": ledger or ScriptedLedger(),
        "runner": runner,
        **extra,
    }
    context["turn_context"] = AgentTurnContext(
        binding=HarnessBinding(name="scripted", harness=runner),
        cost=Cost(),
        sessions=Sessions(),
    )
    return context


def initial_state(tmp_path, *, workflow_run_id="run-1"):
    return {
        "repo_path": str(tmp_path),
        "unit_id": "Target",
        "task_id": 1,
        "workflow_run_id": workflow_run_id,
        "operation_id": "scripted-operation",
        "language": "scripted",
        "batch": ["Target"],
    }


def run_cycle(tmp_path, context, *, recursion_limit=200):
    root = tmp_path / "prompt-state"
    root.mkdir(mode=0o700, exist_ok=True)
    scope = PromptArtifactScope(
        root=root,
        run_id="run-1",
        task_id=1,
        managed=True,
    )
    graph = build_cycle_workflow(
        context={**context, "prompt_artifact_scope": scope}
    )
    return graph.with_config(recursion_limit=recursion_limit).invoke(
        initial_state(tmp_path)
    )


# -- the registry covers the topology ----------------------------------------

def test_every_node_the_topology_names_is_registered():
    """A missing implementation is otherwise found at graph build time, in
    whichever product happens to build it first."""
    spec = generation_cycle_spec()
    missing = sorted({n.uses for n in spec.nodes} - set(resolved_registry().nodes))

    assert missing == []


def test_every_selector_the_topology_names_is_registered():
    spec = generation_cycle_spec()
    missing = sorted({b.selector for b in spec.branches} - set(resolved_registry().selectors))

    assert missing == []


def test_the_shared_agent_turn_is_the_one_from_agent_core():
    """UTA must not grow its own copy of the turn node.

    `agent_turn` resolves through the shared registry, not UTA's -- which is
    the point of extending rather than registering into it.
    """
    from agent_core.workflow.registry import shared_registry

    assert "agent_turn" not in cycle_registry.nodes
    assert resolved_registry().nodes["agent_turn"] is shared_registry.nodes["agent_turn"]


def test_the_graph_compiles():
    assert build_cycle_workflow(context=context_for()) is not None


def test_reconciliation_projects_a_production_ledger_decision_into_state():
    class Ledger:
        def classify(self, **kwargs):
            return {
                "outcome": "run",
                "operation_id": "op-1",
                "operation_step": "turn",
                "logical_attempt": 2,
                "execution_ordinal": 1,
            }

    projected = reconcile_generation_operation(
        {}, {"phase": "generate_tests", "step": "turn"}, {"ledger": Ledger()}
    )

    assert projected == {
        "reconciliation": "run",
        "current_phase": "generate_tests",
        "operation_id": "op-1",
        "operation_step": "turn",
        "logical_attempt": 2,
        "execution_ordinal": 1,
    }


def test_generation_prompt_materializes_owner_only_prompt_pair(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    state = {
        "repo_path": str(repo),
        "task_id": 17,
        "workflow_run_id": "run-17",
        "unit_id": "unit-2",
        "operation_id": "operation-9",
        "language": "java",
        "batch": ["com.example.Service"],
        "logical_attempt": 2,
        "execution_ordinal": 1,
        "repair_feedback": "Use the distinguishing input, not another happy-path case.",
    }
    backend = ScriptedBackend()

    with open_prompt_artifact_scope(
        repo_path=repo,
        task_id=17,
        workflow_run_id="run-17",
    ) as scope:
        result = generation_prompt(
            state,
            {"phase": "generate_tests"},
            {"backend": backend, "prompt_artifact_scope": scope},
        )

    prompt = Path(result["prompt_file"])
    assert state['repair_feedback'] in prompt.read_text()
    inputs = Path(result["prompt_inputs_file"])
    manifest = Path(result["prompt_manifest_file"])
    expected = (
        runner_home
        / "workflow-state"
        / "prompts"
        / "managed"
        / "17"
        / "run-17"
        / "unit-2"
        / "operation-9"
    ).resolve()
    assert prompt.parent == expected
    assert inputs.parent == expected
    assert manifest.parent == expected
    assert prompt.read_text(encoding="utf-8") == (
        "do the generate_tests work\n\nCorrective repair feedback:\n" + state['repair_feedback']
    )
    assert stat.S_IMODE(prompt.stat().st_mode) == 0o600
    assert stat.S_IMODE(inputs.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert json.loads(inputs.read_text(encoding="utf-8")) == {
        "batch": ["com.example.Service"],
        "engine": "durable_v2",
        "execution_ordinal": 1,
        "language": "java",
        "logical_attempt": 2,
        "operation_id": "operation-9",
        "phase": "generate_tests",
        "product": "uta",
        "schema_version": 1,
        "session_id": None,
        "task_id": 17,
        "unit_id": "unit-2",
        "workflow_run_id": "run-17",
    }
    assert not (repo / ".uta_cache" / "cycle-prompts").exists()


def test_generation_prompt_repairs_an_incomplete_pair(tmp_path):
    root = tmp_path / "prompt-state"
    root.mkdir(mode=0o700)
    scope = PromptArtifactScope(
        root=root, run_id="run-1", task_id=1, managed=True
    )
    state = initial_state(tmp_path)
    context = {
        "backend": ScriptedBackend(),
        "prompt_artifact_scope": scope,
    }

    first = generation_prompt(state, {"phase": "plan_tests"}, context)
    Path(first["prompt_inputs_file"]).unlink()
    repaired = generation_prompt(state, {"phase": "plan_tests"}, context)

    assert Path(repaired["prompt_file"]).is_file()
    assert Path(repaired["prompt_inputs_file"]).is_file()
    assert Path(repaired["prompt_manifest_file"]).is_file()


def test_generation_prompt_refuses_a_different_render_after_manifest(tmp_path):
    root = tmp_path / "prompt-state"
    root.mkdir(mode=0o700)
    scope = PromptArtifactScope(
        root=root, run_id="run-1", task_id=1, managed=True
    )
    state = initial_state(tmp_path)
    backend = ScriptedBackend()
    context = {"backend": backend, "prompt_artifact_scope": scope}
    generation_prompt(state, {"phase": "plan_tests"}, context)
    backend.render_prompt = lambda phase, state: "a different prompt"

    with pytest.raises(PromptArtifactScopeError, match="different content"):
        generation_prompt(state, {"phase": "plan_tests"}, context)


def test_generation_prompt_rejects_a_scope_from_another_lineage(tmp_path):
    root = tmp_path / "prompt-state"
    root.mkdir(mode=0o700)
    wrong_scope = PromptArtifactScope(
        root=root, run_id="another-run", task_id=1, managed=True
    )

    with pytest.raises(PromptArtifactScopeError, match="checkpointed task lineage"):
        generation_prompt(
            initial_state(tmp_path),
            {"phase": "plan_tests"},
            {
                "backend": ScriptedBackend(),
                "prompt_artifact_scope": wrong_scope,
            },
        )


def test_invalid_prompt_identity_opens_no_agent_session(tmp_path):
    class InvalidTurnLedger(ScriptedLedger):
        def classify(self, *, phase, step, state):
            if phase == "plan_tests" and step == "turn":
                return {
                    "outcome": "run",
                    "operation_id": "../outside",
                    "logical_attempt": 0,
                    "execution_ordinal": 0,
                }
            return super().classify(phase=phase, step=step, state=state)

    opened = []
    context = context_for(
        ledger=InvalidTurnLedger(),
        open_session=lambda **_kwargs: opened.append(True),
    )

    with pytest.raises(PromptArtifactScopeError):
        run_cycle(tmp_path, context)

    assert opened == []


def test_deterministic_and_interpret_results_are_durable_before_return():
    class Ledger:
        def __init__(self):
            self.calls = []

        def record_result(self, **kwargs):
            self.calls.append(kwargs)
            return {**kwargs["result"], "operation_id": f"{kwargs['step']}-op"}

    ledger = Ledger()
    backend = ScriptedBackend()
    deterministic = generation_operation(
        {}, {"phase": "verify_compile"}, {"backend": backend, "ledger": ledger}
    )
    interpreted = generation_interpret(
        {"turn_result": {"text": "done"}},
        {"phase": "generate_tests"},
        {"backend": backend, "ledger": ledger},
    )

    assert deterministic["operation_id"] == "deterministic-op"
    assert interpreted["operation_id"] == "interpret-op"
    assert [(call["phase"], call["step"]) for call in ledger.calls] == [
        ("verify_compile", "deterministic"),
        ("generate_tests", "interpret"),
    ]


# -- a whole cycle -----------------------------------------------------------

def test_a_clean_run_reaches_completion(tmp_path):
    backend = ScriptedBackend()

    state = run_cycle(tmp_path, context_for(backend))

    assert state["phase_outcome"] == "passed"
    assert "complete_generation" in backend.ran


def test_a_clean_run_visits_every_phase_in_order(tmp_path):
    backend = ScriptedBackend()

    run_cycle(tmp_path, context_for(backend))

    assert backend.ran[0] == "precheck_existing_tests"
    assert backend.prompted == [
        "plan_tests", "generate_tests",
    ], "a clean run should need no repair"
    assert backend.ran == [
        "precheck_existing_tests", "verify_compile", "verify_tests",
        "measure_coverage", "measure_mutation",
        "delegated_quality_gate_verify", "complete_generation",
    ]


def test_an_existing_good_test_suite_skips_the_whole_cycle(tmp_path):
    """The most expensive possible no-op is generating tests for a target that
    already has good ones."""
    backend = ScriptedBackend({"precheck_existing_tests": "skip_target"})

    run_cycle(tmp_path, context_for(backend))

    assert backend.prompted == [], "the model was asked to work on a skipped target"


def test_a_compile_failure_routes_through_repair_and_back(tmp_path):
    backend = ScriptedBackend({
        "verify_compile": ["repair", "passed"],
        "fix_compile": "passed",
    })

    run_cycle(tmp_path, context_for(backend))

    assert "fix_compile" in backend.prompted
    assert backend.ran.count("verify_compile") == 2, "a repair must be re-verified"


def test_a_repair_that_never_works_still_terminates(tmp_path):
    """The attempt budget ends the loop. If it did not, this test hangs — which
    is exactly how an unbounded repair loop shows up in production."""
    backend = ScriptedBackend({
        "verify_compile": "repair",
        "fix_compile": ["retry", "retry", "exhausted"],
    })

    run_cycle(tmp_path, context_for(backend))

    assert "complete_generation" in backend.ran
    assert backend.prompted.count("fix_compile") == 3


# -- reconciliation ----------------------------------------------------------

def test_a_reusable_result_never_re_enters_the_model(tmp_path):
    """The reason reconciliation exists. Re-running the turn would produce the
    same answer at full price, and nothing would report it."""
    ledger = ScriptedLedger({("plan_tests", "turn"): "reuse_result"})
    backend = ScriptedBackend()

    run_cycle(tmp_path, context_for(backend, ledger))

    assert "plan_tests" not in backend.prompted


def test_an_adopted_result_never_re_enters_the_model(tmp_path):
    ledger = ScriptedLedger({("generate_tests", "turn"): "adopt_result"})
    backend = ScriptedBackend()

    run_cycle(tmp_path, context_for(backend, ledger))

    assert "generate_tests" not in backend.prompted


def test_a_retry_with_no_observable_effect_does_run_again(tmp_path):
    """Unlike reuse: nothing was produced, so the work has to happen."""
    ledger = ScriptedLedger({("plan_tests", "turn"): "retry_no_effect"})
    backend = ScriptedBackend()

    run_cycle(tmp_path, context_for(backend, ledger))

    assert "plan_tests" in backend.prompted


def test_an_unsafe_workspace_stops_the_cycle(tmp_path):
    ledger = ScriptedLedger({("plan_tests", "turn"): "fail_unsafe"})
    backend = ScriptedBackend()

    state = run_cycle(tmp_path, context_for(backend, ledger))

    assert "plan_tests" not in backend.prompted
    assert "complete_generation" not in backend.ran
    assert state["stopped_early"] is True
    assert state["terminal_reason"] == "fail_unsafe"


def test_an_unclassifiable_operation_stops_the_cycle(tmp_path):
    ledger = ScriptedLedger({("generate_tests", "turn"): "fail_indeterminate"})
    backend = ScriptedBackend()

    run_cycle(tmp_path, context_for(backend, ledger))

    assert "generate_tests" not in backend.prompted


def test_the_ledger_is_consulted_before_every_side_effect(tmp_path):
    ledger = ScriptedLedger()

    run_cycle(tmp_path, context_for(ledger=ledger))

    assert ("plan_tests", "turn") in ledger.classified
    assert ("plan_tests", "interpret") in ledger.classified
    assert ("verify_compile", "deterministic") in ledger.classified


# -- cancellation ------------------------------------------------------------

def test_a_cancelled_turn_pauses_rather_than_completing(tmp_path):
    """Completing would record a result for work that was deliberately
    stopped, and the checkpoint would then say the phase was done."""
    runner = ScriptedRunner(ScriptedTurn(type="cancelled"))
    backend = ScriptedBackend()

    with pytest.raises(GenerationPaused):
        run_cycle(tmp_path, context_for(backend, runner=runner))

    assert "complete_generation" not in backend.ran
    assert backend.interpreted == [], "a cancelled turn has no answer to interpret"


# -- durability --------------------------------------------------------------

def test_a_completed_cycle_is_not_run_again_on_resume(tmp_path):
    """The reason invocation goes through `invoke_workflow` rather than calling
    the graph directly: supplying the same thread id is *not* enough to resume.
    Invoking a finished lineage with a state mapping re-enters it, and the run
    pays for every model turn a second time while looking perfectly healthy.
    """
    from agent_core.workflow import WorkflowRunIdentity, open_checkpointer

    from uta.testgen.graph.cycle import run_cycle as durable_run_cycle

    identity = WorkflowRunIdentity(
        product="uta", task_id="t1", unit_id="Target",
        workflow_run_id="run-1", cycle="generation", version="v1",
    )
    backend = ScriptedBackend()
    root = tmp_path / "prompt-state"
    root.mkdir(mode=0o700)
    scope = PromptArtifactScope(root=root, run_id="run-1", task_id=1, managed=True)
    context = {**context_for(backend), "prompt_artifact_scope": scope}

    checkpoint_root = tmp_path / "workflow-state"
    checkpoint_root.mkdir(mode=0o700)
    with open_checkpointer(
        checkpoint_root / "checkpoints.sqlite",
        forbidden_roots=(tmp_path / "repo",),
    ) as checkpointer:
        first = durable_run_cycle(
            context=context, identity=identity, checkpointer=checkpointer,
            initial_state=initial_state(tmp_path),
        )
        prompted_after_first = list(backend.prompted)

        second = durable_run_cycle(
            context=context, identity=identity, checkpointer=checkpointer,
            initial_state=initial_state(tmp_path),
        )

    assert first.disposition == "started"
    assert second.disposition == "reused_completed"
    assert backend.prompted == prompted_after_first, "the model was paid for twice"


def test_a_different_run_id_is_a_different_lineage(tmp_path):
    """Reruns must be possible. A new `workflow_run_id` is the deliberate act
    that gets one, rather than a resumed lineage quietly starting over."""
    from agent_core.workflow import WorkflowRunIdentity, open_checkpointer

    from uta.testgen.graph.cycle import run_cycle as durable_run_cycle

    backend = ScriptedBackend()

    def identity_for(run_id):
        return WorkflowRunIdentity(
            product="uta", task_id="t1", unit_id="Target",
            workflow_run_id=run_id, cycle="generation", version="v1",
        )

    checkpoint_root = tmp_path / "workflow-state"
    checkpoint_root.mkdir(mode=0o700)
    with open_checkpointer(
        checkpoint_root / "checkpoints.sqlite",
        forbidden_roots=(tmp_path / "repo",),
    ) as checkpointer:
        first_root = tmp_path / "prompts-run-1"
        first_root.mkdir(mode=0o700)
        first_context = {
            **context_for(backend),
            "prompt_artifact_scope": PromptArtifactScope(
                root=first_root, run_id="run-1", task_id=1, managed=True
            ),
        }
        durable_run_cycle(context=first_context, identity=identity_for("run-1"),
                          checkpointer=checkpointer, initial_state=initial_state(tmp_path))
        after_first = len(backend.prompted)
        second_root = tmp_path / "prompts-run-2"
        second_root.mkdir(mode=0o700)
        second_context = {
            **context_for(backend),
            "prompt_artifact_scope": PromptArtifactScope(
                root=second_root, run_id="run-2", task_id=1, managed=True
            ),
        }
        second = durable_run_cycle(context=second_context, identity=identity_for("run-2"),
                                   checkpointer=checkpointer,
                                   initial_state=initial_state(tmp_path, workflow_run_id="run-2"))

    assert second.disposition == "started"
    assert len(backend.prompted) > after_first


def test_the_prompt_step_can_hand_observations_to_interpretation(tmp_path):
    """A node's return value only survives if `CycleState` declares the key.

    LangGraph drops undeclared keys silently, so the ledger's record of what a
    phase's declared outputs looked like before it ran would go missing with no
    error anywhere -- and interpretation could not tell a file the agent just
    wrote from one that was always there.
    """
    seen = {}

    class ObservingBackend(ScriptedBackend):
        def interpret(self, phase, state, turn):
            if phase == "generate_tests":
                seen["carried"] = "output_fingerprints_before" in state
                seen["value"] = state.get("output_fingerprints_before")
            return super().interpret(phase, state, turn)

    class FingerprintingLedger(ScriptedLedger):
        def classify(self, *, phase, step, state):
            decision = super().classify(phase=phase, step=step, state=state)
            return {
                **decision,
                "output_fingerprints_before": {"tests/generated.py": "<missing>"},
            }

    run_cycle(tmp_path, context_for(ObservingBackend(), FingerprintingLedger()))

    assert seen.get("carried") is True, "the pre-turn observation never reached interpret"
    assert seen.get("value") == {"tests/generated.py": "<missing>"}

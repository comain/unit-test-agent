"""The context-managed durable generation-cycle application."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.harness import AgentSessionRef, SessionSnapshot, SessionUnsupportedError
from agent_core.workflow import WorkflowRunIdentity
from uta.tasks.manager import TaskManager
from uta.testgen.batches import ensure_stable_generation_batches
from uta.testgen.graph.application import (
    UnsafeWorkflowStateError,
    WorkflowResultValidationError,
    open_workflow_application,
    workflow_state_root,
)
from uta.testgen.graph.cycle import GenerationPaused
from uta.testgen.prompts import PromptArtifactScope


@pytest.fixture(autouse=True)
def setup_persistence():
    from uta.app.persistence import register_task_persistence
    from uta.testgen.ports.registry import reset_task_persistence_provider

    register_task_persistence()
    yield
    reset_task_persistence_provider()


class ScriptedBackend:
    DEFAULT = {"precheck_existing_tests": "proceed"}

    def render_prompt(self, phase, state):
        return f"perform {phase}"

    def interpret(self, phase, state, turn):
        return {"phase_outcome": "passed"}

    def run_phase(self, phase, state):
        return {"phase_outcome": self.DEFAULT.get(phase, "passed")}


class Session:
    def __init__(self, owner):
        self.owner = owner
        self.session_id = f"session-{owner.turns + 1}"

    def run_turn(self, **kwargs):
        self.owner.turns += 1
        return SimpleNamespace(
            type="completed",
            result="done",
            session_id=self.session_id,
            tokens={},
            cost_usd=0.0,
            model_id="fake/model",
            error=None,
            raw_log_path=None,
        )

    def snapshot(self):
        return SessionSnapshot(
            session_id=self.session_id,
            session_refs=(AgentSessionRef("scripted", self.session_id),),
        )

    def close(self):
        self.owner.closed += 1


class ResumableRunner:
    def __init__(self):
        self.turns = 0
        self.closed = 0

    def run_turn(self, **kwargs):
        return Session(self).run_turn(**kwargs)

    def open_session(self, **kwargs):
        return Session(self)


class ProgressSink:
    def __init__(self, *, fail_close=False):
        self.closed = 0
        self.fail_close = fail_close

    def close(self):
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("progress storage unavailable")


def _setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    batch = ensure_stable_generation_batches(
        manager.db,
        repo_task_id=task_id,
        ordered_target_ids=["pkg.A"],
        batch_size=1,
        workflow_run_id="run-one",
    )[0]
    workspace = {"fingerprint": "clean"}
    runner = ResumableRunner()
    cancelled = {"value": False}
    context = {
        "repo_path": str(repo),
        "runner": runner,
        "backend": ScriptedBackend(),
        "is_cancelled": lambda: cancelled["value"],
        "fingerprint": lambda state, phase, step: workspace["fingerprint"],
        "allowed_edit": lambda state, row: False,
        "output_fingerprints": lambda state, phase, step: {},
        "prompt_artifact_scope": PromptArtifactScope(
            root=tmp_path / "prompt-state",
            run_id=batch.workflow_run_id,
            task_id=task_id,
            managed=True,
        ),
    }
    context["prompt_artifact_scope"].root.mkdir(mode=0o700)
    state = {"repo_path": str(repo), "language": "java"}
    return manager, task_id, batch, runner, cancelled, context, state


def test_application_uses_owner_only_dedicated_workflow_state(tmp_path):
    manager, _, _, _, _, context, _ = _setup(tmp_path)
    root = workflow_state_root(manager.db_path)

    with open_workflow_application(task_db_path=manager.db_path, context=context):
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "checkpoints.sqlite").stat().st_mode) == 0o600
        assert stat.S_IMODE((root / "results").stat().st_mode) == 0o700


def test_application_compiles_with_the_native_langgraph_saver(tmp_path):
    manager, _, _, _, _, context, _ = _setup(tmp_path)

    with open_workflow_application(
        task_db_path=manager.db_path,
        context=context,
    ) as application:
        saver = application.checkpointer

    assert type(saver).__module__.startswith("langgraph.checkpoint.")
    assert type(saver).__name__ == "SqliteSaver"


def test_checkpoint_identity_matches_the_v2_contract(tmp_path):
    import json

    manager, task_id, batch, _, _, _, _ = _setup(tmp_path)
    contract = json.loads(
        (Path(__file__).parent / "fixtures/contracts/uta_workflow_v2.json").read_text(
            encoding="utf-8"
        )
    )["checkpoint"]
    identity = WorkflowRunIdentity(
        product="uta",
        task_id=str(task_id),
        unit_id=batch.unit_id,
        workflow_run_id=batch.workflow_run_id,
        cycle="test-generation-cycle",
        version="v2",
    )

    assert {
        "cycle": identity.cycle,
        "version": identity.version,
        "unit_id": identity.unit_id,
        "thread_id": identity.thread_id,
    } == contract


def test_application_closes_task_progress_before_checkpointer_exit(tmp_path):
    manager, _, _, _, _, context, _ = _setup(tmp_path)
    sink = ProgressSink()
    context["progress_sink"] = sink

    with open_workflow_application(task_db_path=manager.db_path, context=context):
        assert sink.closed == 0

    assert sink.closed == 1


def test_application_progress_close_failure_never_changes_workflow_truth(tmp_path):
    manager, task_id, batch, runner, _, context, state = _setup(tmp_path)
    sink = ProgressSink(fail_close=True)
    context["progress_sink"] = sink

    with open_workflow_application(task_db_path=manager.db_path, context=context) as app:
        result = app.invoke_batch(
            repo_task_id=task_id, batch=batch, initial_state=state
        )

    assert result.state["phase_outcome"] == "passed"
    assert runner.turns == 2
    assert sink.closed == 1


def test_checkpointed_cycle_preserves_both_prompt_artifact_paths(tmp_path):
    manager, task_id, batch, _, _, context, state = _setup(tmp_path)

    with open_workflow_application(task_db_path=manager.db_path, context=context) as app:
        completed = app.invoke_batch(
            repo_task_id=task_id, batch=batch, initial_state=state
        )
        reused = app.invoke_batch(
            repo_task_id=task_id, batch=batch, initial_state=state
        )

    assert completed.state["prompt_file"] == reused.state["prompt_file"]
    assert completed.state["prompt_inputs_file"] == reused.state["prompt_inputs_file"]
    assert completed.state["prompt_manifest_file"] == reused.state["prompt_manifest_file"]
    assert reused.state["turn_history"] == completed.state["turn_history"]
    assert [
        ref["locator"]
        for turn in completed.state["turn_history"]
        for ref in turn["session_refs"]
    ] == ["session-1", "session-2"]
    assert Path(completed.state["prompt_file"]).is_file()
    assert Path(completed.state["prompt_inputs_file"]).is_file()
    assert Path(completed.state["prompt_file"]).parent.parent == (
        context["prompt_artifact_scope"].root
        / "managed"
        / str(task_id)
        / batch.workflow_run_id
        / batch.unit_id
    )


def test_application_requires_a_session_capable_agent(tmp_path):
    manager, _, _, _, _, context, _ = _setup(tmp_path)
    context["runner"] = SimpleNamespace(run_turn=lambda **kwargs: None)

    with pytest.raises(SessionUnsupportedError):
        with open_workflow_application(task_db_path=manager.db_path, context=context):
            pass


def test_cancelled_cycle_resumes_the_same_lineage_without_spending_an_attempt(tmp_path):
    manager, task_id, batch, runner, cancelled, context, state = _setup(tmp_path)

    with open_workflow_application(task_db_path=manager.db_path, context=context) as app:
        cancelled["value"] = True
        with pytest.raises(GenerationPaused):
            app.invoke_batch(repo_task_id=task_id, batch=batch, initial_state=state)
        with pytest.raises(GenerationPaused):
            app.invoke_batch(repo_task_id=task_id, batch=batch, initial_state=state)
        cancelled["value"] = False
        result = app.invoke_batch(
            repo_task_id=task_id,
            batch=batch,
            initial_state={**state, "this_must_not_restart": "marker"},
        )

    assert result.disposition == "resumed"
    assert "this_must_not_restart" not in result.state
    assert result.state["phase_outcome"] == "passed"
    assert runner.turns == 2, "only plan and generation need model turns after resume"
    plan_rows = [
        row for row in manager.db.list_workflow_operations(task_id)
        if row["phase"] == "plan_tests" and row["operation_step"] == "turn"
    ]
    assert [row["attempt"] for row in plan_rows] == [0, 0]
    assert [row["execution_ordinal"] for row in plan_rows] == [0, 1]


def test_completed_checkpoint_is_rejected_when_terminal_product_evidence_is_missing(tmp_path):
    manager, task_id, batch, runner, _, context, state = _setup(tmp_path)

    with open_workflow_application(task_db_path=manager.db_path, context=context) as app:
        completed = app.invoke_batch(
            repo_task_id=task_id, batch=batch, initial_state=state
        )
        reused = app.invoke_batch(
            repo_task_id=task_id, batch=batch, initial_state=state
        )
        assert reused.disposition == "reused_completed"
        terminal = manager.db.get_workflow_operation(completed.state["operation_id"])
        (workflow_state_root(manager.db_path) / "results" / terminal["result_artifact_path"]).unlink()

        with pytest.raises(WorkflowResultValidationError):
            app.invoke_batch(repo_task_id=task_id, batch=batch, initial_state=state)

    assert runner.turns == 2, "a terminal checkpoint is never re-entered after tampering"


def test_checkpoint_state_rejects_live_objects_and_secret_fields(tmp_path):
    manager, task_id, batch, _, _, context, state = _setup(tmp_path)

    with open_workflow_application(task_db_path=manager.db_path, context=context) as app:
        with pytest.raises(UnsafeWorkflowStateError, match="callable"):
            app.invoke_batch(
                repo_task_id=task_id,
                batch=batch,
                initial_state={**state, "backend": lambda: None},
            )
        with pytest.raises(UnsafeWorkflowStateError, match="secret"):
            app.invoke_batch(
                repo_task_id=task_id,
                batch=batch,
                initial_state={**state, "api_secret": "do-not-checkpoint"},
            )


def test_terminal_validation_says_which_check_failed(tmp_path):
    """`validate_terminal` distinguishes five failures; the wrapper flattened
    them into one line.

    "terminal product evidence is invalid for uta:...:unit-0001-..." tells an
    operator nothing about whether the row was incomplete, the artifact
    missing, the workspace changed underneath, or the outputs diverged -- and
    the daemon logs it without a traceback, so the chained cause is lost in
    practice. These reasons are our own fixed strings, not provider or driver
    text, so there is nothing to redact by hiding them.
    """
    manager, task_id, batch, runner, _, context, state = _setup(tmp_path)

    with open_workflow_application(task_db_path=manager.db_path, context=context) as app:
        completed = app.invoke_batch(
            repo_task_id=task_id, batch=batch, initial_state=state
        )
        terminal = manager.db.get_workflow_operation(completed.state["operation_id"])
        (workflow_state_root(manager.db_path) / "results" / terminal["result_artifact_path"]).unlink()

        with pytest.raises(WorkflowResultValidationError) as raised:
            app.invoke_batch(repo_task_id=task_id, batch=batch, initial_state=state)

    message = str(raised.value)
    assert "artifact is missing" in message, (
        f"the specific reason was dropped; operator sees only: {message}"
    )
    assert batch.unit_id in message, "the failing unit is still identified"


def test_terminal_validation_failure_keeps_its_cause_chained(tmp_path):
    """The wrapper must not replace the original error, only add context."""
    manager, task_id, batch, runner, _, context, state = _setup(tmp_path)

    with open_workflow_application(task_db_path=manager.db_path, context=context) as app:
        completed = app.invoke_batch(
            repo_task_id=task_id, batch=batch, initial_state=state
        )
        terminal = manager.db.get_workflow_operation(completed.state["operation_id"])
        (workflow_state_root(manager.db_path) / "results" / terminal["result_artifact_path"]).unlink()

        with pytest.raises(WorkflowResultValidationError) as raised:
            app.invoke_batch(repo_task_id=task_id, batch=batch, initial_state=state)

    assert raised.value.__cause__ is not None

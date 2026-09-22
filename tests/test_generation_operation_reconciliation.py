"""Crash reconciliation for product-owned generation operation evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.harness import AgentSessionRef
from agent_core.workflow import AgentTurnResult
from uta.tasks.manager import TaskManager
from uta.testgen.graph.application import workflow_state_root
from uta.testgen.operations import (
    ArtifactValidationError,
    OperationArtifactStore,
    OperationResultEnvelope,
    WorkflowOperationLedger,
)


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    workspace = {
        "fingerprint": "input-a",
        "output_fingerprint": "input-a",
        "allowed": False,
    }

    def fingerprint(state, phase, step):
        return workspace["fingerprint"]

    def allowed_edit(state, row):
        return workspace["allowed"]

    ledger = WorkflowOperationLedger(
        manager.db,
        OperationArtifactStore(tmp_path / "workflow-state" / "results"),
        fingerprint=fingerprint,
        allowed_edit=allowed_edit,
        output_fingerprints=lambda state, phase, step: {
            "tests/ATest.java": workspace["output_fingerprint"]
        },
    )
    state = {
        "task_id": task_id,
        "workflow_run_id": "run-1",
        "unit_id": "unit-1",
        "attempts_by_phase": {"generate_tests": 1},
        "prerequisite_operation_ids": [],
    }
    return manager.db, ledger, workspace, state


def classify(ledger, state, *, phase="generate_tests", step="turn"):
    decision = ledger.classify(phase=phase, step=step, state=state)
    state.update({key: value for key, value in decision.items() if key != "outcome"})
    return decision


def test_no_evidence_starts_one_stable_operation(setup):
    db, ledger, _, state = setup

    first = classify(ledger, state)
    second = classify(ledger, state)

    assert first["outcome"] == "run"
    assert second["outcome"] == "retry_no_effect"
    assert second["logical_attempt"] == first["logical_attempt"] == 1
    assert second["execution_ordinal"] == 1
    old = db.get_workflow_operation(first["operation_id"])
    assert old["status"] == "FAILED"
    assert old["superseded_by_operation_id"] == second["operation_id"]


def test_all_seven_reconciliation_outcomes_match_the_v2_contract(setup):
    db, ledger, workspace, state = setup
    observed = {}

    def begin(phase, fingerprint):
        for key in (
            "operation_id",
            "operation_step",
            "logical_attempt",
            "execution_ordinal",
            "input_fingerprint",
            "output_fingerprints_before",
        ):
            state.pop(key, None)
        workspace.update(
            fingerprint=fingerprint,
            output_fingerprint=fingerprint,
            allowed=False,
        )
        state["attempts_by_phase"][phase] = 1
        return classify(ledger, state, phase=phase)

    observed["fresh"] = begin("fresh", "fresh-a")["outcome"]

    begin("crash_no_effect", "crash-a")
    observed["crash_no_effect"] = classify(
        ledger, state, phase="crash_no_effect"
    )["outcome"]

    begin("completed", "completed-a")
    workspace.update(fingerprint="completed-b", output_fingerprint="completed-b")
    ledger.on_result(
        state,
        {"label": "completed"},
        AgentTurnResult(status="completed", text="done"),
    )
    observed["completed"] = classify(ledger, state, phase="completed")["outcome"]

    orphan = begin("orphan", "orphan-a")
    orphan_identity = ledger.identity_for_row(
        db.get_workflow_operation(orphan["operation_id"])
    )
    workspace.update(fingerprint="orphan-b", output_fingerprint="orphan-b")
    ledger.artifacts.write(
        OperationResultEnvelope(
            identity=orphan_identity,
            result_kind="turn",
            result=AgentTurnResult(status="completed", text="orphan").as_dict(),
            resulting_workspace_fingerprint="orphan-b",
            output_fingerprints={"tests/ATest.java": "orphan-b"},
        )
    )
    observed["orphan"] = classify(ledger, state, phase="orphan")["outcome"]

    begin("allowed_edit", "allowed-a")
    workspace.update(fingerprint="allowed-b", allowed=True)
    observed["allowed_edit"] = classify(
        ledger, state, phase="allowed_edit"
    )["outcome"]

    begin("unsafe_edit", "unsafe-a")
    workspace.update(fingerprint="unsafe-b", allowed=False)
    observed["unsafe_edit"] = classify(ledger, state, phase="unsafe_edit")[
        "outcome"
    ]

    begin("replay_exhausted", "replay-a")
    classify(ledger, state, phase="replay_exhausted")
    observed["replay_exhausted"] = classify(
        ledger, state, phase="replay_exhausted"
    )["outcome"]

    contract = json.loads(
        (Path(__file__).parent / "fixtures/contracts/uta_workflow_v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert observed == contract["reconciliation_outcomes"]


def test_a_completed_valid_artifact_is_reused(setup):
    db, ledger, workspace, state = setup
    decision = classify(ledger, state)
    workspace["fingerprint"] = "workspace-b"
    ledger.on_result(
        state,
        {"label": "generate_tests"},
        AgentTurnResult(
            status="completed",
            text="done",
            session_refs=(AgentSessionRef("scripted", "session-1"),),
            usage={"input_tokens": 3},
            elapsed_seconds=2.5,
            retrospective={"summary": "ok"},
            patch_count=1,
            recovered=True,
            diagnostics={"node_status": "accepted"},
            raw_log_path="logs/turn.jsonl",
        ),
    )

    again = classify(ledger, state)

    assert again["operation_id"] == decision["operation_id"]
    assert again["outcome"] == "reuse_result"
    assert db.get_workflow_operation(decision["operation_id"])["status"] == "COMPLETED"


def test_a_completed_artifact_with_changed_output_is_not_reused(setup):
    _, ledger, workspace, state = setup
    classify(ledger, state)
    workspace["fingerprint"] = "workspace-b"
    ledger.on_result(
        state,
        {"label": "generate_tests"},
        AgentTurnResult(status="completed", text="done"),
    )
    workspace["output_fingerprint"] = "tampered-output"

    assert classify(ledger, state)["outcome"] == "fail_unsafe"


def test_a_result_with_a_deleted_prerequisite_is_not_reused(setup):
    db, ledger, workspace, state = setup
    plan = classify(ledger, state, phase="plan_tests", step="deterministic")
    ledger.record_result(
        phase="plan_tests",
        step="deterministic",
        state=state,
        result={"phase_outcome": "passed"},
    )
    state["prerequisite_operation_ids"] = [plan["operation_id"]]
    classify(ledger, state)
    workspace["fingerprint"] = "workspace-b"
    ledger.on_result(
        state,
        {"label": "generate_tests"},
        AgentTurnResult(status="completed", text="done"),
    )
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM workflow_operations WHERE operation_id=?",
            (plan["operation_id"],),
        )

    assert classify(ledger, state)["outcome"] == "fail_indeterminate"


def test_a_fully_renamed_orphan_result_is_adopted(setup):
    db, ledger, workspace, state = setup
    decision = classify(ledger, state)
    identity = ledger.identity_for_row(db.get_workflow_operation(decision["operation_id"]))
    workspace["fingerprint"] = "workspace-b"
    workspace["output_fingerprint"] = "workspace-b"
    ledger.artifacts.write(
        OperationResultEnvelope(
            identity=identity,
            result_kind="turn",
            result=AgentTurnResult(status="completed", text="orphan").as_dict(),
            resulting_workspace_fingerprint="workspace-b",
            output_fingerprints={"tests/ATest.java": "workspace-b"},
            prerequisite_operation_ids=(),
        )
    )

    adopted = classify(ledger, state)

    assert adopted["outcome"] == "adopt_result"
    assert db.get_workflow_operation(decision["operation_id"])["status"] == "COMPLETED"


def test_changed_workspace_routes_to_verification_only_when_the_edit_is_allowed(setup):
    _, ledger, workspace, state = setup
    classify(ledger, state)
    workspace["fingerprint"] = "workspace-b"
    workspace["allowed"] = True

    assert classify(ledger, state)["outcome"] == "verify_existing_edit"

    workspace["allowed"] = False
    assert classify(ledger, state)["outcome"] == "fail_unsafe"


def test_a_second_no_effect_crash_fails_indeterminate(setup):
    _, ledger, _, state = setup
    classify(ledger, state)
    retry = classify(ledger, state)
    state.update({key: value for key, value in retry.items() if key != "outcome"})

    assert classify(ledger, state)["outcome"] == "fail_indeterminate"


def test_an_invalid_final_artifact_is_never_deleted_and_retried(setup):
    db, ledger, _, state = setup
    decision = classify(ledger, state)
    identity = ledger.identity_for_row(db.get_workflow_operation(decision["operation_id"]))
    path = ledger.artifacts.path_for(identity)
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("not json", encoding="utf-8")
    path.chmod(0o600)

    assert classify(ledger, state)["outcome"] == "fail_indeterminate"
    assert path.exists()


def test_rehydrate_restores_the_full_normalized_turn_projection(setup):
    _, ledger, workspace, state = setup
    classify(ledger, state)
    workspace["fingerprint"] = "workspace-b"
    result = AgentTurnResult(
        status="completed",
        text="answer",
        session_refs=(AgentSessionRef("scripted", "session-9"),),
        usage={"input_tokens": 4, "output_tokens": 2},
        retrospective={"summary": "learned"},
        patch_count=3,
        recovered=True,
        attempts=2,
        elapsed_seconds=7.5,
        diagnostics={"turn_type": "completed"},
        raw_log_path="logs/session-9.jsonl",
    )
    ledger.on_result(state, {"label": "generate_tests"}, result)

    restored = ledger.rehydrate(phase="generate_tests", step="turn", state=state)

    assert restored["rehydration"] == "ready"
    assert restored["turn_result"] == result.as_dict()
    assert restored["turn_session_id"] == "session-9"
    assert restored["turn_session_refs"] == [
        {"harness": "scripted", "locator": "session-9", "scope": "durable"}
    ]
    assert restored["turn_usage"] == result.usage
    assert restored["turn_retrospective"] == result.retrospective
    assert restored["turn_patch_count"] == 3
    assert restored["turn_recovered"] is True
    assert restored["turn_raw_log_path"] == "logs/session-9.jsonl"


def test_phase_results_are_persisted_and_rehydrated_without_a_backend(setup):
    db, ledger, workspace, state = setup
    decision = classify(ledger, state, phase="verify_compile", step="deterministic")
    workspace["fingerprint"] = "verified-b"
    result = {
        "phase_outcome": "passed",
        "evidence": {"command": "mvn test-compile"},
        "artifacts": {},
    }

    projected = ledger.record_result(
        phase="verify_compile", step="deterministic", state=state, result=result
    )
    restored = ledger.rehydrate(
        phase="verify_compile", step="deterministic", state=state
    )

    assert projected["operation_id"] == decision["operation_id"]
    assert db.get_workflow_operation(decision["operation_id"])["status"] == "COMPLETED"
    assert restored["phase_outcome"] == "passed"
    assert restored["phase_results"]["verify_compile"]["evidence"] == result["evidence"]


def test_cancelled_turn_writes_one_cancelled_envelope_for_resume(setup):
    db, ledger, _, state = setup
    decision = classify(ledger, state)

    ledger.on_result(
        state, {"label": "generate_tests"}, AgentTurnResult.cancelled()
    )

    row = db.get_workflow_operation(decision["operation_id"])
    assert row["status"] == "CANCELLED"
    assert row["result_artifact_path"]
    assert classify(ledger, state)["outcome"] == "retry_no_effect"


def test_an_orphan_result_whose_outputs_no_longer_match_is_not_adopted(setup):
    """The workspace fingerprint agreeing is not enough to adopt.

    An interrupted operation left a complete artifact behind, and the overall
    workspace looks exactly as that artifact says it should -- but the file the
    operation actually produced has since changed. Adopting here would
    transactionally complete the row and carry on from a result that no longer
    describes what is on disk, and every later phase would build on it.

    The completed-row path already refuses this
    (`test_a_completed_artifact_with_changed_output_is_not_reused`); this is
    the same guarantee on the adopt path, which was reachable but unasserted --
    deleting its check broke no test.
    """
    db, ledger, workspace, state = setup
    decision = classify(ledger, state)
    identity = ledger.identity_for_row(db.get_workflow_operation(decision["operation_id"]))
    workspace["fingerprint"] = "workspace-b"
    ledger.artifacts.write(
        OperationResultEnvelope(
            identity=identity,
            result_kind="turn",
            result=AgentTurnResult(status="completed", text="orphan").as_dict(),
            resulting_workspace_fingerprint="workspace-b",
            # Recorded when the operation ran; the file has since moved on.
            output_fingerprints={"tests/ATest.java": "stale-output"},
            prerequisite_operation_ids=(),
        )
    )
    workspace["output_fingerprint"] = "workspace-b"

    adopted = classify(ledger, state)

    assert adopted["outcome"] == "fail_indeterminate"
    assert db.get_workflow_operation(decision["operation_id"])["status"] == "STARTED", (
        "an unadoptable operation must not be completed"
    )


def test_a_crashed_turn_reclassifies_as_a_counted_replay(setup):
    """The crash path a hard daemon kill actually produces.

    A turn was STARTED and the process died: no artifact, workspace unchanged.
    On restart the cycle re-enters its reconciliation node and classifies the
    same slot again. The design requires that this counts as a replay --
    `retry_no_effect`, a *new* operation id, and `execution_ordinal` advanced --
    because `execution_ordinal` is the only thing bounding crash replays.
    `max_crash_replays_per_operation` refuses at the limit, so an ordinal that
    never advances means an expensive turn can be re-run without bound.

    Observed on beta: the operation id stayed identical and the ordinal stayed
    at 0 across a real SIGKILL, and the model turn ran a second time.
    """
    db, ledger, _, state = setup
    first = classify(ledger, state)
    assert first["outcome"] == "run"
    started = db.get_workflow_operation(first["operation_id"])
    assert started["status"] == "STARTED"

    # The restarted cycle asks again from the same state.
    second = classify(ledger, state)

    assert second["outcome"] == "retry_no_effect"
    assert second["operation_id"] != first["operation_id"], (
        "a crash replay reused the operation id instead of minting a successor"
    )
    assert second["execution_ordinal"] == first["execution_ordinal"] + 1, (
        "execution_ordinal did not advance, so max_crash_replays can never bound it"
    )
    superseded = db.get_workflow_operation(first["operation_id"])
    assert superseded["status"] == "FAILED"
    assert superseded["error_kind"] == "crash_no_observable_effect"


def test_a_prerequisite_may_have_its_outputs_changed_by_a_later_phase(setup):
    """Observed on beta, and it quarantined a task that had worked.

    `precheck_existing_tests` records the target's test file as its output
    while that file does not yet exist. `generate_tests` then writes it --
    which is the entire point of the cycle. At the terminal check,
    `_validate_prerequisites` compared precheck's recorded outputs against the
    *current* workspace, saw they differed, and rejected the run with
    "workflow prerequisite outputs changed".

    A prerequisite ran earlier by definition, so later phases are expected to
    move what it observed. What must still hold is that its artifact is intact,
    complete, and belongs to this task/run/unit -- all of which stay checked.
    Each operation's own outputs are still validated when it is the row being
    validated.
    """
    db, ledger, workspace, state = setup

    classify(ledger, state, phase="precheck_existing_tests", step="deterministic")
    ledger.record_result(
        phase="precheck_existing_tests",
        step="deterministic",
        state=state,
        result={"phase_outcome": "proceed"},
    )

    # The turn writes the test file: the workspace and that output legitimately move.
    workspace["fingerprint"] = "workspace-after-generation"
    workspace["output_fingerprint"] = "test-file-now-exists"

    classify(ledger, state, phase="complete_generation", step="deterministic")
    ledger.record_result(
        phase="complete_generation",
        step="deterministic",
        state=state,
        result={"phase_outcome": "passed"},
    )

    envelope = ledger.validate_terminal(state)

    assert envelope.result["phase_outcome"] == "passed"


def test_a_prerequisite_with_a_tampered_artifact_is_still_rejected(setup):
    """Relaxing the outputs check must not relax artifact integrity."""
    db, ledger, workspace, state = setup

    first = classify(ledger, state, phase="precheck_existing_tests", step="deterministic")
    ledger.record_result(
        phase="precheck_existing_tests",
        step="deterministic",
        state=state,
        result={"phase_outcome": "proceed"},
    )
    prerequisite = db.get_workflow_operation(first["operation_id"])

    classify(ledger, state, phase="complete_generation", step="deterministic")
    ledger.record_result(
        phase="complete_generation",
        step="deterministic",
        state=state,
        result={"phase_outcome": "passed"},
    )
    (
        workflow_state_root(db.path) / "results" / prerequisite["result_artifact_path"]
    ).unlink()

    with pytest.raises(ArtifactValidationError):
        ledger.validate_terminal(state)


def test_interpret_reconciliation_does_not_overwrite_the_pre_turn_outputs(setup):
    """The interpret step reconciles *after* the turn has already written.

    Both steps of a model phase get their own reconciliation node. If the
    interpret one re-sampled the declared outputs, it would record the
    post-turn state and overwrite what the turn's reconciliation captured --
    so interpretation would compare the produced file against itself and
    conclude the phase produced nothing. That is exactly what happened on beta
    to a run that generated 23 passing tests.
    """
    _, ledger, workspace, state = setup

    turn_decision = ledger.classify(
        phase="generate_tests", step="turn", state=state
    )
    assert turn_decision["output_fingerprints_before"], (
        "the turn's reconciliation must record the pre-turn outputs"
    )
    state.update({k: v for k, v in turn_decision.items() if k != "outcome"})

    # The turn runs and changes the declared output.
    workspace["output_fingerprint"] = "written-by-the-agent"

    interpret_decision = ledger.classify(
        phase="generate_tests", step="interpret", state=state
    )

    assert "output_fingerprints_before" not in interpret_decision, (
        "the interpret step re-sampled and would clobber the pre-turn record"
    )

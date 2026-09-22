"""Authoritative product evidence for generation workflow operations."""

from __future__ import annotations

import sqlite3

import pytest

from uta.tasks.db import TaskDB, WorkflowOperationConflict
from uta.tasks.manager import TaskManager
from uta.testgen.operations import WorkflowCostGate
from uta.testgen.task_guard import TaskBudgetExceeded


@pytest.fixture
def db_and_task(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    return manager.db, task_id


def operation(task_id: int, **overrides):
    values = {
        "operation_id": "op-1",
        "repo_task_id": task_id,
        "workflow_run_id": "run-1",
        "unit_id": "unit-1",
        "phase": "generate_tests",
        "operation_step": "turn",
        "attempt": 1,
        "execution_ordinal": 0,
        "input_fingerprint": "input-a",
        "prerequisite_operation_ids": ["plan-op"],
        "schema_version": 1,
        "session_id": "session-1",
    }
    values.update(overrides)
    return values


def test_init_adds_the_operation_ledger_and_recovery_index(db_and_task):
    db, _ = db_and_task

    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(workflow_operations)")}
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(workflow_operations)")}

    assert {"operation_id", "operation_step", "execution_ordinal", "status"} <= columns
    assert "workflow_operations_recovery_idx" in indexes


def test_operation_cost_entries_are_immutable_idempotent_and_aggregated(db_and_task):
    db, task_id = db_and_task
    db.start_workflow_operation(**operation(task_id))

    first = db.record_workflow_operation_cost(
        "op-1", paid_attempt_ordinal=1, provider_cost_usd=0.25,
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    repeated = db.record_workflow_operation_cost(
        "op-1", paid_attempt_ordinal=1, provider_cost_usd=0.25,
        usage={"input_tokens": 10, "output_tokens": 2},
    )

    assert dict(first) == dict(repeated)
    assert db.workflow_operation_cost_total(task_id) == {
        "provider_cost_usd": 0.25,
        "cost_provenance": "recorded",
    }
    with pytest.raises(WorkflowOperationConflict, match="paid attempt"):
        db.record_workflow_operation_cost(
            "op-1", paid_attempt_ordinal=1, provider_cost_usd=0.5,
        )


def test_one_unavailable_attempt_makes_the_operation_total_unavailable(db_and_task):
    db, task_id = db_and_task
    db.start_workflow_operation(**operation(task_id))
    db.record_workflow_operation_cost(
        "op-1", paid_attempt_ordinal=1, provider_cost_usd=0.25,
    )
    db.record_workflow_operation_cost(
        "op-1", paid_attempt_ordinal=2, provider_cost_usd=None,
    )

    assert db.workflow_operation_cost_total(task_id) == {
        "provider_cost_usd": None,
        "cost_provenance": "unavailable",
    }


def test_cost_gate_reads_only_the_immutable_operation_ledger(db_and_task):
    db, task_id = db_and_task
    db.start_workflow_operation(**operation(task_id))
    db.update_repo_task(task_id, hard_cap_usd=0.20)
    gate = WorkflowCostGate(db)

    gate.allow_attempt(operation_id="op-1", paid_attempt_ordinal=1)
    db.record_workflow_operation_cost(
        "op-1", paid_attempt_ordinal=1, provider_cost_usd=0.25,
    )

    with pytest.raises(TaskBudgetExceeded, match="currency cap reached"):
        gate.allow_attempt(operation_id="op-1", paid_attempt_ordinal=2)


def test_start_is_idempotent_only_for_the_same_immutable_operation(db_and_task):
    db, task_id = db_and_task
    first = db.start_workflow_operation(**operation(task_id))
    second = db.start_workflow_operation(**operation(task_id))

    assert first["status"] == "STARTED"
    assert dict(second) == dict(first)

    with pytest.raises(WorkflowOperationConflict, match="op-1"):
        db.start_workflow_operation(**operation(task_id, input_fingerprint="different"))


def test_completion_is_atomic_and_exactly_idempotent(db_and_task):
    db, task_id = db_and_task
    db.start_workflow_operation(**operation(task_id))

    completed = db.complete_workflow_operation(
        "op-1",
        resulting_workspace_fingerprint="workspace-b",
        result_artifact_path="run-1/unit-1/op-1.json",
        result_artifact_sha256="artifact-sha",
        output_fingerprints={"tests/pkg/ATest.java": "test-sha"},
    )
    repeated = db.complete_workflow_operation(
        "op-1",
        resulting_workspace_fingerprint="workspace-b",
        result_artifact_path="run-1/unit-1/op-1.json",
        result_artifact_sha256="artifact-sha",
        output_fingerprints={"tests/pkg/ATest.java": "test-sha"},
    )

    assert completed["status"] == "COMPLETED"
    assert dict(repeated) == dict(completed)
    with pytest.raises(WorkflowOperationConflict, match="already completed"):
        db.complete_workflow_operation(
            "op-1",
            resulting_workspace_fingerprint="workspace-c",
            result_artifact_path="run-1/unit-1/op-1.json",
            result_artifact_sha256="different",
            output_fingerprints={},
        )


@pytest.mark.parametrize("status,method", [("FAILED", "fail_workflow_operation"), ("CANCELLED", "cancel_workflow_operation")])
def test_terminal_failure_transitions_do_not_overwrite_completed_evidence(
    db_and_task, status, method
):
    db, task_id = db_and_task
    db.start_workflow_operation(**operation(task_id))
    terminal = getattr(db, method)("op-1", error_kind="worker", error="stopped")

    assert terminal["status"] == status
    assert getattr(db, method)("op-1", error_kind="worker", error="stopped")["status"] == status
    with pytest.raises(WorkflowOperationConflict):
        db.complete_workflow_operation(
            "op-1",
            resulting_workspace_fingerprint="workspace-b",
            result_artifact_path="result.json",
            result_artifact_sha256="sha",
            output_fingerprints={},
        )


def test_deleting_a_task_cascades_its_operation_evidence(db_and_task):
    db, task_id = db_and_task
    db.start_workflow_operation(**operation(task_id))

    with db.connect() as conn:
        conn.execute("DELETE FROM repo_tasks WHERE id=?", (task_id,))

    assert db.get_workflow_operation("op-1") is None


def test_a_version_two_database_upgrades_idempotently(tmp_path):
    path = tmp_path / "legacy.db"
    db = TaskDB(path)
    db.init()
    with db.connect() as conn:
        conn.execute("DROP TABLE workflow_operations")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (2)")

    db.init()
    db.init()

    with db.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_operations'"
        ).fetchone()
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 7
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='workflow_operation_accounting'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='legacy_prompt_scopes'"
        ).fetchone()
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(legacy_prompt_scopes)")
        }
        assert "legacy_prompt_scopes_retention_idx" in indexes


def test_start_rejects_an_unknown_product_task(tmp_path):
    db = TaskDB(tmp_path / "tasks.db")
    db.init()

    with pytest.raises(sqlite3.IntegrityError):
        db.start_workflow_operation(**operation(999))


def test_start_rejects_an_unknown_step_or_negative_ordinal(db_and_task):
    db, task_id = db_and_task

    with pytest.raises(sqlite3.IntegrityError):
        db.start_workflow_operation(**operation(task_id, operation_step="provider"))
    with pytest.raises(sqlite3.IntegrityError):
        db.start_workflow_operation(
            **operation(task_id, operation_id="op-negative", execution_ordinal=-1)
        )


def test_retry_rolls_back_old_status_when_successor_insert_fails(db_and_task):
    db, task_id = db_and_task
    db.start_workflow_operation(**operation(task_id))
    db.start_workflow_operation(
        **operation(
            task_id,
            operation_id="occupied-successor",
            execution_ordinal=1,
        )
    )

    with pytest.raises(sqlite3.IntegrityError):
        successor = operation(
            task_id,
            operation_id="occupied-successor",
            execution_ordinal=1,
        )
        successor.pop("session_id")
        db.retry_workflow_operation(
            "op-1",
            **successor,
        )

    old = db.get_workflow_operation("op-1")
    assert old["status"] == "STARTED"
    assert old["superseded_by_operation_id"] is None

"""Explicit, audited replacement of generation workflow identities."""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest
from click.testing import CliRunner

from uta.app.cli import main
from uta.tasks.manager import TaskManager
from uta.testgen.batches import ensure_stable_generation_batches


def _setup(tmp_path, *, count=4):
    repo = tmp_path / "repo"
    repo.mkdir()
    targets = [f"pkg.Target{index}" for index in range(count)]
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=targets)
    batches = ensure_stable_generation_batches(
        manager.db,
        repo_task_id=task_id,
        ordered_target_ids=targets,
        batch_size=2,
        workflow_run_id="old-run",
    )
    manager.mark_stopped(task_id, reason="operator stop")
    return manager, task_id, targets, batches


def _keys(manager, task_id):
    return [row["batch_key"] for row in manager.db.list_class_tasks(task_id)]


def _supersessions(manager, task_id):
    with manager.db.connect() as conn:
        return list(
            conn.execute(
                "SELECT * FROM workflow_run_supersessions WHERE repo_task_id=? "
                "ORDER BY prior_identity_kind, old_workflow_run_id",
                (task_id,),
            )
        )


def test_clean_rerun_replaces_every_key_and_records_exact_audit(tmp_path):
    manager, task_id, _, old_batches = _setup(tmp_path)
    rows = manager.db.list_class_tasks(task_id)
    for row, status in zip(rows, ["PASS", "FAIL", "STOPPED", "CREATED"]):
        manager.db.update_class_task(row["id"], status=status)

    new_run = manager.clean_rerun_generation(
        task_id,
        reason="checkpoint corruption: TASK-456",
        confirm_task_id=task_id,
        requested_by="operator",
    )

    updated = manager.db.list_class_tasks(task_id)
    assert all(key.startswith(f"{new_run}/") for key in _keys(manager, task_id))
    assert {row["status"] for row in updated if row["class_fqn"].endswith(("2", "3"))} == {"QUEUED"}
    assert [row["status"] for row in updated[:2]] == ["PASS", "FAIL"]
    task = manager.db.get_repo_task(task_id)
    assert task["status"] == "QUEUED"
    assert task["resume_count"] == 1

    audits = _supersessions(manager, task_id)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["prior_identity_kind"] == "known"
    assert audit["old_workflow_run_id"] == "old-run"
    assert set(json.loads(audit["old_unit_ids_json"])) == {
        batch.unit_id for batch in old_batches
    }
    assert json.loads(audit["affected_class_ids_json"]) == [row["id"] for row in rows]
    assert audit["requeue_policy"] == "ordinary"
    assert audit["reason"] == "checkpoint corruption: TASK-456"
    assert audit["requested_by"] == "operator"
    events = manager.db.latest_events(task_id, limit=10)
    assert any(row["event_type"] == "generation_cycle_clean_rerun_requested" for row in events)


def test_clean_rerun_transactionally_supersedes_authoritative_legacy_scopes(tmp_path):
    manager, task_id, _, _ = _setup(tmp_path, count=2)
    old_legacy_run = uuid.uuid4().hex
    with manager.db.transaction() as conn:
        conn.executemany(
            "INSERT INTO legacy_prompt_scopes("
            "repo_task_id, legacy_run_id, language, opened_at) VALUES (?, ?, ?, ?)",
            [
                (task_id, old_legacy_run, "java", "2026-08-01T00:00:00+00:00"),
                (task_id, "../bad", "java", "2026-08-01T00:00:01+00:00"),
            ],
        )

    manager.clean_rerun_generation(
        task_id,
        reason="replace legacy invocation",
        confirm_task_id=task_id,
        requested_by="operator",
    )

    rerun = next(
        row
        for row in manager.db.latest_events(task_id, limit=10)
        if row["event_type"] == "generation_cycle_clean_rerun_requested"
    )
    payload = json.loads(rerun["payload_json"])
    assert payload["old_legacy_scopes"] == [
        {"language": "java", "run_id": old_legacy_run}
    ]
    with manager.db.connect() as conn:
        rows = conn.execute(
            "SELECT legacy_run_id, superseded_at, superseded_by_workflow_run_id "
            "FROM legacy_prompt_scopes WHERE repo_task_id=? ORDER BY legacy_run_id",
            (task_id,),
        ).fetchall()
    assert all(row["superseded_at"] for row in rows)
    assert {row["superseded_by_workflow_run_id"] for row in rows} == {
        payload["new_workflow_run_id"]
    }


def test_clean_rerun_force_all_requeues_passing_and_cancelled_rows(tmp_path):
    manager, task_id, _, _ = _setup(tmp_path, count=2)
    for row, status in zip(manager.db.list_class_tasks(task_id), ["PASS", "CANCELLED"]):
        manager.db.update_class_task(row["id"], status=status)

    manager.clean_rerun_generation(
        task_id,
        reason="deliberate replacement",
        confirm_task_id=task_id,
        force_rerun_all=True,
        requested_by="operator",
    )

    assert {row["status"] for row in manager.db.list_class_tasks(task_id)} == {"QUEUED"}
    assert _supersessions(manager, task_id)[0]["requeue_policy"] == "all"


def test_clean_rerun_force_failed_requeues_failures_but_not_pass_or_cancel(tmp_path):
    manager, task_id, _, _ = _setup(tmp_path, count=3)
    for row, status in zip(
        manager.db.list_class_tasks(task_id), ["PASS", "FAIL", "CANCELLED"]
    ):
        manager.db.update_class_task(row["id"], status=status)

    manager.clean_rerun_generation(
        task_id,
        reason="retry failed generation",
        confirm_task_id=task_id,
        force_rerun_failed=True,
        requested_by="operator",
    )

    assert [row["status"] for row in manager.db.list_class_tasks(task_id)] == [
        "PASS",
        "QUEUED",
        "CANCELLED",
    ]
    assert _supersessions(manager, task_id)[0]["requeue_policy"] == "failed"


@pytest.mark.parametrize("status", ["CREATED", "QUEUED", "RUNNING", "STOP_REQUESTED"])
def test_clean_rerun_rejects_ineligible_task_status_without_mutation(tmp_path, status):
    manager, task_id, _, _ = _setup(tmp_path)
    manager.db.update_repo_task(task_id, status=status)
    before = _keys(manager, task_id)

    with pytest.raises(RuntimeError, match="STOPPED or terminal"):
        manager.clean_rerun_generation(
            task_id,
            reason="corrupt",
            confirm_task_id=task_id,
            requested_by="operator",
        )

    assert _keys(manager, task_id) == before
    assert _supersessions(manager, task_id) == []


def test_clean_rerun_requires_reason_confirmation_and_no_active_lease(tmp_path):
    manager, task_id, _, _ = _setup(tmp_path)
    before = _keys(manager, task_id)

    with pytest.raises(ValueError, match="reason"):
        manager.clean_rerun_generation(task_id, reason="", confirm_task_id=task_id)
    with pytest.raises(ValueError, match="confirmation"):
        manager.clean_rerun_generation(task_id, reason="corrupt", confirm_task_id=999)

    manager.db.upsert_heartbeat(
        "runner-1",
        repo_task_id=task_id,
        pid=123,
        hostname="test",
        status="RUNNING",
    )
    with pytest.raises(RuntimeError, match="active runner lease"):
        manager.clean_rerun_generation(
            task_id,
            reason="corrupt",
            confirm_task_id=task_id,
            requested_by="operator",
        )

    assert _keys(manager, task_id) == before


def test_clean_rerun_audits_missing_and_multiple_known_old_runs(tmp_path):
    manager, task_id, _, _ = _setup(tmp_path)
    rows = manager.db.list_class_tasks(task_id)
    manager.db.update_class_task(rows[0]["id"], batch_key=None)
    manager.db.update_class_task(rows[1]["id"], batch_key="old-run/unit-a")
    manager.db.update_class_task(rows[2]["id"], batch_key="other-run/unit-b")
    manager.db.update_class_task(rows[3]["id"], batch_key=None)

    manager.clean_rerun_generation(
        task_id,
        reason="repair identity drift",
        confirm_task_id=task_id,
        requested_by="operator",
    )

    audits = _supersessions(manager, task_id)
    assert {(row["prior_identity_kind"], row["old_workflow_run_id"]) for row in audits} == {
        ("known", "old-run"),
        ("known", "other-run"),
        ("missing", None),
    }
    missing = next(row for row in audits if row["prior_identity_kind"] == "missing")
    assert json.loads(missing["affected_class_ids_json"]) == [rows[0]["id"], rows[3]["id"]]


def test_clean_rerun_all_missing_keys_records_no_invented_prior_lineage(tmp_path):
    manager, task_id, _, _ = _setup(tmp_path, count=2)
    rows = manager.db.list_class_tasks(task_id)
    for row in rows:
        manager.db.update_class_task(row["id"], batch_key=None)

    manager.clean_rerun_generation(
        task_id,
        reason="rebuild missing keys",
        confirm_task_id=task_id,
        requested_by="operator",
    )

    audits = _supersessions(manager, task_id)
    assert len(audits) == 1
    assert audits[0]["prior_identity_kind"] == "missing"
    assert audits[0]["old_workflow_run_id"] is None
    assert json.loads(audits[0]["old_unit_ids_json"]) == []


def test_malformed_old_key_and_mid_transaction_failure_leave_all_keys_unchanged(tmp_path):
    manager, task_id, _, _ = _setup(tmp_path)
    rows = manager.db.list_class_tasks(task_id)
    manager.db.update_class_task(rows[0]["id"], batch_key="malformed")
    malformed = _keys(manager, task_id)

    with pytest.raises(RuntimeError, match="malformed"):
        manager.clean_rerun_generation(
            task_id,
            reason="corrupt",
            confirm_task_id=task_id,
            requested_by="operator",
        )
    assert _keys(manager, task_id) == malformed

    manager.db.update_class_task(rows[0]["id"], batch_key="old-run/unit-a")
    before = _keys(manager, task_id)
    with manager.db.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_supersession BEFORE INSERT ON workflow_run_supersessions
            BEGIN SELECT RAISE(ABORT, 'injected supersession failure'); END
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        manager.clean_rerun_generation(
            task_id,
            reason="corrupt",
            confirm_task_id=task_id,
            requested_by="operator",
        )

    assert _keys(manager, task_id) == before
    assert manager.db.get_repo_task(task_id)["status"] == "STOPPED"


def test_ordinary_resume_even_force_all_preserves_workflow_keys(tmp_path):
    manager, task_id, _, _ = _setup(tmp_path)
    before = _keys(manager, task_id)

    manager.resume_task(task_id, force_rerun_all=True)

    assert _keys(manager, task_id) == before


def test_clean_rerun_cli_requires_explicit_confirmation_and_reports_new_run(tmp_path):
    manager, task_id, _, _ = _setup(tmp_path)
    runner = CliRunner()

    missing_confirmation = runner.invoke(
        main,
        [
            "tasks", "clean-rerun-generation", str(task_id),
            "--reason", "checkpoint corrupt",
            "--task-db", str(manager.db_path),
        ],
    )
    assert missing_confirmation.exit_code != 0
    assert "--confirm-task-id" in missing_confirmation.output

    completed = runner.invoke(
        main,
        [
            "tasks", "clean-rerun-generation", str(task_id),
            "--reason", "checkpoint corrupt",
            "--confirm-task-id", str(task_id),
            "--task-db", str(manager.db_path),
        ],
    )
    assert completed.exit_code == 0, completed.output
    assert "queued with new workflow run" in completed.output

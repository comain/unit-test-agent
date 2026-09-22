import json
import sqlite3

import pytest
from click.testing import CliRunner

from uta.app.cli import main
from uta.tasks.manager import TaskManager
from uta.tasks.scheduler import TaskScheduler


def _set_snapshot(manager: TaskManager, task_id: int, raw: str) -> None:
    manager.db.update_repo_task(task_id, config_snapshot_json=raw)


def test_task_creation_dual_writes_durable_engine_at_shared_boundary(tmp_path):
    manager = TaskManager(tmp_path / "tasks.db")

    task_id = manager.create_task(
        repo_path=str(tmp_path / "repo"),
        class_fqns=["pkg.Target"],
        config_snapshot={"custom": "kept"},
    )

    snapshot = json.loads(manager.get_task(task_id)["config_snapshot_json"])
    assert snapshot["custom"] == "kept"
    assert snapshot["generation_cycle_v2_enabled"] is True
    assert snapshot["generation_engine_version"] == 2


@pytest.mark.parametrize(
    "snapshot",
    [
        {"generation_cycle_v2_enabled": False},
        {"generation_engine_version": True},
        {"generation_engine_version": "2"},
        {"generation_engine_version": 1},
    ],
)
def test_task_creation_rejects_explicit_contradictory_engine_metadata(
    tmp_path, snapshot
):
    manager = TaskManager(tmp_path / "tasks.db")

    with pytest.raises(ValueError, match="generation engine"):
        manager.create_task(
            repo_path=str(tmp_path / "repo"),
            class_fqns=["pkg.Target"],
            config_snapshot=snapshot,
        )

    assert manager.list_tasks() == []


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("{}", "missing_generation_engine_version"),
        ('{"generation_engine_version":true}', "invalid_generation_engine_version_type"),
        ('{"generation_engine_version":"2"}', "invalid_generation_engine_version_type"),
        ('{"generation_engine_version":2.0}', "invalid_generation_engine_version_type"),
        ('{"generation_engine_version":null}', "invalid_generation_engine_version_type"),
        ('{"generation_engine_version":1}', "unsupported_generation_engine_version"),
        ("[]", "config_snapshot_not_object"),
        ("{", "malformed_config_snapshot_json"),
    ],
)
def test_cutover_audit_classifies_legacy_and_invalid_snapshots_read_only(
    tmp_path, raw, reason
):
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(tmp_path / "repo"), class_fqns=["pkg.Target"]
    )
    _set_snapshot(manager, task_id, raw)
    before = dict(manager.get_task(task_id))

    result = CliRunner().invoke(
        main,
        ["tasks", "audit-generation-cutover", "--task-db", str(manager.db_path)],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["scanned_task_count"] == 1
    assert payload["blocker_count"] == 1
    assert payload["blockers"] == [
        {"task_id": task_id, "status": "CREATED", "reason": reason}
    ]
    assert isinstance(payload["elapsed_seconds"], float)
    assert dict(manager.get_task(task_id)) == before


def test_cutover_audit_reports_active_children_under_terminal_history(tmp_path):
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(tmp_path / "repo"), class_fqns=["pkg.Target"]
    )
    manager.db.update_repo_task(task_id, status="COMPLETED")

    result = CliRunner().invoke(
        main,
        ["tasks", "audit-generation-cutover", "--task-db", str(manager.db_path)],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["blockers"] == [
        {
            "task_id": task_id,
            "status": "COMPLETED",
            "reason": "active_class_rows_under_terminal_task",
        }
    ]


@pytest.mark.parametrize(
    ("repo_status", "class_status", "reason"),
    [
        ("UNKNOWN", "PASS", "unknown_repo_task_status"),
        ("COMPLETED", "UNKNOWN", "unknown_class_task_status"),
    ],
)
def test_cutover_audit_blocks_unknown_repo_and_class_statuses(
    tmp_path, repo_status, class_status, reason
):
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(tmp_path / "repo"), class_fqns=["pkg.Target"]
    )
    manager.db.update_repo_task(task_id, status=repo_status)
    class_row = manager.db.find_class_task(task_id, "pkg.Target")
    manager.db.update_class_task(class_row["id"], status=class_status)

    result = CliRunner().invoke(
        main,
        ["tasks", "audit-generation-cutover", "--task-db", str(manager.db_path)],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["blockers"] == [
        {"task_id": task_id, "status": repo_status, "reason": reason}
    ]


def test_cutover_audit_blocks_null_class_status_in_legacy_schema(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE repo_tasks(
                id INTEGER PRIMARY KEY,
                status TEXT,
                config_snapshot_json TEXT
            );
            CREATE TABLE class_tasks(repo_task_id INTEGER, status TEXT);
            INSERT INTO repo_tasks VALUES(
                1, 'COMPLETED',
                '{"generation_engine_version":2,"generation_cycle_v2_enabled":true}'
            );
            INSERT INTO class_tasks VALUES(1, NULL);
            """
        )
    finally:
        connection.close()

    result = CliRunner().invoke(
        main,
        ["tasks", "audit-generation-cutover", "--task-db", str(db_path)],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["blockers"] == [
        {
            "task_id": 1,
            "status": "COMPLETED",
            "reason": "unknown_class_task_status",
        }
    ]


def test_cutover_audit_accepts_durable_tasks_and_terminal_legacy_history(tmp_path):
    manager = TaskManager(tmp_path / "tasks.db")
    durable_id = manager.create_task(
        repo_path=str(tmp_path / "durable"), class_fqns=["pkg.Durable"]
    )
    legacy_id = manager.create_task(
        repo_path=str(tmp_path / "legacy"), class_fqns=["pkg.Legacy"]
    )
    _set_snapshot(manager, legacy_id, "{}")
    manager.db.update_repo_task(legacy_id, status="COMPLETED")
    legacy_class = manager.db.find_class_task(legacy_id, "pkg.Legacy")
    manager.db.update_class_task(legacy_class["id"], status="PASS")

    result = CliRunner().invoke(
        main,
        ["tasks", "audit-generation-cutover", "--task-db", str(manager.db_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scanned_task_count"] == 2
    assert payload["blocker_count"] == 0
    assert payload["blockers"] == []
    assert manager.get_task(durable_id)["status"] == "CREATED"
    assert manager.get_task(legacy_id)["status"] == "COMPLETED"


def test_scheduler_refuses_legacy_before_acquire_mutation(tmp_path):
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(tmp_path / "repo"), class_fqns=["pkg.Target"]
    )
    _set_snapshot(manager, task_id, "{}")

    with pytest.raises(RuntimeError, match="submit a new task"):
        TaskScheduler(str(manager.db_path)).acquire_next()

    assert manager.get_task(task_id)["status"] == "CREATED"


def test_direct_run_refuses_legacy_before_running_status(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.Target"])
    _set_snapshot(manager, task_id, "{}")

    result = CliRunner().invoke(
        main,
        ["run", "--task-id", str(task_id), "--task-db", str(manager.db_path)],
    )

    assert result.exit_code != 0
    assert "submit a new task" in result.output
    assert manager.get_task(task_id)["status"] == "CREATED"


@pytest.mark.parametrize("operation", ["resume", "clean_rerun", "unblock"])
def test_mutating_requeue_operations_refuse_terminal_legacy_history(tmp_path, operation):
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(tmp_path / operation), class_fqns=["pkg.Target"]
    )
    _set_snapshot(manager, task_id, "{}")
    manager.db.update_repo_task(task_id, status="FAILED", error="old failure")
    before = dict(manager.get_task(task_id))

    with pytest.raises(RuntimeError, match="submit a new task"):
        if operation == "resume":
            manager.resume_task(task_id, force_rerun_failed=True)
        elif operation == "clean_rerun":
            manager.clean_rerun_generation(
                task_id,
                reason="operator requested",
                confirm_task_id=task_id,
                force_rerun_failed=True,
            )
        else:
            manager.unblock(task_id)

    after = dict(manager.get_task(task_id))
    assert after["status"] == before["status"] == "FAILED"
    assert after["config_snapshot_json"] == before["config_snapshot_json"]
    assert after["resume_count"] == before["resume_count"]
    assert after["error"] == before["error"]


def test_audit_orders_blockers_by_task_id(tmp_path):
    manager = TaskManager(tmp_path / "tasks.db")
    ids = [
        manager.create_task(
            repo_path=str(tmp_path / f"repo-{index}"), class_fqns=[f"pkg.T{index}"]
        )
        for index in range(3)
    ]
    for task_id in reversed(ids):
        _set_snapshot(manager, task_id, "{}")

    result = CliRunner().invoke(
        main,
        ["tasks", "audit-generation-cutover", "--task-db", str(manager.db_path)],
    )

    assert result.exit_code == 2, result.output
    assert [item["task_id"] for item in json.loads(result.output)["blockers"]] == ids


def test_low_level_requeue_guard_closes_manager_check_then_mutate_race(
    tmp_path, monkeypatch
):
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(tmp_path / "repo"), class_fqns=["pkg.Target"]
    )
    durable_row = manager.get_task(task_id)
    manager.db.update_repo_task(task_id, config_snapshot_json="{}")
    monkeypatch.setattr(manager.db, "get_repo_task", lambda _task_id: durable_row)

    with pytest.raises(RuntimeError, match="submit a new task"):
        manager.resume_task(task_id)

    actual = TaskManager(manager.db_path).get_task(task_id)
    assert actual["status"] == "CREATED"
    assert actual["config_snapshot_json"] == "{}"

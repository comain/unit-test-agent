import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from uta.app.cli import main
from uta.shared.config import settings
from uta.tasks.manager import TaskManager
from uta.tasks.render import build_status_payload, html_for_payload
from uta.tasks.scheduler import TaskScheduler


@pytest.fixture(autouse=True)
def reset_opencode_model_health():
    from agent_core.harness import tiered_router

    tiered_router._tracker.reset()
    tiered_router.reset_model_availability_cache()
    yield
    tiered_router._tracker.reset()
    tiered_router.reset_model_availability_cache()


def test_create_task_reuses_same_repo_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")

    first_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"], branch_name=None)
    second_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.B"], branch_name=None)

    first = manager.get_task(first_id)
    second = manager.get_task(second_id)
    assert first["branch_name"] == second["branch_name"]
    assert [row["class_fqn"] for row in manager.list_class_tasks(first_id)] == ["pkg.A"]
    assert [row["class_fqn"] for row in manager.list_class_tasks(second_id)] == ["pkg.B"]


def test_requeue_orphaned_running_task_with_stale_runner_heartbeat(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.mark_running(task_id, stage="mutation_fix", detail="stale opencode turn")
    class_row = manager.db.find_class_task(task_id, "pkg.A")
    assert class_row is not None
    manager.db.update_class_task(
        class_row["id"],
        status="RUNNING",
        stage="mutation_fix",
        current_stage="mutation_fix",
        current_detail="stale opencode turn",
    )
    with manager.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO runner_heartbeats(
                runner_id, repo_task_id, current_repo_task_id, pid, hostname,
                status, message, started_at, heartbeat_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "dead-host:123",
                task_id,
                task_id,
                123,
                "dead-host",
                "RUNNING",
                "task subprocess running",
                "2026-06-12T07:00:00+00:00",
                "2026-06-12T07:00:00+00:00",
                "2026-06-12T07:00:00+00:00",
                "2026-06-12T07:00:00+00:00",
            ),
        )

    recovered = manager.requeue_orphaned_running_tasks(
        active_task_ids=[],
        stale_after_seconds=60,
        now=datetime(2026, 6, 12, 7, 10, tzinfo=timezone.utc),
    )

    assert recovered == [task_id]
    task = manager.get_task(task_id)
    assert task["status"] == "QUEUED"
    assert task["current_stage"] == "queued"
    assert "stale runner heartbeat" in task["current_detail"]
    class_row = manager.db.find_class_task(task_id, "pkg.A")
    assert class_row["status"] == "QUEUED"
    assert class_row["current_stage"] == "queued"
    events = manager.db.latest_events(task_id, limit=5)
    assert any(row["event_type"] == "orphaned_task_requeued" for row in events)


def test_requeue_daemon_shutdown_task_marks_running_rows_queued(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.mark_running(task_id, stage="python_fix_compile", detail="child running")
    class_row = manager.db.find_class_task(task_id, "pkg.A")
    manager.db.update_class_task(
        class_row["id"],
        status="RUNNING",
        stage="python_fix_compile",
        current_stage="python_fix_compile",
        current_detail="child running",
    )

    recovered = manager.requeue_daemon_shutdown_tasks([task_id], reason="daemon exiting")

    assert recovered == [task_id]
    task = manager.get_task(task_id)
    assert task["status"] == "QUEUED"
    assert task["current_stage"] == "queued"
    assert "daemon shutdown" in task["current_detail"]
    class_row = manager.db.find_class_task(task_id, "pkg.A")
    assert class_row["status"] == "QUEUED"
    assert class_row["current_stage"] == "queued"
    events = manager.db.latest_events(task_id, limit=5)
    assert any(row["event_type"] == "daemon_shutdown_requeued" for row in events)


def test_scheduler_can_probe_empty_queue_without_overwriting_active_heartbeat(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    scheduler = TaskScheduler(str(tmp_path / "tasks.db"), runner_id="runner-1")

    assert scheduler.acquire_next()["id"] == task_id
    assert scheduler.acquire_next(record_idle_heartbeat=False) is None

    heartbeat = manager.db.latest_heartbeat()
    assert heartbeat["runner_id"] == "runner-1"
    assert heartbeat["status"] == "RUNNING"
    assert heartbeat["current_repo_task_id"] == task_id


def test_scheduler_does_not_reacquire_task_already_in_daemon_pool(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    scheduler = TaskScheduler(str(manager.db.path), runner_id="host:daemon")

    first = scheduler.acquire_next()
    assert first["id"] == task_id

    # Provider fallback may queue the same task before its current subprocess exits.
    manager.db.update_repo_task(task_id, status="QUEUED", current_stage="provider_fallback")

    assert scheduler.acquire_next(exclude_task_ids={task_id}) is None
    assert manager.get_task(task_id)["status"] == "QUEUED"


def test_duplicate_repair_identity_ignores_rdc_run_id_and_workspace(tmp_path):
    repo_a = tmp_path / "workspace-a" / "demo"
    repo_b = tmp_path / "workspace-b" / "demo"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    manager = TaskManager(tmp_path / "tasks.db")

    def create_repair(repo_path: Path, task_id: str) -> int:
        return manager.create_task(
            repo_path=str(repo_path),
            class_fqns=["pkg.A"],
            branch_name="feature/TASK-1",
            base_ref="origin/master",
            quality_mode="ci_incremental",
            quality_gate_backend="test_enforcer",
            rdc_context={
                "pipeline": {
                    "taskId": task_id,
                    "gitUrl": "git@git.example.com:group/demo.git",
                    "appName": "demo",
                }
            },
        )

    first_id = create_repair(repo_a, "rdc-run-1")
    second_id = create_repair(repo_b, "rdc-run-2")

    assert manager.find_active_duplicate_repair_task(second_id) == first_id
    assert (
        manager.find_active_duplicate_repair_task_for_targets(
            repo_identity="git@git.example.com:group/demo.git",
            branch_name="feature/TASK-1",
            base_ref="origin/master",
            language="java",
            quality_gate_backend="test_enforcer",
            target_ids=["pkg.A"],
        )
        == first_id
    )


def test_duplicate_repair_reuses_active_superset_targets(tmp_path):
    repo_a = tmp_path / "workspace-a" / "demo"
    repo_b = tmp_path / "workspace-b" / "demo"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    manager = TaskManager(tmp_path / "tasks.db")

    def create_repair(repo_path: Path, targets: list[str]) -> int:
        return manager.create_task_targets(
            repo_path=str(repo_path),
            targets=[
                {
                    "target_id": target,
                    "display_name": target.removeprefix("pyfile:"),
                    "source_path": target.removeprefix("pyfile:"),
                    "language": "python",
                    "granularity": "file",
                }
                for target in targets
            ],
            language="python",
            branch_name="feature/TASK-1",
            base_ref="origin/master",
            quality_mode="ci_incremental",
            quality_gate_backend="python_enforcer",
            rdc_context={
                "pipeline": {
                    "taskId": "same-rdc-task",
                    "gitUrl": "git@git.example.com:group/demo.git",
                    "appName": "demo",
                }
            },
        )

    first_id = create_repair(
        repo_a,
        [
            "pyfile:pipecat/main.py",
            "pyfile:pipecat/router/router.py",
            "pyfile:pipecat/security/config.py",
        ],
    )
    subset_id = create_repair(
        repo_b,
        [
            "pyfile:pipecat/router/router.py",
            "pyfile:pipecat/security/config.py",
        ],
    )
    unrelated_id = create_repair(repo_b, ["pyfile:pipecat/llm/intent_classifier.py"])

    assert manager.find_active_duplicate_repair_task(subset_id) == first_id
    assert manager.find_active_duplicate_repair_task(unrelated_id) is None
    assert (
        manager.find_active_duplicate_repair_task_for_targets(
            repo_identity="git@git.example.com:group/demo.git",
            branch_name="feature/TASK-1",
            base_ref="origin/master",
            language="python",
            quality_gate_backend="python_enforcer",
            target_ids=[
                "pyfile:pipecat/router/router.py",
                "pyfile:pipecat/security/config.py",
            ],
        )
        == first_id
    )


def test_create_task_can_force_new_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")

    first_id = manager.create_task(repo_path=str(repo))
    second_id = manager.create_task(repo_path=str(repo), new_branch=True)

    assert manager.get_task(first_id)["branch_name"] != manager.get_task(second_id)["branch_name"]


def test_scheduler_acquires_by_priority_and_blocks_same_repo(tmp_path):
    repo = tmp_path / "repo"
    other_repo = tmp_path / "other"
    repo.mkdir()
    other_repo.mkdir()
    db_path = tmp_path / "tasks.db"
    manager = TaskManager(db_path)
    slow_id = manager.create_task(repo_path=str(repo), priority=100)
    fast_id = manager.create_task(repo_path=str(repo), priority=10)
    other_id = manager.create_task(repo_path=str(other_repo), priority=20)
    manager.start_task(slow_id)
    manager.start_task(fast_id)
    manager.start_task(other_id)

    scheduler = TaskScheduler(str(db_path), runner_id="test-runner")
    first = scheduler.acquire_next()
    second = scheduler.acquire_next()

    assert first["id"] == fast_id
    assert second["id"] == other_id
    assert manager.get_task(slow_id)["status"] == "QUEUED"


def test_resume_clears_stale_repo_error(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    class_id = manager.list_class_tasks(task_id)[0]["id"]
    manager.db.update_class_task(class_id, status="UNSAFE_DIFF", error="unsafe diff")
    manager._refresh_repo_counts(task_id)
    manager.mark_failed(task_id, "unsafe diff", stage="branch_safety")
    assert manager.get_task(task_id)["failed_classes"] == 1

    manager.resume_task(task_id, force_rerun_all=True)
    resumed = manager.get_task(task_id)
    assert resumed["status"] == "QUEUED"
    assert resumed["last_error"] is None
    assert resumed["error"] is None
    assert resumed["completed_classes"] == 0
    assert resumed["failed_classes"] == 0

    manager.mark_running(task_id)
    running = manager.get_task(task_id)
    assert running["status"] == "RUNNING"
    assert running["last_error"] is None
    assert running["error"] is None


def test_record_stage_does_not_overwrite_terminal_class_stage(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    class_id = manager.list_class_tasks(task_id)[0]["id"]

    manager.db.update_class_task(
        class_id,
        status="PASS",
        current_stage="finished",
        stage="finished",
        current_detail="PASS",
    )

    manager.record_stage(task_id, "select_batch", detail="choose next batch", class_fqns=["pkg.A"])

    row = manager.db.get_class_task(class_id)
    assert row["status"] == "PASS"
    assert row["current_stage"] == "finished"
    assert row["stage"] == "finished"


def test_sync_results_and_live_status_include_sessions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.mark_running(task_id)
    TaskScheduler(str(tmp_path / "tasks.db"), runner_id="runner-1").heartbeat(
        repo_task_id=task_id,
        status="RUNNING",
        message="active",
        config_snapshot={"model": "x"},
    )

    manager.sync_results(
        task_id,
        {
            "pkg.A": {
                "status": "PASS",
                "line_coverage": 88.0,
                "mutation_score": 72.0,
                "test_file_path": "src/test/java/pkg/ATest.java",
                "test_file_content": "@Test\nvoid a() {}\n@Test\nvoid b() {}",
                "session_ids": ["ses_1", "ses_2"],
            }
        },
        session_token_usage={"total_tokens": {"input": 10, "output": 2, "cache_read": 4, "cache_write": 1}},
        elapsed_seconds=12.5,
    )

    payload = build_status_payload(manager.db, task_id)
    assert payload["task"]["status"] == "COMPLETED"
    assert payload["task"]["actual_input_tokens"] == 10
    assert payload["task"]["input_tokens"] == 10
    assert payload["metrics"]["cache_hit_ratio"] > 0
    assert payload["latest_heartbeat"]["runner_id"] == "runner-1"
    assert payload["metrics"]["remaining_estimated_tokens"] is None
    assert payload["task"]["config_snapshot_hash"]
    assert payload["classes"][0]["test_count"] == 2
    assert payload["classes"][0]["session_ids"] == ["ses_1", "ses_2"]


def test_sync_results_exposes_test_quality_warnings_in_status_payload(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task_targets(
        repo_path=str(repo),
        targets=[
            {
                "target_id": "pyfile:pkg.py",
                "display_name": "pkg.py",
                "source_path": "pkg.py",
                "language": "python",
                "granularity": "file",
            }
        ],
        language="python",
    )
    manager.mark_running(task_id)

    manager.sync_results(
        task_id,
        {
            "pyfile:pkg.py": {
                "language": "python",
                "status": "PASS",
                "line_coverage": 100.0,
                "mutation_score": 100.0,
                "testQuality": {
                    "warningCount": 1,
                    "warnings": [
                        {
                            "ruleId": "python-weak-assert-not-none",
                            "message": "Primary assertions only check existence.",
                            "filePath": "tests/test_pkg.py",
                            "line": 7,
                        }
                    ],
                },
            }
        },
    )

    payload = build_status_payload(manager.db, task_id)
    row = payload["classes"][0]

    assert row["test_quality"]["warningCount"] == 1
    assert row["test_quality_warning"] == "python-weak-assert-not-none: Primary assertions only check existence."
    assert "python-weak-assert-not-none" in html_for_payload(payload)


def test_sync_results_marks_hard_capped_python_mutation_display(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task_targets(
        repo_path=str(repo),
        targets=[
            {
                "target_id": "pyfile:pkg.py",
                "display_name": "pkg.py",
                "source_path": "pkg.py",
                "language": "python",
                "granularity": "file",
            }
        ],
        language="python",
    )
    manager.mark_running(task_id)

    manager.sync_results(
        task_id,
        {
            "pyfile:pkg.py": {
                "language": "python",
                "status": "PASS",
                "line_coverage": 100.0,
                "mutation_score": 84.0954,
                "mutation_summary": {
                    "candidatePlan": {
                        "generationPolicy": {
                            "truncated": True,
                            "omittedByCap": 42,
                            "caps": {"maxSelected": 100},
                            "selectedOpportunities": 100,
                        }
                    }
                },
            }
        },
    )

    row = manager.list_class_tasks(task_id)[0]
    payload = build_status_payload(manager.db, task_id)

    assert row["mutation_score"] == 84.0954
    assert row["mutation_detail"] == "84.0954(hard capped 100)"
    assert payload["classes"][0]["mutation_display"] == "84.0954(hard capped 100)"
    assert "84.0954(hard capped 100)" in html_for_payload(payload)


def test_sync_results_merges_resume_sessions_and_tokens(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])

    manager.sync_results(
        task_id,
        {
            "pkg.A": {
                "status": "PASS",
                "session_ids": ["ses_1"],
                "phase_token_usage": {
                    "plan": {"input": 10, "output": 2, "cache_read": 5, "cache_write": 0, "reasoning": 1}
                },
            }
        },
    )
    manager.sync_results(
        task_id,
        {
            "pkg.A": {
                "status": "PASS",
                "session_ids": ["ses_2"],
                "phase_token_usage": {
                    "generate": {"input": 20, "output": 3, "cache_read": 7, "cache_write": 0, "reasoning": 2}
                },
            }
        },
    )

    payload = build_status_payload(manager.db, task_id)
    task = payload["task"]
    cls = payload["classes"][0]
    assert cls["session_ids"] == ["ses_1", "ses_2"]
    assert task["session_ids"] == ["ses_1", "ses_2"]
    assert cls["input_tokens"] == 30
    assert cls["output_tokens"] == 5
    assert cls["cache_read_tokens"] == 12
    assert cls["reasoning_tokens"] == 3
    assert cls["total_tokens"] == 50
    assert task["input_tokens"] == 30
    assert task["cache_read_tokens"] == 12
    assert task["total_tokens"] == 50
    assert task["actual_cost"] is None


def test_sync_results_marks_repo_failed_when_setup_fails_before_class_tasks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo))

    manager.sync_results(task_id, {}, final_error="Baseline compilation failed")

    row = manager.get_task(task_id)
    assert row["status"] == "FAILED"
    assert row["current_stage"] == "finished"
    assert row["last_error"] == "Baseline compilation failed"


def test_status_payload_prefers_actual_cost_when_provider_cost_is_zero(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.db.update_repo_task(
        task_id,
        input_tokens=100,
        output_tokens=10,
        cache_read_tokens=50,
        estimated_cost_usd=10.0,
        actual_cost=1.25,
        provider_cost_usd=0.0,
    )

    payload = build_status_payload(manager.db, task_id)
    assert payload["metrics"]["actual_cost"] == 1.25
    expected_budget_pct = (1.25 / (10.0 * float(settings.budget_hard_cap_multiplier))) * 100.0
    assert abs(float(payload["metrics"]["budget_used_pct"]) - expected_budget_pct) < 0.000001


def test_sync_results_splits_shared_batch_tokens_across_classes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])

    manager.sync_results(
        task_id,
        {
            "pkg.A": {"status": "PASS", "session_ids": ["ses_batch"]},
            "pkg.B": {"status": "PASS", "session_ids": ["ses_batch"]},
        },
        session_token_usage={
            "total_tokens": {"input": 11, "output": 5, "cache_read": 7, "cache_write": 0, "reasoning": 3}
        },
    )

    payload = build_status_payload(manager.db, task_id)
    classes = {row["class_fqn"]: row for row in payload["classes"]}
    assert payload["task"]["input_tokens"] == 11
    assert payload["task"]["output_tokens"] == 5
    assert payload["task"]["cache_read_tokens"] == 7
    assert payload["task"]["reasoning_tokens"] == 3
    assert payload["task"]["total_tokens"] == 26
    assert classes["pkg.A"]["input_tokens"] == 6
    assert classes["pkg.B"]["input_tokens"] == 5
    assert classes["pkg.A"]["output_tokens"] == 3
    assert classes["pkg.B"]["output_tokens"] == 2
    assert classes["pkg.A"]["phase_token_usage_json"]


def test_tasks_cli_create_list_show_watch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    runner = CliRunner()

    create = runner.invoke(
        main,
        [
            "tasks",
            "create",
            "--repo",
            str(repo),
            "--class-fqn",
            "pkg.A",
            "--task-db",
            str(db_path),
        ],
    )
    assert create.exit_code == 0, create.output
    assert "Created task 1" in create.output

    list_result = runner.invoke(main, ["tasks", "list", "--task-db", str(db_path)])
    assert list_result.exit_code == 0, list_result.output
    assert "repo" in list_result.output

    show_result = runner.invoke(main, ["tasks", "show", "1", "--sessions", "--task-db", str(db_path)])
    assert show_result.exit_code == 0, show_result.output
    assert "pkg.A" in show_result.output

    watch_result = runner.invoke(main, ["tasks", "watch", "1", "--once", "--task-db", str(db_path)])
    assert watch_result.exit_code == 0, watch_result.output
    assert "Task 1" in watch_result.output


def test_tasks_cli_watch_missing_task_lists_available_ids(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    TaskManager(db_path).create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    runner = CliRunner()

    result = runner.invoke(main, ["tasks", "watch", "2", "--once", "--task-db", str(db_path)])

    assert result.exit_code != 0
    assert "Repo task 2 not found" in result.output
    assert "Available repo task ids: 1" in result.output


def test_tasks_cli_watch_compacts_large_class_lists_unless_detail(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    class_fqns = [f"pkg.C{i:03d}" for i in range(30)]
    TaskManager(db_path).create_task(repo_path=str(repo), class_fqns=class_fqns)
    runner = CliRunner()

    compact = runner.invoke(main, ["tasks", "watch", "1", "--once", "--task-db", str(db_path)])

    assert compact.exit_code == 0, compact.output
    assert "classes total=30" in compact.output
    assert "class rows: showing 25 of 30" in compact.output
    assert "use --detail to show all" in compact.output
    assert "pkg.C000" in compact.output
    assert "pkg.C029" not in compact.output

    detailed = runner.invoke(main, ["tasks", "watch", "1", "--once", "--detail", "--task-db", str(db_path)])

    assert detailed.exit_code == 0, detailed.output
    assert "class rows: showing all" in detailed.output
    assert "pkg.C029" in detailed.output


def test_tasks_cli_summary_outputs_tables_and_json(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    manager = TaskManager(db_path)
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"], config_snapshot={"opencode_model": "gpt-5.4"})
    row = manager.list_class_tasks(task_id)[0]
    manager.db.update_class_task(
        row["id"],
        status="PASS",
        stage="finished",
        current_stage="finished",
        coverage_line=95.0,
        mutation_score=88.0,
        total_mutants=8,
        surviving_mutants=1,
        test_file_path="src/test/java/pkg/ATest.java",
        module="",
        input_tokens=10,
        output_tokens=2,
        cache_read_tokens=5,
        reasoning_tokens=1,
        total_tokens=18,
        session_ids_json=json.dumps(["ses-a"]),
    )
    manager.db.update_repo_task(
        task_id,
        status="COMPLETED",
        started_at="2026-05-01T00:00:00+00:00",
        finished_at="2026-05-01T00:05:00+00:00",
    )

    opencode_db = tmp_path / "opencode.db"
    conn = sqlite3.connect(opencode_db)
    try:
        conn.execute("create table session(id text, directory text, title text, time_created integer)")
        conn.execute("create table message(id text, session_id text, time_created integer, data text)")
        conn.execute(
            "insert into session(id, directory, title, time_created) values (?, ?, ?, ?)",
            ("ses-a", str(repo), "JUnit 4 test for A", 1746057600000),
        )
        conn.execute(
            "insert into message(id, session_id, time_created, data) values (?, ?, ?, ?)",
            (
                "m-a",
                "ses-a",
                1746057601000,
                json.dumps({"info": {"role": "assistant", "tokens": {"input": 10, "output": 2, "reasoning": 1, "total": 18, "cache": {"read": 5, "write": 0}}}}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr("uta.language.java.coverage_recompute.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.language.java.coverage_recompute.find_jacoco_report", lambda *args, **kwargs: str(opencode_db))
    monkeypatch.setattr(
        "uta.language.java.coverage_recompute.parse_jacoco_line_coverage_for_classes",
        lambda *args, **kwargs: {"line": 91.0, "covered_lines": 91, "missed_lines": 9, "matched_classes": 1},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["tasks", "summary", str(task_id), "--task-db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "Task 1 Summary" in result.output
    assert "Recorded Token And Cost Accounting" in result.output
    assert "Accounting Coverage" in result.output
    assert "Project Coverage Recalculation" in result.output

    json_result = runner.invoke(main, ["tasks", "summary", str(task_id), "--json", "--task-db", str(db_path)])
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["classes"]["generated"] == 1
    assert payload["tokens"]["recorded"]["total"] == 18
    assert payload["tokens"]["recorded"]["cost_provenance"] == "unavailable"
    assert payload["coverage"]["total"] == 91.0


def test_tasks_cli_summary_marks_unmatched_project_coverage_recalc(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    manager = TaskManager(db_path)
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"], config_snapshot={"opencode_model": "gpt-5.4"})
    row = manager.list_class_tasks(task_id)[0]
    manager.db.update_class_task(
        row["id"],
        status="PASS",
        stage="finished",
        current_stage="finished",
        coverage_line=95.0,
        mutation_score=88.0,
        total_mutants=8,
        surviving_mutants=1,
        test_file_path="src/test/java/pkg/ATest.java",
        module="",
    )
    manager.db.update_repo_task(task_id, status="COMPLETED")

    monkeypatch.setattr("uta.language.java.coverage_recompute.run_tests_with_jacoco_batch", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr("uta.language.java.coverage_recompute.find_jacoco_report", lambda *args, **kwargs: "/tmp/jacoco.xml")
    monkeypatch.setattr(
        "uta.language.java.coverage_recompute.parse_jacoco_line_coverage_for_classes",
        lambda *args, **kwargs: {"line": 0.0, "covered_lines": 0, "missed_lines": 0, "matched_classes": 0},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["tasks", "summary", str(task_id), "--task-db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "Matched Classes" in result.output
    assert "did not match any target classes" in result.output


def test_schema_contains_plan_fields_and_marks_unmeasured_estimates_unavailable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    manager = TaskManager(db_path)
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])
    task = manager.get_task(task_id)

    assert task["estimate_snapshot_json"]
    assert task["estimated_total_tokens"] is None
    assert task["estimated_cost_usd"] is None
    with manager.db.connect() as conn:
        repo_cols = {row["name"] for row in conn.execute("PRAGMA table_info(repo_tasks)")}
        class_cols = {row["name"] for row in conn.execute("PRAGMA table_info(class_tasks)")}
        event_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_events)")}
        control_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_control)")}
        heartbeat_cols = {row["name"] for row in conn.execute("PRAGMA table_info(runner_heartbeats)")}
    assert {"budget_config_snapshot_json", "latest_commit", "remote_ref", "budget_used_ratio", "session_ids_json"} <= repo_cols
    assert {"phase_token_usage_json", "llm_turn_count", "commit_sha", "pushed_at"} <= class_cols
    assert "ts" in event_cols
    assert {"requested_action", "handled_at"} <= control_cols
    assert "current_repo_task_id" in heartbeat_cols


def test_stop_resume_and_class_priority(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])
    rows = manager.list_class_tasks(task_id)
    manager.reprioritize_class(rows[1]["id"], 1)
    manager.stop_task(task_id, reason="operator")
    assert manager.check_stop_requested(task_id) == "operator"
    manager.mark_stopped(task_id, reason="operator")
    assert manager.get_task(task_id)["status"] == "STOPPED"
    manager.resume_task(task_id)
    assert manager.get_task(task_id)["status"] == "QUEUED"
    class_rows = manager.list_class_tasks(task_id)
    assert class_rows[0]["class_fqn"] == "pkg.B"
    assert class_rows[0]["priority"] == 1


def test_resume_refuses_pending_stop_until_runner_acknowledges(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.stop_task(task_id, reason="operator stop")
    assert manager.check_stop_requested(task_id) == "operator stop"

    with pytest.raises(RuntimeError, match="stop is still pending"):
        manager.resume_task(task_id)

    task = manager.get_task(task_id)
    assert task["status"] == "STOP_REQUESTED"
    assert manager.check_stop_requested(task_id) == "operator stop"

    manager.mark_stopped(task_id, reason="operator stop")
    manager.resume_task(task_id)

    assert manager.get_task(task_id)["status"] == "QUEUED"
    assert manager.check_stop_requested(task_id) is None


def test_resume_force_rerun_failed_and_scheduler_include_failed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    manager = TaskManager(db_path)
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])
    rows = manager.list_class_tasks(task_id)
    manager.db.update_class_task(rows[0]["id"], status="PASS")
    manager.db.update_class_task(rows[1]["id"], status="FAIL")
    manager.db.update_repo_task(task_id, status="FAILED")

    assert TaskScheduler(str(db_path)).acquire_next() is None
    acquired = TaskScheduler(str(db_path)).acquire_next(include_failed=True)
    assert acquired["id"] == task_id

    manager.resume_task(task_id, force_rerun_failed=True)
    rows = {row["class_fqn"]: row["status"] for row in manager.list_class_tasks(task_id)}
    assert rows["pkg.A"] == "PASS"
    assert rows["pkg.B"] == "QUEUED"


def test_push_failure_preserved_after_sync(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.record_push_failed(task_id, branch_name="uta/test", message="push denied", class_fqns=["pkg.A"])

    manager.sync_results(
        task_id,
        {"pkg.A": {"status": "PASS", "line_coverage": 90.0, "mutation_score": 80.0}},
    )

    assert manager.get_task(task_id)["status"] == "FAILED"
    assert manager.list_class_tasks(task_id)[0]["status"] == "PUSH_FAILED"


def test_poisoned_status_preserved_after_final_sync_with_queued_rows(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])
    manager.mark_poisoned(task_id, "Auto-quarantined after repeated push failures")

    manager.sync_results(task_id, {}, final_error="RDC repair auto-push found no test changes to commit")

    task = manager.get_task(task_id)
    assert task["status"] == "POISONED"
    assert task["current_stage"] == "finished"
    assert task["error"] == "RDC repair auto-push found no test changes to commit"


def test_provider_error_status_is_terminal(tmp_path):
    from uta.tasks.models import TERMINAL_CLASS_STATUSES

    db_path = tmp_path / "tasks.db"
    manager = TaskManager(str(db_path))
    task_id = manager.create_task(repo_path=str(tmp_path / "repo"), class_fqns=["pkg.A"])

    manager.sync_results(task_id, {"pkg.A": {"status": "PROVIDER_ERROR", "coverage": 0.0}})

    task = manager.db.get_repo_task(task_id)
    row = manager.list_class_tasks(task_id)[0]
    assert row["status"] == "PROVIDER_ERROR"
    assert row["status"] in TERMINAL_CLASS_STATUSES
    assert task["completed_classes"] == 1


def test_tasks_cli_manifest_and_priority_option(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": [{"repo": str(repo), "class_fqns": ["pkg.A"], "priority": 50}]}))
    runner = CliRunner()

    created = runner.invoke(main, ["tasks", "create-manifest", "--manifest", str(manifest), "--task-db", str(db_path)])
    assert created.exit_code == 0, created.output
    assert "Created 1 task" in created.output

    reprio = runner.invoke(main, ["tasks", "reprioritize", "1", "--priority", "5", "--task-db", str(db_path)])
    assert reprio.exit_code == 0, reprio.output
    assert TaskManager(db_path).get_task(1)["priority"] == 5


def test_schema_migration_is_idempotent(tmp_path):
    from uta.tasks.db import TaskDB

    db = TaskDB(tmp_path / "tasks.db")
    db.init()
    db.init()  # second call must not raise or corrupt data
    with db.connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"repo_tasks", "class_tasks", "task_events", "task_control", "runner_heartbeats", "repo_branches"} <= tables


def test_cancel_cancels_not_yet_started_work(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])
    running = next(row for row in manager.list_class_tasks(task_id) if row["class_fqn"] == "pkg.A")
    manager.db.update_class_task(running["id"], status="RUNNING", current_stage="python_verify")

    manager.cancel_task(task_id, reason="operator cancel")

    task = manager.get_task(task_id)
    assert task["status"] == "CANCELLED"
    statuses = {row["class_fqn"]: row["status"] for row in manager.list_class_tasks(task_id)}
    assert statuses["pkg.A"] == "CANCELLED"
    assert statuses["pkg.B"] == "CANCELLED"


def test_explicit_branch_name_is_respected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")

    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"], branch_name="uta/explicit-branch")

    assert manager.get_task(task_id)["branch_name"] == "uta/explicit-branch"


def test_push_verified_detects_remote_mismatch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.mark_running(task_id)

    manager.record_push_verified(task_id, branch_name="uta/test", local_head="abc123", remote_head="def456")

    task = manager.get_task(task_id)
    assert task["status"] == "FAILED"
    assert "mismatch" in (task["error"] or "")

    events = manager.db.latest_events(task_id, limit=50)
    assert any(e["event_type"] == "push_failed" for e in events)


def test_push_verified_preserves_terminal_success_status(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.mark_completed(task_id, message="done")

    manager.record_push_verified(task_id, branch_name="uta/test", local_head="abc123", remote_head="abc123")

    task = manager.get_task(task_id)
    assert task["status"] == "COMPLETED"
    assert task["latest_commit"] == "abc123"
    assert task["remote_ref"] == "abc123"


def test_push_verified_recovers_push_failed_task(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    manager.mark_running(task_id)
    manager.record_push_failed(task_id, branch_name="uta/test", message="auth failed", class_fqns=["pkg.A"])
    manager.db.update_repo_task(task_id, current_stage="finished", current_detail="auth failed")

    manager.record_push_verified(task_id, branch_name="uta/test", local_head="abc123", remote_head="abc123")

    task = manager.get_task(task_id)
    class_task = manager.db.find_class_task(task_id, "pkg.A")
    assert task["status"] == "COMPLETED"
    assert task["current_stage"] == "finished"
    assert task["error"] is None
    assert task["last_error"] is None
    assert class_task["status"] == "PASS"
    assert class_task["commit_sha"] == "abc123"
    assert class_task["pushed_at"]

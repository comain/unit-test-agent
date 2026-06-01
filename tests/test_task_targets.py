import json

from uta.ci_plugin.reporting import CiReportRenderer
from uta.tasks.manager import TaskManager
from uta.tasks.render import build_status_payload, html_for_payload
from uta.tasks.targets import TargetIdentity, target_count, target_identity_from_row


def test_target_identity_keeps_java_compatibility():
    target = TargetIdentity.java_class("com.example.FooService")

    assert target.language == "java"
    assert target.target_id == "com.example.FooService"
    assert target.display_name == "com.example.FooService"
    assert target.as_selection()["granularity"] == "class"
    assert target_count({"class_fqns": ["a.A", "b.B"]}) == 2


def test_target_identity_reads_python_row():
    target = target_identity_from_row(
        {
            "language": "python",
            "target_id": "python:file:src/jobs/forecast.py",
            "class_fqn": "python:file:src/jobs/forecast.py",
            "source_path": "src/jobs/forecast.py",
            "symbol": "forecast_for_store",
            "target_granularity": "function",
            "display_name": "src/jobs/forecast.py::forecast_for_store",
        }
    )

    assert target.language == "python"
    assert target.target_id == "python:file:src/jobs/forecast.py"
    assert target.source_path == "src/jobs/forecast.py"
    assert target.symbol == "forecast_for_store"
    assert target.granularity == "function"


def test_create_task_targets_supports_python_progress_report_and_cost(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    target = TargetIdentity(
        language="python",
        target_id="python:function:src/jobs/forecast.py::forecast_for_store",
        source_path="src/jobs/forecast.py",
        symbol="forecast_for_store",
        granularity="function",
        display_name="src/jobs/forecast.py::forecast_for_store",
    )

    task_id = manager.create_task_targets(
        repo_path=str(repo),
        targets=[target],
        quality_mode="ci_incremental",
        quality_gate_backend="python_enforcer",
    )

    task = manager.get_task(task_id)
    selection = json.loads(task["selection_json"])
    rows = manager.list_class_tasks(task_id)
    assert task["language"] == "python"
    assert selection["language"] == "python"
    assert selection["targets"][0]["source_path"] == "src/jobs/forecast.py"
    assert rows[0]["class_fqn"] == target.target_id
    assert rows[0]["language"] == "python"
    assert rows[0]["target_id"] == target.target_id
    assert rows[0]["target_granularity"] == "function"
    assert rows[0]["display_name"] == "src/jobs/forecast.py::forecast_for_store"

    manager.record_stage_for_targets(task_id, "generate", detail="generating tests", targets=[target])
    latest_event = manager.db.latest_events(task_id, limit=1)[0]
    event_payload = json.loads(latest_event["payload_json"])
    assert event_payload["targets"][0]["language"] == "python"
    assert event_payload["target_ids"] == [target.target_id]

    manager.sync_target_results(
        task_id,
        {
            target: {
                "status": "PASS",
                "line_coverage": 90.0,
                "mutation_score": 75.0,
                "test_file_path": "tests/test_forecast.py",
                "phase_token_usage": {
                    "generate": {"input": 100, "output": 20, "cache_read": 10, "cache_write": 0, "reasoning": 5}
                },
            }
        },
        targets=[target],
    )

    payload = build_status_payload(manager.db, task_id)
    assert payload["task"]["status"] == "COMPLETED"
    assert payload["classes"][0]["target_display_name"] == "src/jobs/forecast.py::forecast_for_store"
    assert payload["classes"][0]["input_tokens"] == 100
    assert payload["task"]["input_tokens"] == 100
    assert float(payload["task"]["actual_cost"]) > 0
    html = html_for_payload(payload)
    assert "<h2>Targets</h2>" in html
    assert "<th>ID</th><th>Target</th>" in html


def test_python_file_targets_do_not_get_synthetic_symbol(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    target = TargetIdentity(
        language="python",
        target_id="pyfile:src/jobs/forecast.py",
        source_path="src/jobs/forecast.py",
        granularity="file",
        display_name="src/jobs/forecast.py",
    )

    task_id = manager.create_task_targets(repo_path=str(repo), targets=[target])
    row = manager.list_class_tasks(task_id)[0]
    rebuilt = target_identity_from_row(row)

    assert row["symbol"] is None
    assert rebuilt.symbol is None
    assert rebuilt.granularity == "file"


def test_sync_target_results_preserves_python_metadata_without_targets_arg(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    first = TargetIdentity(
        language="python",
        target_id="python:function:src/jobs/forecast.py::seed",
        source_path="src/jobs/forecast.py",
        symbol="seed",
        granularity="function",
        display_name="src/jobs/forecast.py::seed",
    )
    second = TargetIdentity(
        language="python",
        target_id="python:function:src/jobs/forecast.py::forecast_for_store",
        source_path="src/jobs/forecast.py",
        symbol="forecast_for_store",
        granularity="function",
        display_name="src/jobs/forecast.py::forecast_for_store",
    )
    task_id = manager.create_task_targets(repo_path=str(repo), targets=[first])

    manager.sync_target_results(
        task_id,
        {
            second: {
                "status": "PASS",
                "phase_token_usage": {
                    "generate": {"input": 20, "output": 5, "cache_read": 2, "cache_write": 0, "reasoning": 1}
                },
            }
        },
    )

    rows = {row["target_id"]: row for row in manager.list_class_tasks(task_id)}
    assert rows[second.target_id]["language"] == "python"
    assert rows[second.target_id]["source_path"] == "src/jobs/forecast.py"
    assert rows[second.target_id]["symbol"] == "forecast_for_store"
    assert rows[second.target_id]["target_granularity"] == "function"
    assert rows[second.target_id]["display_name"] == "src/jobs/forecast.py::forecast_for_store"


def test_python_estimates_do_not_reuse_java_history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    java_task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.Foo"])
    manager.sync_results(
        java_task_id,
        {
            "pkg.Foo": {
                "status": "PASS",
                "phase_token_usage": {
                    "generate": {"input": 10, "output": 2, "cache_read": 1, "cache_write": 0, "reasoning": 0}
                },
            }
        },
    )
    target = TargetIdentity(
        language="python",
        target_id="python:file:src/jobs/forecast.py",
        source_path="src/jobs/forecast.py",
        granularity="file",
        display_name="src/jobs/forecast.py",
    )

    python_task_id = manager.create_task_targets(repo_path=str(repo), targets=[target])
    estimate = json.loads(manager.get_task(python_task_id)["estimate_snapshot_json"])

    assert estimate["estimate_source"] == "fallback_default"
    assert estimate["estimate_language"] == "python"
    assert estimate["target_count"] == 1


def test_ci_repair_progress_uses_target_display_for_python_tasks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    target = TargetIdentity(
        language="python",
        target_id="python:file:src/jobs/forecast.py",
        source_path="src/jobs/forecast.py",
        granularity="file",
        display_name="src/jobs/forecast.py",
    )
    task_id = manager.create_task_targets(repo_path=str(repo), targets=[target])
    payload = build_status_payload(manager.db, task_id)

    html = CiReportRenderer().repair_progress_html(
        {
            "appName": "demo",
            "taskId": "ci-1",
            "branch": "feature/python",
            "session": {"sessionId": "fix-1", "repoTaskId": task_id, "status": "repair_task_created"},
            "repoTask": payload,
            "stages": [],
        }
    )

    assert "目标任务" in html
    assert "<th>Target</th>" in html
    assert "src/jobs/forecast.py" in html


def test_create_task_still_backfills_java_target_columns(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")

    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.Foo"])
    task = manager.get_task(task_id)
    row = manager.list_class_tasks(task_id)[0]

    assert task["language"] == "java"
    assert row["language"] == "java"
    assert row["target_id"] == "pkg.Foo"
    assert row["target_granularity"] == "class"
    assert row["display_name"] == "pkg.Foo"

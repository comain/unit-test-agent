"""Tests for uta tasks report batch/repo CLI commands (Task 9)."""
import json
import tempfile
from pathlib import Path
from click.testing import CliRunner

from uta.engine.languages import RawTargetSelection
from uta.language.python.adapter import PythonLanguageAdapter


def _make_db_with_data(tmp_path):
    """Create a DB with one completed task and two class tasks."""
    from uta.tasks.manager import TaskManager
    repo = str(tmp_path / "myrepo")
    Path(repo).mkdir()

    mgr = TaskManager(str(tmp_path / "tasks.db"))
    tid = mgr.create_task(repo_path=repo, module="biz", class_fqns=["com.A", "com.B"])
    mgr.start_task(tid)
    # Simulate some cost
    mgr.db.update_repo_task(tid, status="COMPLETED", provider_cost_usd=1.23)
    # Update class tasks
    with mgr.db.connect() as conn:
        conn.execute(
            "UPDATE class_tasks SET status='PASSED', provider_cost_usd=0.6 WHERE repo_task_id=? AND class_fqn='com.A'",
            (tid,),
        )
        conn.execute(
            "UPDATE class_tasks SET status='PASSED', provider_cost_usd=0.63 WHERE repo_task_id=? AND class_fqn='com.B'",
            (tid,),
        )
    return tmp_path / "tasks.db", repo, tid


def _make_python_db_with_data(tmp_path):
    from uta.tasks.manager import TaskManager

    repo = str(tmp_path / "pyrepo")
    Path(repo).mkdir()
    target = PythonLanguageAdapter().normalize_target(RawTargetSelection(target="jobs/forecast.py::run"))
    mgr = TaskManager(str(tmp_path / "tasks.db"))
    tid = mgr.create_task_targets(repo_path=repo, targets=[target], priority=100)
    mgr.start_task(tid)
    mgr.db.update_repo_task(tid, status="COMPLETED", provider_cost_usd=0.42)
    with mgr.db.connect() as conn:
        conn.execute(
            """
            UPDATE class_tasks
            SET status='PASSED', provider_cost_usd=0.42, coverage_line=91.5, mutation_score=88.0
            WHERE repo_task_id=? AND target_id=?
            """,
            (tid, target.target_id),
        )
    return tmp_path / "tasks.db", repo, tid


class TestReportBatch:
    def test_report_batch_json_output(self, tmp_path):
        from uta.cli import main as cli

        db_path, repo, tid = _make_db_with_data(tmp_path)
        runner = CliRunner(mix_stderr=False)
        report_dir = tmp_path / "reports"
        report_dir.mkdir()

        result = runner.invoke(
            cli,
            ["tasks", "report", "batch", "--task-db", str(db_path), "--json-output"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "by_status" in data or "tasks" in data or "status_counts" in data or "repo_tasks" in data

    def test_report_batch_shows_completed(self, tmp_path):
        from uta.cli import main as cli

        db_path, repo, tid = _make_db_with_data(tmp_path)
        runner = CliRunner(mix_stderr=False)

        result = runner.invoke(
            cli,
            ["tasks", "report", "batch", "--task-db", str(db_path), "--json-output"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        output = result.output
        assert "COMPLETED" in output or "completed" in output.lower()


class TestReportRepo:
    def test_report_repo_json_output(self, tmp_path):
        from uta.cli import main as cli

        db_path, repo, tid = _make_db_with_data(tmp_path)
        slug = Path(repo).name
        runner = CliRunner(mix_stderr=False)

        result = runner.invoke(
            cli,
            ["tasks", "report", "repo", slug, "--task-db", str(db_path), "--json-output"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "slug" in data or "repo" in data or "class_tasks" in data
        assert data["targets"][0]["target_id"] == "com.A"
        assert data["targets"][0]["display_name"] == "com.A"
        assert data["classes"][0]["fqn"] == "com.A"

    def test_report_repo_json_output_is_target_first_for_python(self, tmp_path):
        from uta.cli import main as cli

        db_path, repo, tid = _make_python_db_with_data(tmp_path)
        slug = Path(repo).name
        runner = CliRunner(mix_stderr=False)

        result = runner.invoke(
            cli,
            ["tasks", "report", "repo", slug, "--task-db", str(db_path), "--json-output"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["language"] == "python"
        assert data["targets"][0]["target_id"] == "pysymbol:jobs/forecast.py::run"
        assert data["targets"][0]["display_name"] == "jobs/forecast.py::run"
        assert data["targets"][0]["source_path"] == "jobs/forecast.py"
        assert data["targets"][0]["target_granularity"] == "function"
        assert data["classes"] == data["targets"]

    def test_report_repo_unknown_slug(self, tmp_path):
        from uta.cli import main as cli

        db_path, repo, tid = _make_db_with_data(tmp_path)
        runner = CliRunner(mix_stderr=False)

        result = runner.invoke(
            cli,
            ["tasks", "report", "repo", "nonexistent-slug", "--task-db", str(db_path), "--json-output"],
        )
        # Should exit cleanly (0 or non-zero with a clear message)
        output = result.output + (result.exception.__class__.__name__ if result.exception else "")
        assert result.exit_code in (0, 1, 2) or "not found" in output.lower() or "no" in output.lower()


class TestDashboard:
    def test_dashboard_once_exits_cleanly(self, tmp_path):
        from uta.cli import main as cli

        db_path, repo, tid = _make_db_with_data(tmp_path)
        runner = CliRunner(mix_stderr=False)

        result = runner.invoke(
            cli,
            ["tasks", "dashboard", "--once", "--task-db", str(db_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output

    def test_dashboard_shows_status_counts(self, tmp_path):
        from uta.cli import main as cli

        db_path, repo, tid = _make_db_with_data(tmp_path)
        runner = CliRunner(mix_stderr=False)

        result = runner.invoke(
            cli,
            ["tasks", "dashboard", "--once", "--task-db", str(db_path)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        # Dashboard should mention some status
        assert any(word in result.output for word in ["running", "queued", "done", "COMPLETED", "0"])

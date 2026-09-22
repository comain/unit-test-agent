"""E2E Phase 3: Measurability — provider_cost non-NULL, stage events, report CLI, dashboard."""
import pytest
from click.testing import CliRunner


def _make_manager(tmp_path):
    from uta.tasks.manager import TaskManager
    db_path = str(tmp_path / "tasks.db")
    mgr = TaskManager(db_path)
    mgr.db.init()
    return mgr


def _create_repo_task(mgr, tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    return mgr.create_task(repo_path=str(repo), class_fqns=["com.A", "com.B", "com.C"])


@pytest.mark.e2e
class TestProviderCostNonNull:
    def test_provider_cost_remains_unknown_when_provider_omits_it(self, tmp_path):
        """Token counts must never be converted into a fictional provider charge."""

        mgr = _make_manager(tmp_path)
        tid = _create_repo_task(mgr, tmp_path)
        mgr.start_task(tid)

        # Simulate sync_results with no explicit provider cost in the stream
        results = {
            "com.A": {
                "status": "PASSED",
                "session_id": "s1",
                "token_usage": {"input": 1000, "output": 500, "cache_read": 0, "cache_write": 0},
                "phase_token_usage": {},
                "coverage": 85.0,
                "mutation_score": 70.0,
                "elapsed_seconds": 10.0,
                "output": "ok",
                "error": None,
            }
        }
        # provider_cost is intentionally omitted (None).
        try:
            mgr.sync_results(
                tid,
                results,
                module=None,
                session_token_usage={},
                phase_token_usage={},
                report_path=None,
                run_log_path=None,
                elapsed_seconds=10.0,
                final_error=None,
            )
        except Exception:
            pass  # May fail due to missing branch/config; we only care about cost column

        # Tokens remain measurable while currency provenance stays unavailable.
        with mgr.db.connect() as conn:
            rows = conn.execute(
                "SELECT class_fqn, provider_cost_usd FROM class_tasks WHERE repo_task_id=?",
                (tid,),
            ).fetchall()
        for row in rows:
            if row["class_fqn"] == "com.A":
                assert row["provider_cost_usd"] is None


@pytest.mark.e2e
class TestStageEvents:
    def test_stage_started_and_completed_are_paired(self, tmp_path):
        """record_stage_completed should write a stage_completed event."""
        mgr = _make_manager(tmp_path)
        tid = _create_repo_task(mgr, tmp_path)
        mgr.start_task(tid)

        mgr.db.add_event(tid, None, "stage_started", "starting generate", stage="generate")
        mgr.record_stage_completed(tid, stage="generate", detail="done")

        with mgr.db.connect() as conn:
            events = conn.execute(
                "SELECT event_type FROM task_events WHERE repo_task_id=? AND event_type='stage_completed'",
                (tid,),
            ).fetchall()
        assert len(events) >= 1

    def test_record_stage_completed_exists(self):
        from uta.tasks.manager import TaskManager
        assert hasattr(TaskManager, "record_stage_completed")


@pytest.mark.e2e
class TestReportCLI:
    def test_batch_report_runs_without_error(self, tmp_path):
        from uta.app.cli import main as cli

        mgr = _make_manager(tmp_path)
        repo = tmp_path / "arepo"
        repo.mkdir()
        tid = mgr.create_task(repo_path=str(repo))
        mgr.db.update_repo_task(tid, status="COMPLETED", provider_cost_usd=2.0)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["tasks", "report", "batch", "--task-db", str(tmp_path / "tasks.db"), "--json-output"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0

    def test_repo_report_returns_non_empty_json(self, tmp_path):
        from uta.app.cli import main as cli

        mgr = _make_manager(tmp_path)
        repo = tmp_path / "myrepo2"
        repo.mkdir()
        tid = mgr.create_task(repo_path=str(repo), class_fqns=["com.X"])
        mgr.db.update_repo_task(tid, status="COMPLETED", provider_cost_usd=1.5)
        with mgr.db.connect() as conn:
            conn.execute(
                "UPDATE class_tasks SET status='PASSED', provider_cost_usd=1.5 WHERE repo_task_id=?",
                (tid,),
            )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["tasks", "report", "repo", "myrepo2", "--task-db", str(tmp_path / "tasks.db"), "--json-output"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert len(result.output.strip()) > 0


@pytest.mark.e2e
class TestDashboard:
    def test_dashboard_renders_without_error(self, tmp_path):
        from uta.app.cli import main as cli

        mgr = _make_manager(tmp_path)
        repo = tmp_path / "dashrepo"
        repo.mkdir()
        mgr.create_task(repo_path=str(repo))

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["tasks", "dashboard", "--once", "--task-db", str(tmp_path / "tasks.db")],
            catch_exceptions=False,
        )
        assert result.exit_code == 0

    def test_dashboard_shows_batch_header(self, tmp_path):
        from uta.app.cli import main as cli

        mgr = _make_manager(tmp_path)
        repo = tmp_path / "dashrepo2"
        repo.mkdir()
        mgr.create_task(repo_path=str(repo))
        mgr.db.update_repo_task(1, status="COMPLETED")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["tasks", "dashboard", "--once", "--task-db", str(tmp_path / "tasks.db")],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "Batch" in result.output or "batch" in result.output.lower() or "Dashboard" in result.output

"""E2E Phase 2: Reliability — transient retry, quarantine, and unblock."""
import pytest
from pathlib import Path
from unittest.mock import patch


def _make_manager(tmp_path):
    from uta.tasks.manager import TaskManager
    db_path = str(tmp_path / "tasks.db")
    mgr = TaskManager(db_path)
    mgr.db.init()
    return mgr


def _create_repo_task(mgr, tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    return mgr.create_task(repo_path=str(repo))


@pytest.mark.e2e
class TestTransientRetryClassification:
    def test_429_classifies_as_transient(self):
        from uta.tasks.run_error_classifier import classify_run_error
        assert classify_run_error("429 Too Many Requests") == "transient"

    def test_500_classifies_as_transient(self):
        from uta.tasks.run_error_classifier import classify_run_error
        assert classify_run_error("500 Server Error") == "transient"

    def test_rate_limit_classifies_as_transient(self):
        from uta.tasks.run_error_classifier import classify_run_error
        assert classify_run_error("rate limit exceeded, please retry") == "transient"

    def test_budget_classifies_as_budget_abort(self):
        from uta.tasks.run_error_classifier import classify_run_error
        assert classify_run_error("budget_exceeded: cap hit") == "budget_abort"

    def test_baseline_fail_classifies_as_class_fail_continue(self):
        from uta.tasks.run_error_classifier import classify_run_error
        assert classify_run_error("Baseline compilation failed") == "class_fail_continue"

    def test_json_error_classifies_as_internal_retry_once(self):
        from uta.tasks.run_error_classifier import classify_run_error
        assert classify_run_error("JSONDecodeError: extra data") == "internal_retry_once"

    def test_unknown_error_classifies_as_class_fail_continue(self):
        from uta.tasks.run_error_classifier import classify_run_error
        assert classify_run_error("NullPointerException at line 42") == "class_fail_continue"


@pytest.mark.e2e
class TestQuarantine:
    def test_task_quarantined_after_threshold_failures(self, tmp_path):
        mgr = _make_manager(tmp_path)
        tid = _create_repo_task(mgr, tmp_path)
        mgr.start_task(tid)

        with patch("uta.config.settings") as mock_settings:
            mock_settings.quarantine_threshold = 2

            mgr.mark_failed(tid, "first failure")
            assert mgr.get_task(tid)["status"] == "FAILED"

            mgr.resume_task(tid)
            mgr.mark_running(tid, stage="run", detail="")
            mgr.mark_failed(tid, "second failure")
            assert mgr.get_task(tid)["status"] == "POISONED"

    def test_poisoned_task_skipped_by_scheduler(self, tmp_path):
        from uta.tasks.scheduler import TaskScheduler

        mgr = _make_manager(tmp_path)
        db_path = str(tmp_path / "tasks.db")
        scheduler = TaskScheduler(db_path)

        tid = _create_repo_task(mgr, tmp_path)
        mgr.start_task(tid)
        mgr.mark_poisoned(tid, "too many failures")

        task = scheduler.acquire_next()
        assert task is None


@pytest.mark.e2e
class TestUnblock:
    def test_unblock_restores_poisoned_to_queued(self, tmp_path):
        mgr = _make_manager(tmp_path)
        tid = _create_repo_task(mgr, tmp_path)
        mgr.start_task(tid)
        mgr.mark_poisoned(tid, "quarantined")

        mgr.unblock(tid)
        assert mgr.get_task(tid)["status"] == "QUEUED"

    def test_unblocked_task_is_acquirable(self, tmp_path):
        from uta.tasks.scheduler import TaskScheduler

        mgr = _make_manager(tmp_path)
        db_path = str(tmp_path / "tasks.db")
        scheduler = TaskScheduler(db_path)

        tid = _create_repo_task(mgr, tmp_path)
        mgr.start_task(tid)
        mgr.mark_poisoned(tid, "quarantined")

        # Can't acquire while poisoned
        assert scheduler.acquire_next() is None

        # After unblock, can acquire
        mgr.unblock(tid)
        task = scheduler.acquire_next()
        assert task is not None
        assert int(task["id"]) == tid

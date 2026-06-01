"""Tests for task quarantine, budget_exceeded, and unblock (Tasks 5+6)."""
import pytest
import sqlite3
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_db(tmp_path):
    from uta.tasks.db import TaskDB
    db_path = str(tmp_path / "test.db")
    db = TaskDB(db_path)
    db.init()
    return db


def _make_manager(tmp_path):
    from uta.tasks.manager import TaskManager
    db_path = str(tmp_path / "test.db")
    mgr = TaskManager(db_path)
    mgr.db.init()
    return mgr


def _create_task(mgr, tmp_path):
    repo = str(tmp_path / "repo")
    Path(repo).mkdir()
    return mgr.create_task(repo_path=repo, module=None)


class TestMarkPoisoned:
    def test_mark_poisoned_sets_status(self, tmp_path):
        mgr = _make_manager(tmp_path)
        tid = _create_task(mgr, tmp_path)
        mgr.start_task(tid)
        mgr.mark_poisoned(tid, "auto-quarantine after 2 failures")
        task = mgr.get_task(tid)
        assert task["status"] == "POISONED"

    def test_mark_poisoned_records_event(self, tmp_path):
        mgr = _make_manager(tmp_path)
        tid = _create_task(mgr, tmp_path)
        mgr.start_task(tid)
        mgr.mark_poisoned(tid, "test poison")
        with mgr.db.connect() as conn:
            events = conn.execute(
                "SELECT event_type FROM task_events WHERE repo_task_id=? AND event_type='task_poisoned'",
                (tid,),
            ).fetchall()
        assert len(events) >= 1


class TestMarkBudgetExceeded:
    def test_mark_budget_exceeded_sets_status(self, tmp_path):
        mgr = _make_manager(tmp_path)
        tid = _create_task(mgr, tmp_path)
        mgr.start_task(tid)
        mgr.mark_budget_exceeded(tid, "cap exceeded")
        task = mgr.get_task(tid)
        assert task["status"] == "BUDGET_EXCEEDED"

    def test_mark_budget_exceeded_records_event(self, tmp_path):
        mgr = _make_manager(tmp_path)
        tid = _create_task(mgr, tmp_path)
        mgr.start_task(tid)
        mgr.mark_budget_exceeded(tid, "cap exceeded")
        with mgr.db.connect() as conn:
            events = conn.execute(
                "SELECT event_type FROM task_events WHERE repo_task_id=? AND event_type='task_budget_exceeded'",
                (tid,),
            ).fetchall()
        assert len(events) >= 1


class TestAutoQuarantine:
    def test_auto_quarantine_after_threshold(self, tmp_path):
        """mark_failed should poison the task after quarantine_threshold failures."""
        mgr = _make_manager(tmp_path)
        tid = _create_task(mgr, tmp_path)
        mgr.start_task(tid)

        with patch("uta.config.settings") as mock_settings:
            mock_settings.quarantine_threshold = 2
            # First failure — should stay FAILED
            mgr.mark_failed(tid, "error #1")
            task = mgr.get_task(tid)
            assert task["status"] in ("FAILED",), f"Expected FAILED, got {task['status']}"

            # Resume and fail again
            mgr.resume_task(tid)
            mgr.mark_running(tid, stage="test", detail="")
            mgr.mark_failed(tid, "error #2")
            task = mgr.get_task(tid)
            # After 2 failures, should be POISONED
            assert task["status"] == "POISONED", f"Expected POISONED after threshold, got {task['status']}"

    def test_no_quarantine_when_threshold_zero(self, tmp_path):
        mgr = _make_manager(tmp_path)
        tid = _create_task(mgr, tmp_path)
        mgr.start_task(tid)

        with patch("uta.config.settings") as mock_settings:
            mock_settings.quarantine_threshold = 0
            mgr.mark_failed(tid, "error")
            task = mgr.get_task(tid)
            assert task["status"] == "FAILED"


class TestUnblock:
    def test_unblock_poisoned_task(self, tmp_path):
        mgr = _make_manager(tmp_path)
        tid = _create_task(mgr, tmp_path)
        mgr.start_task(tid)
        mgr.mark_poisoned(tid, "bad")
        assert mgr.get_task(tid)["status"] == "POISONED"

        mgr.unblock(tid)
        assert mgr.get_task(tid)["status"] == "QUEUED"

    def test_unblock_budget_exceeded_task(self, tmp_path):
        mgr = _make_manager(tmp_path)
        tid = _create_task(mgr, tmp_path)
        mgr.start_task(tid)
        mgr.mark_budget_exceeded(tid, "cap")
        assert mgr.get_task(tid)["status"] == "BUDGET_EXCEEDED"

        mgr.unblock(tid)
        assert mgr.get_task(tid)["status"] == "QUEUED"


class TestAcquireSkipsPoisoned:
    def test_acquire_skips_poisoned(self, tmp_path):
        from uta.tasks.scheduler import TaskScheduler
        db_path = str(tmp_path / "test.db")
        scheduler = TaskScheduler(db_path)
        mgr = _make_manager(tmp_path)

        repo = str(tmp_path / "repo")
        Path(repo).mkdir()
        tid = mgr.create_task(repo_path=repo)
        mgr.start_task(tid)
        mgr.mark_poisoned(tid, "auto-poison")

        task = scheduler.acquire_next()
        assert task is None, "POISONED task should not be acquired"

    def test_acquire_skips_budget_exceeded(self, tmp_path):
        from uta.tasks.scheduler import TaskScheduler
        db_path = str(tmp_path / "test.db")
        scheduler = TaskScheduler(db_path)
        mgr = _make_manager(tmp_path)

        repo = str(tmp_path / "repo")
        Path(repo).mkdir()
        tid = mgr.create_task(repo_path=repo)
        mgr.start_task(tid)
        mgr.mark_budget_exceeded(tid, "cap")

        task = scheduler.acquire_next()
        assert task is None, "BUDGET_EXCEEDED task should not be acquired"

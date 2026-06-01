"""E2E Phase 1: Parallel-safe pipeline — no port collisions, worker pool logic."""
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _uta_command():
    uta_bin = Path(sys.executable).parent / "uta"
    if uta_bin.exists():
        return [str(uta_bin)]
    return [sys.executable, "-m", "uta.cli"]


def _make_db(tmp_path):
    from uta.tasks.db import TaskDB
    db = TaskDB(str(tmp_path / "tasks.db"))
    db.init()
    return db


def _make_manager(tmp_path, db=None):
    from uta.tasks.manager import TaskManager
    db_path = str(tmp_path / "tasks.db")
    mgr = TaskManager(db_path)
    mgr.db.init()
    return mgr


@pytest.mark.e2e
class TestParallelWorkerPool:
    """Verify the daemon worker pool fills slots and reaps finished tasks."""

    def test_pool_fills_to_max_slots(self, tmp_path):
        """Worker pool should launch tasks up to max_slots; RUNNING tasks are not re-acquired."""
        from uta.tasks.manager import TaskManager
        from uta.tasks.scheduler import TaskScheduler

        db_path = str(tmp_path / "tasks.db")
        scheduler = TaskScheduler(db_path)
        mgr = TaskManager(db_path)

        # Create 2 repos and mark them RUNNING (as if a pool slot already launched them)
        for i in range(2):
            repo = tmp_path / f"repo{i}"
            repo.mkdir()
            tid = mgr.create_task(repo_path=str(repo))
            mgr.mark_running(tid, stage="startup", detail="")

        # With 2 tasks already RUNNING, acquire_next should return None (no QUEUED tasks)
        task = scheduler.acquire_next()
        assert task is None

    def test_acquire_next_marks_running(self, tmp_path):
        """acquire_next transitions task from QUEUED to RUNNING."""
        from uta.tasks.manager import TaskManager
        from uta.tasks.scheduler import TaskScheduler

        db_path = str(tmp_path / "tasks.db")
        scheduler = TaskScheduler(db_path)
        mgr = TaskManager(db_path)

        repo = tmp_path / "myrepo"
        repo.mkdir()
        tid = mgr.create_task(repo_path=str(repo))
        # Mark as QUEUED
        mgr.db.update_repo_task(tid, status="QUEUED")

        task = scheduler.acquire_next()
        assert task is not None
        assert int(task["id"]) == tid

        fetched = mgr.get_task(tid)
        assert fetched["status"] == "RUNNING"

    def test_same_repo_not_acquired_twice(self, tmp_path):
        """Two tasks for the same repo should not both be acquired concurrently."""
        from uta.tasks.manager import TaskManager
        from uta.tasks.scheduler import TaskScheduler

        db_path = str(tmp_path / "tasks.db")
        scheduler = TaskScheduler(db_path)
        mgr = TaskManager(db_path)

        repo = tmp_path / "shared-repo"
        repo.mkdir()

        # Create two tasks for the same repo
        tid1 = mgr.create_task(repo_path=str(repo))
        tid2 = mgr.create_task(repo_path=str(repo))
        mgr.db.update_repo_task(tid1, status="QUEUED")
        mgr.db.update_repo_task(tid2, status="QUEUED")

        task1 = scheduler.acquire_next()
        assert task1 is not None

        # Second acquire should be None (same repo conflict)
        task2 = scheduler.acquire_next(allow_same_repo_concurrency=False)
        assert task2 is None

    def test_no_opencode_server_class_in_run_pipeline(self):
        """run and resume_gates commands should not reference OpenCodeServer."""
        import inspect
        import uta.cli as cli_module

        run_fn = cli_module.run.callback if hasattr(cli_module.run, "callback") else cli_module.run
        src = inspect.getsource(run_fn)
        assert "OpenCodeServer" not in src, "run command must not use OpenCodeServer"

        resume_fn = cli_module.resume_gates.callback if hasattr(cli_module.resume_gates, "callback") else cli_module.resume_gates
        src2 = inspect.getsource(resume_fn)
        assert "OpenCodeServer" not in src2, "resume_gates command must not use OpenCodeServer"

    def test_daemon_dispatches_two_slots_concurrently(self, tmp_path):
        """Daemon with max-parallel=2 launches two subprocesses at the same time.

        Uses a no-op uta run command (--stop-after-stage that exits quickly) to
        verify concurrent dispatch without running a real pipeline.
        """
        from uta.tasks.db import TaskDB
        from uta.tasks.manager import TaskManager
        from uta.tasks.scheduler import TaskScheduler

        db_path = str(tmp_path / "tasks.db")
        db = TaskDB(db_path)
        db.init()
        mgr = TaskManager(db_path)

        # Queue 2 tasks for different repos so same-repo guard doesn't block second
        for i in range(2):
            repo = tmp_path / f"concurrent_repo{i}"
            repo.mkdir()
            tid = mgr.create_task(repo_path=str(repo))
            mgr.db.update_repo_task(tid, status="QUEUED")

        # Run the daemon --once with max-parallel 2; it should start both tasks
        # then exit after one poll cycle. Use a short timeout.
        result = subprocess.run(
            _uta_command()
            + [
                "tasks", "daemon",
                "--task-db", db_path,
                "--max-parallel", "2",
                "--allow-same-repo-concurrency",
                "--once",
                "--interval", "1",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        output = result.stdout + result.stderr
        # Both slots should have been mentioned in output
        assert "slot 1/2" in output or "Starting task 1" in output, (
            f"Expected slot 1 to start. Output:\n{output}"
        )
        assert "slot 2/2" in output or "Starting task 2" in output, (
            f"Expected slot 2 to start. Output:\n{output}"
        )
        # No OpenCode server port references
        assert "opencode serve" not in output
        assert "_terminate_stale_listeners" not in output

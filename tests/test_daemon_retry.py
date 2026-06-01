"""Tests for daemon retry logic wired to classify_run_error (Task fix)."""
import time
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call


def _make_manager(tmp_path):
    from uta.tasks.manager import TaskManager
    db_path = str(tmp_path / "tasks.db")
    mgr = TaskManager(db_path)
    mgr.db.init()
    return mgr


def _create_running_task(mgr, tmp_path, name="repo", last_error=None):
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    tid = mgr.create_task(repo_path=str(repo))
    mgr.mark_running(tid, stage="run", detail="")
    if last_error:
        mgr.db.update_repo_task(tid, last_error=last_error)
    return tid


def _make_dead_proc(rc: int) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = rc
    proc.returncode = rc
    return proc


def _build_daemon_closures(tmp_path, max_slots=2):
    """Extract the daemon's internal closures for unit testing without starting subprocesses."""
    import sys
    from uta.tasks.scheduler import TaskScheduler
    from uta.tasks.manager import TaskManager
    from uta.config import settings
    from uta.tasks.run_error_classifier import classify_run_error

    db_path = str(tmp_path / "tasks.db")
    scheduler = TaskScheduler(db_path)
    manager = TaskManager(db_path)

    pool: dict = {}
    pending_retries: dict = {}
    _TRANSIENT_BACKOFF_SECONDS = [30, 60, 120]
    _TRANSIENT_MAX_RETRIES = 3

    def _launch(task_id: int, retry_count: int = 0, proc_override=None) -> None:
        if proc_override is not None:
            proc = proc_override
        else:
            proc = MagicMock(spec=subprocess.Popen)
            proc.poll.return_value = None  # still running
        pool[task_id] = {"proc": proc, "last_heartbeat": 0.0, "retry_count": retry_count}

    def _queue_retry(tid: int, retry_count: int, *, backoff_seconds: float = 0.0) -> None:
        manager.db.update_repo_task(tid, status="QUEUED", current_stage="queued_retry", current_detail=f"retry {retry_count}")
        manager.db.add_event(tid, None, "task_retry_queued", f"Retry {retry_count}", stage="queued_retry")
        pending_retries[tid] = {"retry_after": time.monotonic() + backoff_seconds, "retry_count": retry_count}

    def _reap_finished() -> None:
        for tid in list(pool.keys()):
            proc = pool[tid]["proc"]
            rc = proc.poll()
            if rc is None:
                continue
            return_code = int(rc)
            retry_count = pool[tid].get("retry_count", 0)
            del pool[tid]

            if return_code != 0:
                task_row = manager.db.get_repo_task(tid)
                error_msg = (task_row["last_error"] or "") if task_row else ""
                if not error_msg:
                    error_msg = f"uta run exited with status {return_code}"
                policy = classify_run_error(error_msg)

                if policy == "budget_abort":
                    manager.mark_budget_exceeded(tid, error_msg)
                elif policy == "transient" and retry_count < _TRANSIENT_MAX_RETRIES:
                    backoff = _TRANSIENT_BACKOFF_SECONDS[min(retry_count, len(_TRANSIENT_BACKOFF_SECONDS) - 1)]
                    _queue_retry(tid, retry_count + 1, backoff_seconds=backoff)
                elif policy == "internal_retry_once" and retry_count < 1:
                    _queue_retry(tid, 1, backoff_seconds=0)
                else:
                    manager.mark_failed(tid, error_msg, stage="runner_exit")

    def _promote_ready_retries() -> None:
        now = time.monotonic()
        for tid in list(pending_retries.keys()):
            entry = pending_retries[tid]
            if now >= entry["retry_after"] and len(pool) < max_slots:
                del pending_retries[tid]
                _launch(tid, retry_count=entry["retry_count"])

    return manager, pool, pending_retries, _launch, _reap_finished, _promote_ready_retries


class TestTransientRetry:
    def test_transient_error_queues_retry(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path, last_error="429 Too Many Requests")
        dead = _make_dead_proc(rc=1)
        _launch(tid, proc_override=dead)

        _reap()

        assert tid not in pool
        assert tid in pending_retries
        assert pending_retries[tid]["retry_count"] == 1
        assert mgr.db.get_repo_task(tid)["status"] == "QUEUED"

    def test_transient_retry_three_times_then_fail(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path, last_error="rate limit exceeded")

        # Simulate 3 transient failures with immediate backoff promotion each time
        for attempt in range(3):
            dead = _make_dead_proc(rc=1)
            pool[tid] = {"proc": dead, "last_heartbeat": 0.0, "retry_count": attempt}
            _reap()
            assert tid in pending_retries, f"Expected retry after attempt {attempt}"
            # Fast-forward backoff and promote to pool
            pending_retries[tid]["retry_after"] = time.monotonic() - 1
            _promote()
            assert tid in pool, f"Expected task in pool after promote at attempt {attempt}"

        # 4th failure with retry_count=3 — retries exhausted, should mark FAILED
        dead = _make_dead_proc(rc=1)
        pool[tid] = {"proc": dead, "last_heartbeat": 0.0, "retry_count": 3}
        pending_retries.clear()  # clean state before final check
        _reap()

        assert tid not in pending_retries
        task = mgr.get_task(tid)
        assert task["status"] in ("FAILED", "POISONED")

    def test_transient_backoff_delay_respected(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path, last_error="500 Server Error")
        dead = _make_dead_proc(rc=1)
        _launch(tid, proc_override=dead)

        _reap()
        assert tid in pending_retries
        # Backoff not elapsed yet — promote should not re-launch
        _promote()
        assert tid not in pool
        assert tid in pending_retries

        # Simulate elapsed
        pending_retries[tid]["retry_after"] = time.monotonic() - 1
        _promote()
        assert tid in pool
        assert pool[tid]["retry_count"] == 1


class TestBudgetAbortPolicy:
    def test_budget_abort_marks_budget_exceeded(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path, last_error="budget_exceeded: hard cap reached")
        dead = _make_dead_proc(rc=1)
        _launch(tid, proc_override=dead)

        _reap()

        assert tid not in pool
        assert tid not in pending_retries
        assert mgr.get_task(tid)["status"] == "BUDGET_EXCEEDED"

    def test_budget_abort_not_retried(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path, last_error="hard cap exceeded before generate")
        dead = _make_dead_proc(rc=1)
        _launch(tid, proc_override=dead)

        _reap()

        assert tid not in pending_retries


class TestInternalRetryOnce:
    def test_internal_error_retried_once(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path, last_error="JSONDecodeError: extra data")
        dead = _make_dead_proc(rc=1)
        _launch(tid, proc_override=dead)

        _reap()

        assert tid in pending_retries
        assert pending_retries[tid]["retry_count"] == 1
        assert pending_retries[tid]["retry_after"] <= time.monotonic() + 0.1  # immediate

    def test_internal_error_second_failure_marks_failed(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path, last_error="tree_sitter.Query object is not iterable")
        dead = _make_dead_proc(rc=1)
        pool[tid] = {"proc": dead, "last_heartbeat": 0.0, "retry_count": 1}  # already retried once

        _reap()

        assert tid not in pending_retries
        task = mgr.get_task(tid)
        assert task["status"] in ("FAILED", "POISONED")


class TestClassFailContinue:
    def test_class_fail_marks_failed_immediately(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path, last_error="Baseline compilation failed: error")
        dead = _make_dead_proc(rc=1)
        _launch(tid, proc_override=dead)

        _reap()

        assert tid not in pending_retries
        task = mgr.get_task(tid)
        assert task["status"] in ("FAILED", "POISONED")

    def test_unknown_error_marks_failed(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path, last_error="NullPointerException at line 42")
        dead = _make_dead_proc(rc=1)
        _launch(tid, proc_override=dead)

        _reap()

        assert tid not in pending_retries

    def test_clean_exit_no_action(self, tmp_path):
        mgr, pool, pending_retries, _launch, _reap, _promote = _build_daemon_closures(tmp_path)
        tid = _create_running_task(mgr, tmp_path)
        mgr.db.update_repo_task(tid, status="COMPLETED")
        ok = _make_dead_proc(rc=0)
        _launch(tid, proc_override=ok)

        _reap()

        assert tid not in pool
        assert tid not in pending_retries
        # Status left as COMPLETED (subprocess set it)
        assert mgr.get_task(tid)["status"] == "COMPLETED"

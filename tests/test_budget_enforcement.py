"""Tests for per-repo hard cap budget enforcement via TaskBudgetExceeded."""

import pytest
from pathlib import Path

from uta.tasks.db import TaskDB
from uta.tasks.manager import TaskManager


def _make_db(tmp_path: Path, hard_cap_usd=None):
    db = TaskDB(str(tmp_path / "test.db"))
    db.init()
    fields = {"repo_path": "/tmp/repo", "repo_slug": "repo", "status": "RUNNING"}
    if hard_cap_usd is not None:
        fields["hard_cap_usd"] = hard_cap_usd
    tid = db.create_repo_task(fields)
    return db, tid


def _make_state(tmp_path, tid, batch=("com.example.Foo",)):
    return {
        "task_id": str(tid),
        "task_db_path": str(tmp_path / "test.db"),
        "current_batch": list(batch),
        "current_class": None,
        "repo_path": "/tmp/repo",
        "model": "myproxy/gpt-5.4",
    }


def _add_class(db: TaskDB, tid: int, fqn: str) -> dict:
    db.create_class_task(tid, fqn, module=None, priority=100)
    row = db.find_class_task(tid, fqn)
    db.update_class_task(row["id"], status="RUNNING", started_at="2026-01-01T00:00:00")
    return row


def test_mark_budget_exceeded_sets_correct_status(tmp_path):
    db, tid = _make_db(tmp_path, hard_cap_usd=0.05)
    mgr = TaskManager(str(tmp_path / "test.db"))

    mgr.mark_budget_exceeded(tid, "Hard cap exceeded: $0.10 >= $0.05")

    task = db.get_repo_task(tid)
    assert task["status"] == "BUDGET_EXCEEDED"
    assert task["current_stage"] == "budget_exceeded"


def test_hard_cap_raises_when_cost_exceeds_explicit_cap(tmp_path):
    from uta.graph.nodes import _llm_guard_before, TaskBudgetExceeded

    db, tid = _make_db(tmp_path, hard_cap_usd=0.05)
    _add_class(db, tid, "com.example.Foo")

    # Running cost above hard_cap_usd
    db.update_repo_task(tid, provider_cost_usd=0.10, actual_cost=0.10)

    state = _make_state(tmp_path, tid)
    with pytest.raises(TaskBudgetExceeded, match="Hard cap exceeded"):
        _llm_guard_before(state, ["com.example.Foo"], "plan_tests")

    task = db.get_repo_task(tid)
    assert task["status"] == "BUDGET_EXCEEDED"


def test_hard_cap_not_raised_when_cost_below_cap(tmp_path):
    from uta.graph.nodes import _llm_guard_before, TaskBudgetExceeded

    db, tid = _make_db(tmp_path, hard_cap_usd=0.05)
    _add_class(db, tid, "com.example.Foo")
    db.update_repo_task(tid, provider_cost_usd=0.02, actual_cost=0.02)

    state = _make_state(tmp_path, tid)
    # Should not raise
    _llm_guard_before(state, ["com.example.Foo"], "plan_tests")


def test_no_cap_set_does_not_raise(tmp_path):
    from uta.graph.nodes import _llm_guard_before, TaskBudgetExceeded

    db, tid = _make_db(tmp_path)  # no hard_cap_usd, no estimated_cost_usd
    _add_class(db, tid, "com.example.Bar")
    db.update_repo_task(tid, provider_cost_usd=999.0)

    state = _make_state(tmp_path, tid, batch=["com.example.Bar"])
    _llm_guard_before(state, ["com.example.Bar"], "plan_tests")


def test_unblock_resets_budget_exceeded_to_queued(tmp_path):
    db, tid = _make_db(tmp_path, hard_cap_usd=0.05)
    mgr = TaskManager(str(tmp_path / "test.db"))

    mgr.mark_budget_exceeded(tid, "cap breach")
    assert db.get_repo_task(tid)["status"] == "BUDGET_EXCEEDED"

    mgr.unblock(tid)
    assert db.get_repo_task(tid)["status"] == "QUEUED"


def test_budget_exceeded_task_not_requeued_by_scheduler(tmp_path):
    """Scheduler must skip BUDGET_EXCEEDED tasks — they require operator unblock."""
    from uta.tasks.scheduler import TaskScheduler

    db, tid = _make_db(tmp_path, hard_cap_usd=0.05)
    mgr = TaskManager(str(tmp_path / "test.db"))
    db.update_repo_task(tid, status="BUDGET_EXCEEDED")

    scheduler = TaskScheduler(str(tmp_path / "test.db"), runner_id="test-runner")
    task = scheduler.acquire_next()
    assert task is None, "BUDGET_EXCEEDED task should not be dequeued without unblock"

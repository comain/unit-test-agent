"""Tests for per-repo hard cap budget enforcement via TaskBudgetExceeded."""

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


def test_hard_cap_not_raised_when_cost_below_cap(tmp_path):
    from uta.language.java.generation import _llm_guard_before

    db, tid = _make_db(tmp_path, hard_cap_usd=0.05)
    _add_class(db, tid, "com.example.Foo")
    db.update_repo_task(tid, provider_cost_usd=0.02, actual_cost=0.02)

    state = _make_state(tmp_path, tid)
    # Should not raise
    _llm_guard_before(state, ["com.example.Foo"], "plan_tests")


def test_no_cap_set_does_not_raise(tmp_path):
    from uta.language.java.generation import _llm_guard_before

    db, tid = _make_db(tmp_path)  # no hard_cap_usd, no estimated_cost_usd
    _add_class(db, tid, "com.example.Bar")
    db.update_repo_task(tid, provider_cost_usd=999.0)

    state = _make_state(tmp_path, tid, batch=["com.example.Bar"])
    _llm_guard_before(state, ["com.example.Bar"], "plan_tests")


def test_estimated_budget_hard_cap_uses_configured_four_x_multiplier(tmp_path):
    from uta.language.java.generation import _llm_guard_before

    db, tid = _make_db(tmp_path)
    _add_class(db, tid, "com.example.Foo")
    db.update_repo_task(tid, estimated_cost_usd=0.10, provider_cost_usd=0.35, actual_cost=0.35)

    state = _make_state(tmp_path, tid)

    _llm_guard_before(state, ["com.example.Foo"], "python_fix_mutations")


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
    TaskManager(str(tmp_path / "test.db"))
    db.update_repo_task(tid, status="BUDGET_EXCEEDED")

    scheduler = TaskScheduler(str(tmp_path / "test.db"), runner_id="test-runner")
    task = scheduler.acquire_next()
    assert task is None, "BUDGET_EXCEEDED task should not be dequeued without unblock"


def test_the_guard_looks_up_a_whole_batch_in_one_query(tmp_path, monkeypatch):
    """The guard runs before every LLM phase of every target, and it used to
    look each class up individually -- and `find_class_task` opens its own
    connection, so a batch of twenty cost twenty connections and twenty
    queries, three times over in the same call.

    Asserted rather than measured, because a loop reintroduced here would
    still return exactly the right answers and only cost more.
    """
    from uta.language.java.generation import _llm_guard_before
    from uta.testgen import task_guard

    db, tid = _make_db(tmp_path)
    batch = [f"com.example.C{i}" for i in range(20)]
    for fqn in batch:
        _add_class(db, tid, fqn)

    lookups = {"single": 0, "batched": 0}
    real_single = TaskDB.find_class_task
    real_batched = TaskDB.find_class_tasks

    def counting_single(self, *a, **kw):
        lookups["single"] += 1
        return real_single(self, *a, **kw)

    def counting_batched(self, *a, **kw):
        lookups["batched"] += 1
        return real_batched(self, *a, **kw)

    monkeypatch.setattr(TaskDB, "find_class_task", counting_single)
    monkeypatch.setattr(TaskDB, "find_class_tasks", counting_batched)
    # The guard cleans runtime residue and snapshots git; neither is what this
    # test is about, and /tmp/repo is not a repository.
    monkeypatch.setattr(task_guard, "git_status_snapshot", lambda *_a, **_k: {})
    monkeypatch.setattr(
        task_guard, "workspace_policy_for",
        lambda *_a, **_k: type("P", (), {"cleanup_runtime_residue": lambda *_: None})(),
    )

    _llm_guard_before(_make_state(tmp_path, tid, batch), batch, "plan_tests")

    assert lookups["single"] == 0, "the guard fell back to per-class lookups"
    assert lookups["batched"] == 1


def test_the_guard_still_counts_a_turn_for_every_class_in_the_batch(tmp_path, monkeypatch):
    """The behaviour the batching must not change: every class in the batch
    gets its turn counted, or a budget stops being enforced."""
    from uta.language.java.generation import _llm_guard_before
    from uta.testgen import task_guard

    db, tid = _make_db(tmp_path)
    batch = ["com.example.A", "com.example.B", "com.example.C"]
    for fqn in batch:
        _add_class(db, tid, fqn)

    monkeypatch.setattr(task_guard, "git_status_snapshot", lambda *_a, **_k: {})
    monkeypatch.setattr(
        task_guard, "workspace_policy_for",
        lambda *_a, **_k: type("P", (), {"cleanup_runtime_residue": lambda *_: None})(),
    )

    _llm_guard_before(_make_state(tmp_path, tid, batch), batch, "plan_tests")

    for fqn in batch:
        assert db.find_class_task(tid, fqn)["llm_turn_count"] == 1

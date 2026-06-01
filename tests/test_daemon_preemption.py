from pathlib import Path

from uta.tasks.manager import TaskManager
from uta.tasks.scheduler import TaskScheduler


def test_urgent_repair_preempts_running_batch_and_waits_for_slot_release(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    batch_id = manager.create_task(repo_path=str(repo), class_fqns=["com.example.Batch"], priority=100)
    repair_id = manager.create_task(repo_path=str(repo), class_fqns=["com.example.Foo"], priority=1)
    manager.mark_running(batch_id, stage="generate", detail="large batch")

    preempted = manager.preempt_running_same_repo_for_urgent(repair_id)
    scheduler = TaskScheduler(str(tmp_path / "tasks.db"), runner_id="test-runner")

    assert preempted == [batch_id]
    assert manager.get_task(batch_id)["status"] == "STOP_REQUESTED"
    assert scheduler.acquire_next() is None

    manager.mark_stopped(batch_id, reason="preempted by urgent repair")
    acquired = scheduler.acquire_next()

    assert acquired["id"] == repair_id


def test_preempted_batch_auto_resumes_after_urgent_repair_terminal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    batch_id = manager.create_task(repo_path=str(repo), class_fqns=["com.example.Batch"], priority=100)
    repair_id = manager.create_task(repo_path=str(repo), class_fqns=["com.example.Foo"], priority=1)
    manager.mark_running(batch_id, stage="generate", detail="large batch")
    manager.preempt_running_same_repo_for_urgent(repair_id)
    manager.mark_stopped(batch_id, reason="preempted by urgent repair")

    manager.mark_completed(repair_id, message="repair green")

    assert manager.get_task(batch_id)["status"] == "QUEUED"
    with manager.db.connect() as conn:
        events = conn.execute(
            "SELECT event_type FROM task_events WHERE repo_task_id=? AND event_type='task_resumed'",
            (batch_id,),
        ).fetchall()
    assert events

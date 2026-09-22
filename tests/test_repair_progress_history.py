"""The repair progress page must receive the whole session history.

`build_status_payload` queried the newest 200 events and then sliced the result
to 30, so the page received the last 30 of a 340-event session (production repo
task 102) however far the reader scrolled. Nothing was pruned at write time --
`progress_truncated: 0` -- the history was simply never sent.

The slice, not the query limit, was the binding constraint; raising only the
query would have changed nothing.
"""

from __future__ import annotations

from pathlib import Path

from uta.tasks.manager import TaskManager
from uta.tasks.render import build_status_payload


def _task_with_events(tmp_path: Path, count: int) -> tuple[TaskManager, int]:
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.db.create_repo_task(
        {
            "repo_path": str(tmp_path),
            "repo_slug": "demo/repo",
            "language": "java",
            "selection": {},
            "branch_name": "feature/x",
            "base_ref": "origin/master",
            "coverage_gate": 80.0,
            "mutation_gate": 70.0,
            "total_classes": 1,
        }
    )
    for index in range(count):
        manager.db.add_event(task_id, None, "agent_progress", f"event {index}", stage="generate")
    return manager, task_id


def test_the_default_stays_bounded(tmp_path):
    """Report pages keep their existing ceiling, which is 30 -- not the 200 the
    query asked for. The slice after the fetch was the real limit, and that is
    what has to stay put for every other consumer."""
    manager, task_id = _task_with_events(tmp_path, 260)

    payload = build_status_payload(manager.db, task_id)

    assert len(payload["latest_events"]) == 30


def test_the_progress_page_gets_every_event(tmp_path):
    """`event_limit=None` is what the progress route passes."""
    manager, task_id = _task_with_events(tmp_path, 260)

    payload = build_status_payload(manager.db, task_id, event_limit=None)
    messages = {event["message"] for event in payload["latest_events"]}

    assert len(payload["latest_events"]) == 260
    assert "event 0" in messages, "the oldest event must survive"
    assert "event 259" in messages


def test_the_oldest_events_are_the_ones_the_default_drops(tmp_path):
    """Names the actual failure: the beginning of the session goes missing."""
    manager, task_id = _task_with_events(tmp_path, 260)

    capped = {e["message"] for e in build_status_payload(manager.db, task_id)["latest_events"]}

    assert "event 259" in capped
    assert "event 0" not in capped
    assert "event 200" not in capped, "the default keeps only the newest 30"

"""Test that stage_started and stage_completed events are paired in task_events."""

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

from uta.tasks.db import TaskDB
from uta.tasks.manager import TaskManager


def _make_db(tmp_path: Path) -> tuple[TaskDB, int]:
    db = TaskDB(str(tmp_path / "test.db"))
    db.init()
    repo_task_id = db.create_repo_task({"repo_path": "/tmp/repo", "repo_slug": "repo", "status": "RUNNING"})
    return db, repo_task_id


def _events(db: TaskDB, repo_task_id: int, event_type: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE repo_task_id=? AND event_type=?",
            (repo_task_id, event_type),
        ).fetchall()
    return [dict(r) for r in rows]


def test_record_stage_completed_emits_event(tmp_path):
    db, tid = _make_db(tmp_path)
    mgr = TaskManager(str(tmp_path / "test.db"))

    mgr.record_stage(tid, "plan_tests")
    mgr.record_stage_completed(tid, "plan_tests")

    started = _events(db, tid, "stage_started")
    completed = _events(db, tid, "stage_completed")
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0]["stage"] == "plan_tests"
    assert completed[0]["stage"] == "plan_tests"


def test_set_stage_auto_closes_previous_stage(tmp_path, monkeypatch):
    """_set_stage should emit stage_completed for previous stage when advancing."""
    db, tid = _make_db(tmp_path)

    # Simulate the pipeline state with task_id and task_db_path wired up
    state: Dict[str, Any] = {
        "task_id": str(tid),
        "task_db_path": str(tmp_path / "test.db"),
        "current_stage": None,
        "current_batch": [],
    }

    from uta.graph.nodes import _set_stage

    _set_stage(state, "plan_tests", "batch=1")
    # Advance to generate — should close plan_tests
    state["current_stage"] = "plan_tests"
    _set_stage(state, "generate", "batch=1")

    started = _events(db, tid, "stage_started")
    completed = _events(db, tid, "stage_completed")

    started_stages = [e["stage"] for e in started]
    completed_stages = [e["stage"] for e in completed]

    assert "plan_tests" in started_stages
    assert "generate" in started_stages
    assert "plan_tests" in completed_stages, (
        "stage_completed must be emitted for plan_tests when generate starts"
    )


def test_stage_completed_pairs_with_started_by_stage_name(tmp_path):
    db, tid = _make_db(tmp_path)
    mgr = TaskManager(str(tmp_path / "test.db"))

    stages = ["baseline_compile", "scan_candidates", "parse_context", "select_batch"]
    for stage in stages:
        mgr.record_stage(tid, stage)
        mgr.record_stage_completed(tid, stage)

    for stage in stages:
        started = _events(db, tid, "stage_started")
        completed = _events(db, tid, "stage_completed")
        assert any(e["stage"] == stage for e in started), f"missing stage_started for {stage}"
        assert any(e["stage"] == stage for e in completed), f"missing stage_completed for {stage}"


def test_duration_queryable_via_join(tmp_path):
    """Verify that duration is computable by joining stage_started/stage_completed."""
    db, tid = _make_db(tmp_path)
    mgr = TaskManager(str(tmp_path / "test.db"))

    mgr.record_stage(tid, "generate")
    mgr.record_stage_completed(tid, "generate")

    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT e2.ts - e1.ts AS duration_seconds
            FROM task_events e1
            JOIN task_events e2
              ON e1.repo_task_id = e2.repo_task_id
             AND e1.stage = e2.stage
            WHERE e1.repo_task_id = ?
              AND e1.event_type = 'stage_started'
              AND e2.event_type = 'stage_completed'
              AND e1.stage = 'generate'
            """,
            (tid,),
        ).fetchone()
    assert row is not None, "duration join returned no row"
    assert row["duration_seconds"] >= 0

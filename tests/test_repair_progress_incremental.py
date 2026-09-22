"""The progress page asks only for events it does not already have.

A repair session runs to several hundred events (340 on production repo task
102, 438 on task 78). Re-sending the whole history every five seconds moved
~340KB per poll to draw a handful of new rows.

Stage detection still walks the full history: a delta on its own looks like a
session that never started, so only the list handed to the caller is trimmed.
"""

from __future__ import annotations

import json
from pathlib import Path

from uta.app.context import RepairContextExporter
from uta.app.protocols import ProtocolRegistry
from uta.app.protocols.rdc import RdcProtocol
from uta.app.service import ApiTriggerService
from uta.app.store import JsonCiTaskStore
from uta.app.workspace import GitWorkspaceManager
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.tasks.manager import TaskManager

from fake_git import fake_git


def _service_with_session(tmp_path: Path):
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
    for index in range(120):
        manager.db.add_event(task_id, None, "agent_progress", f"event {index}", stage="generate")

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "ws", git_bin=fake_git(tmp_path)),
        task_manager=manager,
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([RdcProtocol()]),
    )
    record = CiTaskRecord(
        task_id="task-progress-delta",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="app", git_url="git@example.com:g/app.git", branch="feature/x", language="java"
        ),
        fix_sessions=[{"sessionId": "s1", "status": "repairing", "repoTaskId": task_id}],
    )
    service._tasks[record.task_id] = record
    return service, record, task_id


def test_without_after_the_whole_history_comes_back(tmp_path):
    service, record, _ = _service_with_session(tmp_path)

    progress = service.repair_progress(record, "s1")

    assert len(progress["repoTask"]["latest_events"]) == 120


def test_after_returns_only_newer_events(tmp_path):
    service, record, _ = _service_with_session(tmp_path)

    full = service.repair_progress(record, "s1")["repoTask"]["latest_events"]
    ids = sorted(int(e["id"]) for e in full)
    midpoint = ids[len(ids) // 2]

    delta = service.repair_progress(record, "s1", event_after_id=midpoint)["repoTask"]["latest_events"]

    assert delta, "expected some newer events"
    assert all(int(e["id"]) > midpoint for e in delta)
    assert len(delta) < len(full)


def test_after_does_not_materialize_the_whole_event_history(tmp_path, monkeypatch):
    service, record, _ = _service_with_session(tmp_path)
    db = service.task_manager.db
    newest_id = max(int(event["id"]) for event in service.repair_progress(record, "s1")["repoTask"]["latest_events"])
    latest_limits = []
    cursor_calls = []
    original_latest = db.latest_events
    original_since = db.events_since

    def tracked_latest(repo_task_id, *, limit=20):
        latest_limits.append(limit)
        return original_latest(repo_task_id, limit=limit)

    def tracked_since(repo_task_id, *, after_id=0, limit=500):
        cursor_calls.append((after_id, limit))
        return original_since(repo_task_id, after_id=after_id, limit=limit)

    monkeypatch.setattr(db, "latest_events", tracked_latest)
    monkeypatch.setattr(db, "events_since", tracked_since)

    service.repair_progress(record, "s1", event_after_id=newest_id)

    assert None not in latest_limits, "an incremental poll must not load the unbounded event history"
    assert cursor_calls == [(newest_id, 500)]


def test_the_newest_event_leads_so_a_delta_can_be_prepended(tmp_path):
    """The page inserts new rows at the top; that only works if the newest
    event is first."""
    service, record, _ = _service_with_session(tmp_path)

    events = service.repair_progress(record, "s1")["repoTask"]["latest_events"]

    assert int(events[0]["id"]) > int(events[-1]["id"])


def test_stages_survive_a_delta_request(tmp_path):
    """Stage detection reads the full history, so a delta must not change it."""
    service, record, _ = _service_with_session(tmp_path)

    full = service.repair_progress(record, "s1")
    ids = sorted(int(e["id"]) for e in full["repoTask"]["latest_events"])
    delta = service.repair_progress(record, "s1", event_after_id=ids[-1])

    assert delta["repoTask"]["latest_events"] == []
    assert delta["stages"] == full["stages"], "stages must not depend on how many events were sent"


def test_progress_projection_excludes_large_raw_session_output(tmp_path):
    service, record, repo_task_id = _service_with_session(tmp_path)
    service.task_manager.db.update_repo_task(
        repo_task_id,
        rdc_context_json=json.dumps({"raw": "t" * 1_000_000}),
    )
    session = record.fix_sessions[0]
    session["rdcContext"] = {"raw": "r" * 1_000_000}
    session["rerunEnforcement"] = {
        "passed": False,
        "status": "failed",
        "summary": "mutation gate failed",
        "command": ["mvn", "verify"],
        "stdout": "o" * 5_000_000,
        "stderr": "e" * 5_000_000,
        "evidence": {
            "coverage": {"rate": 100.0, "covered": 4, "total": 4},
            "mutation": {"rate": 50.0, "killed": 1, "generated": 2},
        },
    }

    progress = service.repair_progress(record, "s1", event_after_id=0)
    encoded = json.dumps(progress)

    assert len(encoded) < 200_000
    assert "rdcContext" not in progress["session"]
    assert "rdc_context_json" not in progress["repoTask"]["task"]
    assert "stdout" not in progress["session"]["rerunEnforcement"]
    assert "stderr" not in progress["session"]["rerunEnforcement"]
    assert progress["rerunEvidence"]["coverage"]["rate"] == 100.0
    assert progress["rerunEvidence"]["mutation"]["rate"] == 50.0


def test_terminal_records_loaded_from_disk_are_not_retained_in_live_cache(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    record = CiTaskRecord(
        task_id="terminal-record",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="app",
            git_url="git@example.com:g/app.git",
            branch="feature/x",
            language="java",
        ),
        enforcement_result={"stdout": "x" * 1_000_000},
    )
    store.save(record)
    service = ApiTriggerService(record_store=store)

    assert service._load_record(record.task_id) is not None
    assert record.task_id not in service._tasks

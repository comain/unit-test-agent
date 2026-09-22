"""Durable, bounded progress storage at the product boundary."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from agent_core.runtime import AgentProgressEvent, ProgressBatcher, stream_task_events

from uta.tasks.manager import TaskManager
from uta.testgen.progress import UtaProgressEventStore, UtaTaskEventStreamStore


def _setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    return manager, task_id


def _event(sequence: int, *, session: str = "session-a", detail: str = "safe detail"):
    return AgentProgressEvent(
        sequence=sequence,
        session_id=session,
        phase="generate_tests",
        kind="tool",
        summary=f"Generating tests {sequence}",
        detail=detail,
        tool="write",
        status="running",
    )


def test_progress_batch_persists_safe_projection_and_restart_counters(tmp_path):
    manager, task_id = _setup(tmp_path)
    store = UtaProgressEventStore(
        manager.db,
        repo_task_id=task_id,
        workflow_run_id="run-1",
        unit_id="unit-1",
        attempt=2,
        max_attempts=3,
    )

    admitted = store.append_batch(
        session_id="session-a", events=[_event(1), _event(2)]
    )

    assert admitted.admitted_events == 2
    assert admitted.admitted_serialized_bytes > 0
    assert admitted.truncated is False
    task = manager.db.get_repo_task(task_id)
    assert task["progress_event_count"] == 2
    assert task["progress_event_bytes"] == admitted.admitted_serialized_bytes
    assert task["progress_truncated"] == 0
    assert store.progress_budget().used_events == 2
    assert store.progress_budget().used_serialized_bytes == admitted.admitted_serialized_bytes

    rows = UtaTaskEventStreamStore(manager.db).events_since(task_ref=str(task_id))
    assert [row["event_type"] for row in rows[-2:]] == ["agent_progress", "agent_progress"]
    payload = json.loads(rows[-1]["payload_json"])
    assert payload == {
        "attempt": 2,
        "detail": "safe detail",
        "kind": "tool",
        "maxAttempts": 3,
        "sequence": 2,
        "sessionId": "session-a",
        "status": "running",
        "tool": "write",
        "unitId": "unit-1",
        "workflowRunId": "run-1",
    }


def test_model_fallback_is_visible_in_the_persisted_event_log(tmp_path):
    manager, task_id = _setup(tmp_path)
    store = UtaProgressEventStore(
        manager.db, repo_task_id=task_id, workflow_run_id="run", unit_id="unit"
    )
    event = AgentProgressEvent(
        sequence=1,
        session_id="session-a",
        phase="fix_coverage",
        kind="model_fallback",
        summary="Model fallback: token-pool/a -> token-pool/b (rate_limit)",
        status="running",
    )

    store.append_batch(session_id="session-a", events=[event])

    row = manager.db.latest_events(task_id, limit=1)[0]
    assert row["message"] == "Model fallback: token-pool/a -> token-pool/b (rate_limit)"
    assert row["severity"] == "WARNING"


@pytest.mark.parametrize("phase", ["fix_mutations", "python_fix_mutations"])
def test_runtime_model_and_effort_survive_cross_layer_persistence(tmp_path, phase):
    from agent_core.harness.opencode import _translate_progress
    from agent_core.runtime.progress import project_turn_progress

    manager, task_id = _setup(tmp_path)
    store = UtaProgressEventStore(
        manager.db, repo_task_id=task_id, workflow_run_id="run", unit_id="unit"
    )
    event = project_turn_progress(
        _translate_progress("model-selected: token-pool/gpt-6-astra effort=low"),
        phase=phase,
    )
    assert event is not None
    store.append_batch(session_id="session-a", events=[event])
    row = manager.db.latest_events(task_id, limit=1)[0]
    assert row["message"] == "Selected model token-pool/gpt-6-astra (effort: low)"
    assert row["stage"] == phase
    assert json.loads(row["payload_json"])["kind"] == "model_selected"


def test_agent_core_batcher_uses_uta_as_its_authoritative_append_port(tmp_path):
    manager, task_id = _setup(tmp_path)
    store = UtaProgressEventStore(
        manager.db, repo_task_id=task_id, workflow_run_id="run", unit_id="unit"
    )
    sink = ProgressBatcher(
        store.append_batch,
        budget=store.progress_budget(),
        samples_per_second=0,
    )

    sink.publish(_event(1))
    result = sink.flush()

    assert result.delivered_events == 1
    assert result.failures == {}
    assert manager.db.get_repo_task(task_id)["progress_event_count"] == 1


def test_cursor_reads_are_strictly_after_id_oldest_first(tmp_path):
    manager, task_id = _setup(tmp_path)
    store = UtaProgressEventStore(
        manager.db, repo_task_id=task_id, workflow_run_id="run", unit_id="unit"
    )
    store.append_batch(session_id="session-a", events=[_event(1), _event(2), _event(3)])
    stream = UtaTaskEventStreamStore(manager.db)
    all_rows = stream.events_since(task_ref=str(task_id))

    tail = stream.events_since(task_ref=str(task_id), after_id=all_rows[-3]["id"], limit=2)

    assert [row["id"] for row in tail] == [all_rows[-2]["id"], all_rows[-1]["id"]]


def test_task_cap_admits_prefix_and_writes_one_truncation_marker(tmp_path):
    manager, task_id = _setup(tmp_path)
    store = UtaProgressEventStore(
        manager.db,
        repo_task_id=task_id,
        workflow_run_id="run",
        unit_id="unit",
        max_events=2,
        max_serialized_bytes=20 * 1024 * 1024,
    )

    first = store.append_batch(
        session_id="session-a", events=[_event(1), _event(2), _event(3)]
    )
    second = store.append_batch(session_id="session-b", events=[_event(4, session="session-b")])

    assert first.admitted_events == 2
    assert first.truncated is True
    assert second.admitted_events == 0
    assert second.truncated is True
    rows = UtaTaskEventStreamStore(manager.db).events_since(task_ref=str(task_id))
    assert [row["event_type"] for row in rows].count("agent_progress") == 2
    assert [row["event_type"] for row in rows].count("progress_truncated") == 1
    task = manager.db.get_repo_task(task_id)
    assert task["progress_event_count"] == 2
    assert task["progress_truncated"] == 1


def test_byte_cap_rejects_oversized_event_without_consuming_capacity(tmp_path):
    manager, task_id = _setup(tmp_path)
    store = UtaProgressEventStore(
        manager.db,
        repo_task_id=task_id,
        workflow_run_id="run",
        unit_id="unit",
        max_events=20,
        max_serialized_bytes=200,
    )

    result = store.append_batch(
        session_id="session-a", events=[_event(1, detail="x" * 500)]
    )

    assert result.admitted_events == 0
    assert result.admitted_serialized_bytes == 0
    assert result.truncated is True
    task = manager.db.get_repo_task(task_id)
    assert task["progress_event_count"] == 0
    assert task["progress_event_bytes"] == 0


def test_concurrent_sessions_share_the_authoritative_task_cap(tmp_path):
    manager, task_id = _setup(tmp_path)

    def append(session: str):
        return UtaProgressEventStore(
            manager.db,
            repo_task_id=task_id,
            workflow_run_id="run",
            unit_id=session,
            max_events=5,
            max_serialized_bytes=20 * 1024 * 1024,
        ).append_batch(
            session_id=session,
            events=[_event(index, session=session) for index in range(10)],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, ["session-a", "session-b"]))

    assert sum(result.admitted_events for result in results) == 5
    task = manager.db.get_repo_task(task_id)
    assert task["progress_event_count"] == 5
    rows = UtaTaskEventStreamStore(manager.db).events_since(task_ref=str(task_id))
    assert [row["event_type"] for row in rows].count("progress_truncated") == 1


def test_terminal_status_and_closing_cursor_commit_together(tmp_path):
    manager, task_id = _setup(tmp_path)

    event_id = manager.db.finish_task_with_terminal_event(
        task_id,
        status="COMPLETED",
        message="Task completed",
        stage="finished",
        payload={"result": "pass"},
    )

    task = manager.db.get_repo_task(task_id)
    terminal = UtaTaskEventStreamStore(manager.db).events_since(
        task_ref=str(task_id), after_id=event_id - 1
    )
    assert task["status"] == "COMPLETED"
    assert task["finished_at"] is not None
    assert len(terminal) == 1
    assert terminal[0]["id"] == event_id
    assert terminal[0]["event_type"] == "task_terminal"


def test_terminal_event_failure_rolls_back_the_status_change(tmp_path):
    manager, task_id = _setup(tmp_path)
    with manager.db.connect() as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_terminal BEFORE INSERT ON task_events
            WHEN NEW.event_type='task_terminal'
            BEGIN SELECT RAISE(ABORT, 'injected terminal failure'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected terminal failure"):
        manager.db.finish_task_with_terminal_event(
            task_id,
            status="COMPLETED",
            message="Task completed",
            stage="finished",
        )

    assert manager.db.get_repo_task(task_id)["status"] == "CREATED"


def test_agent_core_sse_stream_closes_after_uta_terminal_cursor(tmp_path):
    manager, task_id = _setup(tmp_path)
    manager.db.add_event(task_id, None, "phase_started", "Planning", stage="plan_tests")
    terminal_id = manager.db.finish_task_with_terminal_event(
        task_id, status="COMPLETED", message="Task completed", stage="finished"
    )

    frames = list(
        stream_task_events(
            UtaTaskEventStreamStore(manager.db),
            task_ref=str(task_id),
            terminal_types=("task_terminal",),
        )
    )

    assert f"id: {terminal_id}" in frames[-1]
    assert "event: task_terminal" in frames[-1]


def test_status_payload_reports_workflow_disposition_counters(tmp_path):
    from uta.tasks.render import build_status_payload

    manager, task_id = _setup(tmp_path)
    for event_type in (
        "workflow_started",
        "workflow_started",
        "workflow_resumed",
        "workflow_reused_completed",
    ):
        manager.db.add_event(task_id, None, event_type, event_type)

    payload = build_status_payload(manager.db, task_id)

    assert payload["workflow_dispositions"] == {
        "started": 2,
        "resumed": 1,
        "reused_completed": 1,
    }


def test_stream_store_rejects_non_numeric_product_task_reference(tmp_path):
    manager, _ = _setup(tmp_path)

    try:
        UtaTaskEventStreamStore(manager.db).events_since(task_ref="not-a-task")
    except ValueError as exc:
        assert "task_ref" in str(exc)
    else:
        raise AssertionError("a client-provided opaque reference reached SQL")


def test_manager_terminal_path_keeps_summary_then_one_closing_cursor(tmp_path):
    manager, task_id = _setup(tmp_path)

    manager.mark_completed(task_id, message="All targets passed")

    rows = UtaTaskEventStreamStore(manager.db).events_since(task_ref=str(task_id))
    assert [row["event_type"] for row in rows[-2:]] == [
        "task_completed",
        "task_terminal",
    ]
    assert json.loads(rows[-1]["payload_json"])["status"] == "COMPLETED"

    manager.mark_completed(task_id, message="All targets passed")
    repeated = UtaTaskEventStreamStore(manager.db).events_since(task_ref=str(task_id))
    assert [row["event_type"] for row in repeated].count("task_terminal") == 1


def test_auto_quarantine_emits_only_the_final_poisoned_terminal(tmp_path, monkeypatch):
    manager, task_id = _setup(tmp_path)
    monkeypatch.setattr("uta.shared.config.settings.quarantine_threshold", 1)

    manager.mark_failed(task_id, "provider failed")

    rows = UtaTaskEventStreamStore(manager.db).events_since(task_ref=str(task_id))
    assert [row["event_type"] for row in rows].count("task_terminal") == 1
    assert [row["event_type"] for row in rows[-3:]] == [
        "task_failed",
        "task_poisoned",
        "task_terminal",
    ]
    assert json.loads(rows[-1]["payload_json"])["status"] == "POISONED"
    assert manager.db.get_repo_task(task_id)["status"] == "POISONED"

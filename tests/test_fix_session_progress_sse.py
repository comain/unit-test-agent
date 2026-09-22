from fastapi.testclient import TestClient

from uta.app.app import create_app
from uta.app.service import ApiTriggerService
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.tasks.manager import TaskManager


def _record(task_id: str, *, session_id: str, repo_task_id=None) -> CiTaskRecord:
    record = CiTaskRecord(
        task_id=task_id,
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@example.invalid:team/demo.git",
                "branch": "feature/live-progress",
            }
        ),
    )
    record.fix_sessions = [
        {
            "sessionId": session_id,
            "status": "repair_task_created" if repo_task_id else "creating_repair_task",
            "repoTaskId": repo_task_id,
            "messages": [],
            "retryCount": 0,
        }
    ]
    return record


def _setup(tmp_path):
    manager = TaskManager(tmp_path / "tasks.db")
    repo_task_id = manager.create_task(
        repo_path=str(tmp_path / "repo-a"),
        class_fqns=["pkg.A"],
    )
    other_task_id = manager.create_task(
        repo_path=str(tmp_path / "repo-b"),
        class_fqns=["pkg.B"],
    )
    service = ApiTriggerService(task_manager=manager)
    service._tasks["report-a"] = _record(
        "report-a", session_id="fix-a", repo_task_id=repo_task_id
    )
    service._tasks["report-b"] = _record(
        "report-b", session_id="fix-b", repo_task_id=other_task_id
    )
    return manager, repo_task_id, other_task_id, TestClient(create_app(service))


def _add_event(manager, task_id, event_type, message, **kwargs):
    manager.db.add_event(task_id, None, event_type, message, **kwargs)
    return int(manager.db.events_since(task_id)[-1]["id"])


def test_fix_session_sse_resumes_by_cursor_and_cannot_cross_tasks(tmp_path):
    manager, task_id, other_task_id, client = _setup(tmp_path)
    first_id = _add_event(
        manager,
        task_id,
        "agent_progress",
        "Planning tests",
        stage="plan_tests",
        payload={"sessionId": "agent-a", "detail": "Inspecting target branches"},
    )
    _add_event(
        manager,
        other_task_id,
        "agent_progress",
        "OTHER TASK MUST NOT LEAK",
        stage="generate_tests",
        payload={"sessionId": "agent-other"},
    )
    _add_event(
        manager,
        task_id,
        "agent_progress",
        "Generating focused tests",
        stage="generate_tests",
        payload={"sessionId": "agent-b", "tool": "workspace_update"},
    )
    terminal_id = manager.db.finish_task_with_terminal_event(
        task_id,
        status="COMPLETED",
        message="Task completed",
        stage="finished",
    )

    response = client.get(
        "/reports/report-a/fix-sessions/fix-a/progress/events",
        headers={"Last-Event-ID": str(first_id)},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert "Planning tests" not in response.text
    assert "Generating focused tests" in response.text
    assert "OTHER TASK MUST NOT LEAK" not in response.text
    assert f"id: {terminal_id}" in response.text
    assert "event: task_terminal" in response.text
    assert response.text.endswith("\n\n")


def test_nonzero_query_cursor_takes_precedence_over_last_event_id(tmp_path):
    manager, task_id, _, client = _setup(tmp_path)
    first_id = _add_event(
        manager, task_id, "phase_started", "First", stage="plan_tests"
    )
    second_id = _add_event(
        manager, task_id, "phase_completed", "Second", stage="plan_tests"
    )
    manager.db.finish_task_with_terminal_event(
        task_id, status="COMPLETED", message="Done", stage="finished"
    )

    response = client.get(
        f"/reports/report-a/fix-sessions/fix-a/progress/events?after_id={first_id}",
        headers={"Last-Event-ID": str(second_id)},
    )

    assert "First" not in response.text
    assert "Second" in response.text


def test_progress_stream_rejects_unknown_or_not_ready_session(tmp_path, monkeypatch):
    manager, _, _, client = _setup(tmp_path)
    service = client.app.state.api_trigger_service
    service._tasks["queued"] = _record("queued", session_id="fix-queued")

    assert client.get(
        "/reports/report-a/fix-sessions/missing/progress/events"
    ).status_code == 404
    queued = client.get(
        "/reports/queued/fix-sessions/fix-queued/progress/events"
    )
    assert queued.status_code == 409
    assert queued.json()["detail"]["retryAfterSeconds"] == 2

    service._tasks["corrupt"] = _record(
        "corrupt", session_id="fix-corrupt", repo_task_id="not-a-task-id"
    )
    monkeypatch.setattr(service, "get", lambda task_id: service._tasks.get(task_id))
    corrupt = client.get(
        "/reports/corrupt/fix-sessions/fix-corrupt/progress/events"
    )
    assert corrupt.status_code == 409
    assert "invalid progress identity" in corrupt.json()["detail"]


def test_progress_page_builds_one_timeline_per_agent_session(tmp_path):
    manager, task_id, _, client = _setup(tmp_path)
    _add_event(
        manager,
        task_id,
        "agent_progress",
        "Agent update",
        stage="generate_tests",
        payload={"sessionId": "agent-a"},
    )

    html = client.get("/reports/report-a/fix-sessions/fix-a/progress").text

    # One events feed, and it is the full one. The SSE panel that used to sit
    # above this carried only agent progress -- no target selection, no model,
    # no fallback event -- so it read as a thinner duplicate of the table.
    assert "实时 Agent 进度" not in html
    assert html.count("<h2>事件</h2>") == 1
    assert 'id="events-body"' in html
    # And it refreshes, which is what the table was missing before.
    assert 'setInterval(refresh' in html
    # The page polls progress/data, now with an `after` cursor so a refresh
    # carries only events it does not already hold.
    assert '"progress/data"' in html
    assert "fetch(url" in html
    assert "?after=" in html
    # The feed grows without bound; before this it pushed the rest of the page
    # off screen rather than scrolling within its own panel.
    assert 'class="events-scroll"' in html
    assert "max-height" in html
    assert "position: sticky" in html
    # The class/target table refreshes from the same payload. It used to be
    # rendered once and sat on CREATED for the whole run while the events
    # beside it showed the work happening.
    assert 'id="class-rows"' in html
    assert "renderClasses(repo.classes" in html
    # Refreshing must not fight the reader: re-rendering rows resets the
    # scroll container, so history could not be scrolled while polling.
    assert "previousTop" in html
    # New rows are prepended (the table is newest-first), so the position that
    # means "following the stream" is the top, and a reader parked in history
    # is held still by offsetting for the height the new rows added.
    assert "wasAtTop" in html
    assert "previousHeight" in html
    # Content-free heartbeat lines are dropped. The fixture above adds an
    # "Agent update" event; it says only that the agent is alive, which the
    # status already says, and at ten a run it crowds out what happened.
    assert '"Agent update": 1' in html, "the refresh path must filter it too"
    assert "<td>Agent update</td>" not in html
    assert 'http-equiv="refresh"' not in html

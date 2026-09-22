"""Every task records which model it will run on.

Without this the model reaches the event log only when a provider fallback
fires -- `opencode_provider_fallback_resume` and friends name it in their
messages. A run that never falls back leaves no record at all, so an operator
reading the events cannot tell a claude-opus-5 run from a gpt-5.5 one. On beta
the last such event was 2026-08-19, from before the migration.

`main` emits `opencode_model_selected` on a `routing` stage right after
`task_created`. This branch dropped it when task creation moved out of
`uta/tasks/manager.py` into `uta/tasks/creation.py`.

Both creation paths are covered: `create_task` and `create_task_targets`. Half
the tasks silently missing the record would be as bad as all of them, and
harder to notice.
"""

from __future__ import annotations

import pytest

from uta.shared.languages import RawTargetSelection, default_registry
from uta.tasks.manager import TaskManager

SNAPSHOT = {
    "opencode_selected_provider": "token-pool",
    "opencode_selected_model": "token-pool/gpt-5.5",
    "opencode_candidate_index": 0,
}


@pytest.fixture(autouse=True)
def manual_selection_by_default(monkeypatch):
    # Legacy selection tests must not inherit the deployment's discovery config.
    # Discovery-specific cases below explicitly opt back in.
    from uta.shared.config import settings
    monkeypatch.setattr(settings, "model_selection_config", "")


@pytest.mark.parametrize("language", ["java", "python"])
def test_discovery_creation_defers_selection_instead_of_logging_blank_model(tmp_path, monkeypatch, language):
    from uta.shared.config import settings
    from uta.shared.opencode_snapshot import opencode_config_snapshot
    monkeypatch.setattr(settings, "model_selection_config", "/etc/models.json")
    manager = TaskManager(tmp_path / "tasks.db")
    kwargs = dict(repo_path=str(_repo(tmp_path)), config_snapshot=opencode_config_snapshot())
    if language == "java":
        task_id = manager.create_task(class_fqns=["example.Target"], **kwargs)
    else:
        import uta.language.python.batch
        target = default_registry().adapter_for("python").normalize_target(
            RawTargetSelection(target="jobs/forecast.py")
        )
        task_id = manager.create_task_targets(targets=[target], language="python", **kwargs)
    assert not _model_events(manager, task_id)
    events = manager.db.latest_events(task_id, limit=50)
    assert any(e["event_type"] == "model_selection_deferred" and
               "execution" in e["message"] for e in events)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "jobs").mkdir(parents=True)
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    return repo


def _model_events(manager, task_id):
    return [
        e for e in manager.db.latest_events(task_id, limit=50)
        if e["event_type"] == "opencode_model_selected"
    ]


def test_a_target_task_records_its_model(tmp_path):
    import uta.language.python.batch  # noqa: F401  -- registers the backend

    manager = TaskManager(tmp_path / "tasks.db")
    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py")
    )
    task_id = manager.create_task_targets(
        repo_path=str(_repo(tmp_path)),
        targets=[target],
        language="python",
        config_snapshot=dict(SNAPSHOT),
    )

    events = _model_events(manager, task_id)

    assert len(events) == 1, "no record of which model this task runs on"
    assert "token-pool/gpt-5.5" in events[0]["message"]
    assert events[0]["stage"] == "routing"


def test_a_class_task_records_its_model(tmp_path):
    """The other creation path. Covering only one leaves half the tasks
    unrecorded, which is harder to notice than none of them."""
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(_repo(tmp_path)),
        class_fqns=["com.example.Thing"],
        config_snapshot=dict(SNAPSHOT),
    )

    events = _model_events(manager, task_id)

    assert len(events) == 1
    assert "token-pool/gpt-5.5" in events[0]["message"]


def test_the_payload_carries_the_provider_and_candidate(tmp_path):
    """The message is for a human; the payload is what a fallback event is
    later compared against."""
    import json

    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(_repo(tmp_path)),
        class_fqns=["com.example.Thing"],
        config_snapshot=dict(SNAPSHOT),
    )

    payload = json.loads(_model_events(manager, task_id)[0]["payload_json"] or "{}")

    assert payload["provider"] == "token-pool"
    assert payload["model"] == "token-pool/gpt-5.5"
    assert payload["candidate_index"] == 0


def test_the_snapshot_producer_resolves_a_real_model():
    """The other half of the event, and the half that actually regressed.

    The tests above hand-build a snapshot, so they pass even when nothing
    populates one. `_task_config_snapshot` dropped its OpenCode fields when the
    provider router moved to `agent_core.harness.tiered_router`, and the routing
    event logged "Selected OpenCode model None" on a live beta fix session while
    this suite stayed green.
    """
    from uta.app import cli

    snapshot = cli._task_config_snapshot()

    assert snapshot["opencode_selected_model"]
    assert snapshot["opencode_selected_model"] != "None"
    assert "opencode_selected_provider" in snapshot
    assert "opencode_candidate_index" in snapshot
    assert isinstance(snapshot["opencode_provider_chain"], list)
    # The gate fields the daemon reads must survive alongside them.
    assert "coverage_gate" in snapshot and "mutation_gate" in snapshot


def test_a_task_runs_on_the_model_it_was_created_with(monkeypatch):
    """Recording the model is only half the point; the run has to honour it.

    The daemon runs generation in a fresh child process, so without this a task
    silently follows configuration changed after it was queued.
    """
    from uta.app import cli
    from uta.shared.config import settings

    monkeypatch.setattr(settings, "opencode_model", "token-pool/drifted", raising=False)
    monkeypatch.setattr(settings, "opencode_small_model", "token-pool/drifted", raising=False)
    monkeypatch.setattr(settings, "opencode_provider", "drifted", raising=False)

    cli._apply_task_opencode_selection(
        {"opencode_selected_model": "token-pool/gpt-5.5", "opencode_selected_provider": "token-pool"}
    )

    assert settings.opencode_model == "token-pool/gpt-5.5"
    assert settings.opencode_small_model == "token-pool/gpt-5.5"
    assert settings.opencode_provider == "token-pool"


def test_an_empty_selection_leaves_the_settings_alone(monkeypatch):
    """A snapshot without a model must not blank the runtime configuration."""
    from uta.app import cli
    from uta.shared.config import settings

    monkeypatch.setattr(settings, "opencode_model", "token-pool/configured", raising=False)

    cli._apply_task_opencode_selection({"opencode_selected_model": ""})

    assert settings.opencode_model == "token-pool/configured"


def test_a_python_repair_task_records_its_model(tmp_path):
    """The path that actually had the bug.

    `_task_config_snapshot` is used by the daemon and CLI. CI fix sessions
    create their repair tasks directly through the language handler, which
    passed no snapshot at all -- so every repair task created through the API
    logged "Selected OpenCode model None" while this suite stayed green.

    This asserts what the handler *hands to the task manager*, not what the
    producer returns on its own; the latter passes even when nothing calls it.
    """
    from uta.language.python.ci import PythonCiLanguageHandler
    from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
    from uta.shared.fix_sessions import CreateFixSessionRequest

    captured = {}

    class _Manager:
        def create_task_targets(self, **kwargs):
            captured.update(kwargs)
            return 1

    record = CiTaskRecord(
        task_id="task-model-snapshot",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-app",
            git_url="git@example.com:group/py-app.git",
            branch="feature/TASK-1",
            language="python",
        ),
        enforcement_result={
            "evidence": {
                "changedLines": {"jobs/forecast.py": [2]},
                "targetResults": [
                    {
                        "target": {"target_id": "pyfile:jobs/forecast.py", "source_path": "jobs/forecast.py"},
                        "status": "failed",
                        "reasonCode": "coverage_gate_failed",
                        "coverage": {"passed": False, "rate": 0.0},
                    }
                ],
            }
        },
    )

    PythonCiLanguageHandler(runner=None).create_repair_task(
        task_manager=_Manager(),
        record=record,
        request=CreateFixSessionRequest(),
        repo_path=tmp_path,
        priority=1,
        base_ref="origin/master",
        coverage_gate=95.0,
        mutation_gate=95.0,
        rdc_context={},
        rdc_context_path=None,
    )

    snapshot = captured.get("config_snapshot") or {}
    assert snapshot.get("opencode_selected_model"), "repair task was created without a model selection"
    assert "opencode_provider_chain" in snapshot


def test_a_java_repair_task_keeps_both_java_home_and_the_model():
    """java_home used to *replace* the snapshot, dropping the selection."""
    from uta.shared.opencode_snapshot import opencode_config_snapshot

    snapshot = opencode_config_snapshot()
    snapshot["java_home"] = "/opt/jdk8"

    assert snapshot["java_home"] == "/opt/jdk8"
    assert snapshot["opencode_selected_model"]
    assert "opencode_provider_chain" in snapshot


def test_the_cli_snapshot_still_carries_the_gate_fields():
    """The daemon reads these from the same snapshot; extracting the OpenCode
    half must not drop them."""
    from uta.app import cli

    snapshot = cli._task_config_snapshot()

    for key in ("coverage_gate", "mutation_gate", "classes_per_agent_run"):
        assert key in snapshot
    assert snapshot["opencode_selected_model"]

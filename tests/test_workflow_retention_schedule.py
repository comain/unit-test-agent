"""The daemon runs workflow retention at startup and on a bounded cadence."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from uta.app.task_daemon import _prune_workflows_if_due


def test_retention_schedule_runs_at_startup_then_waits_for_interval(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "uta.app.retention.prune_workflows",
        lambda path, **kwargs: calls.append((path, kwargs))
        or SimpleNamespace(
            lineages_deleted=0,
            operations_deleted=0,
            artifacts_deleted=0,
            prompt_artifacts_deleted=0,
        ),
    )
    monkeypatch.setattr(
        "uta.app.task_daemon.settings.workflow_retention_interval_seconds", 100
    )
    monkeypatch.setattr(
        "uta.app.task_daemon.settings.workflow_checkpoint_retention_days", 30
    )

    last = _prune_workflows_if_due(
        tmp_path / "tasks.db", last_run_at=None, now_monotonic=10, force=True
    )
    same = _prune_workflows_if_due(
        tmp_path / "tasks.db", last_run_at=last, now_monotonic=109
    )
    due = _prune_workflows_if_due(
        tmp_path / "tasks.db", last_run_at=same, now_monotonic=110
    )

    assert [call[1]["older_than_days"] for call in calls] == [30, 30]
    assert (last, same, due) == (10, 10, 110)


def test_retention_failure_is_contained_from_the_scheduler(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr("uta.app.retention.prune_workflows", fail)

    assert _prune_workflows_if_due(
        tmp_path / "tasks.db", last_run_at=None, now_monotonic=5, force=True
    ) == 5


def test_retention_schedule_reports_standalone_prompt_cleanup(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(
        "uta.app.retention.prune_workflows",
        lambda *_args, **_kwargs: SimpleNamespace(
            lineages_deleted=0,
            operations_deleted=0,
            artifacts_deleted=0,
            prompt_artifacts_deleted=2,
        ),
    )

    with caplog.at_level(logging.INFO, logger="uta"):
        _prune_workflows_if_due(
            tmp_path / "tasks.db", last_run_at=None, now_monotonic=5, force=True
        )

    assert "2 prompt artifacts" in caplog.text

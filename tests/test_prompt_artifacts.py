from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from uta.testgen.prompts import (
    PromptArtifactScopeError,
    PromptMetadataError,
    build_safe_prompt_metadata,
    open_prompt_artifact_scope,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_safe_metadata_has_one_fixed_json_shape():
    metadata = build_safe_prompt_metadata(
        engine="legacy",
        language="java",
        phase="fix_compile",
        task_id=17,
        workflow_run_id=None,
        unit_id=None,
        operation_id=None,
        session_id="session-1",
        batch=("com.example.Foo", "com.example.Bar"),
        logical_attempt=2,
        execution_ordinal=3,
    )

    assert metadata == {
        "schema_version": 1,
        "product": "uta",
        "engine": "legacy",
        "language": "java",
        "phase": "fix_compile",
        "task_id": 17,
        "workflow_run_id": None,
        "unit_id": None,
        "operation_id": None,
        "session_id": "session-1",
        "batch": ["com.example.Foo", "com.example.Bar"],
        "logical_attempt": 2,
        "execution_ordinal": 3,
    }
    assert len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) <= 4096


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"engine": "opencode"}, "engine"),
        ({"task_id": True}, "task_id"),
        ({"logical_attempt": -1}, "logical_attempt"),
        ({"batch": ["ok", 3]}, "batch"),
        ({"session_id": "../escape"}, "session_id"),
        ({"batch": ["x" * 5000]}, "4096"),
    ],
)
def test_safe_metadata_rejects_invalid_or_oversized_values(overrides, message):
    values = {
        "engine": "durable_v2",
        "language": "python",
        "phase": "generate_tests",
        "task_id": 1,
        "workflow_run_id": "run-1",
        "unit_id": "unit-1",
        "operation_id": "a" * 64,
        "session_id": "session-1",
        "batch": ["pyfile:jobs/foo.py"],
        "logical_attempt": 0,
        "execution_ordinal": 0,
    }
    values.update(overrides)

    with pytest.raises(PromptMetadataError, match=message):
        build_safe_prompt_metadata(**values)


def test_managed_scope_uses_canonical_layout_and_owner_only_directories(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    monkeypatch.setenv("UTA_TASK_DB_PATH", str(repo / "inside-repo.sqlite"))

    with open_prompt_artifact_scope(
        repo_path=repo, task_id=42, workflow_run_id="run-1"
    ) as scope:
        assert scope.managed is True
        assert scope.run_id == "run-1"
        assert scope.root == (runner_home / "workflow-state" / "prompts").resolve()

        durable = scope.durable_directory(
            unit_id="unit-0001", operation_id="a" * 64
        )
        assert durable == scope.root / "managed/42/run-1/unit-0001" / ("a" * 64)
        for path in (scope.root.parent, scope.root, durable):
            assert path.is_dir()
            assert _mode(path) == 0o700
        assert not str(durable).startswith(str(repo.resolve()))

    assert durable.is_dir(), "managed artifacts outlive the invocation"


def test_default_scope_root_is_application_state_not_task_db(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.delenv("UTA_RUNNER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UTA_TASK_DB_PATH", str(repo / "task.sqlite"))

    with open_prompt_artifact_scope(
        repo_path=repo, task_id=9, workflow_run_id="run-9"
    ) as scope:
        assert scope.root == (
            home / ".local/share/uta/workflow-state/prompts"
        ).resolve()


def test_scope_rejects_configured_root_inside_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner_home = repo / ".uta-state"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))

    with pytest.raises(PromptArtifactScopeError, match="repository"):
        with open_prompt_artifact_scope(
            repo_path=repo, task_id=1, workflow_run_id="run-1"
        ):
            pass
    assert not runner_home.exists()


def test_scope_rejects_repository_nested_below_configured_prompt_root(
    tmp_path, monkeypatch
):
    runner_home = tmp_path / "runner-home"
    repo = runner_home / "workflow-state/prompts/managed/1/run-1/unit-1/repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))

    with pytest.raises(PromptArtifactScopeError, match="repository"):
        with open_prompt_artifact_scope(
            repo_path=repo, task_id=1, workflow_run_id="run-1"
        ):
            pass


def test_scope_rejects_symlinked_application_state(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    real = tmp_path / "real-state"
    real.mkdir()
    linked = tmp_path / "linked-state"
    linked.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("UTA_RUNNER_HOME", str(linked))

    with pytest.raises(PromptArtifactScopeError, match="symlink"):
        with open_prompt_artifact_scope(
            repo_path=repo, task_id=1, workflow_run_id="run-1"
        ):
            pass


def test_scope_rejects_symlinked_component_even_when_it_points_inside_root(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("UTA_RUNNER_HOME", str(tmp_path / "state"))

    with open_prompt_artifact_scope(
        repo_path=repo, task_id=1, workflow_run_id="run-1"
    ) as scope:
        redirect = scope.root / "redirect"
        redirect.mkdir(mode=0o700)
        (scope.root / "managed").symlink_to(redirect, target_is_directory=True)

        with pytest.raises(PromptArtifactScopeError, match="symlink"):
            scope.durable_directory(unit_id="unit-1", operation_id="a" * 64)


def test_scope_rejects_partial_managed_identity_and_unsafe_components(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("UTA_RUNNER_HOME", str(tmp_path / "state"))

    with pytest.raises(PromptArtifactScopeError, match="together"):
        with open_prompt_artifact_scope(
            repo_path=repo, task_id=1, workflow_run_id=None
        ):
            pass

    with open_prompt_artifact_scope(
        repo_path=repo, task_id=1, workflow_run_id="run-1"
    ) as scope:
        with pytest.raises(PromptArtifactScopeError, match="unit_id"):
            scope.durable_directory(unit_id="../escape", operation_id="a" * 64)

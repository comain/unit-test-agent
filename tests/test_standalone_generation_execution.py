from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from uta.testgen.batch import BatchGenerationRequest
from uta.language.java.adapter import JavaLanguageAdapter
from uta.language.java.batch import JavaBatchGenerationRequest, run_java_batch_generation
from uta.language.python.generation import PythonBatchGenerator
from uta.language.python.adapter import PythonLanguageAdapter
from uta.shared.targets import TargetIdentity
from uta.tasks.db import TaskDB
from uta.testgen.standalone_execution import (
    open_standalone_generation_execution,
    project_standalone_final_state,
    prune_standalone_generation_executions,
)
from uta.testgen.prompts import PromptArtifactScope, PromptArtifactScopeError


def _python_request(repo: Path) -> BatchGenerationRequest:
    return BatchGenerationRequest(
        language="python",
        repo_path=repo,
        targets=[
            TargetIdentity(
                language="python",
                target_id="pysymbol:src/demo.py::build",
                display_name="build",
                source_path="src/demo.py",
                symbol="build",
                granularity="function",
            )
        ],
    )


@pytest.fixture(autouse=True)
def setup_persistence():
    from uta.app.persistence import register_task_persistence
    from uta.testgen.ports.registry import reset_task_persistence_provider

    register_task_persistence()
    yield
    reset_task_persistence_provider()


def test_standalone_execution_confines_database_prompts_and_cleanup(tmp_path, monkeypatch):
    runner_home = tmp_path / "runner"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))

    with open_standalone_generation_execution(_python_request(repo)) as execution:
        assert execution.request.task_id is not None
        assert execution.request.task_db_path == execution.root / "tasks.sqlite"
        assert execution.root.parent == runner_home / "standalone-generation"
        db_path = execution.request.task_db_path
        assert db_path.stat().st_mode & 0o777 == 0o600
        wal_path = db_path.with_name(f"{db_path.name}-wal")
        shm_path = db_path.with_name(f"{db_path.name}-shm")
        connection = TaskDB(db_path).connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE repo_tasks SET updated_at = updated_at WHERE id = ?",
                (int(execution.request.task_id),),
            )
            assert wal_path.is_file()
            assert shm_path.is_file()
            assert wal_path.stat().st_mode & 0o777 == 0o600
            assert shm_path.stat().st_mode & 0o777 == 0o600
        finally:
            connection.rollback()
            connection.close()
        task = TaskDB(db_path).get_repo_task(int(execution.request.task_id))
        assert task is not None
        snapshot = json.loads(task["config_snapshot_json"])
        assert snapshot["generation_engine_version"] == 2
        assert snapshot["generation_cycle_v2_enabled"] is True
        assert (execution.root / "workflow-state" / "checkpoints").is_dir()
        assert (execution.root / "workflow-state" / "results").is_dir()
        prompt_dir = execution.prompt_artifact_scope.durable_directory(
            unit_id="unit-0001", operation_id="op-0001"
        )
        assert prompt_dir.relative_to(execution.root).parts[:1] == ("prompts",)
        assert prompt_dir.stat().st_mode & 0o777 == 0o700
        root = execution.root

    assert not root.exists()


def test_standalone_execution_fails_immediately_without_persistence_provider(tmp_path, monkeypatch):
    from uta.testgen.ports.registry import reset_task_persistence_provider

    reset_task_persistence_provider()
    runner_home = tmp_path / "runner"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))

    with pytest.raises(RuntimeError, match="persistence provider unavailable"):
        with open_standalone_generation_execution(_python_request(repo)):
            pass


def test_standalone_pruner_skips_live_lease_and_deletes_old_orphan(tmp_path, monkeypatch):
    runner_home = tmp_path / "runner"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))

    with open_standalone_generation_execution(_python_request(repo)) as execution:
        old = execution.root.parent / ("a" * 32)
        old.mkdir(mode=0o700)
        (old / "tasks.sqlite").write_bytes(b"old")
        stale = time.time() - 40 * 86400
        os.utime(old, (stale, stale))

        deleted = prune_standalone_generation_executions(
            cutoff_timestamp=time.time() - 30 * 86400
        )

        assert deleted == 1
        assert execution.root.exists()
        assert not old.exists()


def test_standalone_setup_failure_removes_fresh_execution_root(tmp_path, monkeypatch):
    runner_home = tmp_path / "runner"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    monkeypatch.setattr(
        "uta.testgen.standalone_execution._acquire_lease",
        lambda _root: (_ for _ in ()).throw(OSError("lease failed")),
    )

    with pytest.raises(OSError, match="lease failed"):
        with open_standalone_generation_execution(_python_request(repo)):
            pass

    assert list((runner_home / "standalone-generation").glob("*")) == []




def test_standalone_projection_restores_public_identity_and_rejects_internal_paths():
    projected = project_standalone_final_state(
        {
            "results": {"x": {"status": "passed"}},
            "session_ids": ["session"],
            "phase_timings": {"generate": 1.5},
            "current_stage": "complete",
            "task_id": 91,
            "task_db_path": "/private/tasks.sqlite",
            "workflow_run_id": "run",
            "unit_id": "unit",
            "backend_context": {"prompt": "/private/prompt"},
        },
        original_task_id=None,
        original_task_db_path=None,
    )

    assert projected == {
        "results": {"x": {"status": "passed"}},
        "session_ids": ["session"],
        "phase_timings": {"generate": 1.5},
        "current_stage": "complete",
        "task_id": None,
        "task_db_path": None,
    }


def test_ephemeral_prompt_scope_rejects_a_root_different_from_its_owner(tmp_path):
    scope = PromptArtifactScope(
        root=tmp_path / "owner",
        run_id="a" * 32,
        task_id=1,
        managed=True,
        ephemeral_root=tmp_path / "other",
    )

    with pytest.raises(PromptArtifactScopeError, match="must match its owner root"):
        scope.durable_directory(unit_id="unit-0001", operation_id="op-0001")


def test_python_adapter_uses_neutral_harness_factory_from_invocation_context(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = object()
    calls = []
    monkeypatch.setattr(
        "uta.language.python.cycle_inputs.prepare_python_cycle_state",
        lambda state: {"repo_path": state["repo_path"], "language": "python"},
    )

    binding = PythonLanguageAdapter().generation_cycle_binding(
        {
            "repo_path": str(repo),
            "backend_context": {
                "harness_factory": lambda path: calls.append(path) or runner,
            },
        }
    )

    assert binding.runner is runner
    assert calls == [repo]


def test_java_adapter_uses_neutral_harness_factory_from_invocation_context(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = object()
    calls = []
    monkeypatch.setattr(
        "uta.language.java.cycle_inputs.prepare_java_cycle_state",
        lambda state: {"repo_path": state["repo_path"], "language": "java"},
    )

    binding = JavaLanguageAdapter().generation_cycle_binding(
        {
            "repo_path": str(repo),
            "backend_context": {
                "harness_factory": lambda path: calls.append(path) or runner,
            },
        }
    )

    assert binding.runner is runner
    assert calls == [repo]


@pytest.mark.parametrize("language", ["java", "python"])
def test_taskless_batch_entry_uses_synthetic_identity_without_leaking_it(
    language, tmp_path, monkeypatch
):
    runner_home = tmp_path / "runner"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    seen = {}

    class Workflow:
        def invoke(self, state):
            seen.update(state)
            return {
                **state,
                "results": {"target": {"status": "passed"}},
                "session_ids": ["session"],
                "current_stage": "complete",
                "finished": True,
            }

    if language == "java":
        request = JavaBatchGenerationRequest.from_class_fqns(
            repo_path=repo, class_fqns=["demo.Target"]
        )
        result = run_java_batch_generation(request, workflow_app=Workflow())
    else:
        request = _python_request(repo)
        result = PythonBatchGenerator(workflow_app=Workflow()).run(request)

    assert seen["task_id"] is not None
    assert seen["task_db_path"]
    assert result.final_state["task_id"] is None
    assert result.final_state["task_db_path"] is None
    assert "backend_context" not in result.final_state
    assert list((runner_home / "standalone-generation").glob("*")) == []

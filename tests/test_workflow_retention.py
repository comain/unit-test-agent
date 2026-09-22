"""Product-owned retention delegates checkpoint deletion to agent-core."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner
from agent_core.runtime import AgentProgressEvent

from uta.app.cli import main
from uta.tasks.manager import TaskManager
from uta.app.retention import prune_workflows
from uta.testgen.batches import ensure_stable_generation_batches
from uta.testgen.graph.application import workflow_state_root
from uta.testgen.operations import OperationArtifactStore, WorkflowOperationLedger
from uta.testgen.progress import UtaProgressEventStore, UtaTaskEventStreamStore
from uta.testgen.prompts import open_prompt_artifact_scope


@pytest.fixture(autouse=True)
def _isolate_prompt_retention_root(tmp_path, monkeypatch):
    monkeypatch.setenv("UTA_RUNNER_HOME", str(tmp_path / "runner-home"))


class DeleteSpy:
    def __init__(self):
        self.identities = []

    def delete_thread(self, thread_id):
        self.identities.append(thread_id)


def _factory(spy):
    @contextmanager
    def open_spy(path, **kwargs):
        yield spy

    return open_spy


def _completed_operation(manager, task_id, batch, root):
    ledger = WorkflowOperationLedger(
        manager.db,
        OperationArtifactStore(root / "results"),
        fingerprint=lambda state, phase, step: "clean",
        allowed_edit=lambda state, row: False,
        output_fingerprints=lambda state, phase, step: {},
    )
    state = {
        "task_id": task_id,
        "workflow_run_id": batch.workflow_run_id,
        "unit_id": batch.unit_id,
        "attempts_by_phase": {},
    }
    decision = ledger.classify(
        phase="complete_generation", step="deterministic", state=state
    )
    state.update({key: value for key, value in decision.items() if key != "outcome"})
    ledger.record_result(
        phase="complete_generation",
        step="deterministic",
        state=state,
        result={"phase_outcome": "passed"},
    )
    return decision["operation_id"]


def _setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(
        repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"]
    )
    batches = ensure_stable_generation_batches(
        manager.db,
        repo_task_id=task_id,
        ordered_target_ids=["pkg.A", "pkg.B"],
        batch_size=1,
        workflow_run_id="old-run",
    )
    root = workflow_state_root(manager.db_path)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    operation_ids = [
        _completed_operation(manager, task_id, batch, root) for batch in batches
    ]
    return manager, task_id, batches, root, operation_ids


def test_terminal_task_retention_deletes_lineages_then_product_evidence(tmp_path):
    manager, task_id, batches, root, operation_ids = _setup(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    manager.db.update_repo_task(
        task_id, status="COMPLETED", finished_at=old.isoformat(), updated_at=old.isoformat()
    )
    spy = DeleteSpy()

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(spy),
    )

    assert set(spy.identities) == {
        f"uta:test-generation-cycle:v2:{task_id}:{batch.unit_id}:{batch.workflow_run_id}"
        for batch in batches
    }
    assert result.lineages_deleted == 2
    assert result.operations_deleted == 2
    assert result.artifacts_deleted == 2
    assert all(manager.db.get_workflow_operation(item) is None for item in operation_ids)
    assert not (root / "results" / "old-run").exists()


def test_retention_order_matches_the_workflow_v2_contract(tmp_path, monkeypatch):
    import uta.app.retention as retention

    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    batch = ensure_stable_generation_batches(
        manager.db,
        repo_task_id=task_id,
        ordered_target_ids=["pkg.A"],
        batch_size=1,
        workflow_run_id="old-run",
    )[0]
    _completed_operation(manager, task_id, batch, workflow_state_root(manager.db_path))
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    manager.db.update_repo_task(
        task_id,
        status="COMPLETED",
        finished_at=old.isoformat(),
        updated_at=old.isoformat(),
    )
    trace = []

    class Checkpointer:
        def delete_thread(self, thread_id):
            trace.append("checkpoint")

    class Artifacts:
        def __init__(self, root):
            pass

        def delete_unit(self, workflow_run_id, unit_id):
            trace.append("operation_artifacts")
            return 1

    class Prompts:
        def prune_standalone(self, *, cutoff_timestamp):
            return 0

        def delete_managed_unit(self, **kwargs):
            trace.append("prompts")
            return 1

        def delete_managed_legacy(self, **kwargs):
            return 0

        def delete_terminal_task_legacy(self, **kwargs):
            return 0

    original_transaction = manager.db.transaction

    @contextmanager
    def traced_transaction():
        with original_transaction() as connection:
            class Connection:
                def execute(self, statement, parameters=()):
                    if "DELETE FROM workflow_operations" in statement:
                        trace.append("operation_rows")
                    return connection.execute(statement, parameters)

            yield Connection()

    monkeypatch.setattr(retention, "TaskDB", lambda path: manager.db)
    monkeypatch.setattr(retention, "OperationArtifactStore", Artifacts)
    monkeypatch.setattr(
        retention, "_prompt_retention_outside_repositories", lambda db: Prompts()
    )
    monkeypatch.setattr(
        retention, "prune_standalone_generation_executions", lambda **kwargs: 0
    )
    monkeypatch.setattr(manager.db, "transaction", traced_transaction)

    retention.prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(Checkpointer()),
    )

    contract = json.loads(
        (Path(__file__).parent / "fixtures/contracts/uta_workflow_v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert trace == contract["retention_order"]


def test_terminal_task_retention_deletes_managed_prompt_artifacts_by_identity(
    tmp_path, monkeypatch
):
    manager, task_id, batches, _, _ = _setup(tmp_path)
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    prompt_root = runner_home / "workflow-state" / "prompts"
    for batch in batches:
        operation = (
            prompt_root
            / "managed"
            / str(task_id)
            / "old-run"
            / batch.unit_id
            / "op-1"
        )
        operation.mkdir(parents=True)
        (operation / "prompt.md").write_text("durable", encoding="utf-8")
    legacy_run_id = uuid.uuid4().hex
    legacy = (
        prompt_root
        / "managed"
        / str(task_id)
        / legacy_run_id
        / "legacy"
        / "session-1"
        / "generate-1"
    )
    legacy.mkdir(parents=True)
    (legacy / "prompt.md").write_text("legacy", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    manager.db.update_repo_task(
        task_id, status="COMPLETED", finished_at=old.isoformat(), updated_at=old.isoformat()
    )

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert result.prompt_artifacts_deleted == 3
    assert not (prompt_root / "managed" / str(task_id) / "old-run").exists()
    assert not (prompt_root / "managed" / str(task_id) / legacy_run_id).exists()


def test_terminal_legacy_only_task_discovers_validated_run_identity(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    task_root = runner_home / "workflow-state/prompts/managed" / str(task_id)

    valid_run = task_root / uuid.uuid4().hex
    legacy_prompt = valid_run / "legacy/session-1/generate-1/prompt.md"
    legacy_prompt.parent.mkdir(parents=True)
    legacy_prompt.write_text("legacy", encoding="utf-8")
    durable_prompt = valid_run / "unit-0001-safe/op-1/prompt.md"
    durable_prompt.parent.mkdir(parents=True)
    durable_prompt.write_text("durable", encoding="utf-8")

    malformed = task_root / "not-a-run-uuid/legacy/session-1/generate-1/prompt.md"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("malformed", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "prompt.md").write_text("linked", encoding="utf-8")
    linked_run = task_root / uuid.uuid4().hex
    linked_run.symlink_to(outside, target_is_directory=True)
    linked_legacy_run = task_root / uuid.uuid4().hex
    linked_legacy_run.mkdir()
    (linked_legacy_run / "legacy").symlink_to(outside, target_is_directory=True)

    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    manager.db.update_repo_task(
        task_id,
        status="COMPLETED",
        finished_at=old.isoformat(),
        updated_at=old.isoformat(),
    )

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert result.lineages_deleted == 0, "legacy-only task has no durable identity"
    assert result.prompt_artifacts_deleted == 1
    assert not legacy_prompt.exists()
    assert durable_prompt.read_text(encoding="utf-8") == "durable"
    assert malformed.read_text(encoding="utf-8") == "malformed"
    assert linked_run.is_symlink()
    assert (linked_legacy_run / "legacy").is_symlink()
    assert (outside / "prompt.md").read_text(encoding="utf-8") == "linked"


def test_recent_or_active_unsuperseded_lineage_is_retained(tmp_path):
    manager, task_id, _, _, operation_ids = _setup(tmp_path)
    manager.db.update_repo_task(task_id, status="RUNNING")
    spy = DeleteSpy()

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(spy),
    )

    assert result.lineages_deleted == 0
    assert spy.identities == []
    assert all(manager.db.get_workflow_operation(item) is not None for item in operation_ids)


def test_active_managed_prompt_artifacts_are_retained(tmp_path, monkeypatch):
    manager, task_id, batches, _, _ = _setup(tmp_path)
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    prompt = (
        runner_home
        / "workflow-state/prompts/managed"
        / str(task_id)
        / "old-run"
        / batches[0].unit_id
        / "op-1/prompt.md"
    )
    prompt.parent.mkdir(parents=True)
    prompt.write_text("active", encoding="utf-8")
    manager.db.update_repo_task(task_id, status="RUNNING")

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert result.prompt_artifacts_deleted == 0
    assert prompt.read_text(encoding="utf-8") == "active"


def test_old_superseded_lineage_is_pruned_while_replacement_task_is_active(tmp_path):
    manager, task_id, batches, _, operation_ids = _setup(tmp_path)
    manager.mark_stopped(task_id, reason="replace")
    manager.clean_rerun_generation(
        task_id,
        reason="corrupt checkpoint",
        confirm_task_id=task_id,
        requested_by="operator",
    )
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE workflow_run_supersessions SET requested_at=? WHERE repo_task_id=?",
            (old.isoformat(), task_id),
        )
    spy = DeleteSpy()

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(spy),
    )

    assert set(spy.identities) == {
        f"uta:test-generation-cycle:v2:{task_id}:{batch.unit_id}:{batch.workflow_run_id}"
        for batch in batches
    }
    assert result.operations_deleted == 2
    assert all(manager.db.get_workflow_operation(item) is None for item in operation_ids)
    assert manager.db.get_repo_task(task_id)["status"] == "QUEUED"
    with manager.db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM workflow_run_supersessions WHERE repo_task_id=?",
            (task_id,),
        ).fetchone()[0] == 1, "audit rows outlive checkpoint data"


def test_old_superseded_lineage_deletes_its_managed_prompt_identity(
    tmp_path, monkeypatch
):
    manager, task_id, batches, _, _ = _setup(tmp_path)
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    run_root = (
        runner_home / "workflow-state/prompts/managed" / str(task_id) / "old-run"
    )
    prompt = run_root / batches[0].unit_id / "op-1/prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("superseded", encoding="utf-8")
    legacy_prompt = (
        run_root.parent
        / uuid.uuid4().hex
        / "legacy/session-1/generate-1/prompt.md"
    )
    legacy_prompt.parent.mkdir(parents=True)
    legacy_prompt.write_text("unpersisted invocation", encoding="utf-8")
    manager.mark_stopped(task_id, reason="replace")
    manager.clean_rerun_generation(
        task_id,
        reason="corrupt checkpoint",
        confirm_task_id=task_id,
        requested_by="operator",
    )
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE workflow_run_supersessions SET requested_at=? WHERE repo_task_id=?",
            (old.isoformat(), task_id),
        )

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert result.prompt_artifacts_deleted == 1
    assert not prompt.exists()
    assert legacy_prompt.exists(), "supersession does not persist the legacy invocation ID"

    manager.db.update_repo_task(
        task_id,
        status="COMPLETED",
        finished_at=old.isoformat(),
        updated_at=old.isoformat(),
    )
    terminal_result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert terminal_result.prompt_artifacts_deleted == 1
    assert not legacy_prompt.exists()


def test_old_rerun_audit_deletes_only_linked_legacy_scope_while_replacement_active(
    tmp_path, monkeypatch
):
    manager, task_id, _, _, _ = _setup(tmp_path)
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    task_root = runner_home / "workflow-state/prompts/managed" / str(task_id)
    old_run = uuid.uuid4().hex
    old_prompt = task_root / old_run / "legacy/session-1/generate-1/prompt.md"
    old_prompt.parent.mkdir(parents=True)
    old_prompt.write_text("old", encoding="utf-8")
    with manager.db.transaction() as conn:
        conn.execute(
            "INSERT INTO legacy_prompt_scopes("
            "repo_task_id, legacy_run_id, language, opened_at) VALUES (?, ?, ?, ?)",
            (task_id, old_run, "python", "2026-08-01T00:00:00+00:00"),
        )
    manager.mark_stopped(task_id, reason="replace")
    manager.clean_rerun_generation(
        task_id,
        reason="replace old legacy invocation",
        confirm_task_id=task_id,
        requested_by="operator",
    )

    current_run = uuid.uuid4().hex
    current_prompt = (
        task_root / current_run / "legacy/session-2/generate-1/prompt.md"
    )
    current_prompt.parent.mkdir(parents=True)
    current_prompt.write_text("current", encoding="utf-8")
    with manager.db.transaction() as conn:
        conn.execute(
            "INSERT INTO legacy_prompt_scopes("
            "repo_task_id, legacy_run_id, language, opened_at) VALUES (?, ?, ?, ?)",
            (task_id, current_run, "python", "2026-08-02T00:00:00+00:00"),
        )
    forged_run = uuid.uuid4().hex
    forged_prompt = task_root / forged_run / "legacy/session-3/generate-1/prompt.md"
    forged_prompt.parent.mkdir(parents=True)
    forged_prompt.write_text("forged", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    with manager.db.connect() as conn:
        audit = conn.execute(
            "SELECT id, payload_json FROM task_events WHERE repo_task_id=? "
            "AND event_type='generation_cycle_clean_rerun_requested'",
            (task_id,),
        ).fetchone()
        payload = json.loads(audit["payload_json"])
        payload["old_legacy_scopes"].append(
            {"language": "python", "run_id": forged_run}
        )
        conn.execute(
            "UPDATE task_events SET payload_json=?, created_at=?, ts=? WHERE id=?",
            (json.dumps(payload), old.isoformat(), old.isoformat(), audit["id"]),
        )
        conn.execute(
            "UPDATE workflow_run_supersessions SET requested_at=? WHERE repo_task_id=?",
            (old.isoformat(), task_id),
        )
        conn.execute(
            "UPDATE legacy_prompt_scopes SET superseded_at=? "
            "WHERE repo_task_id=? AND legacy_run_id=?",
            (old.isoformat(), task_id, old_run),
        )

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert result.prompt_artifacts_deleted == 1
    assert not old_prompt.exists()
    assert current_prompt.read_text(encoding="utf-8") == "current"
    assert forged_prompt.read_text(encoding="utf-8") == "forged"


def test_standalone_crash_orphan_retention_is_age_bounded_safe_and_idempotent(
    tmp_path, monkeypatch
):
    manager = TaskManager(tmp_path / "tasks.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    runner_home = tmp_path / "runner-home"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    standalone = runner_home / "workflow-state/prompts/standalone"
    current = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
    cutoff = current - timedelta(days=30)

    def orphan(name, age):
        prompt = standalone / name / "python/session-1/generate-1/prompt.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text(name, encoding="utf-8")
        os.utime(standalone / name, (age.timestamp(), age.timestamp()))
        return prompt

    expired = orphan(uuid.uuid4().hex, cutoff - timedelta(seconds=1))
    boundary = orphan(uuid.uuid4().hex, cutoff)
    recent = orphan(uuid.uuid4().hex, cutoff + timedelta(seconds=1))
    malformed = orphan("not-a-run-uuid", cutoff - timedelta(days=1))
    unsafe = orphan(uuid.uuid4().hex, cutoff - timedelta(days=1))
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (unsafe.parent / "escape").symlink_to(outside)
    unsafe_run = unsafe.parents[3]
    os.utime(unsafe_run, ((cutoff - timedelta(days=1)).timestamp(),) * 2)
    linked_run = standalone / uuid.uuid4().hex
    linked_run.symlink_to(tmp_path, target_is_directory=True)

    first = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=current,
        checkpointer_factory=_factory(DeleteSpy()),
    )
    second = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=current,
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert first.prompt_artifacts_deleted == 2
    assert second.prompt_artifacts_deleted == 0
    assert not expired.exists() and not boundary.exists()
    assert recent.exists() and malformed.exists() and unsafe.exists()
    assert linked_run.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_standalone_retention_skips_an_old_but_actively_locked_scope(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    current = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)

    with open_prompt_artifact_scope(
        repo_path=repo, task_id=None, workflow_run_id=None
    ) as scope:
        run_directory = scope.root / "standalone" / scope.run_id
        old = current - timedelta(days=40)
        os.utime(run_directory, (old.timestamp(), old.timestamp()))

        result = prune_workflows(
            manager.db_path,
            older_than_days=30,
            now=current,
            checkpointer_factory=_factory(DeleteSpy()),
        )

        assert result.prompt_artifacts_deleted == 0
        assert run_directory.is_dir()

    assert not run_directory.exists()


def test_standalone_retention_prunes_a_stale_unlocked_crash_lease(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    current = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
    run_directory = (
        tmp_path
        / "runner-home/workflow-state/prompts/standalone"
        / uuid.uuid4().hex
    )
    prompt = run_directory / "python/session-1/generate-1/prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("crashed", encoding="utf-8")
    (run_directory / ".active.lock").write_text("", encoding="utf-8")
    old = current - timedelta(days=40)
    os.utime(run_directory, (old.timestamp(), old.timestamp()))

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=current,
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert result.prompt_artifacts_deleted == 2
    assert not run_directory.exists()


def test_retention_never_scans_prompt_root_inside_target_repository(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    runner_home = repo / ".uta_cache"
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    prompt = (
        runner_home
        / "workflow-state/prompts/standalone"
        / uuid.uuid4().hex
        / "python/session-1/generate-1/prompt.md"
    )
    prompt.parent.mkdir(parents=True)
    prompt.write_text("never delete", encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(days=40)
    os.utime(prompt.parents[3], (old.timestamp(), old.timestamp()))

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert result.prompt_artifacts_deleted == 0
    assert prompt.read_text(encoding="utf-8") == "never delete"


def test_retention_is_disabled_when_target_repository_is_below_prompt_root(
    tmp_path, monkeypatch
):
    runner_home = tmp_path / "runner-home"
    prompt_root = runner_home / "workflow-state/prompts"
    repo = prompt_root / "managed/1/run-1/unit-1/repo"
    repo.mkdir(parents=True)
    manager = TaskManager(tmp_path / "tasks.db")
    manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])
    monkeypatch.setenv("UTA_RUNNER_HOME", str(runner_home))
    orphan = (
        prompt_root
        / "standalone"
        / uuid.uuid4().hex
        / "python/session-1/generate-1/prompt.md"
    )
    orphan.parent.mkdir(parents=True)
    orphan.write_text("never delete", encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(days=40)
    os.utime(orphan.parents[3], (old.timestamp(), old.timestamp()))

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
        checkpointer_factory=_factory(DeleteSpy()),
    )

    assert result.prompt_artifacts_deleted == 0
    assert orphan.read_text(encoding="utf-8") == "never delete"


def test_checkpoint_delete_failure_preserves_product_evidence(tmp_path):
    manager, task_id, _, _, operation_ids = _setup(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    manager.db.update_repo_task(task_id, status="FAILED", finished_at=old.isoformat())

    class Broken(DeleteSpy):
        def delete_thread(self, thread_id):
            raise OSError("checkpoint locked")

    try:
        prune_workflows(
            manager.db_path,
            older_than_days=30,
            now=datetime.now(timezone.utc),
            checkpointer_factory=_factory(Broken()),
        )
    except OSError as exc:
        assert "locked" in str(exc)
    else:
        raise AssertionError("checkpoint deletion failure was swallowed")

    assert all(manager.db.get_workflow_operation(item) is not None for item in operation_ids)


def test_prune_workflows_cli_uses_the_same_retention_path(tmp_path):
    manager, task_id, _, _, operation_ids = _setup(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    manager.db.update_repo_task(task_id, status="COMPLETED", finished_at=old.isoformat())

    result = CliRunner().invoke(
        main,
        [
            "tasks",
            "prune-workflows",
            "--older-than-days",
            "30",
            "--task-db",
            str(manager.db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Pruned 2 workflow lineages" in result.output
    assert "prompt artifacts" in result.output.replace("\n", "")
    assert all(manager.db.get_workflow_operation(item) is None for item in operation_ids)


def test_retention_deletes_a_real_agent_core_checkpoint_lineage(tmp_path):
    from agent_core.workflow import NodeRegistry, WorkflowRunIdentity, WorkflowSpec, open_checkpointer
    from agent_core.workflow.graph import build_graph

    manager, task_id, batches, _, _ = _setup(tmp_path)
    batch = batches[0]
    identity = WorkflowRunIdentity(
        product="uta",
        task_id=str(task_id),
        unit_id=batch.unit_id,
        workflow_run_id=batch.workflow_run_id,
        cycle="test-generation-cycle",
        version="v2",
    )
    registry = NodeRegistry()
    registry.add_node("finish", lambda state: {"finished": True})
    spec = WorkflowSpec.from_dict(
        {
            "name": "retention-fixture",
            "nodes": ["finish"],
            "entry": "finish",
            "edges": [["finish", "__end__"]],
        }
    )
    checkpoint_path = workflow_state_root(manager.db_path) / "checkpoints.sqlite"
    repo_path = Path(str(manager.db.get_repo_task(task_id)["repo_path"]))
    with open_checkpointer(checkpoint_path, forbidden_roots=(repo_path,)) as saver:
        graph = build_graph(spec, registry, checkpointer=saver)
        graph.invoke({}, config=identity.invoke_config(recursion_limit=10))
        assert graph.get_state(identity.invoke_config(recursion_limit=10)).values

    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    manager.db.update_repo_task(task_id, status="COMPLETED", finished_at=old.isoformat())
    prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
    )

    with open_checkpointer(checkpoint_path, forbidden_roots=(repo_path,)) as saver:
        graph = build_graph(spec, registry, checkpointer=saver)
        snapshot = graph.get_state(identity.invoke_config(recursion_limit=10))
        assert not snapshot.values
        assert not snapshot.next


def test_retention_deletes_only_old_agent_progress_detail(tmp_path):
    manager, task_id, _, _, _ = _setup(tmp_path)
    store = UtaProgressEventStore(
        manager.db,
        repo_task_id=task_id,
        workflow_run_id="run",
        unit_id="unit",
    )
    store.append_batch(
        session_id="session-a",
        events=[
            AgentProgressEvent(
                sequence=1,
                session_id="session-a",
                phase="generate_tests",
                kind="text",
                summary="working",
            )
        ],
    )
    manager.db.add_event(
        task_id, None, "phase_completed", "Generation complete", stage="generate_tests"
    )
    manager.db.finish_task_with_terminal_event(
        task_id, status="COMPLETED", message="Task complete", stage="finished"
    )
    old = (datetime.now(timezone.utc) - timedelta(days=40)).replace(microsecond=0)
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE task_events SET created_at=?, ts=? WHERE repo_task_id=?",
            (old.isoformat(), old.isoformat(), task_id),
        )

    result = prune_workflows(
        manager.db_path,
        older_than_days=30,
        now=datetime.now(timezone.utc),
    )

    assert result.progress_events_deleted == 1
    rows = UtaTaskEventStreamStore(manager.db).events_since(task_ref=str(task_id))
    event_types = [row["event_type"] for row in rows]
    assert "agent_progress" not in event_types
    assert "phase_completed" in event_types
    assert event_types[-1] == "task_terminal"

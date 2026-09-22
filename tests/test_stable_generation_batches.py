"""Stable product identities for generation-cycle units."""

from __future__ import annotations

import pytest

from uta.app.persistence import TaskPersistenceAdapter
from uta.tasks.manager import TaskManager
from uta.testgen.batches import (
    GenerationBatchIdentityError,
    ensure_stable_generation_batches,
    first_non_terminal_batch,
)


def _manager(tmp_path, targets=("pkg.A", "pkg.B", "pkg.C")):
    manager = TaskManager(tmp_path / "tasks.db")
    task_id = manager.create_task(repo_path=str(tmp_path / "repo"), class_fqns=targets)
    return manager, task_id


def test_first_partition_is_persisted_and_restart_reconstructs_it(tmp_path):
    manager, task_id = _manager(tmp_path)

    first = ensure_stable_generation_batches(
        manager.db,
        repo_task_id=task_id,
        ordered_target_ids=["pkg.A", "pkg.B", "pkg.C"],
        batch_size=2,
        workflow_run_id="run-one",
    )
    restarted = ensure_stable_generation_batches(
        manager.db,
        repo_task_id=task_id,
        ordered_target_ids=["pkg.A", "pkg.B", "pkg.C"],
        batch_size=99,
    )

    assert [(batch.workflow_run_id, batch.target_ids) for batch in first] == [
        ("run-one", ("pkg.A", "pkg.B")),
        ("run-one", ("pkg.C",)),
    ]
    assert restarted == first, "restart must not regroup persisted membership"


def test_partial_completion_selects_the_original_batch_with_full_membership(tmp_path):
    manager, task_id = _manager(tmp_path)
    batches = ensure_stable_generation_batches(
        manager.db,
        repo_task_id=task_id,
        ordered_target_ids=["pkg.A", "pkg.B", "pkg.C"],
        batch_size=2,
        workflow_run_id="run-one",
    )
    first_row = manager.db.find_class_task(task_id, "pkg.A")
    manager.db.update_class_task(first_row["id"], status="PASS")

    selected = first_non_terminal_batch(manager.db, batches)

    assert selected == batches[0]
    assert selected.target_ids == ("pkg.A", "pkg.B")


def test_persisted_run_rejects_a_conflicting_requested_identity(tmp_path):
    manager, task_id = _manager(tmp_path)
    ensure_stable_generation_batches(
        manager.db,
        repo_task_id=task_id,
        ordered_target_ids=["pkg.A", "pkg.B", "pkg.C"],
        batch_size=2,
        workflow_run_id="run-one",
    )

    with pytest.raises(GenerationBatchIdentityError, match="does not match"):
        ensure_stable_generation_batches(
            manager.db,
            repo_task_id=task_id,
            ordered_target_ids=["pkg.A", "pkg.B", "pkg.C"],
            batch_size=2,
            workflow_run_id="run-two",
        )


def test_mixed_or_malformed_persisted_keys_fail_before_repartition(tmp_path):
    manager, task_id = _manager(tmp_path)
    row_a = manager.db.find_class_task(task_id, "pkg.A")
    manager.db.update_class_task(row_a["id"], batch_key="run-one/unit-one")

    with pytest.raises(GenerationBatchIdentityError, match="mixed"):
        ensure_stable_generation_batches(
            manager.db,
            repo_task_id=task_id,
            ordered_target_ids=["pkg.A", "pkg.B", "pkg.C"],
            batch_size=2,
        )

    for row in manager.db.list_class_tasks(task_id):
        manager.db.update_class_task(row["id"], batch_key="not-a-key")
    with pytest.raises(GenerationBatchIdentityError, match="malformed"):
        ensure_stable_generation_batches(
            manager.db,
            repo_task_id=task_id,
            ordered_target_ids=["pkg.A", "pkg.B", "pkg.C"],
            batch_size=2,
        )


def test_partition_rejects_a_filtered_or_reordered_target_set(tmp_path):
    manager, task_id = _manager(tmp_path)

    with pytest.raises(GenerationBatchIdentityError, match="exact full ordered target list"):
        ensure_stable_generation_batches(
            manager.db,
            repo_task_id=task_id,
            ordered_target_ids=["pkg.B", "pkg.A"],
            batch_size=2,
        )


def test_persistence_adapter_expands_resumed_subset_to_persisted_full_order(tmp_path):
    manager, task_id = _manager(tmp_path)
    adapter = TaskPersistenceAdapter(tmp_path / "tasks.db")

    batches = adapter.ensure_stable_batches(
        repo_task_id=task_id,
        ordered_target_ids=["pkg.A", "pkg.C"],
        batch_size=2,
        workflow_run_id="run-resume",
    )

    assert [batch.target_ids for batch in batches] == [("pkg.A", "pkg.B"), ("pkg.C",)]

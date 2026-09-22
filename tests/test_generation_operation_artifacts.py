"""Stable identities and crash-safe generation operation artifacts."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path

import pytest

from uta.testgen.operations import (
    ArtifactValidationError,
    OperationArtifactStore,
    OperationIdentity,
    OperationResultEnvelope,
)


def identity(**overrides) -> OperationIdentity:
    values = {
        "workflow_run_id": "run-1",
        "unit_id": "unit-1",
        "phase": "generate_tests",
        "operation_step": "turn",
        "logical_attempt": 1,
        "execution_ordinal": 0,
        "input_fingerprint": "input-sha",
    }
    values.update(overrides)
    return OperationIdentity(**values)


def envelope(**overrides) -> OperationResultEnvelope:
    values = {
        "identity": identity(),
        "result_kind": "turn",
        "result": {"status": "completed", "text": "done", "usage": {"input": 3}},
        "resulting_workspace_fingerprint": "workspace-sha",
        "output_fingerprints": {"tests/ATest.java": "test-sha"},
        "prerequisite_operation_ids": ("plan-op",),
    }
    values.update(overrides)
    return OperationResultEnvelope(**values)


def test_operation_id_changes_for_every_identity_component():
    original = identity()
    variants = [
        identity(workflow_run_id="run-2"),
        identity(unit_id="unit-2"),
        identity(phase="fix_compile"),
        identity(operation_step="interpret"),
        identity(logical_attempt=2),
        identity(execution_ordinal=1),
        identity(input_fingerprint="other"),
    ]

    assert len(original.operation_id) == 64
    assert len({original.operation_id, *(item.operation_id for item in variants)}) == 8
    assert identity().operation_id == original.operation_id


def test_artifact_round_trips_with_owner_only_permissions(tmp_path):
    store = OperationArtifactStore(tmp_path / "workflow-state" / "results")

    stored = store.write(envelope())
    restored = store.read(stored.relative_path, expected_sha256=stored.sha256)

    assert restored == envelope()
    assert stored.relative_path == f"run-1/unit-1/{identity().operation_id}.json"
    assert os.stat(store.root).st_mode & 0o777 == 0o700
    assert os.stat(store.root / "run-1").st_mode & 0o777 == 0o700
    assert os.stat(store.root / "run-1" / "unit-1").st_mode & 0o777 == 0o700
    assert os.stat(store.root / stored.relative_path).st_mode & 0o777 == 0o600


def test_the_frozen_v1_operation_artifact_still_rehydrates(tmp_path):
    fixture_root = Path(__file__).parent / "fixtures/contracts"
    contract = json.loads(
        (fixture_root / "uta_workflow_v2.json").read_text(encoding="utf-8")
    )["operation_artifact"]
    data = (fixture_root / "uta_operation_envelope_v1.json").read_bytes()
    store = OperationArtifactStore(tmp_path / "results")
    target = store.root / contract["relative_path"]
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_bytes(data)
    target.chmod(0o600)

    restored = store.read(
        contract["relative_path"], expected_sha256=hashlib.sha256(data).hexdigest()
    )

    assert restored == envelope()
    assert hashlib.sha256(data).hexdigest() == contract["sha256"]


def test_an_identical_write_is_idempotent_but_never_overwrites_a_result(tmp_path):
    store = OperationArtifactStore(tmp_path / "results")
    first = store.write(envelope())

    assert store.write(envelope()) == first
    with pytest.raises(ArtifactValidationError, match="different artifact"):
        store.write(envelope(result={"status": "completed", "text": "changed"}))


def test_tampering_is_detected_before_an_artifact_is_returned(tmp_path):
    store = OperationArtifactStore(tmp_path / "results")
    stored = store.write(envelope())
    path = store.root / stored.relative_path
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(ArtifactValidationError, match="hash"):
        store.read(stored.relative_path, expected_sha256=stored.sha256)


@pytest.mark.parametrize(
    "bad_identity",
    [
        identity(workflow_run_id="../escape"),
        identity(unit_id="unit/escape"),
        identity(phase=""),
    ],
)
def test_identity_components_cannot_escape_the_results_root(tmp_path, bad_identity):
    store = OperationArtifactStore(tmp_path / "results")

    with pytest.raises(ArtifactValidationError, match="identifier"):
        store.write(envelope(identity=bad_identity))


def test_a_symlink_or_insecure_existing_root_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked-results"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ArtifactValidationError, match="symlink"):
        OperationArtifactStore(link)

    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    with pytest.raises(ArtifactValidationError, match="chmod 700"):
        OperationArtifactStore(insecure)


def test_an_orphan_final_artifact_can_be_loaded_by_identity(tmp_path):
    store = OperationArtifactStore(tmp_path / "results")
    store.write(envelope())

    restored, sha256 = store.read_for_identity(identity())

    assert restored.result["text"] == "done"
    assert len(sha256) == 64


def test_envelope_rejects_mismatched_embedded_operation_id(tmp_path):
    store = OperationArtifactStore(tmp_path / "results")
    stored = store.write(envelope())
    path = store.root / stored.relative_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["operation_id"] = "0" * 64
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(data)

    with pytest.raises(ArtifactValidationError, match="operation identity"):
        store.read_for_identity(identity())


def test_a_temporary_fragment_is_not_treated_as_a_final_result(tmp_path):
    store = OperationArtifactStore(tmp_path / "results")
    final = store.path_for(identity())
    final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    (final.parent / f".{final.name}.partial").write_text("partial", encoding="utf-8")

    assert store.read_for_identity(identity()) is None


def test_retention_deletes_one_unit_without_touching_its_sibling(tmp_path):
    store = OperationArtifactStore(tmp_path / "results")
    first = envelope(identity=identity(unit_id="unit-a"))
    second = envelope(identity=identity(unit_id="unit-b"))
    store.write(first)
    store.write(second)

    deleted = store.delete_unit("run-1", "unit-a")

    assert deleted == 1
    assert not store.path_for(first.identity).exists()
    assert store.path_for(second.identity).exists()

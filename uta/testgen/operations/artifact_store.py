"""UTA operation envelopes backed by agent-core's secure artifact store."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from agent_core.runtime import (
    ArtifactError,
    NamespaceLayout,
    SecureArtifactStore,
)

from uta.testgen.operations.models import (
    ArtifactValidationError,
    OperationIdentity,
    OperationResultEnvelope,
    StoredArtifact,
    _validate_identifier,
)


MAX_OPERATION_ARTIFACT_BYTES = 16 * 1024 * 1024


class OperationArtifactStore:
    """Preserve UTA's envelope/path facade while sharing file mechanics."""

    def __init__(
        self, root: Path, *, forbidden_roots: Sequence[Path | str] = ()
    ) -> None:
        self.root = Path(root).expanduser().absolute()
        try:
            self._store = SecureArtifactStore(
                self.root, forbidden_roots=tuple(forbidden_roots)
            )
        except ArtifactError as exc:
            raise ArtifactValidationError(str(exc)) from exc

    def path_for(self, identity: OperationIdentity) -> Path:
        identity.validate()
        return self.root / self._name(identity)

    def write(self, envelope: OperationResultEnvelope) -> StoredArtifact:
        data = _encode(envelope.as_dict())
        name = self._name(envelope.identity)
        try:
            stored = self._store.write_bytes(
                name,
                data,
                immutable=True,
                max_bytes=MAX_OPERATION_ARTIFACT_BYTES,
            )
        except ArtifactError as exc:
            message = str(exc)
            if "different content" in message:
                message = (
                    f"operation {envelope.identity.operation_id} already has "
                    "a different artifact"
                )
            raise ArtifactValidationError(message) from exc
        return StoredArtifact(stored.relative_path, stored.sha256)

    def read(
        self, relative_path: str, *, expected_sha256: str
    ) -> OperationResultEnvelope:
        name = self._validated_relative_path(relative_path)
        try:
            data = self._store.read_verified(
                name,
                sha256=expected_sha256,
                max_bytes=MAX_OPERATION_ARTIFACT_BYTES,
            )
        except (ArtifactError, FileNotFoundError) as exc:
            if isinstance(exc, FileNotFoundError):
                message = f"operation artifact is missing: {name}"
            else:
                message = "operation artifact hash does not match" if "digest" in str(exc) else str(exc)
            raise ArtifactValidationError(message) from exc
        return _decode(data)

    def read_for_identity(
        self, identity: OperationIdentity
    ) -> Optional[Tuple[OperationResultEnvelope, str]]:
        name = self._name(identity)
        if not self._store.exists(name):
            return None
        try:
            data = self._store.read_bytes(name, max_bytes=MAX_OPERATION_ARTIFACT_BYTES)
        except (ArtifactError, FileNotFoundError) as exc:
            raise ArtifactValidationError(str(exc)) from exc
        envelope = _decode(data)
        if envelope.identity != identity:
            raise ArtifactValidationError(
                "operation identity does not match artifact path"
            )
        return envelope, hashlib.sha256(data).hexdigest()

    def delete_unit(self, workflow_run_id: str, unit_id: str) -> int:
        _validate_identifier(workflow_run_id, name="workflow_run_id")
        _validate_identifier(unit_id, name="unit_id")
        namespace = f"{workflow_run_id}/{unit_id}"
        directory = self.root / workflow_run_id / unit_id
        if not directory.exists():
            return 0
        if directory.is_symlink() or not directory.is_dir():
            raise ArtifactValidationError(
                f"operation artifact unit is not a safe directory: {directory}"
            )
        names = frozenset(child.name for child in directory.iterdir())
        for name in names:
            if not _is_operation_artifact_entry(name):
                raise ArtifactValidationError(
                    f"unexpected operation artifact entry: {directory / name}"
                )
        final_count = sum(
            name.endswith(".json") and not name.startswith(".") for name in names
        )
        try:
            self._store.delete_namespace(
                namespace, NamespaceLayout(optional_files=names)
            )
        except ArtifactError as exc:
            raise ArtifactValidationError(str(exc)) from exc
        run_dir = self.root / workflow_run_id
        if run_dir.exists() and not any(run_dir.iterdir()):
            run_dir.rmdir()
        return final_count

    @staticmethod
    def _name(identity: OperationIdentity) -> str:
        identity.validate()
        return (
            f"{identity.workflow_run_id}/{identity.unit_id}/"
            f"{identity.operation_id}.json"
        )

    @staticmethod
    def _validated_relative_path(relative_path: str) -> str:
        relative = Path(relative_path)
        if relative.is_absolute() or len(relative.parts) != 3:
            raise ArtifactValidationError(
                "artifact path must be a relative run/unit/result path"
            )
        _validate_identifier(relative.parts[0], name="workflow_run_id")
        _validate_identifier(relative.parts[1], name="unit_id")
        filename = relative.parts[2]
        if not filename.endswith(".json"):
            raise ArtifactValidationError("artifact path must name a JSON result")
        _validate_identifier(filename[:-5], name="operation_id")
        return relative.as_posix()


def _is_operation_artifact_entry(name: str) -> bool:
    return (
        (name.endswith(".json") and not name.startswith("."))
        or name.endswith(".lock")
        or name.endswith(".partial")
    )


def _encode(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError("operation artifact is not JSON-safe") from exc


def _decode(data: bytes) -> OperationResultEnvelope:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError("operation artifact is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ArtifactValidationError("operation artifact root must be an object")
    return OperationResultEnvelope.from_dict(payload)


__all__ = ["OperationArtifactStore"]

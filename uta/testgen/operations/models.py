"""The values an operation is recorded as.

Dependency-light on purpose: the artifact store, the cost gate, the ledger
and the reconciler all speak in these, so they cannot live with any one of
them without making the other three depend on that one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Tuple





#: Anything that becomes part of a path is matched against this first. It lives
#: with the identity it validates, not with the store that writes the path:
#: `OperationIdentity` checks its own fields on construction, so the rule has to
#: be reachable without importing anything that writes files.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: Bumped when the envelope's shape changes in a way a reader must notice.
ENVELOPE_SCHEMA_VERSION = 1

#: The steps and kinds an operation may claim. Frozen sets rather than strings
#: checked inline, so an unknown value fails where it is introduced.
_STEPS = frozenset({"turn", "interpret", "deterministic"})
_KINDS = frozenset({"turn", "phase"})


class ArtifactValidationError(RuntimeError):
    """An operation artifact cannot be trusted or safely addressed."""


@dataclass(frozen=True)
class OperationIdentity:
    """The stable identity of one logical attempt or its crash replay."""

    workflow_run_id: str
    unit_id: str
    phase: str
    operation_step: str
    logical_attempt: int
    execution_ordinal: int
    input_fingerprint: str

    @property
    def operation_id(self) -> str:
        fields = (
            self.workflow_run_id,
            self.unit_id,
            self.phase,
            self.operation_step,
            str(self.logical_attempt),
            str(self.execution_ordinal),
            self.input_fingerprint,
        )
        return hashlib.sha256("\x00".join(fields).encode("utf-8")).hexdigest()

    def validate(self) -> None:
        for name in ("workflow_run_id", "unit_id", "phase"):
            _validate_identifier(getattr(self, name), name=name)
        if self.operation_step not in _STEPS:
            raise ArtifactValidationError(
                f"operation_step must be one of {sorted(_STEPS)}"
            )
        if self.logical_attempt < 0 or self.execution_ordinal < 0:
            raise ArtifactValidationError(
                "attempt and execution ordinal must be non-negative"
            )
        if not self.input_fingerprint:
            raise ArtifactValidationError("input_fingerprint must not be empty")


@dataclass(frozen=True)
class OperationResultEnvelope:
    """One JSON-safe turn or phase result and the evidence that validates it."""

    identity: OperationIdentity
    result_kind: str
    result: Mapping[str, Any]
    resulting_workspace_fingerprint: str
    output_fingerprints: Mapping[str, str]
    prerequisite_operation_ids: Tuple[str, ...] = ()
    envelope_schema_version: int = ENVELOPE_SCHEMA_VERSION

    def as_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "envelope_schema_version": self.envelope_schema_version,
            "operation_id": self.identity.operation_id,
            "identity": asdict(self.identity),
            "result_kind": self.result_kind,
            "result": dict(self.result),
            "resulting_workspace_fingerprint": self.resulting_workspace_fingerprint,
            "output_fingerprints": dict(self.output_fingerprints),
            "prerequisite_operation_ids": list(self.prerequisite_operation_ids),
        }

    def validate(self) -> None:
        self.identity.validate()
        if self.envelope_schema_version != ENVELOPE_SCHEMA_VERSION:
            raise ArtifactValidationError("unsupported operation artifact schema")
        if self.result_kind not in _KINDS:
            raise ArtifactValidationError(
                f"result_kind must be one of {sorted(_KINDS)}"
            )
        if not isinstance(self.result, Mapping):
            raise ArtifactValidationError("operation result must be a mapping")
        if not self.resulting_workspace_fingerprint:
            raise ArtifactValidationError(
                "resulting workspace fingerprint must not be empty"
            )
        if any(
            not str(key) or not str(value)
            for key, value in self.output_fingerprints.items()
        ):
            raise ArtifactValidationError(
                "output fingerprints must have non-empty keys and values"
            )
        for operation_id in self.prerequisite_operation_ids:
            if not isinstance(operation_id, str) or not operation_id:
                raise ArtifactValidationError(
                    "prerequisite operation IDs must be non-empty"
                )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OperationResultEnvelope":
        try:
            identity = OperationIdentity(**dict(payload["identity"]))
            envelope = cls(
                identity=identity,
                result_kind=str(payload["result_kind"]),
                result=dict(payload["result"]),
                resulting_workspace_fingerprint=str(
                    payload["resulting_workspace_fingerprint"]
                ),
                output_fingerprints={
                    str(key): str(value)
                    for key, value in dict(
                        payload.get("output_fingerprints") or {}
                    ).items()
                },
                prerequisite_operation_ids=tuple(
                    str(item)
                    for item in payload.get("prerequisite_operation_ids") or ()
                ),
                envelope_schema_version=int(payload["envelope_schema_version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactValidationError("malformed operation artifact") from exc
        envelope.validate()
        if payload.get("operation_id") != identity.operation_id:
            raise ArtifactValidationError("operation identity does not match its ID")
        return envelope


@dataclass(frozen=True)
class StoredArtifact:
    relative_path: str
    sha256: str


def _validate_identifier(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ArtifactValidationError(f"{name} is not a safe identifier")


__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "_validate_identifier",
    "ArtifactValidationError",
    "OperationIdentity",
    "OperationResultEnvelope",
    "StoredArtifact",
]

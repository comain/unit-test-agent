"""Application-owned paths and metadata for prompt artifacts.

Prompt text can contain source code and model instructions, so it never lives
under the target repository.  This module owns UTA identity and lifecycle
policy; agent-core owns the generic file materialization mechanics.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Literal, Mapping, Optional, Sequence


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INVOCATION_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_METADATA_MAX_BYTES = 4096
_STANDALONE_LEASE = ".active.lock"


class PromptArtifactScopeError(RuntimeError):
    """Prompt storage cannot be safely addressed outside the repository."""


class PromptMetadataError(ValueError):
    """Prompt identity metadata violates the fixed safe projection."""


@dataclass(frozen=True)
class PromptArtifactScope:
    """One managed workflow run or standalone command's prompt namespace."""

    root: Path
    run_id: str
    task_id: Optional[int]
    managed: bool
    ephemeral_root: Optional[Path] = None

    def durable_directory(self, *, unit_id: str, operation_id: str) -> Path:
        """Return the canonical directory for one durable operation."""
        if not self.managed or self.task_id is None:
            raise PromptArtifactScopeError(
                "durable prompt artifacts require a managed task identity"
            )
        unit = _identifier(unit_id, label="unit_id")
        operation = _identifier(operation_id, label="operation_id")
        if self.ephemeral_root is not None:
            root = Path(self.ephemeral_root).resolve()
            if root != Path(self.root).resolve():
                raise PromptArtifactScopeError(
                    "ephemeral prompt scope must match its owner root"
                )
            run = _identifier(self.run_id, label="workflow_run_id")
            target = root / "prompts" / run / unit / operation
            return _owner_directory(root, target)
        return _owner_directory(
            self.root,
            self.root
            / "managed"
            / str(self.task_id)
            / self.run_id
            / unit
            / operation,
        )

@dataclass(frozen=True)
class PromptArtifactRetention:
    """Delete only prompt identities selected by the product retention owner."""

    root: Path

    def delete_managed_unit(
        self, *, task_id: int, workflow_run_id: str, unit_id: str
    ) -> int:
        task = _non_negative_int(task_id, label="task_id")
        run = _identifier(workflow_run_id, label="workflow_run_id")
        unit = _identifier(unit_id, label="unit_id")
        if unit == "legacy":
            return 0
        return _delete_safe_tree(
            self.root,
            self.root / "managed" / str(task) / run / unit,
        )

    def delete_managed_legacy(
        self, *, task_id: int, workflow_run_id: str
    ) -> int:
        task = _non_negative_int(task_id, label="task_id")
        run = _identifier(workflow_run_id, label="workflow_run_id")
        return _delete_safe_tree(
            self.root,
            self.root / "managed" / str(task) / run / "legacy",
        )

    def delete_terminal_task_legacy(self, *, task_id: int) -> int:
        """Discover only UUID-scoped legacy trees for one eligible terminal task."""
        task = _non_negative_int(task_id, label="task_id")
        task_directory = self.root / "managed" / str(task)
        if task_directory.is_symlink() or not task_directory.is_dir():
            return 0
        try:
            run_directories = tuple(task_directory.iterdir())
        except OSError:
            return 0
        deleted = 0
        for run_directory in run_directories:
            if (
                run_directory.is_symlink()
                or not run_directory.is_dir()
                or not _INVOCATION_RUN_ID.fullmatch(run_directory.name)
            ):
                continue
            # Deliberately name the legacy child: durable units in the same
            # invocation are authoritative only when selected by lineage ID.
            deleted += _delete_safe_tree(
                self.root,
                run_directory / "legacy",
            )
        return deleted

    def prune_standalone(self, *, cutoff_timestamp: float) -> int:
        """Remove validated crash-orphan runs at or older than the cutoff."""
        standalone = self.root / "standalone"
        if standalone.is_symlink() or not standalone.is_dir():
            return 0
        deleted = 0
        for run_directory in standalone.iterdir():
            if (
                run_directory.is_symlink()
                or not run_directory.is_dir()
                or not _INVOCATION_RUN_ID.fullmatch(run_directory.name)
            ):
                continue
            if _standalone_scope_is_active(run_directory):
                continue
            try:
                modified_at = run_directory.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if modified_at <= cutoff_timestamp:
                deleted += _delete_safe_tree(self.root, run_directory)
        return deleted


def configured_prompt_artifact_root() -> Path:
    """Resolve the application-state prompt root without creating it."""
    configured_home = os.environ.get("UTA_RUNNER_HOME")
    application_home = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".local" / "share" / "uta"
    )
    lexical_root = application_home / "workflow-state" / "prompts"
    _reject_symlink_chain(lexical_root)
    return lexical_root.resolve(strict=False)


@contextmanager
def open_prompt_artifact_scope(
    *,
    repo_path: Path,
    task_id: Optional[int],
    workflow_run_id: Optional[str],
) -> Iterator[PromptArtifactScope]:
    """Open one safe prompt namespace for a managed or standalone invocation."""
    repository = Path(repo_path).expanduser()
    if not repository.exists() or not repository.is_dir():
        raise PromptArtifactScopeError(
            f"target repository is not a directory: {repository}"
        )
    repository = repository.resolve()

    if (task_id is None) != (workflow_run_id is None):
        raise PromptArtifactScopeError(
            "task_id and workflow_run_id must be supplied together"
        )
    managed = task_id is not None
    if managed:
        task = _non_negative_int(task_id, label="task_id")
        run_id = _identifier(workflow_run_id, label="workflow_run_id")
    else:
        task = None
        run_id = uuid.uuid4().hex

    resolved_root = configured_prompt_artifact_root()
    if (
        resolved_root == repository
        or repository in resolved_root.parents
        or resolved_root in repository.parents
    ):
        raise PromptArtifactScopeError(
            "prompt artifact root and target repository must not contain one "
            f"another: {resolved_root}"
        )
    _prepare_root(resolved_root)

    scope = PromptArtifactScope(
        root=resolved_root,
        run_id=run_id,
        task_id=task,
        managed=managed,
    )
    lease_fd: Optional[int] = None
    if not managed:
        run_directory = _owner_directory(
            scope.root, scope.root / "standalone" / scope.run_id
        )
        lease_fd = _acquire_standalone_lease(run_directory)
    try:
        yield scope
    finally:
        if not managed:
            try:
                _remove_standalone_run(scope)
            finally:
                _release_standalone_lease(lease_fd)


def build_safe_prompt_metadata(
    *,
    engine: Literal["legacy", "durable_v2"],
    language: str,
    phase: str,
    task_id: Optional[int],
    workflow_run_id: Optional[str],
    unit_id: Optional[str],
    operation_id: Optional[str],
    session_id: Optional[str],
    batch: Sequence[str],
    logical_attempt: int,
    execution_ordinal: int,
) -> Mapping[str, Any]:
    """Build the sole fixed-key metadata shape accepted by UTA prompt writers."""
    if engine not in ("legacy", "durable_v2"):
        raise PromptMetadataError(f"unsupported engine: {engine!r}")
    safe_language = _metadata_identifier(language, label="language")
    safe_phase = _metadata_identifier(phase, label="phase")
    safe_task_id = (
        None if task_id is None else _metadata_non_negative_int(task_id, "task_id")
    )
    safe_workflow_run_id = _optional_metadata_identifier(
        workflow_run_id, label="workflow_run_id"
    )
    safe_unit_id = _optional_metadata_identifier(unit_id, label="unit_id")
    safe_operation_id = _optional_metadata_identifier(
        operation_id, label="operation_id"
    )
    safe_session_id = _optional_metadata_identifier(session_id, label="session_id")
    if isinstance(batch, (str, bytes)) or not isinstance(batch, Sequence):
        raise PromptMetadataError("batch must be a sequence of strings")
    safe_batch = list(batch)
    if any(not isinstance(item, str) for item in safe_batch):
        raise PromptMetadataError("batch must contain only strings")

    metadata: Dict[str, Any] = {
        "schema_version": 1,
        "product": "uta",
        "engine": engine,
        "language": safe_language,
        "phase": safe_phase,
        "task_id": safe_task_id,
        "workflow_run_id": safe_workflow_run_id,
        "unit_id": safe_unit_id,
        "operation_id": safe_operation_id,
        "session_id": safe_session_id,
        "batch": safe_batch,
        "logical_attempt": _metadata_non_negative_int(
            logical_attempt, "logical_attempt"
        ),
        "execution_ordinal": _metadata_non_negative_int(
            execution_ordinal, "execution_ordinal"
        ),
    }
    encoded = json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _METADATA_MAX_BYTES:
        raise PromptMetadataError(
            f"prompt metadata exceeds {_METADATA_MAX_BYTES} UTF-8 bytes"
        )
    return metadata


def _prepare_root(root: Path) -> None:
    if root.is_symlink():
        raise PromptArtifactScopeError(f"prompt artifact root is a symlink: {root}")
    workflow_state = root.parent
    if workflow_state.is_symlink():
        raise PromptArtifactScopeError(
            f"prompt workflow-state directory is a symlink: {workflow_state}"
        )
    workflow_state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if workflow_state.is_symlink() or not workflow_state.is_dir():
        raise PromptArtifactScopeError(
            f"prompt workflow-state path is not a safe directory: {workflow_state}"
        )
    os.chmod(workflow_state, 0o700)
    root.mkdir(exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise PromptArtifactScopeError(
            f"prompt artifact root is not a safe directory: {root}"
        )
    os.chmod(root, 0o700)


def _owner_directory(root: Path, target: Path) -> Path:
    _require_below_root(root, target)
    current = root
    for component in target.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise PromptArtifactScopeError(
                f"prompt artifact directory is a symlink: {current}"
            )
        current.mkdir(mode=0o700, exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise PromptArtifactScopeError(
                f"prompt artifact path is not a safe directory: {current}"
            )
        os.chmod(current, 0o700)
    return target


def _remove_standalone_run(scope: PromptArtifactScope) -> None:
    target = scope.root / "standalone" / scope.run_id
    _require_below_root(scope.root, target)
    if target.is_symlink():
        raise PromptArtifactScopeError(
            f"standalone prompt artifact directory is a symlink: {target}"
        )
    if target.exists():
        shutil.rmtree(target)


def _acquire_standalone_lease(run_directory: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lease_path = run_directory / _STANDALONE_LEASE
    fd = os.open(lease_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_standalone_lease(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _standalone_scope_is_active(run_directory: Path) -> bool:
    lease_path = run_directory / _STANDALONE_LEASE
    if not lease_path.exists():
        return False
    if lease_path.is_symlink() or not lease_path.is_file():
        return True
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lease_path, flags)
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _delete_safe_tree(root: Path, target: Path) -> int:
    """Delete a confined regular tree, retaining any malformed or linked tree."""
    _require_below_root(root, target)
    if root.is_symlink() or not root.is_dir():
        return 0
    count = _validated_tree_file_count(target)
    if count is None:
        return 0
    shutil.rmtree(target)
    _remove_empty_ancestors(target.parent, stop=root)
    return count


def _validated_tree_file_count(path: Path) -> Optional[int]:
    if path.is_symlink() or not path.is_dir():
        return None
    count = 0
    try:
        children = tuple(path.iterdir())
    except OSError:
        return None
    for child in children:
        if child.is_symlink():
            return None
        if child.is_file():
            count += 1
            continue
        if child.is_dir():
            nested = _validated_tree_file_count(child)
            if nested is None:
                return None
            count += nested
            continue
        return None
    return count


def _remove_empty_ancestors(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop and stop in current.parents:
        if current.is_symlink():
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _reject_symlink_chain(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise PromptArtifactScopeError(
                f"prompt artifact path contains a symlink: {current}"
            )
        if current == current.parent:
            return
        current = current.parent


def _require_below_root(root: Path, target: Path) -> None:
    if target == root or root not in target.parents:
        raise PromptArtifactScopeError(
            f"prompt artifact path escapes its root: {target}"
        )


def _identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PromptArtifactScopeError(f"{label} is not a safe identifier: {value!r}")
    return value


def _non_negative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PromptArtifactScopeError(f"{label} must be a non-negative integer")
    return value


def _metadata_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise PromptMetadataError(f"{label} is not a safe identifier: {value!r}")
    return value


def _optional_metadata_identifier(value: Any, *, label: str) -> Optional[str]:
    if value is None:
        return None
    return _metadata_identifier(value, label=label)


def _metadata_non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PromptMetadataError(f"{label} must be a non-negative integer")
    return value


__all__ = [
    "PromptArtifactRetention",
    "PromptArtifactScope",
    "PromptArtifactScopeError",
    "PromptMetadataError",
    "build_safe_prompt_metadata",
    "configured_prompt_artifact_root",
    "open_prompt_artifact_scope",
]

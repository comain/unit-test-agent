"""One confined persistence owner for taskless durable generation calls."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from uta.testgen.batch import BatchGenerationRequest
from uta.shared.targets import legacy_class_fqn_for_storage
from uta.testgen.prompts import PromptArtifactScope, PromptArtifactScopeError


_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_LEASE_NAME = ".active.lock"
_PUBLIC_STATE_KEYS = frozenset(
    {
        "results",
        "session_ids",
        "session_refs",
        "session_token_usage",
        "session_retrospect",
        "phase_token_usage",
        "phase_timings",
        "current_stage",
        "error",
        "finished",
        "stopped_early",
        "current_batch",
        "current_target_batch",
        "current_class",
    }
)


@dataclass(frozen=True)
class StandaloneGenerationExecution:
    """Internal durable identity and resources for one public taskless call."""

    request: BatchGenerationRequest
    root: Path
    prompt_artifact_scope: PromptArtifactScope
    original_task_id: Optional[int]
    original_task_db_path: Optional[Path]

    def project(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        return project_standalone_final_state(
            state,
            original_task_id=self.original_task_id,
            original_task_db_path=self.original_task_db_path,
        )


def configured_standalone_generation_root() -> Path:
    configured = os.environ.get("UTA_RUNNER_HOME")
    application_root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local" / "share" / "uta"
    )
    root = application_root / "standalone-generation"
    _reject_symlink_chain(root)
    return root.resolve(strict=False)


@contextmanager
def open_standalone_generation_execution(
    request: BatchGenerationRequest,
) -> Iterator[StandaloneGenerationExecution]:
    """Give a taskless call a private product-compatible persistence boundary."""
    if request.task_id is not None or request.task_db_path is not None:
        raise ValueError("standalone generation requires no external task identity")
    repository = Path(request.repo_path).expanduser().resolve()
    if not repository.is_dir():
        raise PromptArtifactScopeError(
            f"target repository is not a directory: {repository}"
        )
    parent = configured_standalone_generation_root()
    if parent == repository or parent in repository.parents or repository in parent.parents:
        raise PromptArtifactScopeError(
            "standalone generation root and target repository must not contain one another"
        )
    _prepare_directory(parent)
    run_id = uuid.uuid4().hex
    root = parent / run_id
    lease_fd: Optional[int] = None
    try:
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        lease_fd = _acquire_lease(root)
        _prepare_directory(root / "workflow-state")
        _prepare_directory(root / "workflow-state" / "checkpoints")
        _prepare_directory(root / "workflow-state" / "results")
        database_path = root / "tasks.sqlite"
        from uta.testgen.ports.registry import task_ports_for

        ports = task_ports_for(database_path)
        if ports is None or not hasattr(ports, "create_synthetic_task"):
            raise RuntimeError(
                "persistence provider unavailable for standalone generation execution"
            )
        task_id = ports.create_synthetic_task(request)
        internal_request = replace(
            request,
            task_id=task_id,
            task_db_path=database_path,
        )
        prompt_scope = PromptArtifactScope(
            root=root,
            run_id=run_id,
            task_id=task_id,
            managed=True,
            ephemeral_root=root,
        )
        yield StandaloneGenerationExecution(
            request=internal_request,
            root=root,
            prompt_artifact_scope=prompt_scope,
            original_task_id=request.task_id,
            original_task_db_path=request.task_db_path,
        )
    finally:
        if lease_fd is not None:
            _release_lease(lease_fd)
        if root.exists() and not root.is_symlink():
            shutil.rmtree(root)


def project_standalone_final_state(
    state: Mapping[str, Any],
    *,
    original_task_id: Optional[int],
    original_task_db_path: Optional[Path],
) -> Dict[str, Any]:
    """Return only the stable public state, restoring the caller's identity."""
    projected = {
        key: value
        for key, value in state.items()
        if key in _PUBLIC_STATE_KEYS or key.startswith(("delivery_", "result_"))
    }
    projected["task_id"] = original_task_id
    projected["task_db_path"] = (
        str(original_task_db_path) if original_task_db_path is not None else None
    )
    _reject_internal_projection(projected)
    return projected


def prune_standalone_generation_executions(*, cutoff_timestamp: float) -> int:
    """Delete safe, UUID-named crash orphans after their lease becomes stale."""
    parent = configured_standalone_generation_root()
    if parent.is_symlink() or not parent.is_dir():
        return 0
    deleted = 0
    for candidate in tuple(parent.iterdir()):
        if (
            candidate.is_symlink()
            or not candidate.is_dir()
            or not _RUN_ID.fullmatch(candidate.name)
            or _lease_is_active(candidate)
        ):
            continue
        try:
            modified = candidate.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if modified > float(cutoff_timestamp) or not _regular_tree(candidate):
            continue
        shutil.rmtree(candidate)
        deleted += 1
    return deleted


def _create_synthetic_task(db: Any, request: BatchGenerationRequest) -> int:
    targets = list(request.targets or [])
    task_id = db.create_repo_task(
        {
            "repo_path": str(Path(request.repo_path).resolve()),
            "repo_slug": f"standalone-{request.language}",
            "language": request.language,
            "module_filter": getattr(request, "module", None),
            "selection": {"targets": [target.as_selection() for target in targets]},
            "coverage_gate": request.coverage_gate,
            "mutation_gate": request.mutation_gate,
            "total_classes": len(targets),
        }
    )
    for target in targets:
        db.create_class_task(
            task_id,
            legacy_class_fqn_for_storage(target),
            module=getattr(request, "module", None),
            priority=100,
            language=target.language,
            target_id=target.target_id,
            source_path=target.source_path,
            symbol=target.symbol,
            target_granularity=target.granularity,
            display_name=target.display_name,
        )
    return task_id


def _reject_internal_projection(value: Any, *, path: str = "final_state") -> None:
    forbidden = {
        "workflow_run_id",
        "unit_id",
        "operation_id",
        "backend_context",
        "prompt_file",
        "prompt_inputs_file",
        "result_artifact_path",
        "checkpoint_path",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in forbidden or (
                isinstance(key, str)
                and any(token in key.lower() for token in ("checkpoint_path", "prompt_path"))
            ):
                raise ValueError(f"standalone public state contains internal field {path}.{key}")
            _reject_internal_projection(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_internal_projection(item, path=f"{path}[{index}]")


def _prepare_directory(path: Path) -> None:
    _reject_symlink_chain(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise PromptArtifactScopeError(f"unsafe standalone generation root: {path}")
    os.chmod(path, 0o700)


def _reject_symlink_chain(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise PromptArtifactScopeError(
                f"standalone generation path contains a symlink: {current}"
            )
        if current == current.parent:
            return
        current = current.parent


def _acquire_lease(root: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(root / _LEASE_NAME, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _release_lease(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _lease_is_active(root: Path) -> bool:
    lease = root / _LEASE_NAME
    if not lease.exists():
        return False
    if lease.is_symlink() or not lease.is_file():
        return True
    flags = os.O_RDWR | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    try:
        fd = os.open(lease, flags)
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


def _regular_tree(path: Path) -> bool:
    try:
        children = tuple(path.iterdir())
    except OSError:
        return False
    for child in children:
        if child.is_symlink():
            return False
        if child.is_dir() and not _regular_tree(child):
            return False
        if not child.is_dir() and not child.is_file():
            return False
    return True


__all__ = [
    "StandaloneGenerationExecution",
    "configured_standalone_generation_root",
    "open_standalone_generation_execution",
    "project_standalone_final_state",
    "prune_standalone_generation_executions",
]

"""The resource boundary around durable generation-cycle execution.

One application owns one SQLite checkpointer connection and compiles one child
graph. It may execute many stable batches, but no graph, saver, session, or
product callback is placed on checkpointed state.
"""

from __future__ import annotations

import logging
import math
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from agent_core.harness import (
    ResumableHarness,
    SessionUnsupportedError,
)
from agent_core.workflow import WorkflowRunIdentity, invoke_workflow, open_checkpointer
from agent_core.runtime import SecureArtifactStore

from uta.testgen.batches import StableGenerationBatch
from uta.testgen.graph.cycle import build_cycle_workflow
from uta.testgen.graph.turn_context import TurnInvocationScope, compose_turn_context
from uta.testgen.operations import (
    ArtifactValidationError,
    WorkflowOperationLedger,
)


logger = logging.getLogger(__name__)


CYCLE_NAME = "test-generation-cycle"
CYCLE_VERSION = "v2"
DEFAULT_RECURSION_LIMIT = 120
_FORBIDDEN_KEY = re.compile(
    r"(?:credential|secret|password|api[_-]?key|auth[_-]?token)", re.IGNORECASE
)
_TOKEN_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    }
)


class WorkflowApplicationConfigurationError(RuntimeError):
    """A required production port was not supplied at application startup."""


class UnsafeWorkflowStateError(TypeError):
    """Checkpoint state contains a live object or credential-like field."""


class WorkflowResultValidationError(RuntimeError):
    """A terminal graph snapshot disagrees with authoritative product evidence."""


def workflow_state_root(task_db_path: os.PathLike[str] | str) -> Path:
    """Keep checkpoints and result artifacts in one dedicated owner-only root."""
    return Path(task_db_path).expanduser().resolve().parent / "workflow-state"


@dataclass
class WorkflowApplication:
    """A compiled child graph bound to one product DB and checkpointer."""

    cycle: Any
    checkpointer: Any
    ledger: WorkflowOperationLedger
    turn_scope: TurnInvocationScope
    recursion_limit: int = DEFAULT_RECURSION_LIMIT

    def invoke_batch(
        self,
        *,
        repo_task_id: int,
        batch: StableGenerationBatch,
        initial_state: Mapping[str, Any],
    ):
        if int(repo_task_id) != batch.repo_task_id:
            raise WorkflowApplicationConfigurationError(
                "the stable batch belongs to a different product task"
            )
        state = {
            **dict(initial_state),
            "task_id": int(repo_task_id),
            "workflow_run_id": batch.workflow_run_id,
            "unit_id": batch.unit_id,
            "batch": list(batch.target_ids),
            "attempts_by_phase": dict(initial_state.get("attempts_by_phase") or {}),
            "max_attempts_by_phase": dict(
                initial_state.get("max_attempts_by_phase") or {}
            ),
            "prerequisite_operation_ids": list(
                initial_state.get("prerequisite_operation_ids") or []
            ),
        }
        validate_checkpoint_state(state)
        self.turn_scope.bind(state)
        identity = WorkflowRunIdentity(
            product="uta",
            task_id=str(repo_task_id),
            unit_id=batch.unit_id,
            workflow_run_id=batch.workflow_run_id,
            cycle=CYCLE_NAME,
            version=CYCLE_VERSION,
        )
        result = invoke_workflow(
            self.cycle,
            identity=identity,
            initial_state=state,
            recursion_limit=self.recursion_limit,
        )
        try:
            self.ledger.validate_terminal(result.state)
        except (ArtifactValidationError, KeyError, TypeError, ValueError) as exc:
            # Name the failing check. `validate_terminal` distinguishes five --
            # incomplete row, missing artifact, changed workspace, diverged
            # outputs, bad prerequisites -- and an operator needs to know which
            # before deciding whether to rerun a unit or quarantine it. The
            # reasons are our own fixed strings, not provider or driver text,
            # so there is nothing here to redact; the daemon logs this line
            # without a traceback, so the chained cause alone is not enough.
            raise WorkflowResultValidationError(
                f"terminal product evidence is invalid for {identity.thread_id}: "
                f"{_terminal_detail(result.state, exc)}"
            ) from exc
        return result


def _terminal_detail(state: Mapping[str, Any], exc: Exception) -> str:
    """Say why the unit stopped, not merely which check noticed.

    `terminate_generation` already records the reconciliation outcome that
    stopped it, and then never reaches `complete_generation` -- so
    `validate_terminal` fails on the missing completion row and reports that.
    The row is the symptom. Reporting it alone sent an operator to the ledger
    to explain a unit that had recorded its own reason in state all along, and
    a beta run died that way with nothing in its log but the missing row.

    An error from a unit that did *not* stop early is unchanged: there the
    failing check really is the finding.
    """
    reason = str(state.get("terminal_reason") or "").strip()
    if not state.get("stopped_early") or not reason:
        return str(exc)
    error = str(state.get("error") or "").strip()
    detail = f"unit stopped early ({reason})"
    if error and error != reason:
        detail = f"{detail}: {error}"
    return f"{detail}; {exc}"


@contextmanager
def open_workflow_application(
    *,
    task_db_path: os.PathLike[str] | str,
    context: Mapping[str, Any],
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
) -> Iterator[WorkflowApplication]:
    """Open once per worker-owned request and close every resource in order."""
    runner = context.get("runner")
    if not isinstance(runner, ResumableHarness):
        raise SessionUnsupportedError(
            f"{type(runner).__name__} does not support reusable sessions"
        )

    from uta.testgen.ports.registry import task_ports_for

    ports = task_ports_for(task_db_path)
    if ports is not None and hasattr(ports, "workflow_state_root"):
        root = ports.workflow_state_root()
    else:
        root = workflow_state_root(task_db_path)
    if root.is_symlink():
        raise ArtifactValidationError(f"workflow state root is a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise ArtifactValidationError(f"workflow state root is not a directory: {root}")
    os.chmod(root, 0o700)

    ledger = context.get("ledger")
    if ledger is None:
        required = ("fingerprint", "allowed_edit", "output_fingerprints")
        missing = [name for name in required if not callable(context.get(name))]
        if missing:
            raise WorkflowApplicationConfigurationError(
                f"generation workflow context is missing callable ports: {', '.join(missing)}"
            )
        if ports is not None and hasattr(ports, "create_operation_ledger"):
            ledger = ports.create_operation_ledger(
                root / "results",
                fingerprint=context["fingerprint"],
                allowed_edit=context["allowed_edit"],
                output_fingerprints=context["output_fingerprints"],
            )
        else:
            raise WorkflowApplicationConfigurationError(
                "cannot create operation ledger without persistence provider"
            )
    if not isinstance(ledger, WorkflowOperationLedger):
        raise WorkflowApplicationConfigurationError(
            "generation workflow ledger must be WorkflowOperationLedger"
        )

    turn_scope = TurnInvocationScope()
    bound = {
        **dict(context),
        "ledger": ledger,
    }
    for legacy_key in (
        "before_turn",
        "after_turn",
        "cost_gate",
        "on_cost",
        "on_result",
        "open_session",
        "runner",
    ):
        bound.pop(legacy_key, None)
    bound["turn_context"] = compose_turn_context(
        harness_name=str(context.get("harness_name") or "configured"),
        runner=runner,
        ledger=ledger,
        scope=turn_scope,
        context=context,
    )
    repo_path = context.get("repo_path")
    if not repo_path:
        raise WorkflowApplicationConfigurationError(
            "generation workflow context is missing repo_path"
        )
    prompt_scope = context.get("prompt_artifact_scope")
    if prompt_scope is not None:
        bound["prompt_store"] = SecureArtifactStore(
            prompt_scope.root,
            forbidden_roots=(Path(str(context["repo_path"])),),
        )
    checkpoint_path = root / "checkpoints.sqlite"
    with open_checkpointer(
        checkpoint_path,
        forbidden_roots=(Path(str(repo_path)),),
    ) as checkpointer:
        try:
            cycle = build_cycle_workflow(context=bound, checkpointer=checkpointer)
            yield WorkflowApplication(
                cycle=cycle,
                checkpointer=checkpointer,
                ledger=ledger,
                turn_scope=turn_scope,
                recursion_limit=int(recursion_limit),
            )
        finally:
            progress_sink = bound.get("progress_sink")
            close_progress = getattr(progress_sink, "close", None)
            if callable(close_progress):
                try:
                    close_progress()
                except Exception:  # noqa: BLE001 - progress is best-effort
                    logger.warning(
                        "failed to close generation progress sink",
                        exc_info=True,
                    )


def validate_checkpoint_state(value: Any, *, _path: str = "state") -> None:
    """Fail before LangGraph sees a non-JSON or credential-bearing value."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise UnsafeWorkflowStateError(f"{_path} contains a non-finite number")
        return
    if callable(value):
        raise UnsafeWorkflowStateError(f"{_path} contains a callable")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise UnsafeWorkflowStateError(f"{_path} has a non-string key")
            if _FORBIDDEN_KEY.search(key) and key not in _TOKEN_USAGE_KEYS:
                raise UnsafeWorkflowStateError(
                    f"{_path}.{key} is a secret or credential field"
                )
            validate_checkpoint_state(item, _path=f"{_path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            validate_checkpoint_state(item, _path=f"{_path}[{index}]")
        return
    raise UnsafeWorkflowStateError(
        f"{_path} contains non-serializable {type(value).__name__}"
    )


__all__ = [
    "CYCLE_NAME",
    "CYCLE_VERSION",
    "DEFAULT_RECURSION_LIMIT",
    "UnsafeWorkflowStateError",
    "WorkflowApplication",
    "WorkflowApplicationConfigurationError",
    "WorkflowResultValidationError",
    "open_workflow_application",
    "validate_checkpoint_state",
    "workflow_state_root",
]

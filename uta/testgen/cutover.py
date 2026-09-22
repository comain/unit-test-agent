"""Persisted selection and production ports for generation-cycle v2."""

from __future__ import annotations

import hashlib
import logging
import json
from pathlib import Path

logger = logging.getLogger("uta")
from typing import Any, Dict, Mapping

from agent_core.runtime import ProgressBatcher

from uta.shared.git import git
from uta.shared.workspace_policy import (
    allowed_llm_path,
    git_status_snapshot,
    is_uta_runtime_residue,
)
from uta.testgen.ports.registry import task_ports_from_state
from uta.testgen.task_guard import llm_guard_after, llm_guard_before

GENERATION_ENGINE_VERSION_KEY = "generation_engine_version"
DURABLE_GENERATION_ENGINE_VERSION = 2


class LegacyGenerationTaskError(RuntimeError):
    """A persisted task cannot cross the durable-only execution boundary."""


def require_durable_generation_task(task: Any, *, action: str) -> None:
    raw = getattr(task, "config_snapshot_json", None)
    if raw is None and isinstance(task, Mapping):
        raw = task.get("config_snapshot_json")
    if not raw:
        reason = "missing_generation_engine_version"
    else:
        try:
            snapshot = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(snapshot, dict):
                reason = "config_snapshot_not_object"
            elif GENERATION_ENGINE_VERSION_KEY not in snapshot:
                reason = "missing_generation_engine_version"
            else:
                ver = snapshot[GENERATION_ENGINE_VERSION_KEY]
                if type(ver) is not int or ver != DURABLE_GENERATION_ENGINE_VERSION:
                    reason = "unsupported_generation_engine_version"
                else:
                    reason = None
        except Exception:
            reason = "malformed_config_snapshot_json"
    if reason is not None:
        task_id = getattr(task, "task_id", None) or (task.get("id") if isinstance(task, Mapping) else "<unknown>")
        raise LegacyGenerationTaskError(
            f"Task {task_id} cannot {action}: its persisted generation engine is "
            f"legacy or invalid ({reason}). Finish or cancel the old task, then "
            "submit a new task after the durable-only build starts."
        )


def production_cycle_context(*, state: Mapping[str, Any], batch, backend, runner):
    repo_path = str(state["repo_path"])
    task_id = int(state["task_id"])
    ports = task_ports_from_state(state)
    if ports is not None and hasattr(ports, "create_progress_batcher"):
        progress = ports.create_progress_batcher(
            task_id, batch.workflow_run_id, batch.unit_id
        )
    else:
        progress = ProgressBatcher(lambda _events: None, budget=0)

    def before_turn(turn_state, config):
        snapshot = llm_guard_before(
            dict(turn_state),
            list(turn_state.get("batch") or []),
            str(config.get("label") or "agent_turn"),
        )
        if snapshot is not None:
            snapshot = {
                **dict(snapshot),
                "durable_head_revision": _head_revision(repo_path),
            }
        return snapshot

    def after_turn(turn_state, config, snapshot):
        llm_guard_after(dict(turn_state), snapshot)
        if snapshot is not None:
            expected = str(snapshot.get("durable_head_revision") or "")
            current = _head_revision(repo_path)
            if not expected or current != expected:
                from uta.testgen.workspace_guard import TaskUnsafeDiffError

                raise TaskUnsafeDiffError(
                    "Unsafe agent operation changed repository HEAD"
                )

    return {
        "harness_name": str(state.get("agent_harness") or "opencode"),
        "runner": runner,
        "backend": backend,
        "fingerprint": workspace_fingerprint,
        "allowed_edit": interrupted_edit_is_allowed,
        "output_fingerprints": lambda turn_state, phase, step: output_fingerprints(
            turn_state, phase, step, backend=backend
        ),
        "before_turn": before_turn,
        "after_turn": after_turn,
        "progress_sink": progress,
        "is_cancelled": lambda: _is_cancelled(state),
    }


def workspace_fingerprint(state: Mapping[str, Any], phase: str, step: str) -> str:
    repo = Path(str(state["repo_path"])).resolve()
    relevant_files = {
        relative: _file_digest(repo / relative)
        for relative in _relevant_workspace_paths(state, repo)
    }
    evidence = {
        "phase": str(phase),
        "step": str(step),
        # UTA's own reports and caches are excluded: they are rewritten while a
        # run is in flight, so including them makes the fingerprint recorded by
        # `complete_generation` disagree with the one `validate_terminal`
        # recomputes moments later, and a correct run is rejected.
        "dirty": {
            path: digest
            for path, digest in git_status_snapshot(str(repo)).items()
            if not is_uta_runtime_residue(path)
        },
        "files": relevant_files,
        # Every key always present, defaulting to None. Building this with
        # `if key in state` made an *absent* key hash differently from an unset
        # one, so a fingerprint recorded from a node's state disagreed with one
        # recomputed from the final state even though the workspace and the run
        # were identical.
        "configuration": {
            key: state.get(key)
            for key in (
                "language",
                "module",
                "batch",
                "target",
                "coverage_gate",
                "mutation_gate",
                "quality_mode",
                "quality_gate_backend",
                "quality_gate_command",
                "ci_diff_coverage_gate",
                "ci_diff_mutation_gate",
                "max_attempts_by_phase",
                # `prerequisite_operation_ids` is deliberately absent: it is
                # operation bookkeeping, not workspace state, and it grows as
                # each operation completes. Including it meant
                # `complete_generation` recorded one fingerprint and
                # `validate_terminal` recomputed a different one from the final
                # state, rejecting Java runs whose workspace was byte-identical
                # either side. The prerequisite chain is still validated, by
                # `_validate_prerequisites`, which checks the artifacts exist
                # and belong to this run.
            )
        },
    }
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if phase == "complete_generation":
        # The terminal check compares this value recorded by the phase against
        # one recomputed moments later, and a mismatch there discards a run
        # that fully succeeded. The per-component digests are logged both
        # times, so a disagreement names its own cause instead of costing a
        # whole replay to bisect -- three of those were spent before this
        # existed. DEBUG because it is per-phase noise once the causes are
        # fixed; raise the level on the daemon to bisect a new one.
        logger.debug(
            "workspace fingerprint %s components=%s files=%s",
            digest[:12],
            {
                name: hashlib.sha256(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()[:12]
                for name, value in evidence.items()
            },
            sorted(relevant_files),
        )
    return digest


def _relevant_workspace_paths(
    state: Mapping[str, Any], repo: Path
) -> list[str]:
    """Files whose bytes define this operation's workspace.

    Strictly what the task definition names: the target's source and its
    canonical generated test path. Nothing discovered or accumulated while the
    run proceeds -- `existing_test_path` found by precheck, or a
    `results[...].test_file_path` recorded by a phase -- because those make the
    hashed set grow mid-cycle, so the fingerprint written by
    `complete_generation` disagrees with the one recomputed by
    `validate_terminal` and a run whose files are identical gets discarded.

    Nothing is lost: a discovered file that genuinely changed still appears in
    the `dirty` component.
    """
    candidates = []
    target = state.get("target")
    if isinstance(target, Mapping):
        candidates.append(target.get("source_path"))
    candidates.append(state.get("generated_test_path"))
    paths = []
    for candidate in candidates:
        if not candidate:
            continue
        relative = str(candidate)
        if (repo / relative).is_file() and relative not in paths:
            paths.append(relative)
    return sorted(paths)


def _file_digest(path: Path) -> str:
    if not path.exists():
        return "<missing>"
    if not path.is_file():
        return "<not-file>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _head_revision(repo_path: str) -> str:
    return git().output(repo_path, "rev-parse", "HEAD")


def interrupted_edit_is_allowed(
    state: Mapping[str, Any], operation: Mapping[str, Any]
) -> bool:
    snapshot = git_status_snapshot(str(state["repo_path"]))
    return bool(snapshot) and all(
        allowed_llm_path(path, dict(state), list(state.get("batch") or []))
        for path in snapshot
    )


def output_fingerprints(
    state: Mapping[str, Any], phase: str, step: str, *, backend
) -> Dict[str, str]:
    repo = Path(str(state["repo_path"]))
    paths = list(backend.output_paths(state))
    return {
        path: (
            hashlib.sha256((repo / path).read_bytes()).hexdigest()
            if (repo / path).is_file()
            else "<missing>"
        )
        for path in paths
    }


def _is_cancelled(state: Mapping[str, Any]) -> bool:
    try:
        from uta.testgen.ports.registry import task_ports_from_state

        ports = task_ports_from_state(state)
        if ports is None:
            return False
        return bool(ports.is_stop_requested(str(state["task_id"])))
    except Exception:
        # A stop check must never be the thing that fails a run.
        return False


__all__ = [
    "interrupted_edit_is_allowed",
    "output_fingerprints",
    "production_cycle_context",
    "workspace_fingerprint",
]

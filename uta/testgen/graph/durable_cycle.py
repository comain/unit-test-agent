"""Production invocation of one persisted generation-cycle unit."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Dict

from uta.shared.languages import default_registry
from uta.testgen.batches import GenerationBatchIdentityError
from uta.testgen.graph.generation_cycle import attach_generation_cycle
from uta.testgen.workspace_guard import guard_language


def audit_generation_engine_selection(state: Dict[str, Any]) -> None:
    """Persist one audit event when the outer graph enters the durable engine."""
    if (
        state.get("generation_cycle")
        or not state.get("task_id")
        or not state.get("task_db_path")
    ):
        return
    from uta.testgen.ports.registry import task_ports_from_state

    ports = task_ports_from_state(state)
    if ports is None:
        return
    ports.add_event(
        str(state["task_id"]),
        "generation_engine_selected",
        {
            "message": "Generation engine selected: durable_v2",
            "stage": "run_generation_cycle",
            "payload": {"engine": "durable_v2"},
        },
    )


def run_durable_generation_cycle(state: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke one persisted stable batch through the checkpointed child."""
    from uta.testgen.cutover import (
        production_cycle_context,
        require_durable_generation_task,
    )
    from uta.testgen.graph.application import open_workflow_application
    from uta.testgen.ports.registry import task_ports_for, task_ports_from_state
    from uta.testgen.prompts import open_prompt_artifact_scope

    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if not task_id or not task_db_path:
        raise RuntimeError("generation cycle v2 requires a persisted product task")
    ports = task_ports_from_state(state) or task_ports_for(task_db_path)
    if ports is None:
        raise RuntimeError(f"persistence provider unavailable for task {task_id}")
    persisted_task = ports.get_task_snapshot(str(task_id))
    if persisted_task is None:
        raise RuntimeError(f"generation task {task_id} does not exist")

    require_durable_generation_task(persisted_task, action="execute generation")
    provided_prompt_scope = (state.get("backend_context") or {}).get(
        "prompt_artifact_scope"
    )
    batches = ports.ensure_stable_batches(
        repo_task_id=int(task_id),
        ordered_target_ids=list(state.get("candidates") or []),
        batch_size=max(1, int(state.get("classes_per_agent_run") or 1)),
        workflow_run_id=(
            str(provided_prompt_scope.run_id)
            if getattr(provided_prompt_scope, "ephemeral_root", None) is not None
            else None
        ),
    )
    batch = ports.first_non_terminal_batch(batches)
    if batch is None:
        return {"finished": True, "current_batch": [], "current_class": None}

    language = guard_language(state, list(batch.target_ids))
    prepared_outer = {
        **state,
        "current_batch": list(batch.target_ids),
        "current_target_batch": [
            target
            for target in state.get("target_candidates") or []
            if target.get("target_id") in batch.target_ids
        ],
    }
    adapter = default_registry().adapter_for(language)
    binding = adapter.generation_cycle_binding(prepared_outer)
    initial = dict(binding.initial_state)
    backend = binding.backend
    runner = binding.runner
    initial.update(
        {
            "task_id": int(task_id),
            "task_db_path": str(task_db_path),
            "workflow_run_id": batch.workflow_run_id,
            "unit_id": batch.unit_id,
            # The outer workflow accumulates accounting after each unit. The
            # child reports only its delta so prior timings are not re-added.
            "phase_timings": {},
        }
    )
    prompt_scope_owner = (
        nullcontext(provided_prompt_scope)
        if provided_prompt_scope is not None
        else open_prompt_artifact_scope(
            repo_path=state["repo_path"],
            task_id=int(task_id),
            workflow_run_id=batch.workflow_run_id,
        )
    )
    with prompt_scope_owner as prompt_artifact_scope:
        context = production_cycle_context(
            state=prepared_outer,
            batch=batch,
            backend=backend,
            runner=runner,
        )
        context["repo_path"] = str(state["repo_path"])
        context["prompt_artifact_scope"] = prompt_artifact_scope
        with open_workflow_application(
            task_db_path=task_db_path,
            context=context,
        ) as application:
            invoked = application.invoke_batch(
                repo_task_id=int(task_id),
                batch=batch,
                initial_state=initial,
            )
    _guard_reused_unit(ports=ports, batch=batch, disposition=invoked.disposition)

    ports.add_event(
        str(task_id),
        f"workflow_{invoked.disposition}",
        {
            "message": f"Generation workflow {invoked.disposition}: {batch.unit_id}",
            "stage": str(invoked.state.get("current_phase") or "complete_generation"),
            "payload": {
                "workflowRunId": batch.workflow_run_id,
                "unitId": batch.unit_id,
                "disposition": invoked.disposition,
            },
        },
    )
    child = dict(invoked.state)
    results = dict(child.get("results") or {})
    from uta.testgen.turn_accounting import merge_accounting

    accounting = merge_accounting(state, child)
    return {
        "results": results,
        **accounting,
        # The outer delivery node still owns commit/push. Keep the completed
        # unit selected until that node has consumed it.
        "current_batch": list(batch.target_ids),
        "current_target_batch": list(prepared_outer.get("current_target_batch") or []),
        "current_class": prepared_outer.get("current_class"),
        "current_stage": child.get("current_stage") or "complete_generation",
        "error": child.get("error"),
        "stopped_early": bool(child.get("stopped_early")),
    }


def _guard_reused_unit(*, ports: Any, batch: Any, disposition: str) -> None:
    """Refuse a unit that is complete in its checkpoint and unfinished here.

    A batch is only selected while its class rows are non-terminal, and a
    reused invocation runs no nodes -- so nothing between that selection and
    this check can have written them. The unit is therefore done in the
    checkpoint and unfinished in the product, and the daemon will select it,
    reuse it, and never progress: a worker slot spins until someone notices.
    On beta that ran 70+ times at ten-second intervals against a single-slot
    daemon.

    A unit that actually ran is never checked here: its rows are written by the
    delivery step later in this same pipeline, so checking now would fail every
    healthy run.

    Failing costs one task and names the unit. Looping costs the daemon.
    """
    if disposition != "reused_completed":
        return
    if ports.first_non_terminal_batch([batch]) is None:
        return
    raise GenerationBatchIdentityError(
        f"generation unit {batch.unit_id} is complete in its checkpoint but its "
        "class rows are still unfinished, so it would be selected and reused "
        "forever. Its terminal product evidence was never recorded -- rerun the "
        "unit under a new workflow run id, or repair the class rows, before "
        "retrying."
    )


def run_generation_cycle(state: Dict[str, Any]) -> Dict[str, Any]:
    """Run the sole generation engine and publish its declarative contract."""
    audit_generation_engine_selection(state)
    return attach_generation_cycle(run_durable_generation_cycle(state))


__all__ = [
    "audit_generation_engine_selection",
    "_guard_reused_unit",
    "run_generation_cycle",
    "run_durable_generation_cycle",
]

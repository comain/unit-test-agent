"""Wiring the generation cycle: the nodes and selectors its topology names.

`generation-cycle.yaml` says *what* the cycle does. This says how each node
name is implemented, and nothing more — the work itself belongs to a language
backend, and deciding whether an interrupted operation may be reused belongs to
the operation ledger. Both arrive through `context`, which is what lets the
whole cycle be driven by a scripted backend in a test instead of Maven, PIT and
a live provider.

Three ports, all supplied by the caller:

``backend``   the language backend: renders prompts, interprets answers, runs
              the deterministic phases
``ledger``    classifies a resumed operation into one of seven outcomes, and
              rehydrates a result that must not be recomputed
``runner``    the harness, plus ``open_session`` — consumed by agent-core's
              shared ``agent_turn``, not by anything here

The nodes are deliberately thin. A node that did real work here would be work
that neither the backend nor the ledger owns, and the next language would have
to reimplement it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping

import json

from agent_core.prompts import materialize_bundle
from agent_core.runtime import ArtifactError, SecureArtifactStore
from agent_core.workflow import NodeRegistry
from agent_core.workflow.registry import shared_registry

from uta.enforcement.equivalent_mutants import REVIEW_PHASE
from uta.testgen import equivalence_review
from uta.testgen.graph.cycle_state import CycleState
from uta.testgen.graph.generation_cycle import generation_cycle_spec
from uta.testgen.prompts import build_safe_prompt_metadata
from uta.testgen.prompts.artifacts import PromptArtifactScopeError
from uta.testgen.repair_progress import apply_repair_progress
from uta.testgen.turn_events import record_turn_outcome

logger = logging.getLogger(__name__)

class GenerationPaused(RuntimeError):
    """A cancelled turn stopped the cycle where it stood.

    Raised rather than returned so the graph does *not* checkpoint a state
    update. The lineage stays pending at `pause_generation`, which is what
    makes a deliberate resume able to come back to the phase that was
    interrupted rather than restarting the cycle.
    """

    def __init__(self, phase: str):
        self.phase = phase
        super().__init__(f"generation paused during {phase}")


class MissingPortError(RuntimeError):
    """The cycle was built without something it cannot work without."""


def _port(context: Mapping[str, Any], name: str) -> Any:
    try:
        return context[name]
    except KeyError:
        raise MissingPortError(
            f"the generation cycle needs a {name!r} in its context"
        ) from None


def _phase(config: Mapping[str, Any]) -> str:
    return str(config.get("phase") or "")


def render_phase_prompt(phase: str, state: Mapping[str, Any], backend: Any) -> str:
    """A language renders its phases; the equivalence review belongs to no language."""
    if phase == REVIEW_PHASE:
        return equivalence_review.render_prompt(state)
    return backend.render_prompt(phase, state)


def interpret_phase(phase: str, state: Mapping[str, Any], turn: Any, backend: Any) -> Dict[str, Any]:
    if phase == REVIEW_PHASE:
        return equivalence_review.interpret(state, turn if isinstance(turn, Mapping) else None)
    return backend.interpret(phase, state, turn)


# -- nodes -------------------------------------------------------------------

cycle_registry = NodeRegistry()


@cycle_registry.node("reconcile_generation_operation")
def reconcile_generation_operation(state, config, context) -> Dict[str, Any]:
    """Decide whether this operation may run, be reused, or must stop.

    Every side-effecting step is preceded by one of these. The node itself
    decides nothing: classification needs the product's ledger, the artifacts
    on disk and the workspace fingerprint, none of which belong in a neutral
    graph node.
    """
    phase, step = _phase(config), str(config.get("step") or "run")
    decision = _port(context, "ledger").classify(
        phase=phase, step=step, state=state
    )
    if isinstance(decision, Mapping):
        outcome = str(decision.get("outcome") or "fail_indeterminate")
        evidence = {
            str(key): value
            for key, value in decision.items()
            if key != "outcome"
        }
    else:
        # Scripted/legacy ports used by embedders may still return only the
        # branch label. Keep that deliberately narrow compatibility seam.
        outcome = str(decision)
        evidence = {}
    logger.debug("reconciled %s/%s as %s", phase, step, outcome)
    return {
        **evidence,
        "reconciliation": outcome,
        "current_phase": phase,
    }


@cycle_registry.node("generation_prompt")
def generation_prompt(state, config, context) -> Dict[str, Any]:
    """Render this phase's prompt and put it where the turn can find it.

    The backend renders, because only it knows what the model should be asked.
    Writing the file is here, because every phase does it identically and a
    backend that wrote its own would be free to write it somewhere the turn
    does not look.
    """
    phase = _phase(config)
    backend = _port(context, "backend")
    body = render_phase_prompt(phase, state, backend)
    if state.get("repair_feedback"):
        body = f"{body}\n\nCorrective repair feedback:\n{state['repair_feedback']}"

    scope = _port(context, "prompt_artifact_scope")
    task_id = int(state["task_id"])
    workflow_run_id = str(state["workflow_run_id"])
    if (
        not scope.managed
        or scope.task_id != task_id
        or scope.run_id != workflow_run_id
    ):
        raise PromptArtifactScopeError(
            "durable prompt scope does not match the checkpointed task lineage"
        )
    directory = scope.durable_directory(
        unit_id=str(state["unit_id"]),
        operation_id=str(state["operation_id"]),
    )
    metadata = build_safe_prompt_metadata(
        engine="durable_v2",
        language=str(state["language"]),
        phase=phase,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
        unit_id=str(state["unit_id"]),
        operation_id=str(state["operation_id"]),
        session_id=None,
        batch=tuple(str(item) for item in state.get("batch") or ()),
        logical_attempt=int(state.get("logical_attempt") or 0),
        execution_ordinal=int(state.get("execution_ordinal") or 0),
    )
    store = context.get("prompt_store")
    if store is None:
        store = SecureArtifactStore(scope.root)
    namespace = directory.relative_to(scope.root).as_posix()
    inputs_text = json.dumps(
        dict(metadata),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    try:
        artifact = materialize_bundle(
            store,
            namespace,
            prompt=str(body),
            inputs=inputs_text,
        )
    except (ArtifactError, ValueError) as exc:
        raise PromptArtifactScopeError(str(exc)) from exc

    return {
        "prompt_file": str(scope.root / artifact.prompt.relative_path),
        "prompt_inputs_file": str(scope.root / artifact.inputs.relative_path),
        "prompt_manifest_file": str(scope.root / artifact.manifest.relative_path),
        "current_phase": phase,
    }


@cycle_registry.node("generation_interpret")
def generation_interpret(state, config, context) -> Dict[str, Any]:
    """Turn the model's answer into a phase outcome the graph can route on."""
    phase = _phase(config)
    turn = state.get("turn_result")
    # Before interpreting: the public progress stream can only say "Agent needs
    # attention", so this is where the turn's own vetted account of a failure
    # reaches the event log.
    record_turn_outcome(state, phase=phase, turn=turn)
    result = interpret_phase(phase, state, turn, _port(context, "backend"))
    projection = dict(result or {})
    history = list(state.get("turn_history") or [])
    if isinstance(turn, Mapping):
        history.append({"phase": phase, **dict(turn)})
    projection["turn_history"] = history
    ledger = _port(context, "ledger")
    if hasattr(ledger, "record_result"):
        projection = ledger.record_result(
            phase=phase,
            step="interpret",
            state=state,
            result=projection,
        )
    phase_results = dict(state.get("phase_results") or {})
    # The full history is top-level recovery state and belongs in the current
    # operation artifact, but copying it into every per-phase result would make
    # checkpoint size grow quadratically with repair turns.
    phase_results[phase] = {
        key: value for key, value in projection.items() if key != "turn_history"
    }
    return {
        **projection,
        "phase_results": phase_results,
        "turn_history": history,
        "current_phase": phase,
    }


@cycle_registry.node("generation_operation")
def generation_operation(state, config, context) -> Dict[str, Any]:
    """Run a deterministic phase: compile, test, measure, complete.

    These execute real tools but never a model turn, which is why they need no
    prompt, session or interpretation.
    """
    phase = _phase(config)
    backend = _port(context, "backend")
    result = backend.run_phase(phase, state)
    # A backend measures and asks; how long a score-driven repair loop is worth
    # running is one policy for every language, and it is applied before the
    # ledger records the result so a resumed run replays the same decision.
    projection = apply_repair_progress(phase, state, result or {})
    projection = equivalence_review.prepare_review(phase, state, projection, backend)
    ledger = _port(context, "ledger")
    if hasattr(ledger, "record_result"):
        projection = ledger.record_result(
            phase=phase,
            step="deterministic",
            state=state,
            result=projection,
        )
    phase_results = dict(state.get("phase_results") or {})
    phase_results[phase] = dict(projection)
    return {
        **projection,
        "phase_results": phase_results,
        "current_phase": phase,
    }


@cycle_registry.node("rehydrate_generation_operation")
def rehydrate_generation_operation(state, config, context) -> Dict[str, Any]:
    """Restore a result that already exists instead of producing it again.

    This is the node that makes a resumed run cheap. It must never call the
    backend's prompt or interpret path — doing so would pay for the model
    twice and still produce the right answer, so nothing would report it.
    """
    phase, step = _phase(config), str(config.get("step") or "run")
    restored = _port(context, "ledger").rehydrate(phase=phase, step=step, state=state)
    return {**dict(restored or {}), "current_phase": phase}


@cycle_registry.node("pause_generation")
def pause_generation(state, config, context) -> Dict[str, Any]:
    """Stop the cycle without recording a result.

    The turn was cancelled. agent-core's `on_result` port has already persisted
    the cancelled envelope, so there is nothing left to write, and writing
    anything here would be a second cancellation.
    """
    phase = str(state.get("current_phase") or "generation")
    is_cancelled = context.get("is_cancelled")
    if is_cancelled is None or bool(is_cancelled()):
        logger.info("generation paused during %s", phase)
        raise GenerationPaused(phase)
    logger.info("generation resuming from %s", phase)
    return {"current_phase": phase}


@cycle_registry.node("terminate_generation")
def terminate_generation(state, config, context) -> Dict[str, Any]:
    """Stop without manufacturing a successful completion operation.

    Unsafe or indeterminate reconciliation evidence is already the terminal
    fact. Running ``complete_generation`` after it would overwrite the branch
    label and could make an invalid workspace look successfully committed.
    """
    reason = str(state.get("reconciliation") or "unknown_generation_outcome")
    return {
        "phase_outcome": "failed",
        "terminal_reason": reason,
        "error": str(state.get("error") or f"generation stopped: {reason}"),
        "stopped_early": True,
    }


# -- selectors ---------------------------------------------------------------

@cycle_registry.selector("reconciliation_outcome")
def reconciliation_outcome(state) -> str:
    """One of the seven ways a resumed operation can be classified."""
    return str(state.get("reconciliation") or "run")


@cycle_registry.selector("rehydration_outcome")
def rehydration_outcome(state) -> str:
    """Whether a restored envelope is usable.

    Defaults to something other than `ready` on purpose: a rehydration that
    silently reported success would let the cycle continue from a result it
    never actually recovered.
    """
    return str(state.get("rehydration") or "unavailable")


@cycle_registry.selector("normalized_turn_outcome")
def normalized_turn_outcome(state) -> str:
    """Whether a turn may be interpreted, or stops the cycle.

    Only cancellation diverts. Every other status -- failed, timed out, rate
    limited -- still has an answer or an absence the phase's interpreter is
    responsible for judging; deciding that here would put the same judgement
    in two places.
    """
    return "cancelled" if str(state.get("turn_status")) == "cancelled" else "continue"


@cycle_registry.selector("resume_phase")
def resume_phase(state) -> str:
    """Which phase a resumed run comes back to."""
    return str(state.get("current_phase") or "complete_generation")


def _register_phase_selectors() -> None:
    """Bind one named selector per phase, as the topology names them.

    They all read the same state key, so a single generic selector would work.
    Named ones are kept because the name is the documentation: a branch reading
    `compile_outcome` says what it decides, where a shared `phase_outcome`
    would leave a reader to work out which phase is being judged from the
    surrounding YAML.
    """
    spec = generation_cycle_spec()
    generic = {"reconciliation_outcome", "rehydration_outcome",
               "normalized_turn_outcome", "resume_phase"}

    for branch in spec.branches:
        name = branch.selector
        if name in generic or name in cycle_registry.selectors:
            continue

        def read_outcome(state, _name=name) -> str:
            return str(state.get("phase_outcome") or "failed")

        read_outcome.__name__ = name
        read_outcome.__doc__ = f"The verdict {name.replace('_', ' ')} routes on."
        cycle_registry.add_selector(name, read_outcome)


_register_phase_selectors()


# -- building ----------------------------------------------------------------

def resolved_registry() -> NodeRegistry:
    """Everything the topology can name: agent-core's nodes plus UTA's.

    `shared_registry` is extended rather than registered into, so UTA's node
    names cannot collide with another product's -- and `agent_turn` stays the
    one agent-core ships rather than a copy that drifts.
    """
    return shared_registry.extend(cycle_registry)


def build_cycle_workflow(
    *,
    context: Mapping[str, Any],
    checkpointer: Any = None,
    state_schema: Any = None,
):
    """Compile the generation cycle."""
    # Imported here: `graph` needs the langgraph extra, and importing this
    # module must stay cheap for anything that only wants the node functions.
    from agent_core.workflow.graph import build_graph

    return build_graph(
        generation_cycle_spec(),
        resolved_registry(),
        context=dict(context),
        checkpointer=checkpointer,
        state_schema=state_schema or CycleState,
    )


def run_cycle(
    *,
    context: Mapping[str, Any],
    identity: Any,
    initial_state: Mapping[str, Any],
    checkpointer: Any,
    recursion_limit: int = 200,
):
    """Start or resume one target's cycle, doing the work exactly once.

    Invocation goes through agent-core's `invoke_workflow` rather than calling
    the graph directly: supplying the same thread id is *not* enough to resume,
    and a product that invokes a durable graph itself will eventually restart a
    lineage it meant to continue.
    """
    from agent_core.workflow import invoke_workflow

    graph = build_cycle_workflow(context=context, checkpointer=checkpointer)
    return invoke_workflow(
        graph,
        identity=identity,
        initial_state=dict(initial_state),
        recursion_limit=recursion_limit,
    )

__all__ = [
    "CycleState",
    "GenerationPaused",
    "MissingPortError",
    "build_cycle_workflow",
    "cycle_registry",
    "resolved_registry",
    "run_cycle",
    "terminate_generation",
]

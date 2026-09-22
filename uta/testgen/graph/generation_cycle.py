"""Canonical phase vocabulary for a target's generation and quality loop."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

from agent_core.workflow import WorkflowSpec


GENERATION_CYCLE_FILE = Path(__file__).resolve().parent / "generation-cycle.yaml"


@lru_cache(maxsize=1)
def generation_cycle_spec() -> WorkflowSpec:
    """Load the nested lifecycle shared by every language backend.

    Cached because the file is static and parsing it is not free: the topology
    is 69 nodes, and re-reading it per call cost ~29ms. `attach_generation_cycle`
    runs on every backend state update, so an uncached parse turned a report
    render into minutes of YAML parsing. `WorkflowSpec` is frozen, so one
    shared instance is safe to hand out.
    """
    return WorkflowSpec.from_file(GENERATION_CYCLE_FILE)


def generation_phases(spec: WorkflowSpec | None = None) -> tuple:
    """The phase labels an operator sees, in the order they occur.

    Deliberately *not* the node names. The cycle is 69 nodes, and a person
    watching a run does not want to be told that `fix_compile_turn_rehydrate`
    is happening -- they want to know the run is repairing a compile failure.
    Several nodes therefore share one `operator_phase`, and this publishes the
    ordered unique values.

    That separation is what lets the topology be reshaped without breaking a
    dashboard: node mechanics are internal, the label set is the contract.
    """
    cycle = spec if spec is not None else generation_cycle_spec()
    labels = []
    for node in cycle.nodes:
        label = node.config.get("operator_phase")
        if label and label not in labels:
            labels.append(label)
    return tuple(labels)


GENERATION_PHASES = generation_phases()


def attach_generation_cycle(result: Dict[str, Any]) -> Dict[str, Any]:
    """Publish the nested workflow contract alongside backend state updates."""
    cycle = generation_cycle_spec()
    return {
        **result,
        "generation_cycle": {
            "name": cycle.name,
            "phases": list(generation_phases(cycle)),
        },
    }


__all__ = [
    "GENERATION_CYCLE_FILE",
    "GENERATION_PHASES",
    "attach_generation_cycle",
    "generation_cycle_spec",
]

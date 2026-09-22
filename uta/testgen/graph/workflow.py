"""Building the test-generation graph from its description.

The graph used to be 56 lines of `StateGraph` calls here: eight `add_node`,
four `add_edge`, and three `add_conditional_edges` whose routing lived in
lambdas. agent-core builds the same graph from `test-generation.yaml`, so the
shape is data and the only code left is what a step *does* and how it is
named.

The three lambdas became named selectors. That is the part worth having: a
routing rule called `generation_outcome`, registered and testable on its own,
says what it decides -- where `lambda state: "end" if state.get("stopped_early")
else "continue"` had to be read out of the middle of a builder to find out.
"""

from pathlib import Path

from agent_core.workflow import NodeRegistry, WorkflowSpec
# From the submodule: `graph` needs the langgraph extra, so the package
# deliberately does not import it eagerly.
from agent_core.workflow.graph import build_graph

from uta.testgen.graph.nodes import (
    baseline_validate,
    deliver_target,
    finalize,
    run_generation_cycle,
    prepare_context,
    prepare_workspace,
    select_targets,
    select_next_target,
)
from uta.testgen.graph.state import AgentState

WORKFLOW_FILE = Path(__file__).resolve().parent / "test-generation.yaml"

registry = NodeRegistry()

for _name, _fn in (
    ("prepare_workspace", prepare_workspace),
    ("baseline_validate", baseline_validate),
    ("select_targets", select_targets),
    ("prepare_context", prepare_context),
    ("select_next_target", select_next_target),
    ("run_generation_cycle", run_generation_cycle),
    ("deliver_target", deliver_target),
    ("finalize", finalize),
):
    registry.add_node(_name, _fn)


@registry.selector("baseline_outcome")
def baseline_outcome(state) -> str:
    """Whether the tree compiled before we changed anything."""
    return "error" if state.get("error") else "continue"


@registry.selector("next_target_outcome")
def next_target_outcome(state) -> str:
    """Whether there is another normalized target to write tests for."""
    return "end" if state.get("finished") else "generate"


@registry.selector("generation_outcome")
def generation_outcome(state) -> str:
    """Whether the run should keep going after this target batch.

    `stopped_early` is set for a budget cap, a quarantine, or a failure the
    run cannot continue past. It skips the push deliberately.
    """
    return "end" if state.get("stopped_early") else "continue"


def build_workflow():
    """Compile the flow. Unchanged signature: callers get a runnable graph."""
    spec = WorkflowSpec.from_file(WORKFLOW_FILE)
    return build_graph(spec, registry, state_schema=AgentState)

"""The test-generation flow, now described rather than wired.

`build_workflow` was 56 lines of StateGraph calls: eight add_node, four
add_edge, and three add_conditional_edges whose routing lived in lambdas.
agent-core builds the same graph from `test-generation.yaml`.

The graph shape remains stable while its loop vocabulary is target-oriented
instead of Java-class-oriented. These tests pin the nodes, edges, conditional
flags, and routing rules that used to be anonymous lambdas.
"""

from __future__ import annotations

import pytest

from agent_core.workflow import WorkflowSpec
from uta.testgen.graph.workflow import (
    WORKFLOW_FILE,
    baseline_outcome,
    build_workflow,
    generation_outcome,
    next_target_outcome,
    registry,
)
from uta.testgen.targets import active_target_ids

EXPECTED_EDGES = {
    ("__start__", "prepare_workspace", False),
    ("prepare_workspace", "baseline_validate", False),
    ("baseline_validate", "select_targets", True),
    ("baseline_validate", "__end__", True),
    ("select_targets", "prepare_context", False),
    ("prepare_context", "select_next_target", False),
    ("select_next_target", "run_generation_cycle", True),
    ("select_next_target", "finalize", True),
    ("run_generation_cycle", "deliver_target", True),
    ("run_generation_cycle", "__end__", True),
    ("deliver_target", "select_next_target", False),
    ("finalize", "__end__", False),
}


def compiled_edges():
    graph = build_workflow().get_graph()
    return {(e.source, e.target, e.conditional) for e in graph.edges}


def test_the_graph_is_the_one_the_hand_written_builder_produced():
    """Preserve the original flow shape with target-neutral loop names."""
    assert compiled_edges() == EXPECTED_EDGES


def test_every_node_the_spec_names_is_registered():
    """A spec naming an unregistered node should fail at build, not mid-run."""
    spec = WorkflowSpec.from_file(WORKFLOW_FILE)

    for node in spec.nodes:
        assert registry.get_node(node.name if hasattr(node, "name") else node)


def test_the_loop_is_still_a_loop():
    """Each committed target goes back for the next one."""
    assert ("deliver_target", "select_next_target", False) in compiled_edges()


def test_active_targets_prefer_normalized_target_batch():
    state = {
        "current_target_batch": [
            {"language": "python", "target_id": "pysymbol:jobs/run.py::run"},
        ],
        "current_batch": ["legacy.JavaClass"],
    }

    assert active_target_ids(state) == ["pysymbol:jobs/run.py::run"]


def test_active_targets_keep_java_compatibility_fallback():
    assert active_target_ids({"current_batch": ["com.example.Service"]}) == ["com.example.Service"]


# -- the routing rules, which used to be anonymous lambdas ------------------

def test_a_tree_that_does_not_compile_generates_nothing():
    """It cannot tell us whether what we generate compiles either."""
    assert baseline_outcome({"error": "javac failed"}) == "error"


def test_a_clean_baseline_proceeds():
    assert baseline_outcome({}) == "continue"
    assert baseline_outcome({"error": None}) == "continue"


def test_running_out_of_targets_pushes():
    assert next_target_outcome({"finished": True}) == "end"


def test_another_target_generates():
    assert next_target_outcome({}) == "generate"
    assert next_target_outcome({"finished": False}) == "generate"


def test_stopping_early_skips_the_push():
    """A budget cap or quarantine leaves the branch committed but unpushed."""
    assert generation_outcome({"stopped_early": True}) == "end"


def test_a_normal_class_commits_and_continues():
    assert generation_outcome({}) == "continue"
    assert generation_outcome({"stopped_early": False}) == "continue"


@pytest.mark.parametrize(
    "selector, routes",
    [
        (baseline_outcome, {"error", "continue"}),
        (next_target_outcome, {"end", "generate"}),
        (generation_outcome, {"end", "continue"}),
    ],
)
def test_selectors_only_return_routes_the_spec_declares(selector, routes):
    """A selector returning an undeclared route strands the run."""
    for state in ({}, {"error": "x"}, {"finished": True}, {"stopped_early": True}):
        assert selector(state) in routes

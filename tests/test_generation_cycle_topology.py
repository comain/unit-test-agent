"""The generation cycle's shape, asserted rather than trusted.

This topology is 69 nodes. Nobody is going to catch a wrong route by reading
it, so the properties that matter are tested instead of reviewed.

Three of them are load-bearing:

* **Reuse must never re-enter the expensive node.** The entire point of
  reconciliation is that a resumed run does not pay again for a model turn it
  already completed. A `reuse_result` route pointing at the prompt or turn
  would silently double the cost of every interrupted run, and nothing would
  report it — the run would simply succeed, twice as slowly and twice as
  expensively.
* **Repair loops are bounded by the attempt budget, never by the graph.** Each
  repair phase routes `retry` to itself. That is only safe because `exhausted`
  exists and leaves.
* **Cancellation never reaches interpretation or completion.** A cancelled
  turn has no answer to interpret, and completing it would record a result for
  work that was deliberately stopped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.workflow.spec import WorkflowSpec

SPEC_PATH = Path(__file__).resolve().parents[1] / "uta" / "testgen" / "graph" / "generation-cycle.yaml"

#: The labels an operator sees. Additive to the eleven that shipped before:
#: `precheck_existing_tests` and `delegated_quality_gate` already ran, they
#: were simply invisible. Renaming or merging the others would break dashboards
#: and anyone's memory of what a run looks like.
EXPECTED_PHASES = [
    "precheck_existing_tests", "plan_tests", "generate_tests",
    "verify_compile", "fix_compile", "verify_tests", "fix_tests",
    "measure_coverage", "fix_coverage", "measure_mutation", "fix_mutation",
    "delegated_quality_gate", "review_equivalent_mutants", "complete_generation",
]

REPAIR_PHASES = [
    "plan_tests", "generate_tests", "fix_compile", "fix_tests",
    "fix_coverage", "fix_mutation", "delegated_quality_gate",
]


@pytest.fixture(scope="module")
def spec() -> WorkflowSpec:
    return WorkflowSpec.from_file(SPEC_PATH)


@pytest.fixture(scope="module")
def nodes_by_name(spec):
    return {node.name: node for node in spec.nodes}


# -- defaults ----------------------------------------------------------------

def test_every_branch_has_a_default(spec):
    """A selector returning an unmapped key must be a build-time error, not a
    run that stops somewhere nobody can explain."""
    missing = [branch.source for branch in spec.branches if branch.default is None]

    assert missing == []


def test_every_route_target_is_a_real_node(spec, nodes_by_name):
    known = set(nodes_by_name) | {"__end__"}
    dangling = [
        f"{branch.source} -> {target}"
        for branch in spec.branches
        for target in list(branch.routes.values()) + [branch.default]
        if target and target not in known
    ]

    assert dangling == []


# -- reconciliation ----------------------------------------------------------

def test_reuse_never_re_enters_an_expensive_node(spec):
    """The check this file exists for.

    A `reuse_result` or `adopt_result` route landing on a prompt, turn or
    interpret node means a resumed run pays for the model twice. It would still
    produce a correct answer, which is exactly why it would never be noticed.
    """
    expensive = ("_prompt", "_turn", "_interpret", "_run")
    offenders = []
    for branch in spec.branches:
        for key in ("reuse_result", "adopt_result"):
            target = branch.routes.get(key)
            if target and target.endswith(expensive):
                offenders.append(f"{branch.source}.{key} -> {target}")

    assert offenders == []


def test_reuse_always_lands_on_a_rehydrate_node(spec):
    for branch in spec.branches:
        for key in ("reuse_result", "adopt_result"):
            if target := branch.routes.get(key):
                assert "rehydrate" in target, f"{branch.source}.{key} -> {target}"


def test_every_reconcile_node_handles_all_seven_outcomes(spec):
    """A missing key would fall to the default and silently complete the run
    instead of reconciling it."""
    required = {
        "run", "retry_no_effect", "reuse_result", "adopt_result",
        "verify_existing_edit", "fail_indeterminate", "fail_unsafe",
    }
    for branch in spec.branches:
        if branch.selector == "reconciliation_outcome":
            assert required <= set(branch.routes), f"{branch.source} is missing {required - set(branch.routes)}"


def test_an_unsafe_workspace_never_continues_the_cycle(spec):
    """`fail_unsafe` means the workspace could not be trusted. The only safe
    destination is completion, which records the failure."""
    for branch in spec.branches:
        if target := branch.routes.get("fail_unsafe"):
            assert target == "terminate_generation", f"{branch.source} -> {target}"


def test_indeterminate_evidence_never_runs_product_completion(spec):
    for branch in spec.branches:
        if target := branch.routes.get("fail_indeterminate"):
            assert target == "terminate_generation", f"{branch.source} -> {target}"


def test_an_edit_is_verified_by_a_deterministic_verifier(spec, nodes_by_name):
    """`verify_existing_edit` says an interrupted operation left changes behind.
    Sending that to completion would record a result over an unverified diff, so
    edit-capable phases route to a verifier that can prove it compiles and
    passes."""
    edit_capable = {
        "generate_tests": "verify_compile",
        "fix_compile": "verify_compile",
        "fix_tests": "verify_tests",
        "fix_coverage": "verify_tests",
        "fix_mutation": "verify_tests",
        "delegated_quality_gate": "verify_tests",
    }
    for branch in spec.branches:
        if branch.selector != "reconciliation_outcome":
            continue
        phase = nodes_by_name[branch.source].config.get("phase")
        if phase in edit_capable:
            target = branch.routes["verify_existing_edit"]
            assert target.startswith(edit_capable[phase]), f"{phase} -> {target}"


def test_planning_forbids_edits(spec, nodes_by_name):
    """`plan_tests` produces a plan, not a diff. An edit found there is not
    something to verify and carry on from."""
    for branch in spec.branches:
        if branch.selector == "reconciliation_outcome":
            if nodes_by_name[branch.source].config.get("phase") == "plan_tests":
                assert branch.routes["verify_existing_edit"] == "terminate_generation"


# -- bounded repair ----------------------------------------------------------

@pytest.mark.parametrize("phase", REPAIR_PHASES)
def test_a_repair_phase_can_retry_itself(phase, spec, nodes_by_name):
    interpret = f"{phase}_interpret"
    branch = next(b for b in spec.branches if b.source == interpret)

    assert branch.routes["retry"].startswith(phase)


@pytest.mark.parametrize("phase", REPAIR_PHASES)
def test_a_repair_loop_has_a_way_out(phase, spec):
    """`retry` pointing at itself is only safe because `exhausted` leaves. The
    attempt budget in state ends the loop; the graph must never be what does."""
    branch = next(b for b in spec.branches if b.source == f"{phase}_interpret")

    assert branch.routes["exhausted"].startswith("complete_generation")


def test_a_resumed_result_routes_identically_to_a_fresh_one(spec):
    """A rehydrated result must reach the same next phase as one just computed,
    or a resumed run silently takes a different path through the cycle."""
    for branch in spec.branches:
        if not branch.source.endswith("_result_rehydrate"):
            continue
        phase = branch.source[: -len("_result_rehydrate")]
        fresh = next(b for b in spec.branches if b.source == f"{phase}_interpret")

        assert branch.routes == fresh.routes
        assert branch.default == fresh.default


# -- cancellation ------------------------------------------------------------

def test_a_cancelled_turn_pauses_rather_than_interpreting_or_completing(spec):
    """There is no answer to interpret, and completing would record a result
    for work that was deliberately stopped."""
    turn_branches = [b for b in spec.branches if b.selector == "normalized_turn_outcome"]
    assert turn_branches, "no turn branches found"

    for branch in turn_branches:
        assert branch.routes["cancelled"] == "pause_generation"


def test_every_agent_turn_branches_on_its_normalized_status(spec, nodes_by_name):
    """A turn whose status is never inspected cannot honour a cancellation."""
    turn_nodes = {n.name for n in spec.nodes if n.uses == "agent_turn"}
    branched = {b.source for b in spec.branches if b.selector == "normalized_turn_outcome"}

    assert turn_nodes == branched


def test_agent_turns_bind_phase_specific_wall_budgets(spec):
    nodes = {node.name: node for node in spec.nodes}

    assert nodes["plan_tests_turn"].config["timeout_seconds"] == {
        "from_state": "planning_timeout_seconds"
    }
    assert nodes["fix_compile_turn"].config["timeout_seconds"] == {
        "from_state": "compile_fix_timeout_seconds"
    }
    for name in (
        "fix_tests_turn",
        "fix_coverage_turn",
        "fix_mutation_turn",
        "delegated_quality_gate_turn",
    ):
        assert nodes[name].config["timeout_seconds"] == {
            "from_state": "repair_timeout_seconds"
        }


def test_pause_can_resume_into_every_non_terminal_phase(spec):
    """A run paused in any phase must be able to come back to that phase."""
    branch = next(b for b in spec.branches if b.source == "pause_generation")
    resumable = set(branch.routes)

    assert resumable == set(EXPECTED_PHASES) - {"complete_generation"}


# -- the operator contract ---------------------------------------------------

def test_the_phase_labels_are_exactly_the_expected_set(spec):
    """Additive to the eleven that shipped (plus the equivalent-mutant review).
    Renaming or merging a label breaks dashboards and everyone's memory of what
    a run looks like."""
    seen = []
    for node in spec.nodes:
        label = node.config.get("operator_phase")
        if label and label not in seen:
            seen.append(label)

    assert seen == EXPECTED_PHASES


def test_every_node_reports_an_operator_phase(spec):
    unlabelled = [n.name for n in spec.nodes if not n.config.get("operator_phase")]

    assert unlabelled == []


def test_a_phases_nodes_all_report_the_same_label(spec):
    """`plan_tests_prompt`, `_turn` and `_interpret` are one phase to a reader.
    Node mechanics are internal; the label set is the contract."""
    for node in spec.nodes:
        if node.name in {"pause_generation", "terminate_generation"}:
            continue
        assert node.name.startswith(node.config["operator_phase"]), node.name


# -- shape -------------------------------------------------------------------

def test_the_cycle_starts_by_checking_what_already_exists(spec):
    """Generating tests for a target that already has good ones is the most
    expensive possible no-op."""
    assert spec.entry == "precheck_existing_tests_reconcile"


def test_every_model_phase_has_the_full_triple(spec, nodes_by_name):
    for phase in REPAIR_PHASES:
        for suffix in ("turn_reconcile", "prompt", "turn", "turn_rehydrate",
                       "interpret_reconcile", "interpret", "result_rehydrate"):
            assert f"{phase}_{suffix}" in nodes_by_name


def test_deterministic_nodes_use_the_ledger_step_vocabulary(spec, nodes_by_name):
    """The durable schema says ``deterministic``; ``run`` is a node suffix,
    not an operation step and would fail only with the production ledger."""
    for node in spec.nodes:
        if node.uses != "generation_operation":
            continue
        phase = node.config["phase"]
        reconcile = nodes_by_name[f"{phase}_reconcile"]
        rehydrate = nodes_by_name[f"{phase}_rehydrate"]
        assert reconcile.config["step"] == "deterministic"
        assert rehydrate.config["step"] == "deterministic"


def test_the_model_is_only_reached_through_a_prompt(spec):
    """An `agent_turn` entered without its prompt node would send whatever
    prompt file the previous phase left on state."""
    turn_nodes = {n.name for n in spec.nodes if n.uses == "agent_turn"}
    edge_targets = {b for _, b in spec.edges}
    route_targets = {
        target for branch in spec.branches
        for target in list(branch.routes.values()) + [branch.default] if target
    }

    for turn in turn_nodes:
        assert turn not in route_targets, f"{turn} is branched into directly"
        assert turn in edge_targets

    for source, target in spec.edges:
        if target in turn_nodes:
            assert source.endswith("_prompt"), f"{source} -> {target}"


def test_the_cycle_can_end(spec):
    assert any(target == "__end__" for _, target in spec.edges)


# -- cost --------------------------------------------------------------------

def test_the_topology_is_parsed_once_not_per_call():
    """A real defect, caught only because it looked like a hang.

    `attach_generation_cycle` runs on every backend state update and called
    `generation_cycle_spec()` each time. At 65 lines that was cheap. At 68
    nodes it costs ~29ms per parse, and the suite went from 163 seconds to
    over ten minutes with no failure and no error -- just a run that appeared
    to stop. Growing a config file should not be able to do that.
    """
    from unittest.mock import patch

    from agent_core.workflow.spec import WorkflowSpec as Spec
    from uta.testgen.graph import generation_cycle

    generation_cycle.generation_cycle_spec.cache_clear()
    with patch.object(Spec, "from_file", wraps=Spec.from_file) as from_file:
        for _ in range(20):
            generation_cycle.attach_generation_cycle({})

        assert from_file.call_count == 1


def test_the_equivalence_review_is_a_model_phase_that_cannot_edit_or_pass(spec, nodes_by_name):
    """The review asks a question about the code; it must not change it, retry
    itself, or route anywhere but completion -- only the fix session's fresh
    gate rerun may use what it decided."""
    phase = "review_equivalent_mutants"
    for suffix in ("turn_reconcile", "prompt", "turn", "turn_rehydrate",
                   "interpret_reconcile", "interpret", "result_rehydrate"):
        assert f"{phase}_{suffix}" in nodes_by_name
    for branch in spec.branches:
        node = nodes_by_name.get(branch.source)
        if node is None or node.config.get("phase") != phase:
            continue
        if branch.selector == "reconciliation_outcome":
            assert branch.routes["verify_existing_edit"] == "terminate_generation"
        if branch.source in {f"{phase}_interpret", f"{phase}_result_rehydrate"}:
            assert set(branch.routes.values()) | {branch.default} == {"complete_generation_reconcile"}
    entries = {
        branch.source for branch in spec.branches
        if branch.routes.get("review_equivalence") == f"{phase}_turn_reconcile"
    }
    assert entries == {
        "measure_mutation_run", "measure_mutation_rehydrate",
        "delegated_quality_gate_verify_run", "delegated_quality_gate_verify_rehydrate",
    }

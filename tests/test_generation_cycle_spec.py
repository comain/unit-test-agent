from agent_core.workflow import END

from uta.testgen.graph.generation_cycle import (
    GENERATION_PHASES,
    generation_cycle_spec,
)
from uta.testgen.graph import nodes


def _routes(spec):
    return {
        (branch.source, outcome, destination)
        for branch in spec.branches
        for outcome, destination in branch.routes.items()
    }


def test_generation_cycle_exposes_the_human_quality_workflow():
    """The eleven labels people already read, plus the two that always ran
    invisibly, plus the equivalent-mutant review a stalled CI repair now runs. Additive on purpose: surfacing a phase that is genuinely
    happening is an improvement, while renaming or merging a label breaks
    dashboards and everyone's memory of what a run looks like.

    `verify_compile` and `fix_compile` stay separate so an operator can see
    that a run is repairing rather than merely "in the compile phase".
    """
    assert GENERATION_PHASES == (
        "precheck_existing_tests",
        "plan_tests",
        "generate_tests",
        "verify_compile",
        "fix_compile",
        "verify_tests",
        "fix_tests",
        "measure_coverage",
        "fix_coverage",
        "measure_mutation",
        "fix_mutation",
        "delegated_quality_gate",
        "review_equivalent_mutants",
        "complete_generation",
    )


def test_the_published_phases_are_not_the_node_names():
    """The cycle is 69 nodes. A person watching a run does not want to be told
    `fix_compile_turn_rehydrate` is happening."""
    spec = generation_cycle_spec()

    assert len(GENERATION_PHASES) < len(spec.node_names)
    assert "fix_compile_turn" not in GENERATION_PHASES


def test_generation_repairs_return_through_deterministic_verification():
    """A model that edited the workspace does not get to declare itself
    finished. Every repair routes back into a deterministic verifier, which is
    what keeps "the agent said it fixed it" from being the acceptance
    criterion.

    Expressed against the rewritten node names; the invariant is unchanged.
    """
    routes = _routes(generation_cycle_spec())

    # A repair's own verdict sends it back to a verifier that runs real tools.
    assert ("fix_compile_interpret", "passed", "verify_compile_reconcile") in routes
    assert ("fix_tests_interpret", "passed", "verify_tests_reconcile") in routes
    assert ("fix_coverage_interpret", "passed", "verify_tests_reconcile") in routes
    assert ("fix_mutation_interpret", "passed", "verify_tests_reconcile") in routes

    # And a verifier is what decides a repair is needed in the first place.
    assert ("verify_compile_run", "repair", "fix_compile_turn_reconcile") in routes
    assert ("verify_tests_run", "repair", "fix_tests_turn_reconcile") in routes
    assert ("measure_coverage_run", "repair", "fix_coverage_turn_reconcile") in routes
    assert ("measure_mutation_run", "repair", "fix_mutation_turn_reconcile") in routes


def test_generation_cycle_has_explicit_terminal_routes():
    spec = generation_cycle_spec()
    routes = _routes(spec)

    assert ("complete_generation_run", END) in set(spec.edges)
    assert ("verify_compile_run", "failed", "complete_generation_reconcile") in routes
    assert ("verify_tests_run", "failed", "complete_generation_reconcile") in routes


def test_a_passing_mutation_score_now_faces_the_delegated_quality_gate():
    """This used to complete the cycle directly. The gate always ran -- it was
    simply invisible -- so it is now a phase between the last measurement and
    completion, rather than something hidden inside it.
    """
    routes = _routes(generation_cycle_spec())

    assert (
        "measure_mutation_run",
        "passed",
        "delegated_quality_gate_verify_reconcile",
    ) in routes
    assert (
        "delegated_quality_gate_verify_run",
        "passed",
        "complete_generation_reconcile",
    ) in routes
    assert (
        "delegated_quality_gate_interpret",
        "passed",
        "verify_tests_reconcile",
    ) in routes


def test_outer_generation_node_publishes_the_nested_contract(monkeypatch):
    monkeypatch.setattr(
        "uta.testgen.graph.durable_cycle.run_durable_generation_cycle",
        lambda state: {"current_stage": "complete_generation"},
    )

    result = nodes.run_generation_cycle({"language": "python"})

    assert result["current_stage"] == "complete_generation"
    assert result["generation_cycle"] == {
        "name": "test-generation-cycle",
        "phases": list(GENERATION_PHASES),
    }
    assert "fix_compile_prompt" not in result["generation_cycle"]["phases"]

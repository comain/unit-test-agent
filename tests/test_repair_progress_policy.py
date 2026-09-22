"""The score-driven repair loop spends progress, not a fixed number of turns."""

from __future__ import annotations

import pytest

from uta.testgen.repair_progress import (
    SCORES_EVIDENCE_KEY,
    apply_repair_progress,
    decide_repair_progress,
    repair_round_available,
)


def _measured(scores, outcome="repair"):
    return {
        "phase_outcome": outcome,
        "evidence": {SCORES_EVIDENCE_KEY: dict(scores), "failure_reason": "coverage_gate_failed"},
    }


def _state(**updates):
    state = {"attempts_by_phase": {}, "max_attempts_by_phase": {"fix_coverage": 6}}
    state.update(updates)
    return state


@pytest.mark.parametrize('kind', ['coverage', 'mutation'])
@pytest.mark.parametrize('delegated', [False, True])
def test_two_consecutive_flat_turns_persist_and_stop(kind, delegated):
    repair = 'delegated_quality_gate' if delegated else f'fix_{kind}'
    history = f'{repair}:{kind}' if delegated else repair
    phase = 'delegated_quality_gate_verify' if delegated else f'measure_{kind}'
    state = _state(attempts_by_phase={repair: 1},
                   turn_result={'usage': {'main_model': 'pool/model'}},
                   best_scores_by_phase={history: {'target': 87.0}})
    measured = _measured({'target': 87.0})
    if delegated:
        measured['evidence'].update(repair_kind=kind, repair_phase=repair)
    first = apply_repair_progress(phase, state, measured)
    assert first['phase_outcome'] == 'repair'
    assert first['no_progress_by_phase'][history] == 1
    assert first['repair_feedback']
    assert first['effort_strategy'] == 'higher'
    assert first['model_id'] == 'pool/model'
    state.update(first)
    state['attempts_by_phase'] = {repair: 2}
    second = apply_repair_progress(phase, state, measured)
    assert second['phase_outcome'] == 'failed'
    assert second['no_progress_by_phase'][history] == 2
    improved = apply_repair_progress(phase, state, {
        **measured, 'evidence': {**measured['evidence'], SCORES_EVIDENCE_KEY: {'target': 90.0}}})
    assert improved['no_progress_by_phase'][history] == 0
    assert improved['repair_feedback'] == ''
    assert improved['effort_strategy'] == 'default'
    assert improved['model_id'] is None


def test_missing_measurement_does_not_spend_no_progress_allowance():
    result = apply_repair_progress('measure_mutation', _state(
        attempts_by_phase={'fix_mutation': 2},
        no_progress_by_phase={'fix_mutation': 1, 'fix_coverage': 1}), _measured({}))
    assert result['phase_outcome'] == 'repair'
    assert result['no_progress_by_phase'] == {'fix_mutation': 1, 'fix_coverage': 1}
    assert result['repair_feedback'] == ''


def test_hard_cap_overrides_corrective_turn():
    result = apply_repair_progress('measure_mutation', _state(
        attempts_by_phase={'fix_mutation': 1},
        max_attempts_by_phase={'fix_mutation': 1},
        best_scores_by_phase={'fix_mutation': {'target': 80.0}}), _measured({'target': 80.0}))
    assert result['phase_outcome'] == 'failed'
    assert result['evidence']['failure_reason'] == 'mutation_repair_attempts_exhausted'


def test_delegated_measurement_uses_shared_progress_after_resume():
    state = _state(
        attempts_by_phase={"delegated_quality_gate": 3},
        max_attempts_by_phase={"delegated_quality_gate": 6},
        best_scores_by_phase={"delegated_quality_gate:mutation": {"batch": 90.0}},
    )
    measured = _measured({"batch": 94.0})
    measured["evidence"].update(repair_kind="mutation", repair_phase="delegated_quality_gate")
    result = apply_repair_progress("delegated_quality_gate_verify", state, measured)
    assert result["evidence"]["improved_targets"] == ["batch"]
    state.update(best_scores_by_phase=result["best_scores_by_phase"])
    corrective = apply_repair_progress("delegated_quality_gate_verify", state, measured)
    assert corrective["phase_outcome"] == "repair"
    state.update(corrective)
    stopped = apply_repair_progress("delegated_quality_gate_verify", state, measured)
    assert stopped["phase_outcome"] == "failed"
    assert stopped["evidence"]["failure_reason"] == "mutation_repair_no_progress"


def test_delegated_coverage_to_mutation_starts_new_score_history():
    state = _state(best_scores_by_phase={"delegated_quality_gate:coverage": {"batch": 94.0}})
    measured = _measured({"batch": 50.0})
    measured["evidence"].update(repair_kind="mutation", repair_phase="delegated_quality_gate")
    result = apply_repair_progress("delegated_quality_gate_verify", state, measured)
    assert result["phase_outcome"] == "repair"
    assert result["best_scores_by_phase"]["delegated_quality_gate:mutation"] == {"batch": 50.0}


def test_delegated_no_progress_is_persisted_before_cycle_routes():
    from uta.testgen.graph.cycle import generation_operation

    recorded = []

    class Backend:
        def run_phase(self, phase, state):
            result = _measured({"batch": 96.7})
            result["evidence"].update(repair_kind="mutation", repair_phase="delegated_quality_gate")
            return result

    class Ledger:
        def record_result(self, **kwargs):
            recorded.append(kwargs["result"])
            return kwargs["result"]

    result = generation_operation(
        _state(attempts_by_phase={"delegated_quality_gate": 1},
               no_progress_by_phase={"delegated_quality_gate:mutation": 1},
               best_scores_by_phase={"delegated_quality_gate:mutation": {"batch": 96.7}}),
        {"phase": "delegated_quality_gate_verify"},
        {"backend": Backend(), "ledger": Ledger()},
    )
    assert recorded[0]["evidence"]["failure_reason"] == "mutation_repair_no_progress"
    assert result["phase_outcome"] == "failed"


def test_an_improving_score_keeps_repairing_past_the_old_fixed_budget():
    state = _state(
        attempts_by_phase={"fix_coverage": 3},
        best_scores_by_phase={"fix_coverage": {"pkg.Foo": 58.0}},
    )

    result = apply_repair_progress("measure_coverage", state, _measured({"pkg.Foo": 71.0}))

    assert result["phase_outcome"] == "repair"
    assert result["evidence"]["improved_targets"] == ["pkg.Foo"]
    assert result["best_scores_by_phase"]["fix_coverage"] == {"pkg.Foo": 71.0}


def test_a_stalled_score_stops_the_loop_with_attempts_left():
    state = _state(
        no_progress_by_phase={"fix_coverage": 1},
        attempts_by_phase={"fix_coverage": 1},
        best_scores_by_phase={"fix_coverage": {"pkg.Foo": 71.0}},
    )

    result = apply_repair_progress("measure_coverage", state, _measured({"pkg.Foo": 71.0}))

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == "coverage_repair_no_progress"
    assert result["evidence"]["improved_targets"] == []


def test_a_batch_still_progresses_when_one_target_improves():
    state = _state(
        attempts_by_phase={"fix_coverage": 2},
        best_scores_by_phase={"fix_coverage": {"pkg.Foo": 71.0, "pkg.Bar": 40.0}},
    )

    result = apply_repair_progress(
        "measure_coverage", state, _measured({"pkg.Foo": 71.0, "pkg.Bar": 52.0})
    )

    assert result["phase_outcome"] == "repair"
    assert result["evidence"]["improved_targets"] == ["pkg.Bar"]


def test_the_hard_cap_stops_a_score_that_creeps_up_forever():
    state = _state(
        attempts_by_phase={"fix_coverage": 6},
        best_scores_by_phase={"fix_coverage": {"pkg.Foo": 71.0}},
    )

    result = apply_repair_progress("measure_coverage", state, _measured({"pkg.Foo": 74.0}))

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == "coverage_repair_attempts_exhausted"


def test_noise_is_not_progress():
    decision = decide_repair_progress(
        kind="mutation",
        scores={"pkg.Foo": 70.01},
        previous_best={"pkg.Foo": 70.0},
        attempts=1,
        hard_cap=6,
        previous_no_progress=1,
    )

    assert decision.should_repair is False
    assert decision.failure_reason == "mutation_repair_no_progress"


def test_the_first_measurement_always_earns_a_repair():
    result = apply_repair_progress("measure_coverage", _state(), _measured({"pkg.Foo": 12.0}))

    assert result["phase_outcome"] == "repair"
    assert result["best_scores_by_phase"]["fix_coverage"] == {"pkg.Foo": 12.0}


def test_a_passing_measurement_records_its_score_without_becoming_a_repair():
    result = apply_repair_progress(
        "measure_mutation",
        _state(max_attempts_by_phase={"fix_mutation": 6}),
        _measured({"pkg.Foo": 92.0}, outcome="passed"),
    )

    assert result["phase_outcome"] == "passed"
    assert result["best_scores_by_phase"]["fix_mutation"] == {"pkg.Foo": 92.0}


def test_other_phases_and_outcomes_pass_through_untouched():
    measured = _measured({"pkg.Foo": 12.0}, outcome="failed")

    assert apply_repair_progress("verify_tests", _state(), measured) == measured
    assert apply_repair_progress("measure_coverage", _state(), measured) == measured


def test_a_measurement_without_scores_is_bounded_by_the_ceiling_alone():
    """A failed tool run reports no score. That is not a stalled repair, so it
    still earns rounds -- but it must stay bounded, not loop forever."""
    measured = {"phase_outcome": "repair", "evidence": {"failure_reason": "pitest_execution_failed"}}

    granted = apply_repair_progress(
        "measure_mutation",
        _state(attempts_by_phase={"fix_mutation": 1}, max_attempts_by_phase={"fix_mutation": 6}),
        measured,
    )
    stopped = apply_repair_progress(
        "measure_mutation",
        _state(attempts_by_phase={"fix_mutation": 6}, max_attempts_by_phase={"fix_mutation": 6}),
        measured,
    )

    assert granted["phase_outcome"] == "repair"
    assert stopped["phase_outcome"] == "failed"
    assert stopped["evidence"]["failure_reason"] == "mutation_repair_attempts_exhausted"


def test_a_non_numeric_recorded_score_is_discarded_rather_than_crashing():
    state = _state(
        attempts_by_phase={"fix_coverage": 1},
        best_scores_by_phase={"fix_coverage": {"pkg.Foo": None}},
    )

    result = apply_repair_progress("measure_coverage", state, _measured({"pkg.Foo": 71.0}))

    assert result["phase_outcome"] == "repair"
    assert result["best_scores_by_phase"]["fix_coverage"] == {"pkg.Foo": 71.0}


def test_history_of_other_phases_survives_a_decision():
    state = _state(
        best_scores_by_phase={
            "fix_mutation": {"pkg.Foo": 61.0},
            "fix_coverage": {"pkg.Foo": 58.0},
        }
    )

    result = apply_repair_progress("measure_coverage", state, _measured({"pkg.Foo": 71.0}))

    assert result["best_scores_by_phase"] == {
        "fix_mutation": {"pkg.Foo": 61.0},
        "fix_coverage": {"pkg.Foo": 71.0},
    }


def test_a_phase_can_ask_whether_a_round_is_still_possible():
    """Building a survivor map costs subprocesses, so a measuring phase asks
    first. The answer is the ceiling only -- whether the score still justifies
    a round is decided after the measurement, by the policy."""
    assert repair_round_available(
        _state(attempts_by_phase={"fix_mutation": 2},
               max_attempts_by_phase={"fix_mutation": 6}),
        "measure_mutation",
    )
    assert not repair_round_available(
        _state(attempts_by_phase={"fix_mutation": 6},
               max_attempts_by_phase={"fix_mutation": 6}),
        "measure_mutation",
    )
    # A phase this policy does not govern is never told to skip work.
    assert repair_round_available(_state(), "verify_tests")

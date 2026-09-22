"""When a score-driven repair loop is still worth another turn.

Coverage and mutation repair used to stop at a fixed number of turns. That
number is the wrong thing to spend: a run climbing 40% -> 58% -> 71% towards an
80% gate was cut off with the gate still unmet, while a run stuck at the same
score for two turns kept paying for a third.

So the budget here is progress, not turns. A repair round is granted while the
last measurement improved on the best score seen so far for a target that is
still below its gate, and refused after two consecutive flat rounds. The
attempt ceiling stays as a safety net for a score that creeps up forever by
noise; it is not the expected stopping point.

The policy lives here, above the languages, because it is not a Java or a
Python question. A language backend measures and reports -- the still-failing
targets' scores under `scores_by_target` in its evidence, and whether the gate
is met -- and this module decides whether the cycle spends another turn on it.
Both backends therefore stay free of attempt counters, ceilings and exhaustion
reasons, and there is one place to change how a repair loop gives up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping

from uta.testgen.repair import RepairKind

#: Below this, an "improvement" is measurement noise rather than a better test.
#: Scores here are percentages, so this is a twentieth of a point.
IMPROVEMENT_EPSILON = 0.05

#: The repair kinds a score can judge: `RepairKind` already calls these two the
#: gate shortfalls, and a shortfall is exactly what an improving score closes.
#: Deriving the set from there keeps one classification rather than a second
#: hand-written table beside it.
SCORE_DRIVEN_KINDS = (RepairKind.coverage, RepairKind.mutation)

#: The measurement phase each of those kinds is decided on. Both languages run
#: the same neutral cycle spec (`generation-cycle.yaml`), which names the pair
#: `measure_<kind>` / `fix_<kind>`.
SCORE_DRIVEN_PHASES: Dict[str, RepairKind] = {
    f"measure_{kind.value}": kind for kind in SCORE_DRIVEN_KINDS
}

#: Where a backend reports the scores this policy judges progress by.
SCORES_EVIDENCE_KEY = "scores_by_target"

#: Ceiling used when the cycle state names none, so a policy decision is still
#: bounded for an embedder that drives a phase directly.
DEFAULT_HARD_CAP = 6


@dataclass(frozen=True)
class RepairProgressDecision:
    """Whether to repair again, and the best scores that decision was made on."""

    should_repair: bool
    #: `""` while repairing; otherwise the `failure_reason` to report.
    failure_reason: str
    #: Best score per target, including this measurement. Persist it: the next
    #: measurement is compared against this, not against the previous one.
    best_scores: Dict[str, float]
    #: Targets whose score this measurement improved on.
    improved: List[str]
    no_progress_turns: int = 0


def decide_repair_progress(
    *,
    kind: str,
    scores: Mapping[str, float],
    previous_best: Mapping[str, float],
    attempts: int,
    hard_cap: int,
    previous_no_progress: int = 0,
) -> RepairProgressDecision:
    """Grant or refuse one more repair round for `kind` (`coverage`, `mutation`).

    `attempts` is how many repair turns this phase has already spent, and
    `hard_cap` bounds them. `previous_best` is the best score per target from
    every earlier measurement; on the first measurement it is empty, which
    counts as progress so the first repair is always granted.

    An empty `scores` means the measurement produced no score to judge -- a
    failed tool run, not a stalled repair -- so the round is decided on the
    ceiling alone rather than reported as no progress.
    """
    best = _scores(previous_best)
    improved: List[str] = []
    for target, score in _scores(scores).items():
        if target not in best or score > best[target] + IMPROVEMENT_EPSILON:
            improved.append(target)
        best[target] = max(score, best.get(target, score))
    # Missing measurements are tool failures, not evidence of stalled repair.
    streak = 0 if improved else previous_no_progress + int(bool(_scores(scores)) and attempts > 0)
    if int(attempts) >= int(hard_cap):
        return RepairProgressDecision(
            False, f"{kind}_repair_attempts_exhausted", best, improved, streak
        )
    if not improved and _scores(scores) and streak >= 2:
        return RepairProgressDecision(
            False, f"{kind}_repair_no_progress", best, improved, streak
        )
    return RepairProgressDecision(True, "", best, improved, streak)


def repair_round_available(state: Mapping[str, Any], phase: str) -> bool:
    """Could a repair round still follow `phase`, on the ceiling alone?

    A measuring phase asks this before paying for work only a repair round
    would read -- building a survivor map costs subprocesses, and the round
    that has run out of attempts will never use it. This is deliberately only
    the ceiling: whether the score still justifies a round is decided here,
    after the measurement, by `apply_repair_progress`.
    """
    kind = SCORE_DRIVEN_PHASES.get(phase)
    if kind is None:
        return True
    repair_phase = repair_phase_for(kind)
    attempts = int((state.get("attempts_by_phase") or {}).get(repair_phase, 0))
    hard_cap = int(
        (state.get("max_attempts_by_phase") or {}).get(repair_phase, DEFAULT_HARD_CAP)
    )
    return attempts < hard_cap


def apply_repair_progress(
    phase: str, state: Mapping[str, Any], result: Mapping[str, Any]
) -> Dict[str, Any]:
    """Decide a score-driven phase's repair round on the backend's measurement.

    A backend asks for a repair by returning `repair`; this either lets that
    stand or turns it into `failed` with the reason the loop gave up. Every
    other phase and outcome passes through untouched. Combined verifiers opt
    in with neutral `repair_kind` and `repair_phase` evidence, using the latter
    as their turn-counter key. Their score histories remain separate by kind.
    """
    projection = dict(result or {})
    evidence = projection.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    kind = SCORE_DRIVEN_PHASES.get(phase)
    declared_kind = evidence.get("repair_kind")
    if kind is None and declared_kind in {k.value for k in SCORE_DRIVEN_KINDS}:
        kind = RepairKind(declared_kind)
    outcome = str(projection.get("phase_outcome") or "")
    if kind is None or outcome not in ("passed", "repair"):
        return projection
    repair_phase = str(evidence.get("repair_phase") or repair_phase_for(kind))
    # A combined gate shares turns, but coverage and mutation scores are not
    # comparable. Persist separate histories across phase transitions/resumes.
    history_phase = (
        f"{repair_phase}:{kind.value}" if evidence.get("repair_phase") else repair_phase
    )
    reported = evidence.get(SCORES_EVIDENCE_KEY)
    scores = reported if isinstance(reported, Mapping) else {}

    attempts = int((state.get("attempts_by_phase") or {}).get(repair_phase, 0))
    hard_cap = int(
        (state.get("max_attempts_by_phase") or {}).get(repair_phase, DEFAULT_HARD_CAP)
    )
    decision = decide_repair_progress(
        kind=kind.value,
        scores=scores,
        previous_best=best_scores_for(state, history_phase),
        attempts=attempts,
        hard_cap=hard_cap,
        previous_no_progress=int((state.get("no_progress_by_phase") or {}).get(history_phase, 0)),
    )
    evidence.update(
        {
            "attempt": attempts,
            "max_attempts": hard_cap,
            "best_scores": dict(decision.best_scores),
            "improved_targets": list(decision.improved),
            "no_progress_turns": decision.no_progress_turns,
        }
    )
    if outcome == "repair" and not decision.should_repair:
        evidence["failure_reason"] = decision.failure_reason
        outcome = "failed"
    projection.update(
        {
            "phase_outcome": outcome,
            "effort_strategy": "higher" if outcome == "repair" and decision.no_progress_turns and _scores(scores) else "default",
            "model_id": (
                ((state.get("turn_result") or {}).get("usage") or {}).get("main_model")
                if outcome == "repair" and decision.no_progress_turns else None
            ),
            "evidence": evidence,
            "best_scores_by_phase": merged_best_scores(
                state, history_phase, decision.best_scores
            ),
            "no_progress_by_phase": {
                **(state.get("no_progress_by_phase") or {}),
                history_phase: 0 if outcome == "passed" else decision.no_progress_turns,
            },
            "repair_feedback": (
                "The previous repair did not improve the measured score. Reassess the remaining "
                "actionable groups, rather than repeating the same tests. For every mutation group "
                "identify the original and mutated behavior, a distinguishing input, and an observable "
                "assertion. For example, x <= 0 versus x < 0 differs at 0, not 1; keep other guards "
                "satisfied so they cannot mask that difference. For coverage, identify the unexecuted "
                "branch and a fixture that reaches it. Fix broken fixtures rather than deleting the "
                "only distinguishing case. Account for unattempted groups and source-backed blockers. "
                "Do not claim success without verification or change the gates."
                if outcome == "repair" and decision.no_progress_turns and _scores(scores) else ""
            ),
        }
    )
    return projection


def repair_phase_for(kind: RepairKind) -> str:
    """The repair phase a score-driven kind spends its attempts on."""
    return f"fix_{kind.value}"


def best_scores_for(state: Mapping[str, Any], phase: str) -> Dict[str, float]:
    """The best scores recorded for `phase` so far, from cycle state."""
    by_phase = state.get("best_scores_by_phase")
    recorded = by_phase.get(phase) if isinstance(by_phase, Mapping) else None
    return _scores(recorded)


def merged_best_scores(
    state: Mapping[str, Any], phase: str, best: Mapping[str, float]
) -> Dict[str, Dict[str, float]]:
    """The whole `best_scores_by_phase` map with `phase` replaced by `best`.

    Phase nodes project their return value onto state wholesale, so a partial
    map would drop every other phase's history.
    """
    by_phase = state.get("best_scores_by_phase")
    recorded = by_phase.items() if isinstance(by_phase, Mapping) else ()
    merged = {str(name): _scores(scores) for name, scores in recorded}
    merged[phase] = _scores(best)
    return merged


def _scores(mapping: Any) -> Dict[str, float]:
    """Read a per-target score map, dropping anything that is not a number.

    State comes back from a checkpoint, so this is the boundary where a value
    that cannot be compared has to be discarded rather than crash a phase.
    """
    if not isinstance(mapping, Mapping):
        return {}
    scores: Dict[str, float] = {}
    for target, value in mapping.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        scores[str(target)] = float(value)
    return scores


__all__ = [
    "SCORES_EVIDENCE_KEY",
    "SCORE_DRIVEN_KINDS",
    "SCORE_DRIVEN_PHASES",
    "apply_repair_progress",
    "decide_repair_progress",
    "repair_phase_for",
    "repair_round_available",
]

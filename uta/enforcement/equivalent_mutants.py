"""Whether surviving mutants may be excused as equivalent, and on what evidence.

Some mutants cannot be killed: the mutated program behaves exactly like the
original, so no test tells them apart. Deciding that is undecidable in general,
and the heuristics that guess at it (`likely_equivalent`) are only good enough
to rank prompts. So an LLM review judges equivalence -- and this module decides
whether that judgment may be *used*.

Everything here is pure and fails closed. The review may only excuse mutants it
saw, only when it called every one of them equivalent with an argument over the
whole divergence region, only when nothing else counts against the score, and
only when a fresh gate run reproduces exactly those mutants on unchanged source.
Languages supply the survivors (`ScoringSurvivors`); nothing below knows which
language produced them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple

#: The cycle phase whose evidence carries a unit's saved review.
REVIEW_PHASE = "review_equivalent_mutants"
#: Evidence key under that phase, and the result field `complete_generation` persists.
REVIEW_EVIDENCE_KEY = "equivalence_review"

Verdict = Literal["equivalent", "killable", "uncertain"]
_VERDICTS = ("equivalent", "killable", "uncertain")
ReviewOutcome = Literal["all_equivalent", "rejected", "ineligible"]


@dataclass(frozen=True)
class MutantIdentity:
    """One reviewable survivor, keyed by something its language keeps stable."""

    key: str
    source_path: str
    line: int
    operator: str
    description: str
    mutation_diff: str = ""


@dataclass(frozen=True)
class ScoringSurvivors:
    """Every mutant a gate result counts against its mutation score.

    `mutants` are the reviewable survivors. `unreviewed_scoring_failures` counts
    the rest that also lower the score but cannot be argued equivalent -- an
    uncovered mutant is a coverage gap, a timeout is not a verdict -- and any of
    them rules the whole result out.
    """

    language: str
    mutants: Tuple[MutantIdentity, ...]
    unreviewed_scoring_failures: int
    source_fingerprints: Mapping[str, str]
    mutation_rate: float
    mutation_gate: float

    @property
    def keys(self) -> frozenset:
        return frozenset(mutant.key for mutant in self.mutants)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["mutants"] = [asdict(mutant) for mutant in self.mutants]
        payload["source_fingerprints"] = dict(self.source_fingerprints)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ScoringSurvivors":
        return cls(
            language=str(payload.get("language") or ""),
            mutants=tuple(
                MutantIdentity(
                    key=str(item.get("key") or ""),
                    source_path=str(item.get("source_path") or ""),
                    line=int(item.get("line") or 0),
                    operator=str(item.get("operator") or ""),
                    description=str(item.get("description") or ""),
                    mutation_diff=str(item.get("mutation_diff") or ""),
                )
                for item in payload.get("mutants") or ()
            ),
            unreviewed_scoring_failures=int(payload.get("unreviewed_scoring_failures") or 0),
            source_fingerprints={
                str(path): str(digest)
                for path, digest in dict(payload.get("source_fingerprints") or {}).items()
            },
            mutation_rate=float(payload.get("mutation_rate") or 0.0),
            mutation_gate=float(payload.get("mutation_gate") or 0.0),
        )


@dataclass(frozen=True)
class MutantVerdict:
    key: str
    verdict: Verdict
    divergence_region: str
    why_indistinguishable: str
    source_lines: Tuple[int, ...] = ()


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str = ""


@dataclass(frozen=True)
class ParsedVerdicts:
    outcome: Literal["all_equivalent", "rejected"]
    reason: str
    verdicts: Tuple[MutantVerdict, ...] = ()


@dataclass(frozen=True)
class ReviewDecision:
    """What one unit's review concluded; saved with the unit's result."""

    outcome: ReviewOutcome
    reason: str
    survivors: Optional[ScoringSurvivors]
    verdicts: Tuple[MutantVerdict, ...] = ()
    verdicts_sha256: str = ""
    agent_session_ref: Mapping[str, str] = field(default_factory=dict)
    model_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "survivors": self.survivors.to_dict() if self.survivors else None,
            "verdicts": [
                {**asdict(verdict), "source_lines": list(verdict.source_lines)}
                for verdict in self.verdicts
            ],
            "verdicts_sha256": self.verdicts_sha256,
            "agent_session_ref": dict(self.agent_session_ref),
            "model_id": self.model_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReviewDecision":
        survivors = payload.get("survivors")
        outcome = str(payload.get("outcome") or "rejected")
        return cls(
            outcome=outcome if outcome in ("all_equivalent", "rejected", "ineligible") else "rejected",  # type: ignore[arg-type]
            reason=str(payload.get("reason") or ""),
            survivors=ScoringSurvivors.from_dict(survivors) if isinstance(survivors, Mapping) else None,
            verdicts=tuple(
                verdict
                for verdict in (_verdict_from(item) for item in payload.get("verdicts") or ())
                if verdict is not None
            ),
            verdicts_sha256=str(payload.get("verdicts_sha256") or ""),
            agent_session_ref={
                str(key): str(value)
                for key, value in dict(payload.get("agent_session_ref") or {}).items()
            },
            model_id=str(payload.get("model_id") or ""),
        )


@dataclass(frozen=True)
class GrantDecision:
    granted: bool
    reason: str = ""


def assess_eligibility(survivors: Optional[ScoringSurvivors], *, cap: int) -> Eligibility:
    """May this result be reviewed at all?

    `None` means the language could not prove it found every scoring mutant,
    and a review of an incomplete set could excuse mutants nobody looked at.
    """
    if survivors is None:
        return Eligibility(False, "survivors_unproven")
    if survivors.unreviewed_scoring_failures > 0:
        return Eligibility(False, "unreviewed_scoring_failures")
    if not survivors.mutants:
        return Eligibility(False, "no_survivors")
    if len(survivors.mutants) > int(cap):
        return Eligibility(False, "over_cap")
    return Eligibility(True)


def parse_verdicts(payload: Optional[str], *, expected_keys: Iterable[str]) -> ParsedVerdicts:
    """Read the review's verdict file, refusing anything it cannot trust.

    An `equivalent` verdict with no divergence region or no argument over it is
    downgraded to `uncertain`: a single example that happens to agree is not a
    proof, and this is the part of that rule a machine can check.
    """
    try:
        document = json.loads(payload) if payload is not None else None
    except (TypeError, ValueError):
        return ParsedVerdicts("rejected", "verdicts_invalid")
    items = document.get("verdicts") if isinstance(document, Mapping) else document
    if not isinstance(items, list):
        return ParsedVerdicts("rejected", "verdicts_invalid")

    expected = {str(key) for key in expected_keys}
    verdicts: List[MutantVerdict] = []
    seen: set = set()
    for item in items:
        verdict = _verdict_from(item)
        if verdict is None or verdict.key not in expected or verdict.key in seen:
            return ParsedVerdicts("rejected", "verdicts_invalid")
        seen.add(verdict.key)
        verdicts.append(verdict)
    if seen != expected:
        return ParsedVerdicts("rejected", "verdicts_incomplete", tuple(verdicts))
    if any(verdict.verdict != "equivalent" for verdict in verdicts):
        return ParsedVerdicts("rejected", "verdict_not_equivalent", tuple(verdicts))
    return ParsedVerdicts("all_equivalent", "", tuple(verdicts))


def decide_grant(
    reviews: Sequence[ReviewDecision],
    *,
    fresh: Optional[ScoringSurvivors],
    tests_passed: bool,
    coverage_passed: bool,
    mutation_only_failure: bool,
) -> GrantDecision:
    """Does a fresh gate result stand excused by the reviews saved before it?

    The fresh run is the recheck: the reviews were made on the repair task's
    last measurement, so the grant holds only if the gate now reproduces
    exactly those mutants, on the same source, with nothing else failing.
    """
    decisions = list(reviews)
    if not decisions:
        return GrantDecision(False, "no_review")
    if any(review.outcome != "all_equivalent" or review.survivors is None for review in decisions):
        return GrantDecision(False, "review_not_all_equivalent")
    if not tests_passed:
        return GrantDecision(False, "tests_failed")
    if not coverage_passed:
        return GrantDecision(False, "coverage_failed")
    if not mutation_only_failure:
        return GrantDecision(False, "not_mutation_only")
    if fresh is None:
        return GrantDecision(False, "survivors_unproven")
    reviewed = [review.survivors for review in decisions if review.survivors is not None]
    if fresh.unreviewed_scoring_failures or any(item.unreviewed_scoring_failures for item in reviewed):
        return GrantDecision(False, "unreviewed_scoring_failures")
    if fresh.keys != frozenset().union(*(item.keys for item in reviewed)):
        return GrantDecision(False, "survivor_set_changed")
    reviewed_fingerprints: Dict[str, str] = {}
    for item in reviewed:
        reviewed_fingerprints.update(item.source_fingerprints)
    if dict(fresh.source_fingerprints) != reviewed_fingerprints:
        return GrantDecision(False, "fingerprint_changed")
    return GrantDecision(True)


def _verdict_from(item: Any) -> Optional[MutantVerdict]:
    if not isinstance(item, Mapping):
        return None
    key = str(item.get("key") or "").strip()
    verdict = str(item.get("verdict") or "").strip().lower()
    if not key or verdict not in _VERDICTS:
        return None
    region = str(item.get("divergence_region") or "").strip()
    argument = str(item.get("why_indistinguishable") or "").strip()
    if verdict == "equivalent" and (not region or not argument):
        verdict = "uncertain"
    lines = item.get("source_lines") or ()
    return MutantVerdict(
        key=key,
        verdict=verdict,  # type: ignore[arg-type]
        divergence_region=region,
        why_indistinguishable=argument,
        source_lines=tuple(
            int(line) for line in lines if isinstance(line, int) and not isinstance(line, bool)
        ),
    )


__all__ = [
    "Eligibility",
    "GrantDecision",
    "MutantIdentity",
    "MutantVerdict",
    "ParsedVerdicts",
    "REVIEW_EVIDENCE_KEY",
    "REVIEW_PHASE",
    "ReviewDecision",
    "ScoringSurvivors",
    "assess_eligibility",
    "decide_grant",
    "parse_verdicts",
]

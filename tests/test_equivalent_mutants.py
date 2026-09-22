"""The equivalent-mutant rule is pure and fails closed on anything it cannot prove."""

from __future__ import annotations

import json

import pytest

from uta.enforcement.equivalent_mutants import (
    MutantIdentity,
    ReviewDecision,
    ScoringSurvivors,
    assess_eligibility,
    decide_grant,
    parse_verdicts,
)


def _mutant(key="m1", line=3):
    return MutantIdentity(
        key=key,
        source_path="pkg/text.py",
        line=line,
        operator="or_to_and",
        description="`not a or not b` -> `not a and not b`",
    )


def _survivors(*keys, unreviewed=0, fingerprints=None):
    return ScoringSurvivors(
        language="python",
        mutants=tuple(_mutant(key, line=3 + index) for index, key in enumerate(keys or ("m1",))),
        unreviewed_scoring_failures=unreviewed,
        source_fingerprints=fingerprints or {"pkg/text.py": "sha-a"},
        mutation_rate=90.0,
        mutation_gate=95.0,
    )


# The spec's worked example: a genuine equivalence argued over the whole
# divergence region, not from the single input the repair happened to test.
TEXT_SIMILARITY_VERDICT = {
    "key": "m1",
    "verdict": "equivalent",
    "divergence_region": (
        "The predicates differ only when exactly one of a, b is empty; "
        "both-empty and both-non-empty agree."
    ),
    "why_indistinguishable": (
        "With a empty and b non-empty, SequenceMatcher.ratio() is 2*0/len(b) = 0.0; "
        "grams_a == {''} and every element of grams_b is non-empty, so the intersection "
        "is empty while the union is not, giving jaccard 0.0 and a return of 0.0. "
        "The swapped case is symmetric and no exception or side effect differs."
    ),
    "source_lines": [3, 4, 5, 6, 7, 8, 9],
}


def _verdicts(*items):
    return json.dumps({"verdicts": list(items)})


# --- eligibility -------------------------------------------------------------


def test_eligible_with_reviewable_survivors_under_cap():
    assert assess_eligibility(_survivors("m1", "m2"), cap=30).eligible


@pytest.mark.parametrize(
    "survivors, cap, reason",
    [
        (None, 30, "survivors_unproven"),
        (_survivors("m1", unreviewed=1), 30, "unreviewed_scoring_failures"),
        (_survivors("m1", "m2", "m3"), 2, "over_cap"),
        (
            ScoringSurvivors("python", (), 0, {}, 90.0, 95.0),
            30,
            "no_survivors",
        ),
    ],
)
def test_ineligible_reasons(survivors, cap, reason):
    decision = assess_eligibility(survivors, cap=cap)
    assert not decision.eligible
    assert decision.reason == reason


# --- verdict parsing ---------------------------------------------------------


def test_worked_example_parses_as_all_equivalent():
    parsed = parse_verdicts(_verdicts(TEXT_SIMILARITY_VERDICT), expected_keys=["m1"])
    assert parsed.outcome == "all_equivalent"
    assert parsed.reason == ""
    assert parsed.verdicts[0].verdict == "equivalent"
    assert parsed.verdicts[0].source_lines == (3, 4, 5, 6, 7, 8, 9)


def test_bare_list_payload_is_accepted():
    parsed = parse_verdicts(json.dumps([TEXT_SIMILARITY_VERDICT]), expected_keys=["m1"])
    assert parsed.outcome == "all_equivalent"


def test_single_example_argument_without_divergence_region_is_uncertain():
    single_example = {
        **TEXT_SIMILARITY_VERDICT,
        "divergence_region": "",
        "why_indistinguishable": 'text_similarity("", "ab") returns 0.0 for both.',
    }
    parsed = parse_verdicts(_verdicts(single_example), expected_keys=["m1"])
    assert parsed.outcome == "rejected"
    assert parsed.reason == "verdict_not_equivalent"
    assert parsed.verdicts[0].verdict == "uncertain"


@pytest.mark.parametrize(
    "payload, expected_keys, reason",
    [
        (None, ["m1"], "verdicts_invalid"),
        ("not json", ["m1"], "verdicts_invalid"),
        (json.dumps({"verdicts": "nope"}), ["m1"], "verdicts_invalid"),
        (_verdicts(TEXT_SIMILARITY_VERDICT, TEXT_SIMILARITY_VERDICT), ["m1"], "verdicts_invalid"),
        (_verdicts({**TEXT_SIMILARITY_VERDICT, "key": "ghost"}), ["m1"], "verdicts_invalid"),
        (_verdicts({**TEXT_SIMILARITY_VERDICT, "verdict": "maybe"}), ["m1"], "verdicts_invalid"),
        (_verdicts(TEXT_SIMILARITY_VERDICT), ["m1", "m2"], "verdicts_incomplete"),
        (_verdicts({**TEXT_SIMILARITY_VERDICT, "verdict": "killable"}), ["m1"], "verdict_not_equivalent"),
        (_verdicts({**TEXT_SIMILARITY_VERDICT, "verdict": "uncertain"}), ["m1"], "verdict_not_equivalent"),
    ],
)
def test_parse_fails_closed(payload, expected_keys, reason):
    parsed = parse_verdicts(payload, expected_keys=expected_keys)
    assert parsed.outcome == "rejected"
    assert parsed.reason == reason


# --- prompt ------------------------------------------------------------------


def test_prompt_names_every_mutant_the_output_path_and_the_contract():
    from uta.testgen.equivalence_review import render_review_prompt

    prompt = render_review_prompt(_survivors("m1", "m2"), verdicts_path=".uta_cache/equivalence/u1/verdicts.json")
    assert "m1" in prompt and "m2" in prompt
    assert ".uta_cache/equivalence/u1/verdicts.json" in prompt
    assert "divergence_region" in prompt
    assert "Do not edit" in prompt


# --- grant -------------------------------------------------------------------


def _review(survivors=None, outcome="all_equivalent"):
    return ReviewDecision(outcome=outcome, reason="", survivors=survivors or _survivors("m1"))


def _grant(reviews, fresh, **flags):
    values = {"tests_passed": True, "coverage_passed": True, "mutation_only_failure": True}
    values.update(flags)
    return decide_grant(reviews, fresh=fresh, **values)


def test_grants_when_fresh_rerun_reproduces_reviewed_survivors():
    decision = _grant([_review(_survivors("m1", "m2"))], _survivors("m2", "m1"))
    assert decision.granted
    assert decision.reason == ""


def test_grant_unions_reviews_from_several_units():
    first = _review(_survivors("m1", fingerprints={"a.py": "1"}))
    second = _review(_survivors("m2", fingerprints={"b.py": "2"}))
    fresh = _survivors("m1", "m2", fingerprints={"a.py": "1", "b.py": "2"})
    assert _grant([first, second], fresh).granted


def test_grant_accepts_persisted_dict_reviews():
    stored = _review(_survivors("m1")).to_dict()
    assert _grant([ReviewDecision.from_dict(json.loads(json.dumps(stored)))], _survivors("m1")).granted


@pytest.mark.parametrize(
    "reviews, fresh, flags, reason",
    [
        ([], _survivors("m1"), {}, "no_review"),
        ([_review(outcome="rejected")], _survivors("m1"), {}, "review_not_all_equivalent"),
        ([_review(outcome="ineligible")], _survivors("m1"), {}, "review_not_all_equivalent"),
        ([_review()], _survivors("m1"), {"tests_passed": False}, "tests_failed"),
        ([_review()], _survivors("m1"), {"coverage_passed": False}, "coverage_failed"),
        ([_review()], _survivors("m1"), {"mutation_only_failure": False}, "not_mutation_only"),
        ([_review()], None, {}, "survivors_unproven"),
        ([_review()], _survivors("m1", unreviewed=1), {}, "unreviewed_scoring_failures"),
        ([_review()], _survivors("m1", "m9"), {}, "survivor_set_changed"),
        ([_review()], _survivors("m9"), {}, "survivor_set_changed"),
        ([_review()], _survivors("m1", fingerprints={"pkg/text.py": "sha-b"}), {}, "fingerprint_changed"),
    ],
)
def test_grant_refusals(reviews, fresh, flags, reason):
    decision = _grant(reviews, fresh, **flags)
    assert not decision.granted
    assert decision.reason == reason


def test_reviewed_result_with_unreviewed_failures_never_grants():
    tainted = _review(_survivors("m1", unreviewed=2))
    decision = _grant([tainted], _survivors("m1"))
    assert not decision.granted
    assert decision.reason == "unreviewed_scoring_failures"


# --- persistence projection --------------------------------------------------


def test_review_decision_round_trips():
    original = _review(_survivors("m1", "m2"))
    restored = ReviewDecision.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored == original

import pytest

from uta.enforcement.mutation_candidates import (
    MUTMUT15_LEGACY_CHANGED_LINE_SCOPE,
    MUTMUT3_METADATA_SELECTED_EXECUTION,
    MutationCandidate,
    MutationCandidatePlan,
    MutationOpportunity,
    MutationSamplingLayer,
    MutationVerificationContext,
    candidate_plan_allows_zero_scored_mutation,
    candidate_plan_counts,
    compare_candidate_plan_payloads,
    make_candidate_id,
    make_candidate_plan_id,
    make_opportunity_id,
)
from uta_enforce_core.mutation_suppression import LOW_VALUE_LOGGING, suppression_reason_codes


def _context(**overrides):
    values = {
        "repo_url": "git@example/repo.git",
        "base_ref": "origin/master",
        "base_commit": "base",
        "head_commit": "head",
        "target_id": "pyfile:pkg/app.py",
        "source_path": "pkg/app.py",
        "selected_test_paths": ("tests/test_app.py",),
        "runtime_fingerprint": "python3.11",
        "dependency_fingerprint": "deps-a",
        "mutation_tool_version": "mutmut, version 3.3.1",
        "selected_test_policy_version": "strict-v1",
        "operator_policy_version": "py-mutops-v1",
        "suppression_policy_version": "arid-v1",
        "mutation_tool_api_fingerprint": "mutmut-api-a",
        "candidate_plan_config_fingerprint": "cfg-a",
        "policy_mode": "report_full",
        "enable_ci_sampling": False,
    }
    values.update(overrides)
    return MutationVerificationContext(**values)


def _opportunity(line=10, operator="replace_constant"):
    opportunity_id = make_opportunity_id(
        language="python",
        source_path="pkg/app.py",
        line=line,
        symbol="target",
        operator_name=operator,
        source_line="return 1",
        diff_hunk="-return 1\n+return 2",
    )
    return MutationOpportunity(
        language="python",
        source_path="pkg/app.py",
        line=line,
        line_span=(line, line),
        symbol="target",
        opportunity_id=opportunity_id,
        operator_name=operator,
        family_hint="constant",
        diff_hunk="-return 1\n+return 2",
        operator_priority=10,
        roi_score=1.0,
        selection_rank=(0, 10, "pkg/app.py", line, opportunity_id),
        covered=True,
        executable=True,
        selection_reason="covered changed line",
    )


def test_generated_candidate_requires_tool_candidate_key_but_opportunity_does_not():
    opportunity = _opportunity()

    with pytest.raises(ValueError, match="tool_candidate_key"):
        MutationCandidate(
            opportunity=opportunity,
            candidate_id="bad",
            tool_candidate_key="",
            score=1.0,
        )

    candidate_id = make_candidate_id(
        opportunity_id=opportunity.opportunity_id,
        tool_candidate_key="pkg.app.x_target__mutmut_1",
        mutation_tool_api_fingerprint="mutmut-api-a",
    )
    candidate = MutationCandidate(
        opportunity=opportunity,
        candidate_id=candidate_id,
        tool_candidate_key="pkg.app.x_target__mutmut_1",
        score=1.0,
    )

    assert candidate.candidate_id == candidate_id
    assert opportunity.opportunity_id


def test_plan_id_is_stable_and_changes_with_config_fingerprint():
    opportunity = _opportunity()
    candidate = MutationCandidate(
        opportunity=opportunity,
        candidate_id=make_candidate_id(
            opportunity_id=opportunity.opportunity_id,
            tool_candidate_key="pkg.app.x_target__mutmut_1",
            mutation_tool_api_fingerprint="mutmut-api-a",
        ),
        tool_candidate_key="pkg.app.x_target__mutmut_1",
        score=1.0,
    )
    context = _context()

    plan_id = make_candidate_plan_id(
        context=context,
        selected_candidate_ids=(candidate.candidate_id,),
        filter_mechanism=MUTMUT3_METADATA_SELECTED_EXECUTION,
    )
    same_plan_id = make_candidate_plan_id(
        context=context,
        selected_candidate_ids=(candidate.candidate_id,),
        filter_mechanism=MUTMUT3_METADATA_SELECTED_EXECUTION,
    )
    changed_plan_id = make_candidate_plan_id(
        context=_context(candidate_plan_config_fingerprint="cfg-b"),
        selected_candidate_ids=(candidate.candidate_id,),
        filter_mechanism=MUTMUT3_METADATA_SELECTED_EXECUTION,
    )

    assert plan_id == same_plan_id
    assert plan_id != changed_plan_id

    plan = MutationCandidatePlan(
        language="python",
        target_id=context.target_id,
        source_path=context.source_path,
        policy_mode="report_full",
        candidate_plan_id=plan_id,
        changed_lines=(10,),
        executable_changed_lines=(10,),
        covered_changed_lines=(10,),
        eligible_opportunities=(opportunity,),
        suppressed=(),
        report_full_selected=(candidate,),
        active_selected=(candidate,),
        omitted_by_one_per_line=(),
        suppression_by_reason={},
        sampling_layer=MutationSamplingLayer.disabled(),
        filter_mechanism=MUTMUT3_METADATA_SELECTED_EXECUTION,
        generation_policy_artifact=None,
        generation_policy_fingerprint="policy-a",
        exact_tool_candidate_keys=(candidate.tool_candidate_key,),
        planner_version="candidate-plan-v1",
        arid_rule_version="arid-v1",
        test_selection_policy_version="strict-v1",
        suppression_policy_version="arid-v1",
        operator_policy_version="py-mutops-v1",
        mutation_tool_api_fingerprint="mutmut-api-a",
        mutation_tool_config_fingerprint="cfg-a",
        effective_sampling_config={"enabled": False},
        runtime_fingerprint="python3.11",
        dependency_fingerprint="deps-a",
    )

    assert plan.as_dict()["candidatePlanId"] == plan_id
    assert plan.as_dict()["mutationToolApiFingerprint"] == "mutmut-api-a"
    assert plan.as_dict()["mutationToolConfigFingerprint"] == "cfg-a"
    assert "mutmutInternalApiFingerprint" not in plan.as_dict()
    assert "mutmutConfigFingerprint" not in plan.as_dict()
    assert plan.as_dict()["exactToolCandidateKeys"] == ["pkg.app.x_target__mutmut_1"]
    assert plan.as_dict()["selectedKeyExecutionApplied"] is False
    assert plan.as_dict()["adapterFilteredGenerationApplied"] is False
    assert plan.as_dict()["exactToolCandidateKeyCount"] == 1
    assert plan.as_dict()["opportunityIds"] == [opportunity.opportunity_id]
    assert plan.as_dict()["reportFullCandidateIds"] == [candidate.candidate_id]
    assert plan.as_dict()["activeCandidateIds"] == [candidate.candidate_id]
    assert plan.as_dict()["activeToolCandidateKeys"] == ["pkg.app.x_target__mutmut_1"]
    assert plan.as_dict()["runMutants"] == 0
    assert plan.as_dict()["scoredMutants"] == 0
    assert candidate_plan_counts(plan.as_dict())["exactToolCandidateKeys"] == 1
    assert candidate_plan_counts(plan.as_dict())["reportFullCandidates"] == 1


def test_candidate_plan_counts_normalizes_target_and_aggregate_payloads():
    target_payload = {
        "changedLines": [1, 2],
        "coveredChangedLines": [1],
        "eligibleMutationOpportunities": [{"opportunityId": "o1"}, {"opportunityId": "o2"}],
        "suppressed": [{"reasonCode": "low_value_logging"}],
        "omittedByOnePerLine": [{"opportunityId": "o2"}],
        "reportFullSelected": [{"candidateId": "c1"}],
        "activeSelected": [{"candidateId": "c1", "toolCandidateKey": "pkg.app.x_run__mutmut_1"}],
        "exactToolCandidateKeys": ["pkg.app.x_run__mutmut_1"],
        "runMutants": 1,
        "scoredMutants": 1,
        "killed": 1,
    }
    aggregate_payload = {
        "changedLines": 2,
        "coveredChangedLines": 1,
        "eligibleOpportunities": 2,
        "suppressedOpportunities": 1,
        "omittedByOnePerLine": 1,
        "reportFullCandidates": 1,
        "activeCandidates": 1,
        "exactToolCandidateKeys": 1,
        "runMutants": 1,
        "scoredMutants": 1,
        "killed": 1,
    }

    expected = {
        "changedLines": 2,
        "coveredChangedLines": 1,
        "eligibleOpportunities": 2,
        "suppressedOpportunities": 1,
        "omittedByOnePerLine": 1,
        "reportFullCandidates": 1,
        "activeCandidates": 1,
        "exactToolCandidateKeys": 1,
        "runMutants": 1,
        "scoredMutants": 1,
        "killed": 1,
    }
    for payload in (target_payload, aggregate_payload):
        counts = candidate_plan_counts(payload)
        for key, value in expected.items():
            assert counts[key] == value


def test_python2_legacy_plan_carries_no_exact_mutmut3_keys():
    context = _context(mutation_tool_version="mutmut 1.5.0", mutation_tool_api_fingerprint="mutmut15")
    plan_id = make_candidate_plan_id(
        context=context,
        selected_candidate_ids=(),
        filter_mechanism=MUTMUT15_LEGACY_CHANGED_LINE_SCOPE,
    )

    plan = MutationCandidatePlan(
        language="python",
        target_id=context.target_id,
        source_path=context.source_path,
        policy_mode="report_full",
        candidate_plan_id=plan_id,
        changed_lines=(10,),
        executable_changed_lines=(10,),
        covered_changed_lines=(10,),
        eligible_opportunities=(),
        suppressed=(),
        report_full_selected=(),
        active_selected=(),
        omitted_by_one_per_line=(),
        suppression_by_reason={},
        sampling_layer=MutationSamplingLayer.disabled(),
        filter_mechanism=MUTMUT15_LEGACY_CHANGED_LINE_SCOPE,
        generation_policy_artifact=None,
        generation_policy_fingerprint="legacy",
        exact_tool_candidate_keys=(),
        planner_version="candidate-plan-v1",
        arid_rule_version="arid-v1",
        test_selection_policy_version="strict-v1",
        suppression_policy_version="arid-v1",
        operator_policy_version="legacy",
        mutation_tool_api_fingerprint="mutmut15",
        mutation_tool_config_fingerprint="cfg-a",
        effective_sampling_config={"enabled": False},
        runtime_fingerprint="python2.7",
        dependency_fingerprint="deps-py2",
    )

    payload = plan.as_dict()
    assert payload["filterMechanism"] == MUTMUT15_LEGACY_CHANGED_LINE_SCOPE
    assert payload["exactToolCandidateKeys"] == []
    assert payload["selectedKeyExecutionApplied"] is False


def test_zero_scored_mutation_requires_candidate_plan_proof():
    assert candidate_plan_allows_zero_scored_mutation(
        {
            "eligibleMutationOpportunities": [],
            "suppressed": [],
            "reportFullSelected": [],
            "activeSelected": [],
            "exactToolCandidateKeys": [],
        }
    )
    assert candidate_plan_allows_zero_scored_mutation(
        {
            "eligibleMutationOpportunities": [],
            "suppressed": [{"reasonCode": "low_value_logging"}],
            "reportFullSelected": [],
            "activeSelected": [],
            "exactToolCandidateKeys": [],
        }
    )
    assert not candidate_plan_allows_zero_scored_mutation(
        {
            "eligibleMutationOpportunities": [{"opportunityId": "opp-1"}],
            "suppressed": [],
            "reportFullSelected": [],
            "activeSelected": [],
            "exactToolCandidateKeys": [],
        }
    )


def test_engine_suppression_reason_codes_are_language_neutral():
    codes = suppression_reason_codes()

    assert LOW_VALUE_LOGGING in codes
    assert all("python" not in code.lower() for code in codes)


def test_compare_candidate_plan_payloads_distinguishes_drift_from_changed_fingerprint():
    base = {
        "candidatePlanId": "plan-a",
        "exactToolCandidateKeys": ["a", "b"],
        "runtimeFingerprint": "py3",
        "dependencyFingerprint": "deps",
        "mutationToolApiFingerprint": "api",
        "mutationToolConfigFingerprint": "cfg",
        "testSelectionPolicyVersion": "strict",
        "operatorPolicyVersion": "ops",
        "suppressionPolicyVersion": "supp",
    }
    changed_candidates = {**base, "exactToolCandidateKeys": ["a"]}

    bug = compare_candidate_plan_payloads(base, changed_candidates)
    assert bug["comparable"] is True
    assert bug["determinismBug"] is True

    changed_runtime = {**changed_candidates, "runtimeFingerprint": "py3.12"}
    acceptable = compare_candidate_plan_payloads(base, changed_runtime)
    assert acceptable["comparable"] is False
    assert acceptable["determinismBug"] is False
    assert "runtimeFingerprint" in acceptable["changedFingerprints"]


def test_compare_candidate_plan_payloads_ignores_diagnostic_plan_id_when_keys_match():
    base = {
        "candidatePlanId": "single-plan",
        "exactToolCandidateKeys": ["pkg.app.x_target__mutmut_1", "pkg.app.x_target__mutmut_2"],
        "runtimeFingerprint": "py3",
        "dependencyFingerprint": "deps",
        "mutationToolApiFingerprint": "api",
        "mutationToolConfigFingerprint": "cfg",
        "testSelectionPolicyVersion": "strict",
        "operatorPolicyVersion": "ops",
        "suppressionPolicyVersion": "supp",
        "baseCommit": "base-a",
        "headCommit": "head-a",
    }
    batched = {
        **base,
        "candidatePlanId": "batched-aggregate",
    }

    comparison = compare_candidate_plan_payloads(base, batched)

    assert comparison["comparable"] is True
    assert comparison["samePlan"] is True
    assert comparison["determinismBug"] is False


def test_compare_candidate_plan_payloads_treats_changed_head_as_new_context():
    base = {
        "candidatePlanId": "plan-a",
        "exactToolCandidateKeys": ["pkg.app.x_target__mutmut_1"],
        "runtimeFingerprint": "py3",
        "dependencyFingerprint": "deps",
        "mutationToolApiFingerprint": "api",
        "mutationToolConfigFingerprint": "cfg",
        "testSelectionPolicyVersion": "strict",
        "operatorPolicyVersion": "ops",
        "suppressionPolicyVersion": "supp",
        "baseCommit": "base-a",
        "headCommit": "head-a",
    }
    changed_head = {
        **base,
        "candidatePlanId": "plan-b",
        "exactToolCandidateKeys": ["pkg.app.x_target__mutmut_2"],
        "headCommit": "head-b",
    }

    comparison = compare_candidate_plan_payloads(base, changed_head)

    assert comparison["comparable"] is False
    assert comparison["determinismBug"] is False
    assert comparison["changedFingerprints"] == ["headCommit"]

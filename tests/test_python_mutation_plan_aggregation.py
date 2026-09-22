"""T5: aggregate per-batch candidate-plan dicts into one plan, deterministically."""

from uta.language.python.mutation_batching import aggregate_batch_candidate_plans


def _plan(plan_id, *, opp_ids, keys, killed, survived, no_tests=0, timeout=0, suspicious=0):
    return {
        "language": "python",
        "targetId": "t",
        "sourcePath": "pkg/mod.py",
        "policyMode": "report_full",
        "candidatePlanId": plan_id,
        "changedLines": list(range(len(opp_ids))),
        "eligibleMutationOpportunities": [{"opportunityId": o} for o in opp_ids],
        "activeSelected": [{"opportunityId": o, "toolCandidateKey": k} for o, k in zip(opp_ids, keys)],
        "opportunityIds": list(opp_ids),
        "activeCandidateIds": list(opp_ids),
        "activeToolCandidateKeys": list(keys),
        "exactToolCandidateKeys": list(keys),
        "suppressionByReason": {},
        "selectedCandidates": len(opp_ids),
        "exactToolCandidateKeyCount": len(keys),
        "runMutants": len(keys),
        "scoredMutants": killed + survived + no_tests + timeout + suspicious,
        "killed": killed,
        "survived": survived,
        "noTests": no_tests,
        "timeout": timeout,
        "suspicious": suspicious,
    }


def test_aggregate_sums_counts_and_concats_lists():
    p1 = _plan("id1", opp_ids=["o1", "o2"], keys=["m.x_a__mutmut_1", "m.x_a__mutmut_2"], killed=2, survived=0)
    p2 = _plan("id2", opp_ids=["o3"], keys=["m.x_b__mutmut_1"], killed=0, survived=1)

    agg = aggregate_batch_candidate_plans(
        [p1, p2],
        partition_signature="sig123",
        max_generated_bytes=8_000_000,
        batches_meta=[{"index": 0}, {"index": 1}],
        omitted_by_generated_bytes_cap=(),
        omitted_by_max_batches=(),
    )

    assert agg["killed"] == 2
    assert agg["survived"] == 1
    assert agg["scoredMutants"] == 3
    assert agg["selectedCandidates"] == 3
    assert agg["opportunityIds"] == ["o1", "o2", "o3"]
    # tool keys from different functions stay distinct after concat (no collision)
    assert agg["activeToolCandidateKeys"] == ["m.x_a__mutmut_1", "m.x_a__mutmut_2", "m.x_b__mutmut_1"]
    assert agg["generationStrategy"] == "batch"
    assert agg["batchCount"] == 2
    assert agg["partitionSignature"] == "sig123"


def test_aggregate_plan_id_is_deterministic_and_order_independent():
    p1 = _plan("id1", opp_ids=["o1"], keys=["k1"], killed=1, survived=0)
    p2 = _plan("id2", opp_ids=["o2"], keys=["k2"], killed=0, survived=1)

    a = aggregate_batch_candidate_plans([p1, p2], partition_signature="s", max_generated_bytes=8, batches_meta=[], omitted_by_generated_bytes_cap=(), omitted_by_max_batches=())
    b = aggregate_batch_candidate_plans([p2, p1], partition_signature="s", max_generated_bytes=8, batches_meta=[], omitted_by_generated_bytes_cap=(), omitted_by_max_batches=())

    assert a["candidatePlanId"] == b["candidatePlanId"]
    # changing the partition signature changes the plan id (rerun-comparability anchor)
    c = aggregate_batch_candidate_plans([p1, p2], partition_signature="other", max_generated_bytes=8, batches_meta=[], omitted_by_generated_bytes_cap=(), omitted_by_max_batches=())
    assert c["candidatePlanId"] != a["candidatePlanId"]


def test_aggregate_records_lossy_reason_codes():
    p1 = _plan("id1", opp_ids=["o1"], keys=["k1"], killed=1, survived=0)
    agg = aggregate_batch_candidate_plans(
        [p1],
        partition_signature="s",
        max_generated_bytes=8,
        batches_meta=[{"index": 0}],
        omitted_by_generated_bytes_cap=(7, 8),
        omitted_by_max_batches=(9,),
    )
    assert agg["omittedByGeneratedBytesCap"] == [7, 8]
    assert agg["omittedByMaxBatches"] == [9]


def test_aggregate_empty_returns_empty():
    assert aggregate_batch_candidate_plans([], partition_signature="s", max_generated_bytes=8, batches_meta=[], omitted_by_generated_bytes_cap=(), omitted_by_max_batches=()) == {}

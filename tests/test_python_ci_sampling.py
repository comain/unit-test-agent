"""The CI cap profile: what it keeps, and that it keeps it stably.

Cost control, not correctness. A CI run bounds how much mutation work a large
diff can demand; getting the ranking wrong does not fail a build, it quietly
samples different mutants on every run, so a failure stops reproducing.
"""

from __future__ import annotations

from uta.language.python.ci_sampling import CiCapSamplingPolicy


def _opportunity(line: int, priority: int) -> dict:
    return {
        "line": line,
        "operatorPriority": priority,
        "opportunityId": f"opp-{line}",
        "selectionRank": [line],
    }


def test_a_selection_within_the_caps_is_untouched():
    policy = CiCapSamplingPolicy(max_changed_lines=300, max_opportunities=1000, max_selected=300)
    items = [_opportunity(line, 1) for line in range(10)]

    assert policy.select_mutants(items) == items


def test_the_tightest_cap_wins():
    policy = CiCapSamplingPolicy(max_changed_lines=300, max_opportunities=1000, max_selected=5)
    items = [_opportunity(line, 1) for line in range(50)]

    assert len(policy.select_mutants(items)) == 5


def test_higher_priority_opportunities_survive_the_cap():
    """Ranking is the whole value of a cap: keeping an arbitrary 5% of the
    mutants is barely better than keeping none."""
    policy = CiCapSamplingPolicy(max_changed_lines=0, max_opportunities=0, max_selected=3)
    items = [_opportunity(line, priority=line) for line in range(10)]

    kept = policy.select_mutants(items)

    assert sorted(item["operatorPriority"] for item in kept) == [7, 8, 9]


def test_the_plan_order_is_preserved():
    """The policy file's line lists are read as a plan. Returning the kept
    items in rank order would make two identical runs look like different
    plans."""
    policy = CiCapSamplingPolicy(max_changed_lines=0, max_opportunities=0, max_selected=3)
    items = [_opportunity(line, priority=line) for line in range(10)]

    kept = policy.select_mutants(items)

    assert [item["line"] for item in kept] == sorted(item["line"] for item in kept)


def test_the_same_diff_samples_the_same_mutants():
    """A CI failure has to reproduce. Ties break on a digest of the
    opportunity id, so the choice is stable without being alphabetical."""
    policy = CiCapSamplingPolicy(max_changed_lines=0, max_opportunities=0, max_selected=4)
    items = [_opportunity(line, priority=1) for line in range(20)]

    first = policy.select_mutants(items)
    second = policy.select_mutants(list(items))

    assert [item["line"] for item in first] == [item["line"] for item in second]


def test_an_empty_selection_is_not_a_crash():
    policy = CiCapSamplingPolicy(max_changed_lines=1, max_opportunities=1, max_selected=1)

    assert list(policy.select_mutants([])) == []


def test_a_target_allocated_nothing_runs_nothing():
    """Zero is an answer, not a missing limit.

    The report budget is divided across targets up front, so a target can
    legitimately be allocated zero candidates once earlier targets have spent
    it. Treating that like "no cap configured" would hand the target an
    unbounded run -- the opposite of what an exhausted budget means.
    """
    policy = CiCapSamplingPolicy(
        max_changed_lines=0,
        max_opportunities=0,
        max_selected=300,
        allocations={"pkg/worker.py": 0},
    )
    items = [_opportunity(line, 1) for line in range(10)]

    kept = policy.select_mutants(items, target_context={"sourcePath": "pkg/worker.py"})

    assert kept == []


def test_each_target_is_capped_by_its_own_share_of_the_report_budget():
    policy = CiCapSamplingPolicy(
        max_changed_lines=0,
        max_opportunities=0,
        max_selected=300,
        allocations={"pkg/a.py": 2, "pkg/b.py": 1},
    )
    items = [_opportunity(line, 1) for line in range(10)]

    assert len(policy.select_mutants(items, target_context={"sourcePath": "pkg/a.py"})) == 2
    assert len(policy.select_mutants(items, target_context={"sourcePath": "pkg/b.py"})) == 1


def test_an_unallocated_target_falls_back_to_the_raw_caps():
    """A single-target call computes no allocation; the configured caps still
    have to apply, or the profile would stop bounding anything."""
    policy = CiCapSamplingPolicy(
        max_changed_lines=0,
        max_opportunities=0,
        max_selected=3,
        allocations={"pkg/other.py": 1},
    )
    items = [_opportunity(line, 1) for line in range(10)]

    kept = policy.select_mutants(items, target_context={"sourcePath": "pkg/worker.py"})

    assert len(kept) == 3

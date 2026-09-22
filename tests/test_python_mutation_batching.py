"""Unit tests for the deterministic mutation-generation batch partitioner (T2)."""

from uta.language.python.mutation_batching import (
    MutationUnit,
    mutation_units_from_source,
    partition_units_into_batches,
)


SOURCE = '''\
import os


def top_a(x):
    y = x + 1
    return y


def top_b(x):
    z = x - 1
    return z


class C:
    def method(self, a):
        b = a * 2
        return b
'''


def test_units_from_source_groups_by_top_level_function_and_method():
    # selected (line, priority) pairs across top_a, top_b, and C.method
    selected = [(5, 0), (6, 0), (10, 1), (17, 2)]
    units = {u.symbol: u for u in mutation_units_from_source(SOURCE, selected)}
    # class method symbol uses the mutmut class separator "ǁ"
    assert set(units) == {"top_a", "top_b", "Cǁmethod"}
    assert set(units["top_a"].lines) == {5, 6}
    assert units["top_a"].unit_bytes > 0
    assert units["Cǁmethod"].unit_bytes > 0
    assert units["Cǁmethod"].priority == 2
    # module-level import line (line 1) is not a mutation unit
    assert all(1 not in u.lines for u in units.values())


def _u(symbol, unit_bytes, lines, priority=0):
    return MutationUnit(symbol=symbol, unit_bytes=unit_bytes, lines=tuple(lines), priority=priority)


def test_small_units_pack_into_fewer_batches_under_budget():
    # Each unit est = (len(lines)+1)*unit_bytes. Three 2 MB-ish units, 8 MB budget.
    units = [
        _u("a", 1_000_000, [1, 2]),   # (2+1)*1MB = 3MB
        _u("b", 1_000_000, [10, 11]), # 3MB
        _u("c", 1_000_000, [20]),     # 2MB
    ]
    res = partition_units_into_batches(units, budget=8_000_000, max_batches=12)
    # 3+3+2 = 8MB total fits in a single 8MB batch.
    assert len(res.batches) == 1
    assert res.batches[0].lines == (1, 2, 10, 11, 20)
    assert res.batches[0].estimated_bytes == 8_000_000
    assert not res.omitted_by_generated_bytes_cap
    assert not res.omitted_by_max_batches


def test_units_split_when_budget_exceeded():
    units = [
        _u("a", 3_000_000, [1]),   # 6MB
        _u("b", 3_000_000, [2]),   # 6MB
    ]
    res = partition_units_into_batches(units, budget=8_000_000, max_batches=12)
    assert len(res.batches) == 2
    assert all(b.estimated_bytes <= 8_000_000 for b in res.batches)


def test_deterministic_regardless_of_input_order():
    units = [_u("a", 1_000_000, [1, 2]), _u("b", 2_000_000, [10]), _u("c", 500_000, [20, 21, 22])]
    r1 = partition_units_into_batches(units, budget=4_000_000, max_batches=12)
    r2 = partition_units_into_batches(list(reversed(units)), budget=4_000_000, max_batches=12)
    assert r1.signature == r2.signature
    assert [b.lines for b in r1.batches] == [b.lines for b in r2.batches]


def test_oversized_single_unit_is_capped_with_reason():
    # unit_bytes 5MB: one line => (1+1)*5MB = 10MB > 8MB budget. Cannot fit.
    units = [_u("huge", 5_000_000, [1, 2, 3], priority=5)]
    res = partition_units_into_batches(units, budget=8_000_000, max_batches=12)
    # At least one line kept; the rest dropped with a cap reason.
    kept = [ln for b in res.batches for ln in b.lines]
    assert len(kept) >= 1
    assert res.omitted_by_generated_bytes_cap
    assert set(kept).isdisjoint(res.omitted_by_generated_bytes_cap)
    assert sorted(kept + list(res.omitted_by_generated_bytes_cap)) == [1, 2, 3]


def test_max_batches_trims_lowest_priority_units():
    # Four units each needing their own batch (6MB each > half of 8MB), max_batches=2.
    units = [
        _u("hi1", 3_000_000, [1], priority=10),
        _u("hi2", 3_000_000, [2], priority=9),
        _u("lo1", 3_000_000, [3], priority=1),
        _u("lo2", 3_000_000, [4], priority=0),
    ]
    res = partition_units_into_batches(units, budget=4_000_000, max_batches=2)
    assert len(res.batches) <= 2
    kept = {ln for b in res.batches for ln in b.lines}
    # Highest-priority units survive; lowest-priority lines are omitted.
    assert kept == {1, 2}
    assert set(res.omitted_by_max_batches) == {3, 4}


def test_runner_generation_policy_priority_keeps_high_roi_units_when_trimmed():
    from uta.language.python.verification.runner import _mutation_units_from_generation_policy

    source = '''\
def high(value):
    if value > 0:
        return value + 1


def low(value):
    return str(value)
'''
    selected = [
        {"line": 2, "operatorPriority": 100},
        {"line": 7, "operatorPriority": 20},
    ]

    units = _mutation_units_from_generation_policy(source, selected)
    res = partition_units_into_batches(units, budget=64, max_batches=1)

    kept = {ln for batch in res.batches for ln in batch.lines}
    assert kept == {2}
    assert set(res.omitted_by_max_batches) == {7}


def test_each_unit_lands_in_exactly_one_batch():
    units = [_u("a", 500_000, [1, 2]), _u("b", 500_000, [3]), _u("c", 500_000, [4, 5])]
    res = partition_units_into_batches(units, budget=2_000_000, max_batches=12)
    placements = {}
    for b in res.batches:
        for sym in b.symbols:
            assert sym not in placements, "symbol split across batches"
            placements[sym] = b.index
    assert set(placements) == {"a", "b", "c"}


def test_partition_is_lossless_and_collision_free_line_conservation():
    # T7/AC2/AC5: every input line is in exactly one batch OR a reason-coded omission,
    # batches are pairwise disjoint, and nothing is silently dropped.
    units = [
        _u("a", 1_000_000, [1, 2, 3], priority=5),
        _u("b", 2_000_000, [10, 11], priority=4),
        _u("huge", 9_000_000, [20, 21, 22], priority=1),  # oversized -> cap
        _u("c", 1_000_000, [30], priority=0),
    ]
    all_lines = {ln for u in units for ln in u.lines}
    res = partition_units_into_batches(units, budget=8_000_000, max_batches=3)

    batch_lines = [set(b.lines) for b in res.batches]
    # pairwise disjoint (no collision across batches)
    seen: set = set()
    for s in batch_lines:
        assert seen.isdisjoint(s), "line appears in more than one batch"
        seen |= s

    kept = seen
    omitted = set(res.omitted_by_generated_bytes_cap) | set(res.omitted_by_max_batches)
    # conservation: kept ∪ omitted == all input lines, and kept ∩ omitted == ∅
    assert kept | omitted == all_lines
    assert kept.isdisjoint(omitted)


def test_per_batch_policy_filters_are_a_disjoint_cover():
    # The per-batch policy filter (runner) must reproduce exactly each batch's lines so the
    # union of generated batches equals the kept set with no overlap.
    from uta.language.python.verification.runner import _filter_generation_policy_to_lines

    units = [_u("a", 1_000_000, [1, 2]), _u("b", 1_000_000, [10]), _u("c", 1_000_000, [20, 21])]
    res = partition_units_into_batches(units, budget=4_000_000, max_batches=12)
    full_policy = {
        "selectedLines": [1, 2, 10, 20, 21],
        "selected": [{"line": n} for n in (1, 2, 10, 20, 21)],
    }
    covered: set = set()
    for b in res.batches:
        fp = _filter_generation_policy_to_lines(full_policy, set(b.lines))
        lines = {it["line"] for it in fp["selected"]}
        assert lines == set(b.lines)            # filter matches the batch exactly
        assert covered.isdisjoint(lines)        # disjoint across batches
        covered |= lines
    assert covered == {1, 2, 10, 20, 21}

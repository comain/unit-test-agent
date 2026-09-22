"""Tests for uta.language.java.scoring.mutation_roi and its integration with pitest.summarize."""
import os
import tempfile

import pytest

from uta.enforcement.mutation_repair import MutationRepairRoundState, plan_mutation_repair
from uta.language.java.maven.pitest import (
    java_pit_families_to_repair_context,
    summarize_surviving_mutants,
    format_mutation_families_markdown,
)
from uta.language.java.scoring.mutation_roi import (
    _likely_equivalent,
    _family_effort,
)


PITEST_XML = """<?xml version='1.0' encoding='UTF-8'?>
<mutations>
  <mutation status='SURVIVED'>
    <mutatedClass>com.example.Foo</mutatedClass>
    <mutatedMethod>calculate</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator</mutator>
    <description>changed conditional boundary</description>
    <lineNumber>10</lineNumber>
  </mutation>
  <mutation status='SURVIVED'>
    <mutatedClass>com.example.Foo</mutatedClass>
    <mutatedMethod>calculate</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator</mutator>
    <description>changed conditional boundary</description>
    <lineNumber>11</lineNumber>
  </mutation>
  <mutation status='SURVIVED'>
    <mutatedClass>com.example.Foo</mutatedClass>
    <mutatedMethod>sendAll</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.VoidMethodCallMutator</mutator>
    <description>removed call to com.example.Bus::publish</description>
    <lineNumber>30</lineNumber>
  </mutation>
  <mutation status='SURVIVED'>
    <mutatedClass>com.example.Foo</mutatedClass>
    <mutatedMethod>getName</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.VoidMethodCallMutator</mutator>
    <description>removed call to com.example.NameTrimmer::trim</description>
    <lineNumber>50</lineNumber>
  </mutation>
</mutations>
"""


@pytest.fixture
def pitest_xml_path():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
        f.write(PITEST_XML)
        path = f.name
    yield path
    os.unlink(path)


def _method_effort(name, effort_score, band="cheap"):
    return {"name": name, "fqn": f"com.example.Foo.{name}", "effort_score": effort_score, "effort_band": band}


def test_likely_equivalent_getter_side_effect():
    assert _likely_equivalent("getName", "side_effect", "removed call to Trimmer::trim")
    assert _likely_equivalent("getCount", "side_effect", "removed call")
    # isValid is NOT a pure getter — real boolean logic
    assert not _likely_equivalent("calculate", "side_effect", "removed call")


def test_likely_equivalent_logging_removed_call():
    assert _likely_equivalent("sendAll", "side_effect", "removed call to log::debug")
    assert _likely_equivalent("sendAll", "side_effect", "removed call to slf4j::info")


def test_family_effort_adds_delta():
    assert _family_effort(2, "boundary", "") == 2  # +0
    assert _family_effort(2, "side_effect", "") == 4  # +2
    assert _family_effort(2, "side_effect", "removed call to Bus::publish") == 5  # +2 +1
    assert _family_effort(0, "other", "") == 1  # min floor


def test_roi_ranks_cheap_boundary_above_heavy_side_effect(pitest_xml_path):
    method_efforts = [
        _method_effort("calculate", 1, "cheap"),       # boundary (cheap method)
        _method_effort("sendAll", 6, "expensive"),     # side_effect on heavy method
    ]
    ranked = summarize_surviving_mutants(
        pitest_xml_path, "com.example.Foo", method_efforts=method_efforts,
    )
    # getName (likely_equivalent) should be last; calculate should be first
    methods = [r["method"] for r in ranked]
    assert methods[0] == "calculate"
    assert "getName" in methods
    assert methods[-1] == "getName"
    calculate = next(r for r in ranked if r["method"] == "calculate")
    send = next(r for r in ranked if r["method"] == "sendAll")
    assert calculate["roi"] > send["roi"]
    assert calculate["effort_band"] == "cheap"


def test_flag_off_preserves_legacy_ordering(pitest_xml_path):
    # Without method_efforts: falls back to count/killability ordering.
    ranked = summarize_surviving_mutants(pitest_xml_path, "com.example.Foo")
    # No ROI fields attached when flag off
    for fam in ranked:
        assert "roi" not in fam
    # calculate (count=2, killability=3) should be first regardless
    assert ranked[0]["method"] == "calculate"


def test_equivalent_mutant_marked(pitest_xml_path):
    method_efforts = [
        _method_effort("calculate", 1),
        _method_effort("sendAll", 3),
        _method_effort("getName", 1),
    ]
    ranked = summarize_surviving_mutants(
        pitest_xml_path, "com.example.Foo", method_efforts=method_efforts,
    )
    getname = next(r for r in ranked if r["method"] == "getName")
    assert getname["likely_equivalent"] is True
    assert getname["deprioritized"] is True
    assert getname["roi"] == 0.0


def test_markdown_includes_roi_columns_when_scored(pitest_xml_path):
    method_efforts = [_method_effort("calculate", 1), _method_effort("sendAll", 4)]
    ranked = summarize_surviving_mutants(
        pitest_xml_path, "com.example.Foo", method_efforts=method_efforts,
    )
    md = format_mutation_families_markdown(ranked)
    assert "kill-per-effort" in md
    assert "effort" in md
    assert "roi" in md


def test_markdown_omits_roi_columns_when_flag_off(pitest_xml_path):
    ranked = summarize_surviving_mutants(pitest_xml_path, "com.example.Foo")
    md = format_mutation_families_markdown(ranked)
    assert "kill-per-effort" not in md


def test_pit_families_map_losslessly_onto_shared_model(pitest_xml_path):
    # J1 step 3 (model-only convergence): PIT families now flow through the shared
    # MutationRepairGroup. Assert the adapter carries every field the Java prompt
    # relies on, with ROI fields set only when scored.
    from uta.language.java.maven.pitest import pit_families_to_repair_groups

    method_efforts = [_method_effort("calculate", 1), _method_effort("sendAll", 4)]
    ranked = summarize_surviving_mutants(pitest_xml_path, "com.example.Foo", method_efforts=method_efforts)
    groups = pit_families_to_repair_groups(ranked)

    assert [g.symbol for g in groups] == [f["method"] for f in ranked]
    top = groups[0]
    assert top.symbol == "calculate"
    assert top.family == "boundary"
    assert top.killability == "high"
    assert "ConditionalsBoundaryMutator" in top.mutator
    assert top.lines == (10, 11)
    assert top.roi is not None and top.effort_score and top.effort_band
    assert len(top.survivors) == 2  # examples preserved as survivors
    equiv = next(g for g in groups if g.symbol == "getName")
    assert equiv.likely_equivalent is True

    # ROI disabled -> roi unset on every group.
    plain = pit_families_to_repair_groups(summarize_surviving_mutants(pitest_xml_path, "com.example.Foo"))
    assert all(g.roi is None and g.effort_score == "" for g in plain)


def test_java_pit_families_feed_shared_mutation_repair_planner(pitest_xml_path):
    method_efforts = [_method_effort("calculate", 1), _method_effort("sendAll", 4)]
    ranked = summarize_surviving_mutants(pitest_xml_path, "com.example.Foo", method_efforts=method_efforts)
    context = java_pit_families_to_repair_context(
        target_id="com.example.Foo",
        source_path="/repo/src/main/java/com/example/Foo.java",
        test_paths=("/repo/src/test/java/com/example/FooTest.java",),
        reproduce_command="mvn org.pitest:pitest-maven:mutationCoverage",
        families=ranked,
        artifact_path="/repo/.uta_cache/context/Foo.mutation_families.md",
    )

    first = plan_mutation_repair(
        context,
        MutationRepairRoundState(target_id=context.target_id, attempt_index=1),
        focused_group_count=1,
    )

    assert context.language == "java"
    assert first.language == "java"
    assert first.round_kind == "full_roi"
    assert [group.symbol for group in first.selected_groups] == [group.symbol for group in context.groups]
    assert first.prompt_flags["mutation_repair_roi_guided_full"] is True

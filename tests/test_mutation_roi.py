"""Tests for uta.language.java.scoring.mutation_roi and its integration with pitest.summarize."""
import os
import tempfile
import textwrap

import pytest

from uta.maven.pitest import summarize_surviving_mutants, format_mutation_families_markdown
from uta.language.java.scoring.mutation_roi import (
    _likely_equivalent,
    _family_effort,
    roi_sort_key,
    score_families,
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

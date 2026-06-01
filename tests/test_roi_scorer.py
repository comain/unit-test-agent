"""Tests for uta.language.java.scoring.coverage_roi."""
import pytest
from uta.language.java.parse.models import CodeGraph, GraphNode, GraphEdge
from uta.language.java.scoring.coverage_roi import (
    ROI_SCORER_VERSION,
    compute_method_effort,
    compute_class_roi,
    compute_roi_cache_key,
    format_roi_markdown,
    is_degenerate_roi_data,
    is_degenerate_roi_markdown,
    _control_flow_score,
    _dependency_score,
    _effort_band,
    _purity_bonus,
)


def _make_graph(nodes=None, edges=None):
    g = CodeGraph()
    if nodes:
        g.nodes = nodes
    if edges:
        g.edges = edges
    return g


def _method_node(fqn, *, parent_fqn, cyclomatic=1, body_lines=10, params=None, line=1, modifiers=None):
    return GraphNode(
        fqn=fqn,
        kind="method",
        file_path="Foo.java",
        line=line,
        metadata={
            "parent_fqn": parent_fqn,
            "params": params or [],
            "return_type": "void",
            "annotations": [],
            "modifiers": ["public"] if modifiers is None else modifiers,
            "complexity": {
                "cyclomatic_approx": cyclomatic,
                "body_lines": body_lines,
                "branches": max(0, cyclomatic - 1),
                "loops": 0,
                "catches": 0,
                "throws": 0,
                "ternaries": 0,
                "switch_cases": 0,
            },
        },
    )


def _class_node(fqn, *, line=1):
    return GraphNode(
        fqn=fqn,
        kind="class",
        file_path="Foo.java",
        line=line,
        metadata={"annotations": [], "imports": [], "modifiers": []},
    )


def _field_node(fqn, *, parent_fqn, field_type="String", annotations=None):
    return GraphNode(
        fqn=fqn,
        kind="field",
        file_path="Foo.java",
        line=1,
        metadata={
            "parent_fqn": parent_fqn,
            "field_type": field_type,
            "annotations": annotations or [],
        },
    )


# --- Unit tests for scoring components ---

class TestControlFlowScore:
    def test_low_complexity(self):
        assert _control_flow_score({"cyclomatic_approx": 1}) == 0
        assert _control_flow_score({"cyclomatic_approx": 3}) == 0

    def test_medium_complexity(self):
        assert _control_flow_score({"cyclomatic_approx": 5}) == 1
        assert _control_flow_score({"cyclomatic_approx": 7}) == 1

    def test_high_complexity(self):
        assert _control_flow_score({"cyclomatic_approx": 10}) == 2
        assert _control_flow_score({"cyclomatic_approx": 12}) == 2

    def test_very_high(self):
        assert _control_flow_score({"cyclomatic_approx": 20}) == 3

    def test_none(self):
        assert _control_flow_score(None) == 0


class TestDependencyScore:
    def test_no_deps(self):
        assert _dependency_score([], {}) == 0

    def test_few_deps(self):
        assert _dependency_score(["a", "b"], {}) == 1

    def test_many_deps(self):
        assert _dependency_score(["a", "b", "c", "d", "e", "f"], {}) == 3

    def test_cross_boundary_bonus(self):
        # Storage + remote = 2 boundaries → +1
        score = _dependency_score(
            ["com.x.FooMapper.find", "com.x.BarClient.call"],
            {"fooMapper": "FooMapper", "barClient": "BarClient"},
        )
        assert score >= 2  # base 1 (2 calls) + boundary bonus


class TestEffortBand:
    def test_cheap(self):
        assert _effort_band(0) == "cheap"
        assert _effort_band(2) == "cheap"

    def test_medium(self):
        assert _effort_band(3) == "medium"
        assert _effort_band(5) == "medium"

    def test_expensive(self):
        assert _effort_band(6) == "expensive"
        assert _effort_band(10) == "expensive"


class TestPurityBonus:
    def test_pure(self):
        assert _purity_bonus(0, 2) == -2

    def test_near_pure(self):
        assert _purity_bonus(1, 4) == -1

    def test_not_pure(self):
        assert _purity_bonus(3, 8) == 0


# --- Integration tests ---

class TestComputeMethodEffort:
    def test_simple_method(self):
        class_fqn = "com.example.Foo"
        method_fqn = "com.example.Foo.bar"
        graph = _make_graph(
            nodes={
                class_fqn: _class_node(class_fqn),
                method_fqn: _method_node(method_fqn, parent_fqn=class_fqn, cyclomatic=2, body_lines=5),
            },
        )
        result = compute_method_effort(method_fqn, graph, class_fqn)
        assert result["effort_band"] == "cheap"
        assert result["effort_score"] <= 2
        assert "pure-calc" in result["effort_reasons"] or "simple" in result["effort_reasons"]

    def test_complex_method_with_deps(self):
        class_fqn = "com.example.Foo"
        method_fqn = "com.example.Foo.process"
        dep_fqn = "com.example.BarService.doStuff"
        dep2_fqn = "com.example.BazMapper.query"
        dep3_fqn = "com.example.QuxClient.call"
        graph = _make_graph(
            nodes={
                class_fqn: _class_node(class_fqn),
                method_fqn: _method_node(
                    method_fqn, parent_fqn=class_fqn, cyclomatic=10, body_lines=50,
                    params=[("String", "a"), ("int", "b"), ("List", "c"), ("Map", "d"), ("boolean", "e")],
                ),
                dep_fqn: GraphNode(fqn=dep_fqn, kind="method", file_path="Bar.java", line=1, metadata={}),
                dep2_fqn: GraphNode(fqn=dep2_fqn, kind="method", file_path="Baz.java", line=1, metadata={}),
                dep3_fqn: GraphNode(fqn=dep3_fqn, kind="method", file_path="Qux.java", line=1, metadata={}),
            },
            edges=[
                GraphEdge(source=method_fqn, target=dep_fqn, relation="CALLS"),
                GraphEdge(source=method_fqn, target=dep2_fqn, relation="CALLS"),
                GraphEdge(source=method_fqn, target=dep3_fqn, relation="CALLS"),
            ],
        )
        result = compute_method_effort(method_fqn, graph, class_fqn)
        assert result["effort_band"] in ("medium", "expensive")
        assert result["effort_score"] >= 3

    def test_missing_method(self):
        graph = _make_graph()
        result = compute_method_effort("com.example.Foo.missing", graph, "com.example.Foo")
        assert result["effort_score"] == 0
        assert result["effort_band"] == "cheap"


class TestComputeClassRoi:
    def test_basic_class(self):
        class_fqn = "com.example.Foo"
        m1 = "com.example.Foo.simple"
        m2 = "com.example.Foo.complex"
        graph = _make_graph(
            nodes={
                class_fqn: _class_node(class_fqn),
                m1: _method_node(m1, parent_fqn=class_fqn, cyclomatic=2, body_lines=10),
                m2: _method_node(m2, parent_fqn=class_fqn, cyclomatic=15, body_lines=80),
            },
        )
        result = compute_class_roi(class_fqn, graph)
        assert result["class_fqn"] == class_fqn
        assert len(result["methods"]) == 2
        assert result["summary"]["total_methods"] == 2
        # Both methods ranked; complex has higher ROI due to 80 uncovered lines
        # even though effort is higher (80/3=26.7 vs 10/1=10.0)
        assert result["methods"][0]["name"] == "complex"
        assert result["summary"]["cheap_count"] >= 1

    def test_empty_class(self):
        class_fqn = "com.example.Empty"
        graph = _make_graph(nodes={class_fqn: _class_node(class_fqn)})
        result = compute_class_roi(class_fqn, graph)
        assert result["methods"] == []
        assert result["summary"]["total_methods"] == 0

    def test_degenerate_roi_detection(self):
        roi_data = {
            "methods": [
                {"missed_lines": 0, "roi_score": 0.0, "effort_score": 0},
                {"missed_lines": 0, "roi_score": 0.0, "effort_score": 0},
            ],
            "summary": {
                "estimated_cheap_coverage_lines": 0,
                "estimated_medium_coverage_lines": 0,
            },
        }
        assert is_degenerate_roi_data(roi_data) is True

    def test_non_degenerate_roi_detection(self):
        roi_data = {
            "methods": [
                {"missed_lines": 10, "roi_score": 10.0, "effort_score": 1},
                {"missed_lines": 0, "roi_score": 0.0, "effort_score": 2},
            ],
            "summary": {
                "estimated_cheap_coverage_lines": 10,
                "estimated_medium_coverage_lines": 0,
            },
        }
        assert is_degenerate_roi_data(roi_data) is False

    def test_zero_gain_wrappers_are_demoted_in_gate_ranking(self, tmp_path):
        class_fqn = "com.example.Foo"
        cheap_wrapper = "com.example.Foo.wrapper"
        medium_worker = "com.example.Foo.worker"
        graph = _make_graph(
            nodes={
                class_fqn: _class_node(class_fqn),
                cheap_wrapper: _method_node(cheap_wrapper, parent_fqn=class_fqn, cyclomatic=1, body_lines=5),
                medium_worker: _method_node(
                    medium_worker,
                    parent_fqn=class_fqn,
                    cyclomatic=8,
                    body_lines=20,
                    params=[("String", "id"), ("boolean", "flag"), ("int", "limit")],
                ),
            },
        )
        jacoco = tmp_path / "jacoco.xml"
        jacoco.write_text(
            """<report>
  <package name="com/example">
    <class name="com/example/Foo">
      <method name="wrapper" desc="()V" line="1">
        <counter type="LINE" missed="0" covered="5"/>
      </method>
      <method name="worker" desc="()V" line="10">
        <counter type="LINE" missed="18" covered="2"/>
      </method>
    </class>
  </package>
</report>""",
            encoding="utf-8",
        )

        result = compute_class_roi(class_fqn, graph, jacoco_xml_path=str(jacoco))

        assert result["methods"][0]["name"] == "worker"
        assert result["methods"][0]["has_coverage_gain"] is True
        assert result["methods"][1]["name"] == "wrapper"
        assert result["methods"][1]["has_coverage_gain"] is False
        assert result["summary"]["cheap_gain_count"] == 0
        assert result["summary"]["medium_gain_count"] == 1
        assert result["summary"]["zero_gain_count"] == 1

    def test_public_entrypoint_sorts_ahead_of_internal_helper_by_reach(self):
        class_fqn = "com.example.Foo"
        public_entry = "com.example.Foo.entry"
        internal_helper = "com.example.Foo.helper"
        graph = _make_graph(
            nodes={
                class_fqn: _class_node(class_fqn),
                public_entry: _method_node(
                    public_entry,
                    parent_fqn=class_fqn,
                    cyclomatic=2,
                    body_lines=8,
                    modifiers=["public"],
                ),
                internal_helper: _method_node(
                    internal_helper,
                    parent_fqn=class_fqn,
                    cyclomatic=6,
                    body_lines=30,
                    modifiers=[],
                ),
            },
            edges=[
                GraphEdge(source=public_entry, target=internal_helper, relation="CALLS"),
            ],
        )

        result = compute_class_roi(class_fqn, graph)

        assert result["methods"][0]["name"] == "entry"
        assert result["methods"][0]["planning_reach_lines"] == 38
        assert result["methods"][1]["name"] == "helper"
        assert result["methods"][1]["planning_reach_lines"] == 30

    def test_branch_richer_method_outranks_thin_wrapper_with_helper_reach(self):
        class_fqn = "com.example.Foo"
        wrapper = "com.example.Foo.wrapper"
        helper = "com.example.Foo.helper"
        stateful = "com.example.Foo.stateful"
        graph = _make_graph(
            nodes={
                class_fqn: _class_node(class_fqn),
                wrapper: _method_node(
                    wrapper,
                    parent_fqn=class_fqn,
                    cyclomatic=1,
                    body_lines=20,
                    modifiers=["public"],
                ),
                helper: _method_node(
                    helper,
                    parent_fqn=class_fqn,
                    cyclomatic=1,
                    body_lines=80,
                    modifiers=[],
                ),
                stateful: _method_node(
                    stateful,
                    parent_fqn=class_fqn,
                    cyclomatic=8,
                    body_lines=70,
                    modifiers=["public"],
                ),
            },
            edges=[
                GraphEdge(source=wrapper, target=helper, relation="CALLS"),
            ],
        )

        result = compute_class_roi(class_fqn, graph)
        ordered = [m["name"] for m in result["methods"][:3]]

        assert ordered[0] == "stateful"
        assert result["methods"][0]["planning_score"] > result["methods"][1]["planning_score"]

    def test_public_helper_called_by_public_entry_is_demoted(self):
        class_fqn = "com.example.Foo"
        entry = "com.example.Foo.entry"
        helper = "com.example.Foo.helper"
        graph = _make_graph(
            nodes={
                class_fqn: _class_node(class_fqn),
                entry: _method_node(
                    entry,
                    parent_fqn=class_fqn,
                    cyclomatic=5,
                    body_lines=40,
                    modifiers=["public"],
                ),
                helper: _method_node(
                    helper,
                    parent_fqn=class_fqn,
                    cyclomatic=6,
                    body_lines=60,
                    modifiers=["public"],
                ),
            },
            edges=[
                GraphEdge(source=entry, target=helper, relation="CALLS"),
            ],
        )

        result = compute_class_roi(class_fqn, graph)
        assert result["methods"][0]["name"] == "entry"
        helper_row = next(m for m in result["methods"] if m["name"] == "helper")
        assert helper_row["same_class_public_callers"] == 1

    def test_batch_wrapper_family_is_demoted_below_stateful_methods(self):
        class_fqn = "com.example.Foo"
        batch = "com.example.Foo.batchFinished"
        finished = "com.example.Foo.finished"
        downgrade = "com.example.Foo.markPickingOrdersDowngrade"
        helper = "com.example.Foo.helper"
        graph = _make_graph(
            nodes={
                class_fqn: _class_node(class_fqn),
                batch: _method_node(
                    batch,
                    parent_fqn=class_fqn,
                    cyclomatic=1,
                    body_lines=23,
                    modifiers=["public"],
                ),
                finished: _method_node(
                    finished,
                    parent_fqn=class_fqn,
                    cyclomatic=5,
                    body_lines=77,
                    modifiers=["public"],
                ),
                downgrade: _method_node(
                    downgrade,
                    parent_fqn=class_fqn,
                    cyclomatic=7,
                    body_lines=90,
                    modifiers=["public"],
                ),
                helper: _method_node(
                    helper,
                    parent_fqn=class_fqn,
                    cyclomatic=1,
                    body_lines=110,
                    modifiers=[],
                ),
            },
            edges=[
                GraphEdge(source=batch, target=finished, relation="CALLS"),
                GraphEdge(source=batch, target=helper, relation="CALLS"),
            ],
        )

        result = compute_class_roi(class_fqn, graph)
        ordered = [m["name"] for m in result["methods"][:3]]

        assert set(ordered[:2]) == {"markPickingOrdersDowngrade", "finished"}
        assert ordered[2] == "batchFinished"


class TestFormatRoiMarkdown:
    def test_basic_format(self):
        roi_data = {
            "class_fqn": "com.example.Foo",
            "methods": [
                {
                    "name": "bar",
                    "effort_score": 1,
                    "effort_band": "cheap",
                    "missed_lines": 20,
                    "planning_reach_lines": 20,
                    "roi_score": 20.0,
                    "effort_reasons": ["simple"],
                },
            ],
            "summary": {
                "total_methods": 1,
                "cheap_count": 1,
                "medium_count": 0,
                "expensive_count": 0,
                "estimated_cheap_coverage_lines": 20,
                "estimated_medium_coverage_lines": 0,
            },
        }
        md = format_roi_markdown(roi_data)
        assert "# ROI Scores: Foo" in md
        assert "| 1 | `bar` | 1 | cheap | 20 | 20 | 20.0 | simple |" in md
        assert "cheap" in md
        assert "Coverage Strategy Guidance" in md

    def test_mixed_bands(self):
        roi_data = {
            "class_fqn": "com.example.Mixed",
            "methods": [
                {"name": "a", "effort_score": 1, "effort_band": "cheap", "missed_lines": 30, "planning_reach_lines": 30, "roi_score": 30.0, "effort_reasons": ["simple"], "visibility_rank": 0, "planning_score": 30.0},
                {"name": "b", "effort_score": 4, "effort_band": "medium", "missed_lines": 20, "planning_reach_lines": 20, "roi_score": 5.0, "effort_reasons": ["3-collaborators"], "visibility_rank": 0, "planning_score": 5.0},
                {"name": "c", "effort_score": 8, "effort_band": "expensive", "missed_lines": 10, "planning_reach_lines": 10, "roi_score": 1.2, "effort_reasons": ["non-deterministic"], "visibility_rank": 0, "planning_score": 1.2},
            ],
            "summary": {
                "total_methods": 3,
                "cheap_count": 1,
                "medium_count": 1,
                "expensive_count": 1,
                "cheap_gain_count": 1,
                "medium_gain_count": 1,
                "expensive_gain_count": 1,
                "zero_gain_count": 0,
                "estimated_cheap_coverage_lines": 30,
                "estimated_medium_coverage_lines": 20,
                "estimated_expensive_coverage_lines": 10,
            },
        }
        md = format_roi_markdown(roi_data)
        assert "Cheap (effort 0-2): 1 methods with uncovered reach" in md
        assert "Medium (effort 3-5): 1 methods with uncovered reach" in md
        assert "Expensive (effort 6+): 1 methods with uncovered reach" in md
        assert "Zero-gain public wrappers: 0 methods" in md

    def test_debug_markdown_includes_provenance(self):
        roi_data = {
            "class_fqn": "com.example.Debug",
            "methods": [
                {"name": "a", "effort_score": 1, "effort_band": "cheap", "missed_lines": 3, "planning_reach_lines": 3, "roi_score": 3.0, "effort_reasons": ["simple"]},
            ],
            "summary": {
                "total_methods": 1,
                "cheap_count": 1,
                "medium_count": 0,
                "expensive_count": 0,
                "cheap_gain_count": 1,
                "medium_gain_count": 0,
                "expensive_gain_count": 0,
                "zero_gain_count": 0,
                "estimated_cheap_coverage_lines": 3,
                "estimated_medium_coverage_lines": 0,
                "estimated_expensive_coverage_lines": 0,
            },
            "provenance": {
                "roi_scorer_version": ROI_SCORER_VERSION,
                "jacoco_used": True,
                "jacoco_xml_path": "/tmp/jacoco.xml",
            },
        }
        md = format_roi_markdown(roi_data, debug=True)
        assert "## Provenance" in md
        assert "/tmp/jacoco.xml" in md
        assert "`True`" in md

    def test_degenerate_markdown_detection(self):
        md = """# ROI Scores: Foo

## Summary
- Cheap (effort 0-2): 10 methods with uncovered reach, ~0 uncovered lines
- Medium (effort 3-5): 0 methods with uncovered reach, ~0 uncovered lines

| # | Method | Effort | Band | Missed Lines | Reach | ROI | Reasons |
|---|--------|--------|------|-------------|-------|-----|---------|
| 1 | `a` | 0 | cheap | 0 | 0 | 0.0 | simple |
"""
        assert is_degenerate_roi_markdown(md) is True

    def test_format_demotes_zero_gain_wrappers_in_guidance(self):
        roi_data = {
            "class_fqn": "com.example.Gate",
            "methods": [
                {"name": "worker", "effort_score": 4, "effort_band": "medium", "missed_lines": 18, "planning_reach_lines": 18, "roi_score": 4.5, "effort_reasons": ["3-collaborators"], "visibility_rank": 0, "planning_score": 4.5},
                {"name": "wrapper", "effort_score": 1, "effort_band": "cheap", "missed_lines": 0, "planning_reach_lines": 0, "roi_score": 0.0, "effort_reasons": ["simple"], "visibility_rank": 0, "planning_score": 0.0},
            ],
            "summary": {
                "total_methods": 2,
                "cheap_count": 1,
                "medium_count": 1,
                "expensive_count": 0,
                "cheap_gain_count": 0,
                "medium_gain_count": 1,
                "expensive_gain_count": 0,
                "zero_gain_count": 1,
                "estimated_cheap_coverage_lines": 0,
                "estimated_medium_coverage_lines": 18,
                "estimated_expensive_coverage_lines": 0,
            },
        }
        md = format_roi_markdown(roi_data)
        assert "Cheap-band methods are mostly zero-gain wrappers here" in md
        assert "continue to medium-band methods (`worker`) for ~18 more lines" in md
        assert "Do not count zero-gain public wrappers" in md


class TestRoiCacheKey:
    def test_cache_key_changes_with_jacoco_and_debug(self, tmp_path):
        src = tmp_path / "Foo.java"
        jacoco = tmp_path / "jacoco.xml"
        src.write_text("class Foo {}", encoding="utf-8")
        jacoco.write_text("<report/>", encoding="utf-8")
        base = compute_roi_cache_key(str(src))
        with_jacoco = compute_roi_cache_key(str(src), str(jacoco))
        debug = compute_roi_cache_key(str(src), str(jacoco), debug=True)
        assert base != with_jacoco
        assert with_jacoco != debug

from pathlib import Path

from uta.language.java.parse.models import CodeGraph, GraphNode

from uta.language.java.context_builder import ContextBuilder
from uta.language.java.scoring.coverage_roi import compute_roi_cache_key


def _class_node(fqn: str, file_path: str) -> GraphNode:
    return GraphNode(
        fqn=fqn,
        kind="class",
        file_path=file_path,
        line=1,
        metadata={"annotations": [], "imports": [], "modifiers": []},
    )


def test_export_roi_scores_overwrites_degenerate_cached_artifact(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "Foo.java"
    source.write_text("class Foo {}", encoding="utf-8")

    graph = CodeGraph()
    class_fqn = "com.example.Foo"
    graph.nodes[class_fqn] = _class_node(class_fqn, str(source))

    builder = ContextBuilder(str(repo), graph, [])
    cache_key = compute_roi_cache_key(str(source))
    roi_path = repo / ".uta_cache" / "context" / "Foo.roi.md"
    roi_path.parent.mkdir(parents=True, exist_ok=True)
    roi_path.write_text(
        f"<!-- cache:{cache_key} -->\n# ROI Scores: Foo\n\n## Summary\n- Cheap (effort 0-2): 1 methods, ~0 uncovered lines\n- Medium (effort 3-5): 0 methods, ~0 uncovered lines\n",
        encoding="utf-8",
    )

    roi_data = {
        "class_fqn": class_fqn,
        "methods": [
            {
                "name": "bar",
                "effort_score": 1,
                "effort_band": "cheap",
                "missed_lines": 5,
                "missed_branches": 0,
                "roi_score": 5.0,
                "effort_reasons": ["simple"],
            }
        ],
        "summary": {
            "total_methods": 1,
            "cheap_count": 1,
            "medium_count": 0,
            "expensive_count": 0,
            "estimated_cheap_coverage_lines": 5,
            "estimated_medium_coverage_lines": 0,
        },
        "provenance": {
            "roi_scorer_version": "test",
            "jacoco_used": False,
            "jacoco_xml_path": "",
        },
    }

    out = builder.export_roi_scores(class_fqn, roi_data, source_path=str(source))

    assert out.endswith("/Foo.roi.md")
    content = roi_path.read_text(encoding="utf-8")
    assert "- Cheap (effort 0-2): 1 methods with uncovered reach, ~5 uncovered lines" in content
    assert "| 1 | `bar` | 1 | cheap | 5 | 5 | 5.0 | simple |" in content


def test_clear_roi_scores_removes_cached_artifact(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "Foo.java"
    source.write_text("class Foo {}", encoding="utf-8")

    graph = CodeGraph()
    class_fqn = "com.example.Foo"
    graph.nodes[class_fqn] = _class_node(class_fqn, str(source))

    builder = ContextBuilder(str(repo), graph, [])
    roi_path = repo / ".uta_cache" / "context" / "Foo.roi.md"
    roi_path.parent.mkdir(parents=True, exist_ok=True)
    roi_path.write_text("# ROI Scores: Foo\n", encoding="utf-8")

    builder.clear_roi_scores(class_fqn)

    assert not roi_path.exists()

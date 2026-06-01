"""Unit tests for coverage hardening loop."""

from pathlib import Path
from unittest.mock import MagicMock

from uta.graph.nodes import run_coverage_fix_loop


def test_coverage_fix_loop_retries_until_gate(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    test_file = repo / "DemoTest.java"
    test_file.write_text("class DemoTest {}", encoding="utf-8")
    source_file = repo / "Demo.java"
    source_file.write_text("class Demo {}", encoding="utf-8")

    attempts = []

    def fake_run_test_with_jacoco(repo_path, test_class_name, module):
        attempts.append(1)
        return True, ""

    def fake_find_jacoco_report(repo_path, module):
        return "jacoco.xml"

    coverages = iter([45.0, 82.0])

    def fake_parse_jacoco_report(report, class_fqn):
        return {"line": next(coverages)}

    monkeypatch.setattr("uta.graph.nodes.run_test_with_jacoco", fake_run_test_with_jacoco)
    monkeypatch.setattr("uta.graph.nodes._run_test", lambda *a, **kw: (True, ""))
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", fake_find_jacoco_report)
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", fake_parse_jacoco_report)

    client = MagicMock()
    client.send_message = MagicMock()
    client.send_message_split = MagicMock()
    client.poll_completion = MagicMock(return_value={"type": "completed", "result": ""})

    ok, coverage, _, focused_session_ids = run_coverage_fix_loop(
        repo_path=str(repo),
        module="biz",
        class_fqn="com.example.Demo",
        session_id="sess-1",
        client=client,
        test_class_name="DemoTest",
        test_file_abs=test_file,
        source_file_abs=source_file,
        coverage_gate=80,
        current_coverage=20.0,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=2,
    )

    assert ok is True
    assert coverage == 82.0
    assert focused_session_ids == [client.create_session.return_value]
    assert len(attempts) == 2
    assert client.send_message_split.call_count == 2


def test_coverage_fix_loop_stops_if_tests_break(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    test_file = repo / "FooTest.java"
    test_file.write_text("class FooTest {}", encoding="utf-8")

    monkeypatch.setattr("uta.graph.nodes.run_test_with_jacoco", lambda *args, **kwargs: (False, "boom"))
    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: (False, "still broken"))

    client = MagicMock()
    client.send_message = MagicMock()
    client.send_message_split = MagicMock()
    client.poll_completion = MagicMock(return_value={"type": "completed", "result": ""})

    ok, coverage, output, focused_session_ids = run_coverage_fix_loop(
        repo_path=str(repo),
        module=None,
        class_fqn="com.example.Foo",
        session_id="sess-1",
        client=client,
        test_class_name="FooTest",
        test_file_abs=test_file,
        source_file_abs=None,
        coverage_gate=80,
        current_coverage=10.0,
        maven_module_flag="",
        max_fix_attempts=2,
    )

    assert ok is False
    assert coverage == 10.0
    assert output == "still broken"
    assert focused_session_ids == [client.create_session.return_value]
    assert client.send_message.call_count == 2
    assert client.send_message_split.call_count == 1


def test_coverage_fix_loop_routes_jacoco_test_error_into_test_fix(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    test_file = repo / "FooTest.java"
    test_file.write_text("class FooTest {}", encoding="utf-8")
    source_file = repo / "Foo.java"
    source_file.write_text("class Foo {}", encoding="utf-8")

    jacoco_calls = {"count": 0}

    def fake_run_test_with_jacoco(*args, **kwargs):
        jacoco_calls["count"] += 1
        if jacoco_calls["count"] == 1:
            return False, "jacoco boom"
        return True, ""

    monkeypatch.setattr("uta.graph.nodes.run_test_with_jacoco", fake_run_test_with_jacoco)
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda *args, **kwargs: {"line": 82.0})
    monkeypatch.setattr("uta.graph.nodes._run_test_selector", lambda *args, **kwargs: (True, ""))

    client = MagicMock()
    client.send_message = MagicMock()
    client.send_message_split = MagicMock()
    client.poll_completion = MagicMock(return_value={"type": "completed", "result": ""})

    ok, coverage, output, focused_session_ids = run_coverage_fix_loop(
        repo_path=str(repo),
        module=None,
        class_fqn="com.example.Foo",
        session_id="sess-1",
        client=client,
        test_class_name="FooTest",
        test_file_abs=test_file,
        source_file_abs=source_file,
        coverage_gate=80,
        current_coverage=10.0,
        maven_module_flag="",
        max_fix_attempts=1,
    )

    assert ok is True
    assert coverage == 82.0
    assert output == ""
    assert jacoco_calls["count"] == 2
    assert client.send_message_split.call_count == 1
    assert client.send_message.call_count == 1
    assert "coverage-hardening turn made" in client.send_message.call_args[0][1]
    assert focused_session_ids == [client.create_session.return_value]


def test_coverage_fix_loop_refreshes_roi_from_jacoco_and_ignores_degenerate_artifact(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    test_file = repo / "DemoTest.java"
    test_file.write_text("class DemoTest {}", encoding="utf-8")
    source_file = repo / "Demo.java"
    source_file.write_text("class Demo {}", encoding="utf-8")
    roi_file = repo / ".uta_cache" / "context" / "Demo.roi.md"
    roi_file.parent.mkdir(parents=True, exist_ok=True)
    roi_file.write_text(
        "<!-- cache:bad -->\n# ROI Scores: Demo\n\n## Summary\n- Cheap (effort 0-2): 1 methods, ~0 uncovered lines\n- Medium (effort 3-5): 0 methods, ~0 uncovered lines\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("uta.graph.nodes.run_test_with_jacoco", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr("uta.graph.nodes._run_test", lambda *a, **kw: (True, ""))
    monkeypatch.setattr("uta.graph.nodes.find_jacoco_report", lambda *args, **kwargs: "jacoco.xml")
    monkeypatch.setattr("uta.graph.nodes.parse_jacoco_report", lambda *args, **kwargs: {"line": 82.0})
    monkeypatch.setattr(
        "uta.graph.nodes.extract_uncovered_clusters",
        lambda *args, **kwargs: {"methods": [{"name": "m", "missed_lines": 5}], "line_clusters": []},
    )

    captured = {}

    def fake_round(**kwargs):
        captured["roi_abs"] = kwargs["roi_abs"]
        return "focused-session"

    monkeypatch.setattr("uta.graph.nodes._run_focused_coverage_fix_round", fake_round)
    monkeypatch.setattr(
        "uta.language.java.scoring.coverage_roi.compute_class_roi",
        lambda *args, **kwargs: {
            "class_fqn": "com.example.Demo",
            "methods": [{"name": "m", "effort_score": 1, "effort_band": "cheap", "missed_lines": 5, "missed_branches": 0, "roi_score": 5.0, "effort_reasons": ["simple"]}],
            "summary": {"total_methods": 1, "cheap_count": 1, "medium_count": 0, "expensive_count": 0, "estimated_cheap_coverage_lines": 5, "estimated_medium_coverage_lines": 0},
            "provenance": {"roi_scorer_version": "test", "jacoco_used": True, "jacoco_xml_path": "jacoco.xml"},
        },
    )

    class DummyGraph:
        nodes = {}
        edges = []

    client = MagicMock()

    ok, coverage, _, focused_session_ids = run_coverage_fix_loop(
        repo_path=str(repo),
        module="biz",
        class_fqn="com.example.Demo",
        session_id="sess-1",
        client=client,
        test_class_name="DemoTest",
        test_file_abs=test_file,
        source_file_abs=source_file,
        coverage_gate=80,
        current_coverage=20.0,
        maven_module_flag=" -pl biz -am",
        max_fix_attempts=1,
        roi_abs=str(roi_file),
        graph=DummyGraph(),
    )

    assert ok is True
    assert coverage == 82.0
    assert focused_session_ids == ["focused-session"]
    assert captured["roi_abs"].endswith("/Demo.roi.md")
    assert "Missed Lines | Reach | ROI" in roi_file.read_text(encoding="utf-8")

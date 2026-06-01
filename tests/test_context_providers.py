import shutil
from pathlib import Path

import pytest

from uta.engine.context import ContextProvider, ContextQuery
from uta.language.java.context import JavaContextProvider
from uta.language.python.context import PythonContextProvider
from uta.engine.context import make_context_provider
from uta.engine.languages import RawTargetSelection, default_registry
from uta.language.java.parse.models import CodeGraph, GraphEdge, GraphNode
from uta.tasks.targets import TargetIdentity


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "python_projects"


def _java_graph(repo: Path) -> CodeGraph:
    source = repo / "src" / "main" / "java" / "com" / "example" / "Foo.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package com.example;\n"
        "public class Foo {\n"
        "  Bar bar;\n"
        "  public String greet(String name) { return bar.value(name); }\n"
        "}\n",
        encoding="utf-8",
    )
    bar = repo / "src" / "main" / "java" / "com" / "example" / "Bar.java"
    bar.write_text(
        "package com.example;\n"
        "public class Bar { public String value(String name) { return name; } }\n",
        encoding="utf-8",
    )
    graph = CodeGraph()
    graph.nodes["com.example.Foo"] = GraphNode(
        fqn="com.example.Foo",
        kind="class",
        file_path=str(source),
        line=2,
        metadata={"annotations": [], "imports": [], "modifiers": []},
    )
    graph.nodes["com.example.Foo.bar"] = GraphNode(
        fqn="com.example.Foo.bar",
        kind="field",
        file_path=str(source),
        line=3,
        metadata={"parent_fqn": "com.example.Foo", "field_type": "Bar", "annotations": []},
    )
    graph.nodes["com.example.Foo.greet"] = GraphNode(
        fqn="com.example.Foo.greet",
        kind="method",
        file_path=str(source),
        line=4,
        metadata={
            "parent_fqn": "com.example.Foo",
            "return_type": "String",
            "params": [("String", "name")],
            "annotations": [],
        },
    )
    graph.nodes["com.example.Bar"] = GraphNode(
        fqn="com.example.Bar",
        kind="class",
        file_path=str(bar),
        line=2,
        metadata={"annotations": [], "imports": [], "modifiers": []},
    )
    graph.edges.append(GraphEdge(source="com.example.Foo.greet", target="com.example.Bar.value", relation="CALLS"))
    return graph


def test_java_context_provider_exports_and_queries_target_context(tmp_path):
    graph = _java_graph(tmp_path)
    provider = JavaContextProvider(tmp_path, graph, [])
    target = TargetIdentity.java_class("com.example.Foo")

    exported = provider.export_target_context(target, module="biz", test_file_rel="src/test/java/FooTest.java")
    payload = provider.query_target(target, ContextQuery(sections=("class", "methods"), limit=5))

    assert isinstance(provider, ContextProvider)
    assert Path(exported["context_abs"]).is_file()
    assert Path(exported["symbols_abs"]).is_file()
    assert payload["language"] == "java"
    assert payload["target"]["target_id"] == "com.example.Foo"
    assert payload["class"]["fqn"] == "com.example.Foo"
    assert payload["methods"][0]["name"] == "greet"


def test_context_provider_factory_returns_java_and_python_providers(tmp_path):
    graph = _java_graph(tmp_path / "java")
    java_provider = make_context_provider("java", tmp_path / "java", graph=graph, flows=[])
    python_provider = make_context_provider("python", FIXTURE_ROOT / "py3_flat_project")

    assert isinstance(java_provider, JavaContextProvider)
    assert isinstance(python_provider, PythonContextProvider)
    assert java_provider.language == "java"
    assert python_provider.language == "python"


def test_python_context_provider_contract_exports_project_and_target_context(tmp_path):
    repo = tmp_path / "py3_flat_project"
    shutil.copytree(FIXTURE_ROOT / "py3_flat_project", repo)
    provider = make_context_provider("python", repo)
    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py::forecast_for_store")
    )

    index = provider.export_project_context(max_files=2)
    exported = provider.export_target_context(target)
    payload = provider.query_target(target)

    assert isinstance(provider, PythonContextProvider)
    assert isinstance(provider, ContextProvider)
    assert index["language"] == "python"
    assert Path(exported["json_abs"]).is_file()
    assert Path(exported["context_abs"]).is_file()
    assert payload["target"]["target_id"] == target.target_id
    assert payload["found"] is True


def test_java_context_provider_requires_graph(tmp_path):
    with pytest.raises(ValueError, match="CodeGraph"):
        make_context_provider("java", tmp_path)

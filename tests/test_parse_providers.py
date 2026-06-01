import shutil
from pathlib import Path

from uta.engine.parse import ParseProjectRequest, ParseProvider, make_parse_provider
from uta.language.java.parse import JavaParseProjectResult
from uta.language.python.parse import PythonParseProjectResult


PY_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "python_projects"


def _copy_java_fixture_repo(fixtures_dir: str, repo: Path) -> None:
    service_dir = repo / "src" / "main" / "java" / "com" / "example" / "service"
    mapper_dir = repo / "src" / "main" / "java" / "com" / "example" / "mapper"
    service_dir.mkdir(parents=True)
    mapper_dir.mkdir(parents=True)
    shutil.copy(Path(fixtures_dir) / "SampleService.java", service_dir / "SampleService.java")
    shutil.copy(Path(fixtures_dir) / "SampleBizImpl.java", service_dir / "SampleBizImpl.java")
    shutil.copy(Path(fixtures_dir) / "SampleMapper.java", mapper_dir / "SampleMapper.java")


def test_java_parse_provider_builds_graph_and_normalized_callables(fixtures_dir, tmp_path):
    _copy_java_fixture_repo(fixtures_dir, tmp_path)
    provider = make_parse_provider("java")

    result = provider.parse_project(ParseProjectRequest(repo_path=tmp_path))

    assert isinstance(provider, ParseProvider)
    assert isinstance(result, JavaParseProjectResult)
    assert result.language == "java"
    assert result.contains_target("com.example.service.SampleService")
    assert result.target_id_for_source_path("src/main/java/com/example/service/SampleService.java") == "com.example.service.SampleService"
    assert result.target_selections(["com.example.service.SampleService"])[0]["target_id"] == "com.example.service.SampleService"
    assert any(item.qualified_name.endswith(".SampleService") for item in result.callables)
    assert result.graph.nodes


def test_python_parse_provider_builds_normalized_symbols():
    repo = PY_FIXTURE_ROOT / "py3_flat_project"
    provider = make_parse_provider("python")

    result = provider.parse_project(ParseProjectRequest(repo_path=repo))

    assert isinstance(result, PythonParseProjectResult)
    assert result.language == "python"
    assert "jobs/forecast.py" in result.source_files
    assert any(item.qualified_name == "forecast_for_store" for item in result.callables)
    assert result.target_id_for_source_path("jobs/forecast.py") == "pyfile:jobs/forecast.py"

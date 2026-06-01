import json
import shutil
from pathlib import Path

from click.testing import CliRunner

from uta.cli import main
from uta.engine.languages import RawTargetSelection, default_registry
from uta.language.python.adapter import PythonLanguageAdapter
from uta.language.python.context_builder import PythonContextBuilder
from uta.language.python.parse.parser import PythonParser


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "python_projects"


def test_python3_parser_extracts_symbols_without_importing_target():
    repo = FIXTURE_ROOT / "py3_flat_project"
    result = PythonParser().parse_file(repo / "jobs" / "forecast.py", repo_path=repo)
    symbols = {symbol.qualified_name: symbol for symbol in result.symbols}

    assert result.syntax_version == "python3"
    assert result.parser_backend in {"tree_sitter", "ast"}
    assert result.relative_path == "jobs/forecast.py"
    assert "forecast_for_store" in symbols
    assert "StoreForecast" in symbols
    assert "StoreForecast.predict" in symbols
    assert symbols["forecast_for_store"].kind == "function"
    assert symbols["StoreForecast.predict"].parent == "StoreForecast"


def test_python2_parser_classifies_legacy_syntax_without_importing_target():
    repo = FIXTURE_ROOT / "py2_legacy_project"
    result = PythonParser().parse_file(repo / "legacy_job.py", repo_path=repo)
    symbols = {symbol.qualified_name: symbol for symbol in result.symbols}

    assert result.syntax_version == "python2"
    assert result.syntax_error is not None
    assert "legacy_total" in symbols
    assert "legacy_ratio" in symbols


def test_python_context_export_includes_target_symbol_and_companions(tmp_path):
    repo = FIXTURE_ROOT / "py3_flat_project"
    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py::forecast_for_store")
    )
    builder = PythonContextBuilder(repo)

    context = builder.build_target_context(target)
    exported = builder.export_target_context(target, output_dir=tmp_path)

    assert context["target"]["symbol"] == "forecast_for_store"
    assert any(symbol["qualified_name"] == "forecast_for_store" for symbol in context["symbols"])
    assert any(item["path"] == "jobs/__init__.py" for item in context["companion_files"])
    assert any(item["path"] == "tests/test_forecast.py" for item in context["companion_files"])
    assert Path(exported["json_abs"]).is_file()
    assert Path(exported["context_abs"]).is_file()
    assert "forecast_for_store" in Path(exported["context_abs"]).read_text(encoding="utf-8")


def test_python_query_index_returns_context_payload(tmp_path):
    repo = tmp_path / "py3_flat_project"
    shutil.copytree(FIXTURE_ROOT / "py3_flat_project", repo)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(repo),
            "--language",
            "python",
            "--target",
            "jobs/forecast.py::forecast_for_store",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["found"] is True
    assert payload["language"] == "python"
    assert payload["syntax"]["version"] == "python3"
    assert any(symbol["qualified_name"] == "forecast_for_store" for symbol in payload["symbols"])
    assert payload["context"]["json_abs"]
    assert payload["context"]["context_abs"]


def test_python_query_index_reports_missing_symbol(tmp_path):
    repo = tmp_path / "py3_flat_project"
    shutil.copytree(FIXTURE_ROOT / "py3_flat_project", repo)
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "query-index",
            "--repo",
            str(repo),
            "--language",
            "python",
            "--target",
            "jobs/forecast.py::does_not_exist",
        ],
    )

    assert result.exit_code != 0
    assert "Python target symbol not found" in result.output


def test_python_parse_command_writes_project_index(tmp_path):
    repo = tmp_path / "py3_flat_project"
    shutil.copytree(FIXTURE_ROOT / "py3_flat_project", repo)
    runner = CliRunner()

    result = runner.invoke(main, ["parse", "--repo", str(repo), "--language", "python"])

    assert result.exit_code == 0, result.output
    index_path = repo / ".uta_cache" / "python_context" / "index.json"
    assert index_path.is_file()
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["language"] == "python"
    assert payload["files"][0]["path"] == "jobs/forecast.py"


def test_python_scan_candidates_applies_deterministic_cap_and_skip_reasons(tmp_path):
    repo = tmp_path / "repo"
    for idx in range(5):
        path = repo / "jobs" / f"job_{idx}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"def run_{idx}():\n    return {idx}\n", encoding="utf-8")

    selection = PythonLanguageAdapter().select_candidates(repo, max_files=3)

    assert [target.source_path for target in selection.targets] == [
        "jobs/job_0.py",
        "jobs/job_1.py",
        "jobs/job_2.py",
    ]
    assert selection.selected_count == 3
    assert selection.skipped_count == 2
    assert selection.skipped_targets == [
        {"target_id": "pyfile:jobs/job_3.py", "source_path": "jobs/job_3.py", "reason": "max_files_exceeded"},
        {"target_id": "pyfile:jobs/job_4.py", "source_path": "jobs/job_4.py", "reason": "max_files_exceeded"},
    ]


def test_python_project_index_records_selection_limits_and_skipped_targets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for idx in range(4):
        path = repo / f"job_{idx}.py"
        path.write_text(f"def run_{idx}():\n    return {idx}\n", encoding="utf-8")

    payload = PythonContextBuilder(repo).export_project_index(output_dir=tmp_path / "out", max_files=2)

    assert [item["path"] for item in payload["files"]] == ["job_0.py", "job_1.py"]
    assert payload["selection"]["selected_count"] == 2
    assert payload["selection"]["skipped_count"] == 2
    assert payload["selection"]["max_files"] == 2
    assert [item["source_path"] for item in payload["selection"]["skipped_targets"]] == ["job_2.py", "job_3.py"]


def test_python_parse_command_accepts_max_files_and_reports_skipped(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for idx in range(3):
        path = repo / f"job_{idx}.py"
        path.write_text(f"def run_{idx}():\n    return {idx}\n", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(main, ["parse", "--repo", str(repo), "--language", "python", "--max-files", "1"])

    assert result.exit_code == 0, result.output
    assert "Found 1 Python files" in result.output
    assert "Skipped: 2 Python files (max_files_exceeded)" in result.output
    index_path = repo / ".uta_cache" / "python_context" / "index.json"
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["selection"]["max_files"] == 1
    assert payload["selection"]["skipped_count"] == 2

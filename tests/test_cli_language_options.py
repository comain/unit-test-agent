import json
from pathlib import Path

from click.testing import CliRunner

from uta.cli import main
from uta.tasks.manager import TaskManager


def _write_python_repo(repo: Path) -> None:
    (repo / "jobs").mkdir(parents=True)
    (repo / "jobs" / "forecast.py").write_text(
        "def forecast_for_store(sales):\n"
        "    return sum(sales)\n"
    )
    (repo / "requirements.txt").write_text("pytest\n")


def test_tasks_create_accepts_python_symbol_targets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
    db_path = tmp_path / "tasks.db"
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "tasks",
            "create",
            "--repo",
            str(repo),
            "--language",
            "python",
            "--target",
            "jobs/forecast.py::forecast_for_store",
            "--task-db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    task = TaskManager(db_path).get_task(1)
    selection = json.loads(task["selection_json"])
    rows = TaskManager(db_path).list_class_tasks(1)
    assert task["language"] == "python"
    assert selection["language"] == "python"
    assert selection["targets"][0]["target_id"] == "pysymbol:jobs/forecast.py::forecast_for_store"
    assert rows[0]["language"] == "python"
    assert rows[0]["source_path"] == "jobs/forecast.py"
    assert rows[0]["symbol"] == "forecast_for_store"


def test_tasks_create_auto_detects_python_from_target(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
    db_path = tmp_path / "tasks.db"
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "tasks",
            "create",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--task-db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    row = TaskManager(db_path).list_class_tasks(1)[0]
    assert row["language"] == "python"
    assert row["target_id"] == "pyfile:jobs/forecast.py"


def test_tasks_create_keeps_java_class_fqn_compatibility(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "tasks.db"
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "tasks",
            "create",
            "--repo",
            str(repo),
            "--class-fqn",
            "pkg.A",
            "--task-db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    task = TaskManager(db_path).get_task(1)
    row = TaskManager(db_path).list_class_tasks(1)[0]
    assert task["language"] == "java"
    assert row["class_fqn"] == "pkg.A"


def test_tasks_manifest_accepts_python_targets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
    db_path = tmp_path / "tasks.db"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "repo": str(repo),
                        "language": "python",
                        "targets": [{"sourcePath": "jobs/forecast.py", "symbol": "forecast_for_store"}],
                    }
                ]
            }
        )
    )
    runner = CliRunner()

    result = runner.invoke(main, ["tasks", "create-manifest", "--manifest", str(manifest), "--task-db", str(db_path)])

    assert result.exit_code == 0, result.output
    row = TaskManager(db_path).list_class_tasks(1)[0]
    assert row["language"] == "python"
    assert row["target_id"] == "pysymbol:jobs/forecast.py::forecast_for_store"


def test_reprioritize_target_alias_updates_python_target(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
    db_path = tmp_path / "tasks.db"
    runner = CliRunner()
    create = runner.invoke(
        main,
        [
            "tasks",
            "create",
            "--repo",
            str(repo),
            "--language",
            "python",
            "--target",
            "jobs/forecast.py",
            "--task-db",
            str(db_path),
        ],
    )
    assert create.exit_code == 0, create.output

    result = runner.invoke(main, ["tasks", "reprioritize-target", "1", "--priority", "7", "--task-db", str(db_path)])

    assert result.exit_code == 0, result.output
    assert TaskManager(db_path).list_class_tasks(1)[0]["priority"] == 7


def test_query_index_python_target_lookup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
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
    assert payload["target"]["target_id"] == "pysymbol:jobs/forecast.py::forecast_for_store"
    assert payload["target"]["source_path"] == "jobs/forecast.py"
    assert payload["languageDecision"]["source"] == "cli"


def test_python_targets_reject_paths_outside_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
    runner = CliRunner()

    for bad_target in ("../outside.py", "jobs/../../outside.py", str(tmp_path / "outside.py")):
        result = runner.invoke(
            main,
            [
                "query-index",
                "--repo",
                str(repo),
                "--language",
                "python",
                "--target",
                bad_target,
                "--json-output",
            ],
        )
        assert result.exit_code != 0
        assert "repo-relative" in result.output


def test_scan_and_parse_accept_python_language(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
    runner = CliRunner()

    scan = runner.invoke(main, ["scan", "--repo", str(repo), "--language", "python", "--all"])
    assert scan.exit_code == 0, scan.output
    assert "jobs/forecast.py" in scan.output

    parse = runner.invoke(main, ["parse", "--repo", str(repo), "--language", "python"])
    assert parse.exit_code == 0, parse.output
    assert "Found 1 Python files" in parse.output


def test_scan_prunes_excluded_python_directories(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / "lib" / "vendored.py").write_text("def ignored():\n    return 1\n")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_forecast_extra.py").write_text("def test_ignored():\n    pass\n")
    runner = CliRunner()

    scan = runner.invoke(main, ["scan", "--repo", str(repo), "--language", "python", "--all"])

    assert scan.exit_code == 0, scan.output
    assert "jobs/forecast.py" in scan.output
    assert "vendored.py" not in scan.output
    assert "test_forecast_extra.py" not in scan.output


def test_scan_python_defaults_to_git_history_ranking(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
    runner = CliRunner()
    calls = {}

    def fake_changed(repo_path, days=30, module=None):
        calls["changed"] = (repo_path, days, module)
        return [("jobs/forecast.py", 3)]

    def fail_all(*args, **kwargs):
        raise AssertionError("--all scanner should not be used")

    monkeypatch.setattr("uta.engine.source_selection.get_changed_python_files", fake_changed)
    monkeypatch.setattr("uta.engine.source_selection.get_all_python_files", fail_all)

    scan = runner.invoke(main, ["scan", "--repo", str(repo), "--language", "python", "--days", "7"])

    assert scan.exit_code == 0, scan.output
    assert calls["changed"] == (str(repo), 7, None)
    assert "jobs/forecast.py" in scan.output
    assert "3" in scan.output


def test_run_help_exposes_language_target_options():
    runner = CliRunner()

    result = runner.invoke(main, ["run", "--help"])

    assert result.exit_code == 0, result.output
    assert "--language" in result.output
    assert "--target" in result.output


def test_enforce_and_python_enforce_dry_run_resolve_python_targets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_repo(repo)
    runner = CliRunner()

    enforce = runner.invoke(
        main,
        [
            "enforce",
            "--repo",
            str(repo),
            "--language",
            "python",
            "--target",
            "jobs/forecast.py::forecast_for_store",
            "--dry-run",
            "--json-output",
        ],
    )
    assert enforce.exit_code == 0, enforce.output
    payload = json.loads(enforce.output)
    assert payload["backend"] == "python_enforcer"
    assert payload["targets"][0]["target_id"] == "pysymbol:jobs/forecast.py::forecast_for_store"

    alias = runner.invoke(main, ["python-enforce", "--repo", str(repo), "--target", "jobs/forecast.py", "--dry-run"])
    assert alias.exit_code == 0, alias.output
    assert "language=python" in alias.output

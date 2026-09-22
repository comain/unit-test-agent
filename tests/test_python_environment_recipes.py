import json
from pathlib import Path
import subprocess

from uta.app.service_composition import build_language_handler_registry
from uta.app.task_daemon import _daemon_child_environment
from uta.language.python.ci import PythonCiLanguageHandler
from uta.language.python.enforcement_runner import PythonEnforcementRunner
from uta.language.python.environment_recipes import PythonEnvironmentRecipeResolver
from uta.language.python.environment_bootstrap import ensure_environment
from uta.shared.config import Settings
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.shared.fix_sessions import CreateFixSessionRequest


def _record(app_name="w_ais_paimian"):
    return CiTaskRecord(
        task_id="ci-python-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            appName=app_name,
            gitUrl="git@example.invalid:md/paimian.git",
            branch="feature/test",
            language="python",
        ),
    )


def _recipes(tmp_path):
    env_dir = tmp_path / "paimian-py311"
    return {
        "w_ais_paimian": {
            "python": "/opt/app/unit_test_agent/.venv311/bin/python",
            "venv": str(env_dir),
            "profile": "paimian-unit-test-py311-v1",
            "ci_mutation_max_selected": 100,
            "packages": [
                "numpy==1.23.5",
                "pytest",
                "coverage",
                "pytest-timeout",
                "python-json-logger",
                "mutmut==3.5.0",
            ],
        }
    }


def test_recipe_resolver_builds_managed_runtime_overrides_for_exact_app(tmp_path):
    resolver = PythonEnvironmentRecipeResolver(_recipes(tmp_path))

    recipe = resolver.resolve("w_ais_paimian")

    assert recipe is not None
    assert recipe.python_bin == str(tmp_path / "paimian-py311" / "bin" / "python")
    assert recipe.mutmut_bin == str(tmp_path / "paimian-py311" / "bin" / "mutmut")
    assert recipe.environment_profile == "paimian-unit-test-py311-v1"
    assert recipe.ci_mutation_max_selected == 100
    assert recipe.setup_command[:3] == (
        "/opt/app/unit_test_agent/.venv311/bin/python",
        "-m",
        "uta.language.python.environment_bootstrap",
    )
    assert resolver.resolve("w_ais_paimian_copy") is None


def test_python_ci_handler_binds_recipe_without_mutating_shared_runner(tmp_path):
    base = PythonEnforcementRunner(command="uta python-enforce")
    handler = PythonCiLanguageHandler(base, PythonEnvironmentRecipeResolver(_recipes(tmp_path)))

    selected = handler.runner_for(_record())

    assert selected is not base
    assert selected.runtime_overrides["python_bin"].endswith("paimian-py311/bin/python")
    assert base.runtime_overrides == {}
    child_env = selected._child_env()
    assert child_env["UTA_PYTHON_BIN"].endswith("paimian-py311/bin/python")
    assert child_env["UTA_PYTHON_ENVIRONMENT_PROFILE"] == "paimian-unit-test-py311-v1"
    assert child_env["UTA_PYTHON_DEPENDENCY_OVERLAY_ENABLED"] == "0"
    assert child_env["UTA_PYTHON_MUTATION_GENERATION_CI_MAX_SELECTED"] == "100"
    assert "environment_bootstrap" in child_env["UTA_PYTHON_SETUP_COMMAND"]

    full_env = selected._child_env(enable_ci_mutation_sampling=False)
    assert "UTA_PYTHON_MUTATION_GENERATION_CI_MAX_SELECTED" not in full_env


def test_python_runner_prepares_managed_environment_before_enforcement(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    recipe = PythonEnvironmentRecipeResolver(_recipes(tmp_path)).resolve("w_ais_paimian")
    runner = PythonEnforcementRunner(
        command="uta python-enforce",
        run_command=run,
        runtime_overrides=recipe.runtime_overrides(),
    )

    assert runner._prepare_managed_runtime(tmp_path, ["uta", "python-enforce"]) is None
    assert calls[0][0] == list(recipe.setup_command)
    assert calls[0][1]["env"]["UTA_PYTHON_DEPENDENCY_OVERLAY_ENABLED"] == "0"


def test_python_repair_task_snapshots_selected_recipe(tmp_path):
    captured = {}

    class FakeTaskManager:
        def create_task_targets(self, **kwargs):
            captured.update(kwargs)
            return 31

    handler = PythonCiLanguageHandler(
        PythonEnforcementRunner(command="uta python-enforce"),
        PythonEnvironmentRecipeResolver(_recipes(tmp_path)),
    )
    task_id = handler.create_repair_task(
        task_manager=FakeTaskManager(),
        record=_record(),
        request=CreateFixSessionRequest(targetIds=["pyfile:main/target.py"]),
        repo_path=tmp_path,
        priority=1,
        base_ref="origin/master",
        coverage_gate=95,
        mutation_gate=95,
        rdc_context={"enforcement": {"evidence": {"changedLines": {"main/target.py": [1]}}}},
        rdc_context_path=None,
    )

    assert task_id == 31
    snapshot = captured["config_snapshot"]
    assert snapshot["python_environment_recipe"]["profile"] == "paimian-unit-test-py311-v1"
    assert snapshot["python_environment_recipe"]["packages"][0] == "numpy==1.23.5"
    assert snapshot["python_environment_recipe"]["ci_mutation_max_selected"] == 100


def test_python_daemon_child_restores_snapshotted_recipe(tmp_path):
    recipe = PythonEnvironmentRecipeResolver(_recipes(tmp_path)).resolve("w_ais_paimian")
    task = {
        "language": "python",
        "repo_slug": "paimian",
        "config_snapshot_json": json.dumps({"python_environment_recipe": recipe.as_dict()}),
    }

    env = _daemon_child_environment(task, environ={"PATH": "/usr/bin", "KEEP": "yes"})

    assert env["UTA_PYTHON_BIN"].endswith("paimian-py311/bin/python")
    assert env["UTA_PYTHON_MUTMUT_BIN"].endswith("paimian-py311/bin/mutmut")
    assert env["UTA_PYTHON_ENVIRONMENT_PROFILE"] == "paimian-unit-test-py311-v1"
    assert "UTA_PYTHON_MUTATION_GENERATION_CI_MAX_SELECTED" not in env
    assert env["KEEP"] == "yes"


def test_python_daemon_child_without_recipe_keeps_environment_unchanged():
    inherited = {"PATH": "/usr/bin", "KEEP": "yes"}

    assert _daemon_child_environment(
        {"language": "python", "repo_slug": "ordinary-python"},
        environ=inherited,
    ) == inherited


def test_settings_parse_application_recipe_json(monkeypatch, tmp_path):
    monkeypatch.setenv("UTA_PYTHON_ENVIRONMENT_RECIPES", json.dumps(_recipes(tmp_path)))

    configured = Settings(_env_file=None).python_environment_recipes

    assert configured["w_ais_paimian"]["packages"][0] == "numpy==1.23.5"


def test_composition_wires_application_recipe_resolver(tmp_path):
    config = Settings(_env_file=None, python_environment_recipes=_recipes(tmp_path))

    registry = build_language_handler_registry(config=config)
    python = next(handler for handler in registry.handlers if handler.language == "python")

    assert python.runtime_resolver is not None
    assert python.runner_for(_record()).runtime_overrides["environment_profile"] == (
        "paimian-unit-test-py311-v1"
    )


def test_environment_bootstrap_reuses_matching_cached_environment(monkeypatch, tmp_path):
    calls = []
    venv = tmp_path / "managed"

    def run(command, check):
        calls.append(command)
        if command[1:3] == ["-m", "venv"]:
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "python").touch()
            (venv / "bin" / "mutmut").touch()

    monkeypatch.setattr("subprocess.run", run)

    kwargs = {
        "venv": Path(venv),
        "python": "/service/python",
        "packages": ("numpy==1.23.5", "pytest", "mutmut==3.5.0"),
    }
    ensure_environment(**kwargs)
    ensure_environment(**kwargs)

    assert len(calls) == 2
    assert calls[0] == ["/service/python", "-m", "venv", "--clear", str(venv)]
    assert calls[1][:5] == [str(venv / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check"]

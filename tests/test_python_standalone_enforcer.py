import json
import os
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "tools" / "python-enforcement"
sys.path.insert(0, str(PACKAGE_ROOT))

from uta_enforce_core.diff import is_production_python_path, parse_added_diff_lines  # noqa: E402
from uta_enforce_core.evidence import EVIDENCE_PREFIX, finalize, format_evidence_markers  # noqa: E402
from uta_enforce_core.targets import target_payload, target_source_path  # noqa: E402
from uta_enforce_core.commands import SafeProcessRunner  # noqa: E402
from uta_enforce_core.contracts import (  # noqa: E402
    EnforcementInvocationContext,
    EnforcementRequest,
    EnforcementStatus,
    EnforcementTarget,
    RuntimeSelection,
)
from uta_py_enforce import mutation as lightweight_mutation  # noqa: E402
from uta_py_enforce import api as lightweight_api  # noqa: E402
from uta_enforce_core import evidence as core_evidence  # noqa: E402
from uta_py_enforce.mutation_workspace import mutation_support_copy_paths  # noqa: E402


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def test_lightweight_core_diff_and_target_contracts():
    diff = "\n".join(
        [
            "@@ -1 +1,3 @@",
            "+def added():",
            "+    return 1",
            " context",
            "-old",
            "+new",
        ]
    )

    assert parse_added_diff_lines(diff) == {1, 2, 4}
    assert is_production_python_path("pkg/service.py") is True
    assert is_production_python_path("tests/test_service.py") is False
    assert target_source_path("pkg/service.py::Service.run") == "pkg/service.py"
    assert target_payload("pkg/service.py")["target_id"] == "pyfile:pkg/service.py"


def test_documentation_scripts_are_not_production_targets():
    from uta.language.python.enforcement import _is_production_python_path

    for selector in (is_production_python_path, _is_production_python_path):
        for path in ("docs/.create_porridge_merge_prd.py", "doc/build.py",
                     "package/docs/conf.py", "package\\doc\\build.py"):
            assert not selector(path), path
        assert selector("package/documents.py")
        assert selector("package/docs_service.py")


def test_lightweight_core_evidence_marker_contract():
    evidence = finalize(
        {
            "evidenceId": "",
            "status": "passed",
            "reasonCode": "passed",
            "coverage": {"rate": 100.0, "passed": True, "covered": 2, "total": 2},
            "mutation": {"rate": 100.0, "passed": True, "survived": 0, "changedLineMutantsGenerated": 1},
        }
    )

    marker = format_evidence_markers(evidence)

    assert evidence["evidenceId"].startswith("uta-python-enforcement-")
    assert EVIDENCE_PREFIX in marker
    assert "python diff line coverage 100.00% passed (2/2)" in marker


def test_lightweight_evidence_marker_reports_unexecuted_mutants_as_tool_failure():
    marker = format_evidence_markers(
        {
            "status": "failed",
            "reasonCode": "mutation_backend_failed",
            "mutation": {
                "passed": False,
                "reasonCode": "mutation_backend_failed",
                "notChecked": 65,
                "changedLineMutantsGenerated": 69,
            },
        }
    )

    assert "python diff mutation unavailable" in marker
    assert "65 selected mutants were not executed" in marker
    assert "python diff mutation score" not in marker


def test_lightweight_evidence_schema_fixture_matches_cli_output_contract():
    schema_path = Path(__file__).resolve().parent / "fixtures" / "python_enforcement_evidence_schema_v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    evidence = {
        "schemaVersion": 1,
        "language": "python",
        "backend": "python_enforcer",
        "headCommit": "abc123",
        "coverage": {"passed": True},
        "mutation": {
            "passed": True,
            "candidatePlan": {
                "activeSelected": [],
                "eligibleMutationOpportunities": [],
                "exactToolCandidateKeys": [],
                "reportFullSelected": [],
                "samplingLayer": {"enabled": False},
            },
        },
        "setup": {"tool": "lightweight_tool"},
        "enforcementCoreVersion": "lightweight-1.0.0",
    }

    for field in schema["requiredTopLevelFields"]:
        assert field in evidence
    for field in schema["requiredMutationFields"]:
        assert field in evidence["mutation"]
    for field in schema["requiredCandidatePlanFields"]:
        assert field in evidence["mutation"]["candidatePlan"]
    assert evidence["mutation"]["candidatePlan"]["samplingLayer"]["enabled"] == schema["localEvidenceRules"]["samplingLayerEnabled"]


def test_lightweight_candidate_plan_aggregate_preserves_generation_policy():
    aggregate = core_evidence.aggregate_candidate_plan(
        [
            {
                "filterMechanism": "mutmut3_metadata_selected_execution",
                "adapterFilteredGenerationApplied": True,
                "activeSelected": [{"toolCandidateKey": "pkg.a.x_run__mutmut_1"}],
                "reportFullSelected": [{"toolCandidateKey": "pkg.a.x_run__mutmut_1"}],
                "exactToolCandidateKeys": ["pkg.a.x_run__mutmut_1"],
                "eligibleMutationOpportunities": [{"opportunityId": "a:2:return_value"}],
                "suppressed": [{"reasonCode": "logging_only"}],
                "generationPolicy": {
                    "changedLineCount": 3,
                    "eligibleOpportunities": 2,
                    "selectedOpportunitiesBeforeCap": 1,
                    "selectedOpportunities": 1,
                    "omittedByOnePerLine": 1,
                    "omittedByHardCap": 0,
                    "suppressedOpportunities": 1,
                    "truncated": False,
                },
            }
        ]
    )

    assert aggregate["filterMechanism"] == "mutmut3_metadata_selected_execution"
    assert aggregate["filterMechanisms"] == ["mutmut3_metadata_selected_execution"]
    assert aggregate["adapterFilteredGenerationApplied"] is True
    assert aggregate["suppressionByReason"] == {"logging_only": 1}
    assert aggregate["generationPolicy"] == {
        "changedLineCount": 3,
        "eligibleOpportunities": 2,
        "selectedOpportunitiesBeforeCap": 1,
        "selectedOpportunities": 1,
        "omittedByOnePerLine": 1,
        "omittedByHardCap": 0,
        "suppressedOpportunities": 1,
        "truncated": False,
    }


def test_lightweight_mutmut_config_focuses_selected_test_and_copies_src_dependencies(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "src" / "agent_core" / "git" / "retry.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def retryable(error):\n"
        "    return bool(error)\n",
        encoding="utf-8",
    )
    dependency = source.parent / "workspace.py"
    dependency.write_text("class GitTimeout(Exception):\n    pass\n", encoding="utf-8")
    (repo / "src" / "agent_core" / "__init__.py").write_text("", encoding="utf-8")
    (source.parent / "__init__.py").write_text("from .workspace import GitTimeout\n", encoding="utf-8")
    test_file = repo / "tests" / "test_git_retry.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "from agent_core.git.retry import retryable\n"
        "\n"
        "def test_retryable():\n"
        "    assert retryable(object())\n",
        encoding="utf-8",
    )

    lightweight_mutation.write_mutmut_setup(
        repo,
        "src/agent_core/git/retry.py",
        "tests/test_git_retry.py",
        python_bin=sys.executable,
    )

    config = (repo / "setup.cfg").read_text(encoding="utf-8")
    assert "tests_dir=tests/test_git_retry.py" in config
    assert "pytest_add_cli_args_test_selection" not in config
    assert "pytest_add_cli_args=" not in config
    assert "tests/test_git_retry.py" in next(line for line in config.splitlines() if line.startswith("runner="))
    assert "src/agent_core/git/workspace.py" in config
    assert "src/agent_core/git/retry.py" not in next(
        section for section in config.split("also_copy=", 1)[1].split("\nrunner=", 1)
    )


def test_lightweight_mutmut_config_uses_existing_pyproject_configuration_source(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pipecat" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    test_file = repo / "tests" / "test_app.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_run():\n    assert True\n", encoding="utf-8")
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        '[tool.pytest.ini_options]\npythonpath = ["pipecat"]\n', encoding="utf-8"
    )

    lightweight_mutation.write_mutmut_setup(
        repo,
        "pipecat/app.py",
        "tests/test_app.py",
        python_bin=sys.executable,
    )

    config = pyproject.read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" in config
    assert "[tool.mutmut]" in config
    assert 'paths_to_mutate = ["pipecat/app.py"]' in config
    assert 'tests_dir = ["tests/test_app.py"]' in config
    assert "pytest_add_cli_args_test_selection" not in config
    assert not (repo / "setup.cfg").exists()


def test_lightweight_mutmut_workspace_copies_repo_relative_files_read_by_test(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "uta" / "testgen" / "agent_runtime.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run():\n    return True\n", encoding="utf-8")
    support = repo / "uta" / "testgen" / "graph" / "nodes.py"
    support.parent.mkdir(parents=True)
    support.write_text("NODES = {}\n", encoding="utf-8")
    test_file = repo / "tests" / "test_agent_runtime.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "from pathlib import Path\n\n"
        "def test_registration_surface():\n"
        "    assert Path('uta/testgen/graph/nodes.py').read_text()\n",
        encoding="utf-8",
    )

    support_paths = mutation_support_copy_paths(
        repo,
        "uta/testgen/agent_runtime.py",
        ("tests/test_agent_runtime.py",),
    )

    assert "uta/testgen/graph/nodes.py" in support_paths
    assert "uta/testgen/graph" not in support_paths
    assert "uta/testgen/agent_runtime.py" not in support_paths


def test_mutmut_support_does_not_treat_python_directory_literal_as_workspace_data(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "app" / "target.py"
    source.parent.mkdir(parents=True)
    source.write_text("SUPPORT_ROOT = 'support/'\n", encoding="utf-8")
    support = repo / "support" / "large_module.py"
    support.parent.mkdir()
    support.write_text("VALUE = 1\n", encoding="utf-8")

    support_paths = mutation_support_copy_paths(repo, "app/target.py")

    assert "support" not in support_paths


def test_mutmut_support_copies_namespace_sibling_imported_via_package_root(tmp_path):
    source = tmp_path / "pipecat/wechatlet_ai/wechat_reply_orchestrator.py"
    source.parent.mkdir(parents=True)
    (tmp_path / "pipecat/__init__.py").write_text("")
    source.write_text("from wechatlet_ai.session_id import build_session_id\n")
    (source.parent / "session_id.py").write_text(
        "def build_session_id():\n    return 'session'\n"
    )

    paths = mutation_support_copy_paths(
        tmp_path, "pipecat/wechatlet_ai/wechat_reply_orchestrator.py"
    )

    assert "pipecat/wechatlet_ai/session_id.py" in paths
    assert "pipecat/__init__.py" in paths
    # Copy support files only; never overwrite the generated mutant target.
    assert "pipecat" not in paths
    assert "pipecat/wechatlet_ai" not in paths
    assert "pipecat/wechatlet_ai/wechat_reply_orchestrator.py" not in paths


def test_mutmut_support_copies_non_python_resource_root_referenced_by_selected_test(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "chat_robot" / "views.py"
    test_file = repo / "tests" / "uta_generated" / "test_chat_robot_views.py"
    template = repo / "templates" / "manage_task_api_key.html"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    template.parent.mkdir(parents=True)
    source.write_text("def render_page():\n    return 'manage_task_api_key.html'\n", encoding="utf-8")
    test_file.write_text(
        "from pathlib import Path\n"
        "\n"
        "def test_template_contract():\n"
        "    template = (Path.cwd() / 'templates' / 'manage_task_api_key.html').read_text()\n"
        "    assert 'mapping.app_binding_count' in template\n",
        encoding="utf-8",
    )
    template.write_text("{{ mapping.app_binding_count }}\n", encoding="utf-8")

    support_paths = mutation_support_copy_paths(
        repo,
        "chat_robot/views.py",
        ("tests/uta_generated/test_chat_robot_views.py",),
    )

    assert "templates" in support_paths


def test_mutmut_support_copies_static_resource_referenced_by_target_source(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "src" / "web_pages.py"
    resource = repo / "src" / "static" / "settings.html"
    test_file = repo / "tests" / "test_web_pages.py"
    source.parent.mkdir(parents=True)
    resource.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_text(
        "from pathlib import Path\n\n"
        "def settings_page():\n"
        "    return (Path(__file__).parent / 'static' / 'settings.html').read_text()\n",
        encoding="utf-8",
    )
    resource.write_text("settings", encoding="utf-8")
    test_file.write_text(
        "from pathlib import Path\n"
        "from src.web_pages import settings_page\n\n"
        "ROOT = Path(__file__).resolve().parents[2]\n\n"
        "def test_settings_asset():\n"
        "    assert (ROOT / 'src' / 'static' / 'settings.html').read_text() == 'settings'\n",
        encoding="utf-8",
    )

    support_paths = mutation_support_copy_paths(
        repo,
        "src/web_pages.py",
        ("tests/test_web_pages.py",),
    )

    assert "src/static" in support_paths


def test_mutmut_support_copies_file_relative_config_read_via_fstring(tmp_path):
    """Production 1a298d80 aborted 62 mutants as mutation_backend_failed.

    ``async_task_process_service.py`` loads ``visual_padded/config/config.cfg``
    through ``f'{FILE_DIR}/../config/config.cfg'`` after
    ``FILE_DIR = os.path.dirname(os.path.abspath(__file__))``. Coverage tests
    passed in the real workspace; the focused mutmut tree omitted that file, so
    import-time ``configparser.NoSectionError: No section: 'env'`` stopped the
    clean probe.
    """
    repo = tmp_path / "repo"
    source = repo / "visual_padded" / "service" / "async_task_process_service.py"
    config = repo / "visual_padded" / "config" / "config.cfg"
    test_file = repo / "visual_padded" / "tests" / "test_async_task_process_service.py"
    source.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_text(
        "import os\n"
        "import configparser\n"
        "FILE_DIR = os.path.dirname(os.path.abspath(__file__))\n"
        "cfg = configparser.ConfigParser()\n"
        "cfg.read(f'{FILE_DIR}/../config/config.cfg')\n"
        "env = cfg.get('env', 'env_name')\n",
        encoding="utf-8",
    )
    config.write_text("[env]\nenv_name = beta\n", encoding="utf-8")
    test_file.write_text(
        "def test_env():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    support_paths = mutation_support_copy_paths(
        repo,
        "visual_padded/service/async_task_process_service.py",
        ("visual_padded/tests/test_async_task_process_service.py",),
    )

    assert "visual_padded/config/config.cfg" in support_paths


def test_mutmut_support_copies_file_relative_config_read_via_join(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "visual_padded" / "service" / "service.py"
    config = repo / "visual_padded" / "config" / "config.cfg"
    source.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    source.write_text(
        "import os\n"
        "FILE_DIR = os.path.dirname(__file__)\n"
        "os.path.join(FILE_DIR, '..', 'config', 'config.cfg')\n",
        encoding="utf-8",
    )
    config.write_text("[env]\nenv_name = beta\n", encoding="utf-8")

    support_paths = mutation_support_copy_paths(repo, "visual_padded/service/service.py")

    assert "visual_padded/config/config.cfg" in support_paths


def test_lightweight_mutation_reports_test_collection_failure(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "src" / "agent_core" / "git" / "retry.py"
    source.parent.mkdir(parents=True)
    source.write_text("def retryable(error):\n    return bool(error)\n", encoding="utf-8")

    def fake_run(name, command, cwd, timeout, commands, env=None):
        if name == "mutmut_generate_metadata":
            meta = repo / "mutants" / "src" / "agent_core" / "git" / "retry.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps({"exit_code_by_key": {"x_retryable__mutmut_1": 35}}),
                encoding="utf-8",
            )
            meta.with_suffix(meta.suffix + ".uta.json").write_text(
                json.dumps({"line_by_key": {"x_retryable__mutmut_1": 2}}),
                encoding="utf-8",
            )
            payload = {"name": name, "command": list(command), "exitCode": 0, "stdout": "", "stderr": ""}
        elif name == "mutmut_run_selected":
            payload = {"name": name, "command": list(command), "exitCode": 1, "stdout": "mutation progress", "stderr": ""}
        else:
            assert name == "mutmut_collection_probe"
            payload = {
                "name": name,
                "command": list(command),
                "exitCode": 2,
                "stdout": "",
                "stderr": (
                    "ERROR collecting tests/test_git_retry.py\n"
                    "ModuleNotFoundError: No module named 'agent_core.git.workspace'\n"
                    "Interrupted: 1 error during collection\n"
                ),
            }
        commands.append(payload)
        return payload

    monkeypatch.setattr(lightweight_mutation, "run_command", fake_run)
    commands = []
    result = lightweight_mutation.run_mutation(
        repo,
        "src/agent_core/git/retry.py",
        "tests/test_git_retry.py",
        {"src/agent_core/git/retry.py": [2]},
        95.0,
        "mutmut",
        60,
        commands,
        python_bin=sys.executable,
        syntax_version="python3",
    )

    assert result["passed"] is False
    assert result["reasonCode"] == "mutation_test_collection_failed"
    assert result["suspicious"] == 1
    assert [command["name"] for command in commands] == [
        "mutmut_generate_metadata",
        "mutmut_run_selected",
        "mutmut_collection_probe",
    ]


def test_lightweight_mutation_reports_unchecked_mutants_as_backend_failure(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run(value):\n    return value + 1\n", encoding="utf-8")
    test_file = repo / "tests" / "test_service.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_run():\n    assert True\n", encoding="utf-8")

    def fake_run(name, command, cwd, timeout, commands, env=None):
        if name == "mutmut_generate_metadata":
            meta = repo / "mutants" / "pkg" / "service.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps({"exit_code_by_key": {"pkg.service.x_run__mutmut_1": None}}),
                encoding="utf-8",
            )
            meta.with_suffix(meta.suffix + ".uta.json").write_text(
                json.dumps({"line_by_key": {"pkg.service.x_run__mutmut_1": 2}}),
                encoding="utf-8",
            )
            payload = {"name": name, "command": list(command), "exitCode": 0, "stdout": "", "stderr": ""}
        elif name == "mutmut_run_selected":
            payload = {
                "name": name,
                "command": list(command),
                "exitCode": 1,
                "stdout": "failed to collect stats. runner returned 1",
                "stderr": "FileNotFoundError: uta/testgen/graph/nodes.py",
            }
        else:
            assert name == "mutmut_clean_test_probe"
            assert env["MUTANT_UNDER_TEST"] == ""
            payload = {
                "name": name,
                "command": list(command),
                "exitCode": 1,
                "stdout": "",
                "stderr": "FileNotFoundError: uta/testgen/graph/nodes.py",
            }
        commands.append(payload)
        return payload

    monkeypatch.setattr(lightweight_mutation, "run_command", fake_run)
    commands = []
    result = lightweight_mutation.run_mutation(
        repo,
        "pkg/service.py",
        "tests/test_service.py",
        {"pkg/service.py": [2]},
        95.0,
        "mutmut",
        60,
        commands,
        python_bin=sys.executable,
        syntax_version="python3",
    )

    assert result["passed"] is False
    assert result["reasonCode"] == "mutation_backend_failed"
    assert result["failureStage"] == "mutation_execution"
    assert result["notChecked"] == 1
    assert result["suspicious"] == 0
    assert result["changedLineMutantsScored"] == 0
    assert result["rate"] == 0.0
    assert [command["name"] for command in commands] == [
        "mutmut_generate_metadata",
        "mutmut_run_selected",
        "mutmut_clean_test_probe",
    ]


def test_lightweight_mutation_puts_import_compat_on_mutmut_pythonpath(tmp_path, monkeypatch):
    """Production 581d973b scored 62 mutants as noTests.

    ``test_async_task_process_service.py`` loads the target with
    ``spec_from_file_location("process_service_under_test", SERVICE_PATH)``.
    The full UTA verifier remaps that to the canonical module via
    import_compat sitecustomize; the lightweight CI lane did not, so mutmut
    never associated tests with trampolines.
    """
    repo = tmp_path / "repo"
    source = repo / "visual_padded" / "service" / "async_task_process_service.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run(value):\n    return value + 1\n", encoding="utf-8")
    test_file = repo / "visual_padded" / "tests" / "test_async_task_process_service.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "import importlib.util\n"
        "from pathlib import Path\n"
        "SERVICE_PATH = Path(__file__).resolve().parents[1] / 'service' / 'async_task_process_service.py'\n"
        "def test_run():\n"
        "    spec = importlib.util.spec_from_file_location('process_service_under_test', SERVICE_PATH)\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    spec.loader.exec_module(module)\n"
        "    assert module.run(1) == 2\n",
        encoding="utf-8",
    )
    captured_envs = []

    def fake_run(name, command, cwd, timeout, commands, env=None):
        captured_envs.append((name, dict(env or {})))
        if name == "mutmut_generate_metadata":
            meta = repo / "mutants" / "visual_padded" / "service" / "async_task_process_service.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps({"exit_code_by_key": {"visual_padded.service.async_task_process_service.x_run__mutmut_1": 1}}),
                encoding="utf-8",
            )
            meta.with_suffix(meta.suffix + ".uta.json").write_text(
                json.dumps({"line_by_key": {"visual_padded.service.async_task_process_service.x_run__mutmut_1": 2}}),
                encoding="utf-8",
            )
        payload = {"name": name, "command": list(command), "exitCode": 0, "stdout": "", "stderr": ""}
        commands.append(payload)
        return payload

    monkeypatch.setattr(lightweight_mutation, "run_command", fake_run)
    commands = []
    lightweight_mutation.run_mutation(
        repo,
        "visual_padded/service/async_task_process_service.py",
        "visual_padded/tests/test_async_task_process_service.py",
        {"visual_padded/service/async_task_process_service.py": [2]},
        95.0,
        "mutmut",
        60,
        commands,
        python_bin=sys.executable,
        syntax_version="python3",
    )

    sitecustomize = (
        repo / ".uta_cache" / "python-enforcement" / "mutation" / "import_compat" / "sitecustomize.py"
    )
    assert sitecustomize.is_file()
    metadata_env = next(env for name, env in captured_envs if name == "mutmut_generate_metadata")
    run_env = next(env for name, env in captured_envs if name == "mutmut_run_selected")
    compat_dir = str(sitecustomize.parent)
    for env in (metadata_env, run_env):
        assert env["UTA_MUTMUT_TARGET_REL"] == "visual_padded/service/async_task_process_service.py"
        assert env["UTA_MUTMUT_CANONICAL_MODULE"] == "visual_padded.service.async_task_process_service"
        assert env["PYTHONPATH"].split(os.pathsep)[0] == compat_dir


def test_lightweight_mutation_does_not_pass_when_all_mutants_have_no_tests(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run(value):\n    return value + 1\n", encoding="utf-8")
    test_file = repo / "tests" / "test_service.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_run():\n    assert True\n", encoding="utf-8")

    def fake_run(name, command, cwd, timeout, commands, env=None):
        if name == "mutmut_generate_metadata":
            meta = repo / "mutants" / "pkg" / "service.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps({"exit_code_by_key": {"pkg.service.x_run__mutmut_1": 33}}),
                encoding="utf-8",
            )
            meta.with_suffix(meta.suffix + ".uta.json").write_text(
                json.dumps({"line_by_key": {"pkg.service.x_run__mutmut_1": 2}}),
                encoding="utf-8",
            )
        payload = {"name": name, "command": list(command), "exitCode": 0, "stdout": "", "stderr": ""}
        commands.append(payload)
        return payload

    monkeypatch.setattr(lightweight_mutation, "run_command", fake_run)
    commands = []
    result = lightweight_mutation.run_mutation(
        repo,
        "pkg/service.py",
        "tests/test_service.py",
        {"pkg/service.py": [2]},
        95.0,
        "mutmut",
        60,
        commands,
        python_bin=sys.executable,
        syntax_version="python3",
    )

    assert result["noTests"] == 1
    assert result["generated"] == 1
    assert result["changedLineMutantsScored"] == 0
    assert result["passed"] is False
    assert result["reasonCode"] == "mutation_no_tests"
    assert result["rate"] == 0.0


def test_lightweight_mutation_does_not_execute_after_metadata_generation_failure(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run(value):\n    return value + 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_service.py").write_text("def test_run():\n    assert True\n", encoding="utf-8")

    def fake_run(name, command, cwd, timeout, commands, env=None):
        assert name == "mutmut_generate_metadata"
        payload = {
            "name": name,
            "command": list(command),
            "exitCode": 1,
            "stdout": "",
            "stderr": "failed to prepare mutation workspace",
        }
        commands.append(payload)
        return payload

    monkeypatch.setattr(lightweight_mutation, "run_command", fake_run)
    commands = []
    result = lightweight_mutation.run_mutation(
        repo,
        "pkg/service.py",
        "tests/test_service.py",
        {"pkg/service.py": [2]},
        95.0,
        "mutmut",
        60,
        commands,
        python_bin=sys.executable,
        syntax_version="python3",
    )

    assert result["reasonCode"] == "mutation_backend_failed"
    assert result["failureStage"] == "metadata_generation"
    assert [command["name"] for command in commands] == ["mutmut_generate_metadata"]


def test_lightweight_python3_mutation_filters_generation_to_changed_lines(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def calculate(value):\n"
        "    adjusted = value + 1\n"
        "    return adjusted > 0\n",
        encoding="utf-8",
    )
    test_file = repo / "tests" / "test_service.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "from pkg.service import calculate\n\n"
        "def test_calculate():\n"
        "    assert calculate(1) is True\n",
        encoding="utf-8",
    )

    observed_policy = {}

    def fake_run(name, command, cwd, timeout, commands, env=None):
        payload = {"name": name, "command": list(command), "exitCode": 0, "stdout": "", "stderr": ""}
        commands.append(payload)
        if name == "mutmut_generate_metadata":
            policy_path = Path(command[-1])
            observed_policy.update(json.loads(policy_path.read_text(encoding="utf-8")))
            meta = repo / "mutants" / "pkg" / "service.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps({"exit_code_by_key": {"pkg.service.x_calculate__mutmut_1": 1}}),
                encoding="utf-8",
            )
            meta.with_suffix(meta.suffix + ".uta.json").write_text(
                json.dumps({"line_by_key": {"pkg.service.x_calculate__mutmut_1": 3}}),
                encoding="utf-8",
            )
        return payload

    monkeypatch.setattr(lightweight_mutation, "run_command", fake_run)
    commands = []
    result = lightweight_mutation.run_mutation(
        repo,
        "pkg/service.py",
        "tests/test_service.py",
        {"pkg/service.py": [3]},
        95.0,
        "mutmut",
        60,
        commands,
        python_bin=sys.executable,
        syntax_version="python3",
    )

    assert [command["name"] for command in commands] == ["mutmut_generate_metadata", "mutmut_run_selected"]
    # Line 2 is executable but unchanged. The adapter policy sent to mutmut is
    # the generation boundary, so it must never reach metadata or execution.
    assert observed_policy["selectedLines"] == [3]
    assert observed_policy["changedLines"] == [3]
    assert {item["line"] for item in observed_policy["selectedBeforeCap"]} == {3}
    assert {item["line"] for item in observed_policy["selected"]} == {3}
    assert result["generated"] == 1
    assert result["changedLineMutantsGenerated"] == 1
    assert result["candidatePlan"]["filterMechanism"] == "mutmut3_metadata_selected_execution"
    assert result["candidatePlan"]["runMutants"] == 1
    assert result["candidatePlan"]["scoredMutants"] == 1
    assert result["candidatePlan"]["killed"] == 1


def test_lightweight_python3_mutation_suppresses_changed_module_constants(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "settings.py"
    source.parent.mkdir(parents=True)
    source.write_text("DEFAULT_TIMEOUT = 30\n", encoding="utf-8")
    test_file = repo / "tests" / "test_settings.py"
    test_file.parent.mkdir()
    test_file.write_text("from pkg.settings import DEFAULT_TIMEOUT\n", encoding="utf-8")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("suppressed constants must not start mutmut")

    monkeypatch.setattr(lightweight_mutation, "run_command", unexpected_run)
    result = lightweight_mutation.run_mutation(
        repo,
        "pkg/settings.py",
        "tests/test_settings.py",
        {"pkg/settings.py": [1]},
        95.0,
        "mutmut",
        60,
        [],
        python_bin=sys.executable,
        syntax_version="python3",
    )

    assert result["passed"] is True
    assert result["generated"] == 0
    assert result["candidatePlan"]["suppressionByReason"] == {"pure_config_constant": 1}



def _binding_target_result(repo, source_path, test_path):
    """One target's evidence, from the binding that now owns per-target runs.

    These assertions used to poke `cli.verify_target`. The CLI no longer
    verifies anything -- it builds a request and dispatches -- so the same
    behaviour is exercised where it moved, through the public binding.
    """
    from uta_enforce_core.commands import SafeProcessRunner
    from uta_enforce_core.contracts import (
        EnforcementInvocationContext,
        EnforcementRequest,
        EnforcementTarget,
        QualityGates,
        RuntimeSelection,
    )

    # The binding checks the target and its test exist before it runs anything,
    # which `cli.verify_target` did not. Create them so the assertions reach the
    # mutation stage they are about rather than stopping at missing_test_file.
    source = repo / source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def run(value):\n    return value * 2\n", encoding="utf-8")
    test_file = repo / test_path
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_run():\n    assert True\n", encoding="utf-8")

    binding = lightweight_api.create_python_enforcement_binding()
    request = EnforcementRequest(
        repo_path=repo,
        language="python",
        targets=(
            EnforcementTarget(
                language="python",
                target_id=source_path,
                source_path=source_path,
                test_paths=(test_path,),
            ),
        ),
        test_paths=(test_path,),
        quality_gates=QualityGates(diff_coverage_min=0.95, diff_mutation_min=0.95),
        runtime=RuntimeSelection(syntax_version="python3", timeout_seconds=60),
    )
    result = binding.enforce(
        request, EnforcementInvocationContext(run_command=SafeProcessRunner())
    )
    return (result.evidence.get("targets") or [{}])[0]


def test_lightweight_target_keeps_collection_failure_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lightweight_api,
        "run_coverage",
        lambda *args, **kwargs: {"passed": True, "rate": 100.0},
    )

    def collection_failure(*args, **kwargs):
        commands = args[7]
        commands.append({"name": "mutmut_collection_probe", "exitCode": 2})
        return {
            "passed": False,
            "rate": 0.0,
            "reasonCode": "mutation_test_collection_failed",
        }

    monkeypatch.setattr(lightweight_api, "run_mutation", collection_failure)

    result = _binding_target_result(
        tmp_path, "src/agent_core/git/retry.py", "tests/test_git_retry.py"
    )

    assert result["reasonCode"] == "mutation_test_collection_failed"
    assert result["message"] == "Python mutation test collection failed in the isolated mutmut workspace"
    assert result["commands"] == [{"name": "mutmut_collection_probe", "exitCode": 2}]


def test_lightweight_binding_propagates_dependency_overlay_to_both_gates(tmp_path, monkeypatch):
    dependency_dir = tmp_path / ".uta_cache" / "python-enforcement" / "dependencies" / "deps"
    dependency_dir.mkdir(parents=True)
    seen = {}

    monkeypatch.setattr(
        lightweight_api,
        "prepare_dependency_overlay",
        lambda *args, **kwargs: (
            {"name": "dependency_overlay_cached", "command": [], "exitCode": 0},
            dependency_dir,
            "digest",
        ),
    )

    def coverage(*args, **kwargs):
        seen["coverage"] = kwargs.get("execution_env")
        return {"passed": True, "rate": 100.0, "testsPass": True}

    def mutation(*args, **kwargs):
        seen["mutation"] = kwargs.get("execution_env")
        return {"passed": True, "rate": 100.0}

    monkeypatch.setattr(lightweight_api, "run_coverage", coverage)
    monkeypatch.setattr(lightweight_api, "run_mutation", mutation)

    _binding_target_result(
        tmp_path, "src/agent_core/git/retry.py", "tests/test_git_retry.py"
    )

    for gate in ("coverage", "mutation"):
        pythonpath = str((seen[gate] or {}).get("PYTHONPATH") or "").split(os.pathsep)
        assert dependency_dir.as_posix() in pythonpath


def test_lightweight_binding_skips_overlay_for_caller_managed_runtime(tmp_path, monkeypatch):
    def unexpected_overlay(*args, **kwargs):
        raise AssertionError("managed runtime must not install the repository manifest")

    seen = {}
    monkeypatch.setattr(lightweight_api, "prepare_dependency_overlay", unexpected_overlay)

    def coverage(*args, **kwargs):
        seen["python_bin"] = args[5]
        seen["execution_env"] = kwargs.get("execution_env")
        return {"passed": True, "rate": 100.0, "testsPass": True}

    monkeypatch.setattr(lightweight_api, "run_coverage", coverage)
    monkeypatch.setattr(
        lightweight_api,
        "run_mutation",
        lambda *args, **kwargs: {"passed": True, "rate": 100.0},
    )

    source_path = "src/agent_core/git/retry.py"
    test_path = "tests/test_git_retry.py"
    source = tmp_path / source_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def run(value):\n    return value * 2\n", encoding="utf-8")
    test_file = tmp_path / test_path
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_run():\n    assert True\n", encoding="utf-8")

    request = EnforcementRequest(
        repo_path=tmp_path,
        language="python",
        targets=(
            EnforcementTarget(
                language="python",
                target_id=source_path,
                source_path=source_path,
                test_paths=(test_path,),
            ),
        ),
        runtime=RuntimeSelection(
            python_executable="/managed/bin/python",
            dependency_overlay_enabled=False,
        ),
    )

    result = lightweight_api.create_python_enforcement_binding().enforce(
        request, EnforcementInvocationContext(run_command=SafeProcessRunner())
    )

    assert result.status == EnforcementStatus.PASSED
    assert seen == {"python_bin": "/managed/bin/python", "execution_env": {}}


def test_lightweight_binding_skips_mutation_until_coverage_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lightweight_api,
        "run_coverage",
        lambda *args, **kwargs: {
            "passed": False,
            "rate": 50.0,
            "testsPass": True,
        },
    )

    def unexpected_mutation(*args, **kwargs):
        raise AssertionError("mutation must not run before coverage passes")

    monkeypatch.setattr(lightweight_api, "run_mutation", unexpected_mutation)

    result = _binding_target_result(
        tmp_path, "src/agent_core/git/retry.py", "tests/test_git_retry.py"
    )

    assert result["reasonCode"] == "coverage_failed"


def test_lightweight_target_explains_mutation_backend_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(lightweight_api, "run_coverage", lambda *args, **kwargs: {"passed": True, "rate": 100.0})

    def backend_failure(*args, **kwargs):
        return {
            "passed": False,
            "rate": 0.0,
            "reasonCode": "mutation_backend_failed",
            "notChecked": 65,
            "failureStage": "mutation_execution",
        }

    monkeypatch.setattr(lightweight_api, "run_mutation", backend_failure)

    result = _binding_target_result(
        tmp_path, "uta/testgen/agent_runtime.py", "tests/test_agent_runtime.py"
    )

    assert result["reasonCode"] == "mutation_backend_failed"
    assert result["message"] == "Python mutation backend failed before executing 65 selected mutants"


def test_lightweight_python_enforcer_runs_without_uta_checkout(tmp_path):
    """Run it from a sparse copy, the way a third party gets it.

    This used to run the script in place, inside the UTA checkout, with the UTA
    virtualenv -- where `uta` is importable, on disk, and one accidental import
    away from passing anyway. It asserted the distribution boundary while
    standing on the wrong side of it.

    Now the tools tree is copied out on its own, the interpreter is given a
    PYTHONPATH containing only that copy, and the subprocess is asked to prove
    `uta` and `agent_core` are unimportable before doing anything else.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test User"], repo)
    (repo / "jobs").mkdir()
    (repo / "tests").mkdir()
    (repo / "jobs" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "jobs" / "forecast.py").write_text(
        "def run(value):\n"
        "    if value:\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_forecast.py").write_text(
        "from jobs.forecast import run\n\n"
        "def test_run():\n"
        "    assert run(True) == 1\n"
        "    assert run(False) == 2\n",
        encoding="utf-8",
    )
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "base"], repo)
    (repo / "jobs" / "forecast.py").write_text(
        "def run(value):\n"
        "    if value:\n"
        "        return 1\n"
        "    return 3\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_forecast.py").write_text(
        "from jobs.forecast import run\n\n"
        "def test_run():\n"
        "    assert run(True) == 1\n"
        "    assert run(False) == 3\n",
        encoding="utf-8",
    )
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "change"], repo)

    import shutil

    sparse = tmp_path / "sparse"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "tools" / "python-enforcement", sparse
    )
    isolated_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(sparse),
    }

    # The boundary itself, asserted before the run that depends on it.
    # `-S` skips site-packages, which is what makes this isolation real: the
    # UTA virtualenv has `uta` installed, so any subprocess on this interpreter
    # can import it no matter what PYTHONPATH says. The tool is stdlib-only by
    # design, so it runs perfectly well without site-packages -- and if it ever
    # stops doing so, that is a distribution defect and this test is where it
    # should surface.
    proof = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import importlib.util as u, sys;"
            "missing = [n for n in ('uta', 'agent_core') if u.find_spec(n) is None];"
            "sys.exit(0 if missing == ['uta', 'agent_core'] else 1)",
        ],
        cwd=str(sparse),
        env=isolated_env,
        capture_output=True,
        text=True,
    )
    assert proof.returncode == 0, f"the sparse copy can still import UTA: {proof.stderr}"

    script = sparse / "uta_python_test_enforce.py"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(script),
            "--repo",
            str(repo),
            "--base-ref",
            "HEAD~1",
            "--test-path",
            "tests/test_forecast.py",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "0",
            "--json-output",
        ],
        cwd=str(repo),
        env=isolated_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    evidence = json.loads(result.stdout)
    assert evidence["backend"] == "python_enforcer"
    assert evidence["enforcementCoreVersion"].startswith("lightweight-")
    assert evidence["headCommit"] == _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    assert evidence["changedProductionFiles"] == ["jobs/forecast.py"]
    assert evidence["coverage"]["passed"] is True
    assert evidence["mutation"]["passed"] is True
    assert evidence["mutation"]["candidatePlan"]["samplingLayer"]["enabled"] is False


def test_old_standalone_script_path_fails_closed():
    script = Path(__file__).resolve().parents[1] / "scripts" / "uta_python_test_enforce.py"
    result = subprocess.run([sys.executable, str(script)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    assert result.returncode == 2
    assert "tools/python-enforcement/uta_python_test_enforce.py" in result.stderr


def test_lightweight_packages_import_without_uta_on_pythonpath(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import uta_py_enforce; import uta_enforce_core; import uta_py_enforce.cli",
        ],
        cwd=str(tmp_path),
        env={"PYTHONPATH": str(PACKAGE_ROOT)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0, result.stderr

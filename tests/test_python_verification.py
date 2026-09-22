import subprocess
import os
import json
import sys
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from uta_py_enforce import mutation as lightweight_mutation
from uta.shared.languages import RawTargetSelection, default_registry
from uta.language.python.verification import candidate_planning
from uta.language.python.verification import dependency_requirements
from uta.language.python.verification import mutation_execution
from uta.language.python.verification import mutation_phase
from uta.language.python.verification import mutation_policy
from uta.language.python.verification import process as py_process
from uta.language.python.verification import runner as py_runner
from uta.language.python.verification import runtime_setup
from uta.language.python.verification.runner import (
    CoverageSummary,
    PythonRuntimeConfig,
    PythonVerificationResult,
    _annotate_mutmut_survivor_diffs,
    _filter_changed_lines_for_mutation,
    _run_command,
    _mutmut_config_overlay,
    _subprocess_run,
    _write_mutmut_import_compat,
    parse_coverage_xml,
    parse_mutmut_summary,
    resolve_python_runtime_config,
    verify_python_target,
)
from uta.shared.config import settings as uta_settings
from uta.shared.targets import TargetRef


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _is_coverage_run(cmd, python_bin):
    return (len(cmd) > 3 and cmd[0] == python_bin
            and Path(cmd[1]).name == "pytest_process.py" and "--coverage-include" in cmd)


def _is_py_compile(cmd):
    return len(cmd) >= 4 and cmd[1:3] == ["-m", "py_compile"]


def test_run_command_records_elapsed_seconds(monkeypatch, tmp_path):
    ticks = iter([10.0, 12.5])
    monkeypatch.setattr(py_process.time, "monotonic", lambda: next(ticks))

    evidence = _run_command(
        "probe",
        ["python3", "--version"],
        tmp_path,
        30,
        lambda cmd, cwd=None, timeout=None, env=None: _completed(cmd, stdout="ok"),
    )

    assert evidence.elapsed_seconds == 2.5
    assert evidence.as_dict()["elapsed_seconds"] == 2.5


def test_subprocess_run_timeout_kills_process_group(monkeypatch):
    killed = []
    popen_kwargs = {}

    class FakePopen:
        pid = 4242
        returncode = None

        def __init__(self, cmd, **kwargs):
            self.args = cmd
            popen_kwargs.update(kwargs)

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(self.args, timeout, output=b"partial stdout", stderr=b"partial stderr")

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(self.args, timeout)

        def terminate(self):
            killed.append(("terminate", None))

        def kill(self):
            killed.append(("kill", None))

    monkeypatch.setattr(py_process.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(py_process.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    result = _subprocess_run(["mutmut", "run"], cwd=Path("/tmp"), timeout=1, env={"X": "1"})

    assert result.returncode == 124
    assert result.stdout == "partial stdout"
    assert result.stderr == "partial stderr\ncommand timed out"
    assert popen_kwargs["start_new_session"] is True
    assert (4242, py_process.signal.SIGTERM) in killed
    assert (4242, py_process.signal.SIGKILL) in killed


def test_python_result_fields_keep_missing_mutation_metrics_unknown():
    result = PythonVerificationResult(
        status="failed",
        reason_code="mutation_backend_failed",
        tests_pass=True,
        coverage=CoverageSummary(
            covered=28,
            total=28,
            rate=100.0,
            gate=95.0,
            passed=True,
            xml_path=".uta_cache/python/coverage/coverage.xml",
        ),
        mutation=None,
        message="mutmut stats pytest command failed",
    )

    fields = result.as_result_fields()

    assert fields["coverage"] == 100.0
    assert fields["mutation_score"] is None
    assert fields["total_mutants"] is None
    assert fields["killed_mutants"] is None
    assert fields["surviving_mutants"] is None
    assert fields["no_coverage_mutants"] is None
    assert fields["no_tests_mutants"] is None
    assert fields["timeout_mutants"] is None
    assert fields["suspicious_mutants"] is None
    assert fields["verification_reason"] == "mutation_backend_failed"


def test_python_verification_isolates_selected_tests_from_broken_repo_root_init(tmp_path):
    repo = tmp_path / "react_agent"
    package = repo / "pkg"
    tests_dir = repo / "tests" / "uta_generated"
    package.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    (repo / "__init__.py").write_text("from .missing import nope\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "app.py").write_text(
        "def classify(value):\n"
        "    if value > 0:\n"
        "        return 'positive'\n"
        "    return 'other'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_pkg_app.py").write_text(
        "import sys\n"
        "from pathlib import Path\n\n"
        "ROOT = Path(__file__).resolve().parents[2]\n"
        "if str(ROOT) not in sys.path:\n"
        "    sys.path.insert(0, str(ROOT))\n\n"
        "from pkg.app import classify\n\n"
        "def test_classify_positive():\n"
        "    assert classify(1) == 'positive'\n",
        encoding="utf-8",
    )

    result = verify_python_target(
        repo,
        TargetRef(
            language="python",
            target_id="pyfile:pkg/app.py",
            display_name="pkg/app.py",
            source_path="pkg/app.py",
            granularity="file",
        ),
        test_paths=["tests/uta_generated/test_pkg_app.py"],
        coverage_gate=50.0,
        run_mutation=False,
    )

    assert result.status == "passed"
    coverage_command = next(command for command in result.commands if command.name == "pytest_coverage_run")
    assert "tests/uta_generated/test_pkg_app.py" not in coverage_command.command
    assert any("uta-pytest-isolated" in part for part in coverage_command.command)


def test_isolated_pytest_context_preserves_bare_imports_from_test_root(tmp_path):
    repo = tmp_path / "react_agent"
    package = repo / "pkg"
    tests_root = repo / "tests"
    tests_dir = tests_root / "uta_generated"
    package.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    (repo / "__init__.py").write_text("from .missing import nope\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "app.py").write_text(
        "def classify(value):\n"
        "    if value > 0:\n"
        "        return 'positive'\n"
        "    return 'other'\n",
        encoding="utf-8",
    )
    (tests_root / "test_helpers.py").write_text(
        "def assert_positive(value):\n"
        "    assert value == 'positive'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_pkg_app.py").write_text(
        "import sys\n"
        "from pathlib import Path\n\n"
        "ROOT = Path(__file__).resolve().parents[2]\n"
        "if str(ROOT) not in sys.path:\n"
        "    sys.path.insert(0, str(ROOT))\n\n"
        "from pkg.app import classify\n"
        "from test_helpers import assert_positive\n\n"
        "def test_classify_positive():\n"
        "    assert_positive(classify(1))\n",
        encoding="utf-8",
    )

    result = verify_python_target(
        repo,
        TargetRef(
            language="python",
            target_id="pyfile:pkg/app.py",
            display_name="pkg/app.py",
            source_path="pkg/app.py",
            granularity="file",
        ),
        test_paths=["tests/uta_generated/test_pkg_app.py"],
        coverage_gate=50.0,
        run_mutation=False,
    )

    assert result.status == "passed"
    coverage_command = next(command for command in result.commands if command.name == "pytest_coverage_run")
    assert "tests/uta_generated/test_pkg_app.py" not in coverage_command.command
    assert any("uta-pytest-isolated" in part for part in coverage_command.command)


@pytest.fixture(autouse=True)
def _isolate_python_runtime_env(monkeypatch):
    for name in (
        "UTA_PYTHON_BIN",
        "UTA_PYTHON_MUTMUT_BIN",
        "UTA_PYTHON2_BIN",
        "UTA_PYTHON2_MUTMUT_BIN",
        "UTA_SERVICE_PYTHON_BIN",
        "UTA_PYTHON_GATE_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_parse_coverage_xml_summarizes_selected_python_file(tmp_path):
    xml_path = tmp_path / "coverage.xml"
    xml_path.write_text(
        """<?xml version="1.0" ?>
<coverage>
  <packages>
    <package name="jobs">
      <classes>
        <class filename="jobs/forecast.py">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="0"/>
            <line number="3" hits="3"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
""",
        encoding="utf-8",
    )

    summary = parse_coverage_xml(xml_path, ["jobs/forecast.py"], gate=80.0)

    assert summary.covered == 2
    assert summary.total == 3
    assert round(summary.rate, 2) == 66.67
    assert summary.passed is False


def test_annotate_mutmut_survivor_diffs_captures_show_output(tmp_path):
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append((cmd, cwd, timeout, env))
        return _completed(
            cmd,
            0,
            stdout="--- before\n+++ after\n-    return True\n+    return False\n57/57 🎉 56 🙁 1\n",
        )

    survivors = _annotate_mutmut_survivor_diffs(
        [{"id": "pkg.app.x_target__mutmut_1", "file": "", "line": 0, "description": "survived"}],
        mutmut_bin="mutmut",
        repo=tmp_path,
        timeout=300,
        runner=fake_run,
        env_overrides={"PYTHONPATH": "compat"},
    )

    assert calls[0][0] == ["mutmut", "show", "pkg.app.x_target__mutmut_1"]
    assert calls[0][2] == 10
    assert calls[0][3]["PYTHONPATH"] == "compat"
    assert survivors[0]["mutmut_show_command"] == "mutmut show pkg.app.x_target__mutmut_1"
    assert "return False" in survivors[0]["mutmut_show_output"]
    assert "57/57" not in survivors[0]["mutmut_show_output"]


def test_annotate_mutmut_survivor_diffs_prefers_internal_output(tmp_path, monkeypatch):
    from uta.language.python import mutation_context

    calls = []

    def fake_internal(repo, survivor, *, source_path, module_cache):
        calls.append((repo, dict(survivor), source_path, module_cache))
        return {
            "command": f"mutmut internal show {survivor['id']} --path {source_path}",
            "output": "--- before\n+++ after\n-    return True\n+    return False\n57/57 🎉 56 🙁 1\n",
        }

    def fail_run(cmd, cwd=None, timeout=None, env=None):
        raise AssertionError(f"external mutmut show should not run: {cmd}")

    monkeypatch.setattr(mutation_context, "_mutmut_internal_show_diff", fake_internal)

    survivors = _annotate_mutmut_survivor_diffs(
        [{"id": "pkg.app.x_target__mutmut_1", "file": "", "line": 0, "description": "survived"}],
        mutmut_bin="mutmut",
        repo=tmp_path,
        source_path="pkg/app.py",
        timeout=300,
        runner=fail_run,
    )

    assert calls
    assert calls[0][0] == tmp_path
    assert calls[0][2] == "pkg/app.py"
    assert survivors[0]["mutmut_show_command"] == "mutmut internal show pkg.app.x_target__mutmut_1 --path pkg/app.py"
    assert "return False" in survivors[0]["mutmut_show_output"]
    assert "57/57" not in survivors[0]["mutmut_show_output"]


def test_annotate_mutmut_survivor_diffs_respects_show_budget(tmp_path, monkeypatch):

    monkeypatch.setattr(uta_settings, "python_mutant_verify_show_max_calls", 1)
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(cmd)
        return _completed(cmd, 0, stdout="--- before\n+++ after\n-a\n+b\n")

    survivors = _annotate_mutmut_survivor_diffs(
        [
            {"id": "pkg.app.x_target__mutmut_1", "file": "", "line": 0, "description": "survived"},
            {"id": "pkg.app.x_target__mutmut_2", "file": "", "line": 0, "description": "survived"},
        ],
        mutmut_bin="mutmut",
        repo=tmp_path,
        timeout=300,
        runner=fake_run,
    )

    assert calls == [["mutmut", "show", "pkg.app.x_target__mutmut_1"]]
    assert survivors[0]["mutmut_show_output"]
    assert "mutmut_show_command" not in survivors[1]


def test_parse_coverage_xml_can_scope_to_changed_executable_lines(tmp_path):
    xml_path = tmp_path / "coverage.xml"
    xml_path.write_text(
        """<?xml version="1.0" ?>
<coverage>
  <packages>
    <package name="jobs">
      <classes>
        <class filename="jobs/forecast.py">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="0"/>
            <line number="3" hits="1"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
""",
        encoding="utf-8",
    )

    summary = parse_coverage_xml(
        xml_path,
        ["jobs/forecast.py"],
        gate=100.0,
        changed_lines={"jobs/forecast.py": {2}},
    )

    assert summary.covered == 0
    assert summary.total == 1
    assert summary.rate == 0.0
    assert summary.passed is False


def test_parse_coverage_xml_fails_closed_when_changed_lines_are_missing_from_report(tmp_path):
    xml_path = tmp_path / "coverage.xml"
    xml_path.write_text(
        """<?xml version="1.0" ?>
<coverage>
  <packages>
    <package name="jobs">
      <classes>
        <class filename="other.py">
          <lines><line number="2" hits="1"/></lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
""",
        encoding="utf-8",
    )

    summary = parse_coverage_xml(
        xml_path,
        ["jobs/forecast.py"],
        gate=100.0,
        changed_lines={"jobs/forecast.py": {2}},
    )

    assert summary.covered == 0
    assert summary.total == 0
    assert summary.rate == 0.0
    assert summary.passed is False


def test_parse_coverage_xml_passes_when_changed_lines_are_not_executable(tmp_path):
    xml_path = tmp_path / "coverage.xml"
    xml_path.write_text(
        """<?xml version="1.0" ?>
<coverage>
  <packages>
    <package name="jobs">
      <classes>
        <class filename="jobs/forecast.py">
          <lines><line number="2" hits="1"/></lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
""",
        encoding="utf-8",
    )

    summary = parse_coverage_xml(
        xml_path,
        ["jobs/forecast.py"],
        gate=100.0,
        changed_lines={"jobs/forecast.py": {3}},
    )

    assert summary.covered == 0
    assert summary.total == 0
    assert summary.rate == 100.0
    assert summary.passed is True
    assert summary.no_executable_changed_lines is True


def test_parse_mutmut_summary_uses_test_strength_denominator():
    summary = parse_mutmut_summary(
        "Mutation testing done: 6 generated, 3 killed, 1 survived, 2 no coverage",
        gate=75.0,
        runtime_lane="mutmut-modern",
    )

    assert summary.generated == 6
    assert summary.killed == 3
    assert summary.survived == 1
    assert summary.no_coverage == 2
    assert summary.rate == 75.0
    assert summary.passed is True


def test_parse_mutmut_summary_reads_real_progress_output():
    summary = parse_mutmut_summary(
        "2. Checking mutants\n"
        "⠴ 804/804  🎉 127  ⏰ 0  🤔 0  🙁 677  🔇 0\n",
        gate=10.0,
        runtime_lane="mutmut-modern",
    )

    assert summary.generated == 804
    assert summary.killed == 127
    assert summary.survived == 677
    assert summary.no_coverage == 0
    assert round(summary.rate, 2) == 15.8
    assert summary.passed is True


def test_parse_mutmut_summary_reads_mutmut3_no_tests_bucket():
    summary = parse_mutmut_summary(
        "Running mutation testing\n"
        "⠹ 806/806  🎉 0 🫥 806  ⏰ 0  🤔 0  🙁 0  🔇 0  🧙 0\n",
        gate=100.0,
        runtime_lane="mutmut-modern",
    )

    assert summary.generated == 806
    assert summary.killed == 0
    assert summary.no_tests == 806
    assert summary.survived == 0
    assert summary.no_coverage == 0
    assert summary.rate == 100.0
    assert summary.passed is True


def test_parse_mutmut_summary_leaves_no_test_association_to_verifier_metadata():
    summary = parse_mutmut_summary(
        "FAILED: Unable to force test failures\n"
        "Stopping early, because we could not find any test case for any mutant.\n",
        gate=100.0,
        runtime_lane="mutmut-modern",
    )

    assert summary.generated == 0
    assert summary.killed == 0
    assert summary.no_tests == 0


def test_reconcile_no_test_association_uses_generated_metadata_count():
    from uta.language.python.verification.runner import _reconcile_no_test_association

    summary = parse_mutmut_summary(
        "Stopping early, because we could not find any test case for any mutant.\n",
        gate=95.0,
        runtime_lane="mutmut-modern",
    )

    reconciled = _reconcile_no_test_association(
        summary,
        output="Stopping early, because we could not find any test case for any mutant.\n",
        metadata_count=1,
    )

    assert reconciled.generated == 1
    assert reconciled.no_tests == 1
    assert reconciled.rate == 100.0
    assert reconciled.passed is True


def test_batched_verification_preserves_no_test_mutant_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_candidate_plan_enabled", True)
    monkeypatch.setattr(uta_settings, "python_mutation_generation_strategy", "batch")
    repo = tmp_path / "repo"
    source = repo / "pkg" / "worker.py"
    test_file = repo / "tests" / "test_worker.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    (repo / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8")
    source.write_text("def run(raw):\n    return raw.upper()\n", encoding="utf-8")
    test_file.write_text("from pkg.worker import run\n\ndef test_run():\n    assert run('a') == 'A'\n", encoding="utf-8")
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    mutation_run_env = {}
    mutation_config = {}

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut, version 3.3.1")
        if len(cmd) >= 3 and cmd[:2] == ["python3", "-c"] and cmd[-2] == "metadata":
            meta = repo / "mutants" / "pkg" / "worker.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps({"exit_code_by_key": {"pkg.worker.x_run__mutmut_1": 1}}),
                encoding="utf-8",
            )
            meta.with_suffix(meta.suffix + ".uta.json").write_text(
                json.dumps({"line_by_key": {"pkg.worker.x_run__mutmut_1": 2}}),
                encoding="utf-8",
            )
            return _completed(cmd, stdout="UTA_MUTMUT_GENERATION_STATS mutated=1 unmodified=0 ignored=0\n")
        if len(cmd) >= 3 and cmd[:2] == ["python3", "-c"] and cmd[-2] == "run":
            mutation_run_env.update(env or {})
            mutation_config["text"] = (repo / "pyproject.toml").read_text(encoding="utf-8")
            return _completed(
                cmd,
                returncode=1,
                stderr="Stopping early, because we could not find any test case for any mutant.\n",
            )
        return _completed(cmd)

    result = verify_python_target(
        repo,
        default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py")),
        test_paths=["tests/test_worker.py"],
        syntax_version="python3",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
        changed_lines={"pkg/worker.py": {2}},
        run_command=fake_run,
    )

    assert result.status == "failed"
    assert result.reason_code == "mutation_no_tests"
    assert result.mutation
    assert result.mutation.generated == 1
    assert result.mutation.no_tests == 1
    assert result.mutation.rate == 0.0
    assert result.mutation.passed is False
    assert result.mutation.candidate_plan["runMutants"] == 1
    assert result.mutation.candidate_plan["noTests"] == 1
    pythonpath = mutation_run_env["PYTHONPATH"].split(os.pathsep)
    import_compat = mutation_run_env["UTA_MUTMUT_IMPORT_COMPAT_DIR"]
    assert pythonpath[0] == import_compat
    mutation_import_roots = [
        value for value in mutation_run_env.get("UTA_PYTEST_IMPORT_ROOTS", "").split(os.pathsep) if value
    ]
    assert all(value.startswith((repo / "mutants").as_posix()) for value in mutation_import_roots)
    assert (repo / "mutants").as_posix() in pythonpath
    assert pythonpath.index((repo / "mutants").as_posix()) < pythonpath.index(repo.as_posix())
    assert "tests/test_worker.py" in mutation_config["text"]
    assert "uta-pytest-isolated" not in mutation_config["text"]


def test_mutmut_config_selects_package_local_test_without_copying_its_source_root(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pipecat" / "knowledge_retrieval" / "store.py"
    test_file = repo / "pipecat" / "tests" / "test_store_product.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_text("def read():\n    return 1\n", encoding="utf-8")
    test_file.write_text("from pipecat.knowledge_retrieval import store\n", encoding="utf-8")
    (repo / "pipecat" / "tests" / "conftest.py").write_text("import sop\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = [\".\"]\n", encoding="utf-8")
    import_compat = repo / ".uta_cache" / "python" / "mutation" / "import_compat"

    overlay = _mutmut_config_overlay(
        repo,
        "pipecat/knowledge_retrieval/store.py",
        ["pipecat/tests/test_store_product.py"],
        "mutmut, version 3.3.1",
        python_bin="python3",
        import_compat_dir=import_compat,
    )

    overlay.apply()

    config = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.mutmut]" in config
    assert 'also_copy = ["pipecat"]' not in config
    assert "pipecat/tests/test_store_product.py" in config
    assert 'pytest_add_cli_args = ["--noconftest"]' in config
    assert 'tests_dir = ["pipecat/tests/test_store_product.py"]' in config


def test_mutmut_setup_cfg_overlay_replaces_stale_test_selection(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "chat_robot" / "models.py"
    test_file = repo / "tests" / "uta_generated" / "test_chat_robot_models.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_text("def read():\n    return 1\n", encoding="utf-8")
    test_file.write_text("from chat_robot import models\n", encoding="utf-8")
    (repo / "setup.cfg").write_text(
        "[mutmut]\n"
        "pytest_add_cli_args_test_selection =\n"
        "    tests/uta_generated/test_old_broad.py\n"
        "    tests/uta_generated/test_other_broad.py\n",
        encoding="utf-8",
    )
    import_compat = repo / ".uta_cache" / "python" / "mutation" / "import_compat"

    overlay = _mutmut_config_overlay(
        repo,
        "chat_robot/models.py",
        ["tests/uta_generated/test_chat_robot_models.py"],
        "mutmut, version 3.5.0",
        python_bin="python3",
        import_compat_dir=import_compat,
    )

    overlay.apply()

    config = (repo / "setup.cfg").read_text(encoding="utf-8")
    assert "tests_dir = tests/uta_generated/test_chat_robot_models.py" in config
    assert "pytest_add_cli_args_test_selection" not in config
    assert "test_old_broad.py" not in config
    assert "test_other_broad.py" not in config


def test_mutmut_pyproject_overlay_scopes_initial_stats_to_selected_tests(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "chat_robot" / "views.py"
    test_file = repo / "tests" / "uta_generated" / "test_chat_robot_views.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_text("def read():\n    return 1\n", encoding="utf-8")
    test_file.write_text("from chat_robot import views\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[tool.mutmut]\npaths_to_mutate = [\"old.py\"]\n", encoding="utf-8")
    import_compat = repo / ".uta_cache" / "python" / "mutation" / "import_compat"

    overlay = _mutmut_config_overlay(
        repo,
        "chat_robot/views.py",
        ["tests/uta_generated/test_chat_robot_views.py"],
        "mutmut, version 3.5.0",
        python_bin="python3",
        import_compat_dir=import_compat,
    )

    overlay.apply()

    config = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'paths_to_mutate = ["chat_robot/views.py"]' in config
    assert 'tests_dir = ["tests/uta_generated/test_chat_robot_views.py"]' in config
    assert "pytest_add_cli_args =" not in config
    assert "pytest_add_cli_args_test_selection" not in config


def test_mutmut_overlay_does_not_collect_relative_and_isolated_test_copies(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "chat_robot" / "views.py"
    relative_test = repo / "tests" / "uta_generated" / "test_chat_robot_views.py"
    isolated_test = tmp_path / "uta-pytest-isolated" / "tests" / "uta_generated" / relative_test.name
    source.parent.mkdir(parents=True)
    relative_test.parent.mkdir(parents=True)
    isolated_test.parent.mkdir(parents=True)
    source.write_text("def read():\n    return 1\n", encoding="utf-8")
    relative_test.write_text("from chat_robot import views\n", encoding="utf-8")
    isolated_test.write_text(relative_test.read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "pyproject.toml").write_text("[tool.mutmut]\n", encoding="utf-8")

    overlay = _mutmut_config_overlay(
        repo,
        "chat_robot/views.py",
        [str(isolated_test)],
        "mutmut, version 3.5.0",
        python_bin="python3",
        import_compat_dir=repo / ".uta_cache" / "python" / "mutation" / "import_compat",
        support_test_paths=["tests/uta_generated/test_chat_robot_views.py"],
    )

    overlay.apply()

    config = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert f'tests_dir = ["{isolated_test}"]' in config
    assert "pytest_add_cli_args_test_selection" not in config
    assert "pytest_add_cli_args =" not in config


def test_parse_mutmut_summary_reads_legacy_python2_progress_output():
    summary = parse_mutmut_summary(
        "2. Checking mutants\n"
        "� 174/174  🎉 31  ⏰ 0  🤔 0  🙁 143\n",
        gate=1.0,
        runtime_lane="mutmut-legacy-py2",
    )

    assert summary.generated == 174
    assert summary.killed == 31
    assert summary.survived == 143
    assert summary.no_coverage == 0
    assert round(summary.rate, 2) == 17.82
    assert summary.passed is True


def test_python2_verify_reports_mutmut15_legacy_candidate_plan(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "worker.py").write_text(
        "def run(raw):\n"
        "    return raw.upper()\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python2", "--version"]:
            return _completed(cmd, stdout="Python 2.7.18")
        if cmd[:3] == ["python2", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 4.6.11")
        if cmd[:3] == ["python2", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 5.5")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python2"):
            return _completed(cmd)
        if cmd[:4] == ["python2", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut 1.5.0")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(cmd, stdout="2. Checking mutants\n� 1/1  🎉 1  ⏰ 0  🤔 0  🙁 0\n")
        return _completed(cmd)

    result = verify_python_target(
        repo,
        default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py")),
        test_paths=["tests/test_worker.py"],
        syntax_version="python2",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(python2_bin="python2", python2_mutmut_bin="mutmut"),
        changed_lines={"pkg/worker.py": {2}},
        run_command=fake_run,
    )

    mutation = result.as_result_fields()["mutation_summary"]
    assert mutation["candidatePlan"]["filterMechanism"] == "mutmut15_legacy_changed_line_scope"
    assert mutation["candidatePlan"]["exactToolCandidateKeys"] == []
    assert "mutmut2" not in json.dumps(mutation["candidatePlan"]).lower()


def test_python3_candidate_plan_flag_records_mutmut3_exact_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_candidate_plan_enabled", True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "worker.py").write_text(
        "def run(raw):\n"
        "    return raw.upper()\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def write_meta():
        meta = repo / "mutants" / "pkg" / "worker.py.meta"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(
            json.dumps(
                {
                    "exit_code_by_key": {"pkg.worker.x_run__mutmut_1": 1},
                    "line_by_key": {"pkg.worker.x_run__mutmut_1": 2},
                }
            ),
            encoding="utf-8",
        )

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if len(cmd) >= 3 and cmd[:2] == ["python3", "-c"]:
            if cmd[-2] == "metadata":
                write_meta()
                return _completed(cmd)
            if cmd[-2] == "run":
                return _completed(cmd, stdout="1/1  🎉 1  ⏰ 0  🤔 0  🙁 0\n")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut, version 3.3.1")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(cmd, stdout="1/1  🎉 1  ⏰ 0  🤔 0  🙁 0\n")
        return _completed(cmd)

    result = verify_python_target(
        repo,
        default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py")),
        test_paths=["tests/test_worker.py"],
        syntax_version="python3",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
        changed_lines={"pkg/worker.py": {2}},
        run_command=fake_run,
        base_ref="origin/master",
        base_commit="base-123",
        head_commit="head-456",
        repo_url="git@example.com:group/repo.git",
    )

    mutation = result.as_result_fields()["mutation_summary"]
    plan = mutation["candidatePlan"]
    assert plan["filterMechanism"] == "mutmut3_metadata_selected_execution"
    assert plan["exactToolCandidateKeys"] == ["pkg.worker.x_run__mutmut_1"]
    assert plan["baseRef"] == "origin/master"
    assert plan["baseCommit"] == "base-123"
    assert plan["headCommit"] == "head-456"
    assert plan["repoUrl"] == "git@example.com:group/repo.git"
    assert plan["selectedKeyExecutionApplied"] is False
    assert plan["runMutants"] == 1
    assert plan["scoredMutants"] == 1
    assert plan["killed"] == 1
    artifact_path = repo / plan["candidatePlanArtifactPath"]
    assert artifact_path.exists()
    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert persisted["candidatePlanId"] == plan["candidatePlanId"]
    assert persisted["activeToolCandidateKeys"] == ["pkg.worker.x_run__mutmut_1"]


def test_python3_candidate_plan_uses_ci_generation_cap_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_candidate_plan_enabled", True)
    monkeypatch.setattr(uta_settings, "python_mutation_generation_ci_max_changed_lines", 10)
    monkeypatch.setattr(uta_settings, "python_mutation_generation_ci_max_selected", 1)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "worker.py").write_text(
        "def run(raw):\n"
        "    value = raw + 1\n"
        "    other = raw - 1\n"
        "    return value if raw > 0 else other\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def write_meta(policy_path):
        policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        selected_lines = [int(line) for line in policy["selectedLines"]]
        assert len(selected_lines) == 1
        meta = repo / "mutants" / "pkg" / "worker.py.meta"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(
            json.dumps(
                {
                    "exit_code_by_key": {
                        "pkg.worker.x_run__mutmut_1": 1,
                    },
                    "line_by_key": {
                        "pkg.worker.x_run__mutmut_1": selected_lines[0],
                    },
                }
            ),
            encoding="utf-8",
        )

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if len(cmd) >= 3 and cmd[:2] == ["python3", "-c"]:
            if cmd[-2] == "metadata":
                write_meta(cmd[-1])
                return _completed(cmd)
            if cmd[-2] == "run":
                return _completed(cmd, stdout="1/1  🎉 1  ⏰ 0  🤔 0  🙁 0\n")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='2' hits='1'/>"
                "<line number='3' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut, version 3.3.1")
        if cmd[:2] == ["mutmut", "run"]:
            assert cmd == ["mutmut", "run"]
            return _completed(cmd, stdout="1/1  🎉 1  ⏰ 0  🤔 0  🙁 0\n")
        if cmd[:2] == ["mutmut", "results"]:
            return _completed(cmd, stdout="SURVIVED pkg.worker.x_run__mutmut_2 pkg/worker.py:2 changed return\n")
        if cmd[:2] == ["mutmut", "show"]:
            return _completed(cmd, stdout="--- pkg/worker.py\n+++ pkg/worker.py\n@@ -2,1 +2,1 @@\n-    return raw.upper()\n+    return None\n")
        return _completed(cmd)

    result = verify_python_target(
        repo,
        default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py")),
        test_paths=["tests/test_worker.py"],
        syntax_version="python3",
        coverage_gate=100.0,
        mutation_gate=1.0,
        config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
        changed_lines={"pkg/worker.py": {2, 3}},
        run_command=fake_run,
        enable_mutation_sampling=True,
    )

    plan = result.as_result_fields()["mutation_summary"]["candidatePlan"]
    assert len(plan["reportFullSelected"]) == 1
    assert len(plan["activeSelected"]) == 1
    assert plan["samplingLayer"]["enabled"] is False
    assert plan["generationPolicy"]["capProfile"] == "ci"
    assert plan["generationPolicy"]["truncated"] is True
    assert plan["generationPolicy"]["omittedByCap"] == 1
    assert plan["generationPolicy"]["hardCapSelectedOpportunities"] == 2
    assert plan["generationPolicy"]["omittedByHardCap"] == 0
    assert plan["generationPolicy"]["omittedByRepresentativeSelection"] == 1
    assert plan["generationPolicy"]["representativeSelection"]["enabled"] is True
    assert plan["generationPolicy"]["caps"]["maxSelected"] == 1
    assert plan["effectiveCapConfig"]["profile"] == "ci"


def test_generation_policy_cap_keeps_useful_operator_alternatives(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_generation_ci_max_changed_lines", 1)
    monkeypatch.setattr(uta_settings, "python_mutation_generation_ci_max_selected", 1)
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "pkg" / "worker.py"
    source.parent.mkdir()
    source.write_text(
        "def run(raw):\n"
        "    return raw.replace('x', 'y')\n",
        encoding="utf-8",
    )

    policy = mutation_policy._write_mutmut_generation_policy(
        repo / ".uta_cache" / "python" / "mutation",
        source_file=source,
        source_path="pkg/worker.py",
        changed_lines={"pkg/worker.py": {2}},
        covered_lines={2},
        use_ci_cap_profile=True,
    )
    assert policy["operatorByLine"] == {}
    assert "2" not in policy["opportunityIdByLine"]
    assert policy["retainedOperatorAlternatives"] >= 1
    assert policy["omittedByOnePerLine"] == 0
    assert len(policy["selected"]) == 1

    meta = repo / "mutants" / "pkg" / "worker.py.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        json.dumps(
            {
                "exit_code_by_key": {"pkg.worker.x_run__mutmut_1": 1},
                "line_by_key": {"pkg.worker.x_run__mutmut_1": 2},
            }
        ),
        encoding="utf-8",
    )

    plan = mutation_execution.build_mutmut3_candidate_plan_from_meta(
        repo,
        source_path="pkg/worker.py",
        target_id="pkg/worker.py",
        changed_lines={"pkg/worker.py": {2}},
        covered_lines={2},
        selected_test_paths=("tests/test_worker.py",),
        mutmut_version="mutmut, version 3.3.1",
        runtime_fingerprint="python3",
        dependency_fingerprint="deps",
        config_fingerprint_value="cfg",
        mutmut_internal_api_fingerprint="mutmut3-meta-v3",
        generation_policy=policy,
    )

    assert len(plan.active_selected) == 1
    assert plan.active_selected[0].tool_candidate_key == "pkg.worker.x_run__mutmut_1"


def test_candidate_plan_evidence_reports_materialization_ratio():
    plan = candidate_planning._candidate_plan_with_execution_evidence(
        {
            "activeSelected": [{"candidateId": f"c{index}"} for index in range(100)],
            "reportFullSelected": [{"candidateId": f"c{index}"} for index in range(100)],
            "eligibleMutationOpportunities": [{"opportunityId": "o1"}, {"opportunityId": "o2"}],
            "exactToolCandidateKeys": ["pkg.worker.x_run__mutmut_1"],
            "generationPolicy": {
                "selectedOpportunities": 300,
            },
        },
        py_runner.MutationSummary(
            runtime_lane="mutmut3",
            generated=1,
            killed=1,
            survived=0,
            no_coverage=0,
            rate=100.0,
            gate=95.0,
            passed=True,
        ),
    )

    assert plan["preGenerationSelectedOpportunities"] == 300
    assert plan["preGenerationMaterializationRatio"] == 0.3333
    assert plan["preGenerationMaterializationWarning"] is True
    assert plan["selectedOpportunities"] == 100
    assert plan["materializedCandidates"] == 100
    assert plan["materializedCandidateRatio"] == 1.0
    assert plan["materializedCandidateWarning"] is False


def test_candidate_plan_persists_exact_timeout_status_and_diff(tmp_path, monkeypatch):
    meta = tmp_path / "mutants" / "processor" / "main.py.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "processor.main.x_initialize__mutmut_1": 1,
                    "processor.main.x_initialize__mutmut_2": 36,
                }
            }
        ),
        encoding="utf-8",
    )
    exact_diff = "--- processor/main.py\n+++ processor/main.py\n-old\n+new"

def test_ci_generation_selection_is_representative_after_hard_cap():
    def opportunity(line, symbol, operator_name="comparison_boundary", priority=100):
        return SimpleNamespace(
            line=line,
            symbol=symbol,
            operator_name=operator_name,
            operator_priority=priority,
            opportunity_id=f"{symbol}-{operator_name}-{line}",
            selection_rank=(0, -priority, "pkg/worker.py", line, symbol, operator_name, f"id-{line}"),
        )

    hard_capped = (
        opportunity(1, "alpha"),
        opportunity(2, "alpha"),
        opportunity(3, "alpha"),
        opportunity(4, "alpha"),
        opportunity(5, "beta"),
        opportunity(6, "gamma"),
    )

    selected, reasons = mutation_policy._representative_generation_policy_selected(
        hard_capped,
        {"maxSelected": 3},
    )

    assert reasons == ["selectedOpportunities=6 representative-selected to 3"]
    assert {item.symbol for item in selected} == {"alpha", "beta", "gamma"}


def test_verify_python_target_restores_source_mask_when_mutation_setup_raises(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "pkg" / "worker.py"
    source.parent.mkdir()
    original = "def run(raw):\n    value = raw + 1\n    return value\n"
    source.write_text(original, encoding="utf-8")
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='2' hits='1'/>"
                "<line number='3' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:1] == ["mutmut"] and "--version" in cmd:
            return _completed(cmd, stdout="mutmut, version 3.3.1")
        return _completed(cmd)

    def fail_import_compat(*args, **kwargs):
        raise RuntimeError("import compat setup failed")

    monkeypatch.setattr(mutation_phase, "_write_mutmut_import_compat", fail_import_compat)

    with pytest.raises(RuntimeError, match="import compat setup failed"):
        verify_python_target(
            repo,
            default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py")),
            test_paths=["tests/test_worker.py"],
            syntax_version="python3",
            coverage_gate=100.0,
            mutation_gate=100.0,
            config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
            changed_lines={"pkg/worker.py": {2}},
            run_command=fake_run,
        )

    assert source.read_text(encoding="utf-8") == original


def test_python3_candidate_plan_runs_adapter_generated_set(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_candidate_plan_enabled", True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "worker.py").write_text(
        "def run(raw):\n"
        "    return raw.upper()\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    adapter_run_commands = []

    def write_meta():
        meta = repo / "mutants" / "pkg" / "worker.py.meta"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(
            json.dumps(
                {
                    "exit_code_by_key": {
                        "pkg.worker.x_run__mutmut_1": 1,
                        "pkg.worker.x_run__mutmut_2": 0,
                    },
                    "line_by_key": {
                        "pkg.worker.x_run__mutmut_1": 2,
                        "pkg.worker.x_run__mutmut_2": 2,
                    },
                }
            ),
            encoding="utf-8",
        )

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if len(cmd) >= 3 and cmd[:2] == ["python3", "-c"]:
            if cmd[-2] == "metadata":
                write_meta()
                return _completed(cmd)
            if cmd[-2] == "run":
                adapter_run_commands.append(list(cmd))
                return _completed(cmd, stdout="1/1  🎉 1  ⏰ 0  🤔 0  🙁 0\n")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut, version 3.3.1")
        if cmd[:2] == ["mutmut", "run"]:
            raise AssertionError(f"modern adapter mode should not call the stock mutmut binary directly: {cmd}")
        if cmd[:2] == ["mutmut", "results"]:
            return _completed(cmd, stdout="")
        return _completed(cmd)

    result = verify_python_target(
        repo,
        default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py")),
        test_paths=["tests/test_worker.py"],
        syntax_version="python3",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
        changed_lines={"pkg/worker.py": {2}},
        run_command=fake_run,
        enable_mutation_sampling=True,
    )

    assert result.status == "passed"
    assert len(adapter_run_commands) == 1
    assert adapter_run_commands[0][-2] == "run"


def test_mutmut_runner_uses_per_mutant_timeout_wrapper(monkeypatch, tmp_path):
    monkeypatch.setattr(uta_settings, "python_mutation_per_mutant_timeout_seconds", 7)
    repo = tmp_path / "repo"
    repo.mkdir()
    wrapper_dir = repo / ".uta_cache" / "python" / "mutation" / "import_compat"
    wrapper_dir.mkdir(parents=True)

    command = py_runner._mutmut_runner_command(
        "python3",
        ["tests/test_worker.py"],
        repo=repo,
        import_compat_dir=wrapper_dir,
        source_path="pkg/worker.py",
    )

    assert "mutmut_timeout_runner.py" in command
    assert " 7 -- python3 " in command
    assert "pytest_process.py -- -x --assert=plain tests/test_worker.py" in command


def test_python3_candidate_plan_cap_truncates_before_metadata_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_candidate_plan_enabled", True)
    monkeypatch.setattr(uta_settings, "python_mutation_generation_strategy", "hard_cap")
    monkeypatch.setattr(uta_settings, "python_mutation_generation_max_changed_lines", 1)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "worker.py").write_text(
        "def run(raw):\n"
        "    value = raw + 1\n"
        "    return value or 1\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    adapter_run_commands = []
    observed_policy = {}

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='2' hits='1'/>"
                "<line number='3' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut, version 3.3.1")
        if len(cmd) >= 3 and cmd[:2] == ["python3", "-c"]:
            if cmd[-2] == "run":
                adapter_run_commands.append(list(cmd))
                return _completed(cmd, stdout="1/1  🎉 1  ⏰ 0  🤔 0  🙁 0\n")
            policy_path = Path(cmd[-1])
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            observed_policy.update(policy)
            assert policy["truncated"] is True
            assert len(policy["selectedLines"]) == 1
            selected_line = int(policy["selectedLines"][0])
            meta = repo / "mutants" / "pkg" / "worker.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps(
                    {
                        "exit_code_by_key": {
                            "pkg.worker.x_run__mutmut_1": 1,
                        },
                        "line_by_key": {
                            "pkg.worker.x_run__mutmut_1": selected_line,
                        },
                    }
                ),
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "run"]:
            raise AssertionError(f"modern adapter mode should not call the stock mutmut binary directly: {cmd}")
        return _completed(cmd)

    result = verify_python_target(
        repo,
        default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py")),
        test_paths=["tests/test_worker.py"],
        syntax_version="python3",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
        changed_lines={"pkg/worker.py": {2, 3}},
        run_command=fake_run,
    )

    assert result.status == "passed"
    assert result.reason_code == "passed"
    assert observed_policy["omittedByCap"] >= 1
    assert "mutmut_generate_metadata" in [command.name for command in result.commands]
    assert len(adapter_run_commands) == 1
    assert adapter_run_commands[0][-2] == "run"
    assert "mutmut_generation_policy" in result.mutation.artifacts
    assert result.mutation.candidate_plan["generationPolicy"]["truncated"] is True


def test_mutmut_generation_policy_carries_selected_operator_identity(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "pkg" / "worker.py"
    source.parent.mkdir()
    source.write_text(
        "def run(raw):\n"
        "    value = raw + 1\n"
        "    return value or 1\n",
        encoding="utf-8",
    )

    policy = mutation_policy._write_mutmut_generation_policy(
        repo / ".uta_cache" / "python" / "mutation",
        source_file=source,
        source_path="pkg/worker.py",
        changed_lines={"pkg/worker.py": {2, 3}},
        covered_lines={2, 3},
        use_ci_cap_profile=False,
    )

    assert policy["operatorByLine"] == {}
    assert policy["opportunityIdByLine"] == {}
    assert len(policy["selected"]) == len(policy["selectedLines"])
    assert policy["retainedOperatorAlternatives"] >= 1


def test_mutmut_adapter_enforces_operator_policy_not_only_line():
    command = py_runner._mutmut_generate_metadata_command(
        "python3",
        max_children=1,
        policy_path=Path("/tmp/policy.json"),
    )
    adapter_code = command[2]

    assert "operator_names" in adapter_code
    assert "operatorByLine" in adapter_code
    assert "_operator_matches" in adapter_code
    assert "policy_operator_by_name" in adapter_code
    assert "operator_swap_op" in adapter_code
    assert "ComparisonTarget" in adapter_code
    assert "BooleanOperation" in adapter_code
    assert "BinaryOperation" in adapter_code


def test_full_and_lightweight_python_enforcement_share_mutmut_adapter():
    assert py_runner._shared_mutmut_adapter_command is lightweight_mutation.adapter_command


def test_mutmut_adapter_traverses_decorated_classes_but_delegates_decorated_functions():
    command = py_runner._mutmut_generate_metadata_command(
        "python3",
        max_children=1,
        policy_path=Path("/tmp/policy.json"),
    )
    adapter_code = command[2]

    assert "skip_node_and_children" in adapter_code
    assert "ClassDef" in adapter_code
    assert "isinstance(node, file_mutation.cst.FunctionDef)" not in adapter_code
    assert "MutationVisitor._skip_node_and_children = skip_node_and_children" in adapter_code


def test_large_source_cap_skips_in_process_ast_preplanning(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_generation_max_source_bytes", 10)

    def boom(*args, **kwargs):
        raise AssertionError("large source should not be parsed by the in-process preplanner")

    monkeypatch.setattr(mutation_policy, "collect_python_mutation_opportunities", boom)
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "pkg" / "huge.py"
    source.parent.mkdir()
    source.write_text("def run():\n" + "\n".join(f"    value_{i} = {i}" for i in range(20)), encoding="utf-8")

    policy = mutation_policy._write_mutmut_generation_policy(
        repo / ".uta_cache" / "python" / "mutation",
        source_file=source,
        source_path="pkg/huge.py",
        changed_lines={"pkg/huge.py": set(range(2, 22))},
        covered_lines=set(range(2, 22)),
        use_ci_cap_profile=False,
    )

    assert "failure" not in policy
    assert policy["truncated"] is True
    assert policy["preplanSkippedReason"] == "source_size_cap"
    assert policy["selectedLines"]
    assert len(policy["selectedLines"]) <= uta_settings.python_mutation_generation_max_changed_lines


def test_python3_candidate_plan_reads_uta_line_mapping_sidecar(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "worker.py").write_text(
        "def run(raw):\n"
        "    return raw.upper()\n",
        encoding="utf-8",
    )
    meta = repo / "mutants" / "pkg" / "worker.py.meta"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps(
            {
                "exit_code_by_key": {"pkg.worker.x_run__mutmut_1": 1},
                "hash_by_function_name": {"x_run": "abc"},
            }
        ),
        encoding="utf-8",
    )
    meta.with_suffix(meta.suffix + ".uta.json").write_text(
        json.dumps({"line_by_key": {"pkg.worker.x_run__mutmut_1": 2}}),
        encoding="utf-8",
    )

    plan = mutation_execution.build_mutmut3_candidate_plan_from_meta(
        repo,
        source_path="pkg/worker.py",
        target_id="pyfile:pkg/worker.py",
        changed_lines={"pkg/worker.py": {2}},
        covered_lines={2},
        selected_test_paths=("tests/test_worker.py",),
        mutmut_version="mutmut, version 3.3.1",
        runtime_fingerprint="python3",
        dependency_fingerprint="deps",
        config_fingerprint_value="cfg",
        mutmut_internal_api_fingerprint="mutmut3-meta-v3",
    )

    assert plan.exact_tool_candidate_keys == ("pkg.worker.x_run__mutmut_1",)


def test_python3_candidate_plan_fails_closed_when_metadata_lacks_line_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_candidate_plan_enabled", True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "worker.py").write_text(
        "def run(raw):\n"
        "    return raw.upper()\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut, version 3.3.1")
        if len(cmd) >= 3 and cmd[:2] == ["python3", "-c"] and cmd[-2] == "metadata":
            meta = repo / "mutants" / "pkg" / "worker.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps({"exit_code_by_key": {"pkg.worker.x_run__mutmut_1": 1}}),
                encoding="utf-8",
            )
            return _completed(cmd, stdout="UTA_MUTMUT_GENERATION_STATS mutated=1 unmodified=0 ignored=0\n")
        return _completed(cmd)

    result = verify_python_target(
        repo,
        default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py")),
        test_paths=["tests/test_worker.py"],
        syntax_version="python3",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
        changed_lines={"pkg/worker.py": {2}},
        run_command=fake_run,
    )

    assert result.status == "failed"
    assert result.reason_code == "mutation_candidate_plan_failed"
    assert result.mutation and result.mutation.candidate_plan["exactToolCandidateKeys"] == []


def test_python3_candidate_plan_passes_when_adapter_generates_no_mutants(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_candidate_plan_enabled", True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "models.py").write_text(
        "class Ticket:\n"
        "    id = models.AutoField(primary_key=True)\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/models.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut, version 3.5.0")
        if len(cmd) >= 3 and cmd[:2] == ["python3", "-c"] and cmd[-2] == "metadata":
            meta = repo / "mutants" / "pkg" / "models.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps(
                    {
                        "exit_code_by_key": {},
                        "durations_by_key": {},
                        "estimated_durations_by_key": {},
                        "type_check_error_by_key": {},
                    }
                ),
                encoding="utf-8",
            )
            return _completed(cmd, stdout="UTA_MUTMUT_GENERATION_STATS mutated=0 unmodified=1 ignored=0\n")
        if len(cmd) >= 3 and cmd[:2] == ["python3", "-c"] and cmd[-2] == "run":
            raise AssertionError("adapter run should be skipped when metadata has no generated mutants")
        return _completed(cmd)

    result = verify_python_target(
        repo,
        default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/models.py")),
        test_paths=["tests/test_models.py"],
        syntax_version="python3",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
        changed_lines={"pkg/models.py": {2}},
        run_command=fake_run,
    )

    assert result.status == "passed"
    assert result.reason_code == "passed"
    assert result.mutation
    assert result.mutation.generated == 0
    assert result.mutation.rate == 100.0
    assert result.mutation.candidate_plan["exactToolCandidateKeys"] == []
    assert result.mutation.candidate_plan["runMutants"] == 0


def test_mutmut_support_copy_paths_include_relative_import_sibling_package(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "src" / "shidiao" / "runners" / "windows_wechat_meituan_runner.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from ..parsers.meituan_parser import parse_responses\n"
        "\n"
        "def run(raw):\n"
        "    return parse_responses(raw)\n",
        encoding="utf-8",
    )
    parser = repo / "src" / "shidiao" / "parsers" / "meituan_parser.py"
    parser.parent.mkdir(parents=True)
    parser.write_text("def parse_responses(raw):\n    return raw\n", encoding="utf-8")
    runner_state = repo / "src" / "shidiao" / "runner_state.py"
    runner_state.write_text("STATE = {}\n", encoding="utf-8")
    source.write_text(
        "from ..parsers.meituan_parser import parse_responses\n"
        "from ..runner_state import STATE\n"
        "\n"
        "def run(raw):\n"
        "    return parse_responses(raw), STATE\n",
        encoding="utf-8",
    )
    for package in [
        repo / "src" / "__init__.py",
        repo / "src" / "shidiao" / "__init__.py",
        repo / "src" / "shidiao" / "runners" / "__init__.py",
        repo / "src" / "shidiao" / "parsers" / "__init__.py",
    ]:
        package.write_text("", encoding="utf-8")

    support_paths = py_runner._mutmut_support_copy_paths(
        repo,
        "src/shidiao/runners/windows_wechat_meituan_runner.py",
        test_paths=("tests/uta_generated/test_windows_runner.py",),
    )

    assert "src/shidiao/parsers/meituan_parser.py" in support_paths
    assert "src/shidiao/parsers" not in support_paths
    assert "src/shidiao/runner_state.py" in support_paths
    assert "src/shidiao/runners" not in support_paths


def test_python3_candidate_plan_fails_closed_when_planner_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(uta_settings, "python_mutation_candidate_plan_enabled", True)
    monkeypatch.setattr(uta_settings, "python_mutation_generation_strategy", "hard_cap")

    def boom(*args, **kwargs):
        raise RuntimeError("metadata shape changed")

    monkeypatch.setattr(mutation_execution, "build_mutmut3_candidate_plan_from_meta", boom)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "worker.py").write_text(
        "def run(raw):\n"
        "    return raw.upper()\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut, version 3.3.1")
        if cmd[:2] == ["mutmut", "run"]:
            meta = repo / "mutants" / "pkg" / "worker.py.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(
                json.dumps(
                    {
                        "exit_code_by_key": {"pkg.worker.x_run__mutmut_1": 1},
                        "line_by_key": {"pkg.worker.x_run__mutmut_1": 2},
                    }
                ),
                encoding="utf-8",
            )
            return _completed(cmd, stdout="1/1  🎉 1  ⏰ 0  🤔 0  🙁 0\n")
        return _completed(cmd)

    result = verify_python_target(
        repo,
        default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py")),
        test_paths=["tests/test_worker.py"],
        syntax_version="python3",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
        changed_lines={"pkg/worker.py": {2}},
        run_command=fake_run,
    )

    assert result.status == "failed"
    assert result.reason_code == "mutation_candidate_plan_failed"
    assert result.mutation and result.mutation.candidate_plan["planningError"] == "metadata shape changed"


def test_python_mutation_filter_excludes_low_value_side_effect_changed_lines(tmp_path):
    source = tmp_path / "injection_detector.py"
    source.write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "def build(raw):\n"
        "    try:\n"
        "        return raw.upper()\n"
        "    except ValueError as exc:\n"
        "        logger.warning('invalid pattern %r: %s', raw, exc)\n"
        "        metrics.increment('invalid_pattern')\n"
        "        _emit_audit('invalid_pattern', raw)\n"
        "        return metrics.value\n",
        encoding="utf-8",
    )

    filtered = _filter_changed_lines_for_mutation(
        source,
        {"pkg/injection_detector.py": {6, 8, 9, 10, 11}},
        "pkg/injection_detector.py",
    )

    assert filtered == {"pkg/injection_detector.py": {6, 11}}


def test_verify_python_target_skips_mutmut_when_only_low_value_side_effect_lines_changed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "worker.py").write_text(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "def run(raw):\n"
        "    logger.warning('invalid %s', raw)\n"
        "    return raw.upper()\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    commands = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        commands.append(cmd)
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pkg/worker.py'><lines>"
                "<line number='5' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut 3.0.0")
        if cmd[:2] == ["mutmut", "run"]:
            raise AssertionError("mutmut run should be skipped for low-value side-effect-only changes")
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="pkg/worker.py"))
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_pkg_worker.py"],
        coverage_gate=100.0,
        mutation_gate=100.0,
        changed_lines={"pkg/worker.py": {5}},
        run_command=fake_run,
    )

    assert result.status == "passed"
    assert result.mutation.scope == "changed_lines"
    assert result.mutation.changed_line_mutants_scored == 0
    assert not any(cmd[:2] == ["mutmut", "run"] for cmd in commands)


def test_subprocess_runner_decodes_legacy_tool_bytes_with_replacement(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    script = repo / "emit_bytes.py"
    script.write_text("import sys\nsys.stdout.buffer.write(b'bad: ' + bytes([0xe2]))\n", encoding="utf-8")

    from uta.language.python.verification.runner import _subprocess_run

    result = _subprocess_run(["python3", str(script)], cwd=repo, timeout=30)

    assert result.returncode == 0
    assert "bad:" in result.stdout


def test_mutmut_import_compat_normalizes_file_based_target_loaders(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="jobs/forecast.py",
    )

    script = tmp_path / "probe.py"
    script.write_text(
        """
import importlib.machinery
import importlib.util
import json
import runpy
import sys

path = r'''{source}'''
spec = importlib.util.spec_from_file_location("alias_spec", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
loaded = importlib.machinery.SourceFileLoader("alias_loader", path).load_module()
run_path_globals = runpy.run_path(path)
print(json.dumps([
    module.__name__,
    module.answer.__module__,
    sys.modules["jobs.forecast"] is module,
    sys.modules["alias_spec"] is module,
    loaded.__name__,
    loaded.answer.__module__,
    sys.modules["alias_loader"] is loaded,
    run_path_globals["answer"].__module__,
]))
""".format(source=source.as_posix()),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{repo.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "jobs/forecast.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "jobs.forecast",
    }
    result = subprocess.run(["python3", str(script)], env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        '["jobs.forecast", "jobs.forecast", true, true, "jobs.forecast", "jobs.forecast", true, "jobs.forecast"]'
    )


def test_mutmut_import_compat_reuses_generated_ast_within_active_mutant_only(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="jobs/forecast.py",
    )
    script = tmp_path / "probe_ast_cache.py"
    script.write_text(
        """
import ast
import json
import os

original = ast.parse
materializations = []

def repository_compat_parse(source, *args, **kwargs):
    tree = original(source, *args, **kwargs)
    if any(isinstance(node, ast.FunctionDef) and node.name == "_mutmut_trampoline" for node in tree.body):
        materializations.append(os.environ["MUTANT_UNDER_TEST"])
        tree.body = [node for node in tree.body if node.name != "_mutmut_trampoline"]
    return tree

ast.parse = repository_compat_parse
generated = "def _mutmut_trampoline():\\n    return 1\\n"
os.environ["MUTANT_UNDER_TEST"] = "stats"
first = ast.parse(generated)
second = ast.parse(generated)
os.environ["MUTANT_UNDER_TEST"] = "pkg.answer__mutmut_1"
third = ast.parse(generated)
fourth = ast.parse(generated)
print(json.dumps([first is second, second is third, third is fourth, materializations]))
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{repo.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "jobs/forecast.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "jobs.forecast",
        "UTA_MUTMUT_REPO_ROOT": repo.as_posix(),
    }

    result = subprocess.run(["python3", str(script)], env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, False, True, ["stats", "pkg.answer__mutmut_1"]]


def test_mutmut_import_compat_aliases_repo_root_mutants_package_without_root_side_effects(tmp_path):
    repo = tmp_path / "react_agent"
    source = repo / "src" / "app.py"
    mutants_dir = repo / "mutants"
    source.parent.mkdir(parents=True)
    (repo / "__init__.py").write_text("import missing_runtime_dependency\n", encoding="utf-8")
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="src/app.py",
    )

    script = tmp_path / "probe_repo_root_mutants.py"
    script.write_text(
        """
import importlib
import json
from pathlib import Path

Path(r'''{mutants_dir}''').mkdir(parents=True, exist_ok=True)
Path(r'''{mutants_dir}''' + "/__init__.py").write_text("")
package = importlib.import_module("react_agent")
mutants = importlib.import_module("react_agent.mutants")
print(json.dumps([
    package.__file__,
    list(package.__path__),
    mutants.__file__,
    list(mutants.__path__),
]))
""".format(mutants_dir=mutants_dir.as_posix()),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{repo.as_posix()}:{repo.parent.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "src/app.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "src.app",
    }

    result = subprocess.run(["python3", str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        (repo / "__init__.py").as_posix(),
        [repo.as_posix()],
        (mutants_dir / "__init__.py").as_posix(),
        [mutants_dir.as_posix()],
    ]


def test_mutmut_import_compat_normalizes_src_trampoline_hits(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "src" / "app.py"
    fake_mutmut = tmp_path / "fake_mutmut"
    source.parent.mkdir(parents=True)
    (fake_mutmut / "mutmut").mkdir(parents=True)
    (fake_mutmut / "mutmut" / "__init__.py").write_text("", encoding="utf-8")
    (fake_mutmut / "mutmut" / "__main__.py").write_text(
        "hits = []\n"
        "def record_trampoline_hit(name):\n"
        "    if name.startswith('src.'):\n"
        "        raise AssertionError('src prefix not normalized')\n"
        "    hits.append(name)\n",
        encoding="utf-8",
    )
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="src/app.py",
    )

    script = tmp_path / "probe_src_trampoline.py"
    script.write_text(
        """
import json
import mutmut.__main__ as mutmut_main

mutmut_main.record_trampoline_hit("src.shidiao.runner.run")
print(json.dumps(mutmut_main.hits))
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{fake_mutmut.as_posix()}:{repo.as_posix()}:{repo.parent.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "src/app.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "src.app",
        "UTA_MUTMUT_REPO_ROOT": repo.as_posix(),
    }

    result = subprocess.run(["python3", str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["shidiao.runner.run"]


def test_mutmut_import_compat_does_not_alias_mutmut_cli_for_main_target(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "__main__.py"
    fake_mutmut = tmp_path / "fake_mutmut"
    marker = tmp_path / "mutmut-main-name.txt"
    repo.mkdir()
    (fake_mutmut / "mutmut").mkdir(parents=True)
    (fake_mutmut / "mutmut" / "__init__.py").write_text("", encoding="utf-8")
    (fake_mutmut / "mutmut" / "__main__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(__name__)\n"
        "def record_trampoline_hit(name):\n"
        "    return name\n",
        encoding="utf-8",
    )
    source.write_text("def main():\n    return 0\n", encoding="utf-8")
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="__main__.py",
    )
    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{fake_mutmut.as_posix()}:{repo.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "__main__.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "__main__",
        "UTA_MUTMUT_REPO_ROOT": repo.as_posix(),
    }

    result = subprocess.run(
        ["python3", "-c", "print('ready')"], cwd=repo, env=env,
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "mutmut.__main__"


def test_mutmut_import_compat_keeps_real_src_package_module(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "src" / "app_shidiao.py"
    source.parent.mkdir(parents=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "log_config.py").write_text("VALUE = 42\n", encoding="utf-8")
    source.write_text("from .log_config import VALUE\n\ndef answer():\n    return VALUE\n", encoding="utf-8")
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="src/app_shidiao.py",
    )

    script = tmp_path / "probe_src_package.py"
    script.write_text(
        """
import importlib
import importlib.util
import json
import sys

path = r'''{source}'''
mod = importlib.import_module("src.app_shidiao")
spec = importlib.util.spec_from_file_location("alias_spec", path)
loaded = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loaded)
print(json.dumps([mod.__name__, mod.answer.__module__, loaded.__name__, loaded.answer.__module__]))
""".format(source=source.as_posix()),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{repo.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "src/app_shidiao.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "src.app_shidiao",
    }

    result = subprocess.run(["python3", str(script)], env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '["src.app_shidiao", "src.app_shidiao", "src.app_shidiao", "src.app_shidiao"]'


def test_mutmut_import_compat_redirects_file_loaders_to_mutant_copy(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    mutant_source = repo / "mutants" / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    mutant_source.parent.mkdir(parents=True)
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    mutant_source.write_text("def answer():\n    return 99\n", encoding="utf-8")
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="jobs/forecast.py",
    )

    script = tmp_path / "probe_mutant.py"
    script.write_text(
        """
import importlib.machinery
import importlib.util
import importlib
import json
import runpy
import sys

path = r'''{source}'''
imported = importlib.import_module("jobs.forecast")
spec = importlib.util.spec_from_file_location("alias_spec", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
loaded = importlib.machinery.SourceFileLoader("alias_loader", path).load_module()
run_path_globals = runpy.run_path(path)
print(json.dumps([
    imported.__name__,
    imported.answer(),
    module.__name__,
    module.answer(),
    sys.modules["jobs.forecast"] is module,
    loaded.__name__,
    loaded.answer(),
    run_path_globals["answer"](),
    run_path_globals["answer"].__module__,
]))
""".format(source=source.as_posix()),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{repo.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "jobs/forecast.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "jobs.forecast",
        "MUTANT_UNDER_TEST": "jobs.forecast.x_answer__mutmut_1",
    }
    result = subprocess.run(["python3", str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        '["jobs.forecast", 99, "jobs.forecast", 99, true, "jobs.forecast", 99, 99, "jobs.forecast"]'
    )

    env["MUTANT_UNDER_TEST"] = "stats"
    result = subprocess.run(["python3", str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        '["jobs.forecast", 42, "jobs.forecast", 42, true, "jobs.forecast", 42, 42, "jobs.forecast"]'
    )


def test_mutmut_import_compat_mirrors_repo_resource_dirs_for_mutant_imports(tmp_path):
    repo = tmp_path / "repo"
    real_module = repo / "sdk" / "util" / "properties.py"
    mutant_module = repo / "mutants" / "sdk" / "util" / "properties.py"
    resources = repo / "resources.beta"
    real_module.parent.mkdir(parents=True)
    mutant_module.parent.mkdir(parents=True)
    resources.mkdir()
    for package in (
        repo / "sdk" / "__init__.py",
        repo / "sdk" / "util" / "__init__.py",
        repo / "mutants" / "sdk" / "__init__.py",
        repo / "mutants" / "sdk" / "util" / "__init__.py",
    ):
        package.write_text("", encoding="utf-8")
    (resources / "config.properties").write_text("answer=42\n", encoding="utf-8")
    module_body = (
        "import os\n"
        "root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "config_path = os.path.join(root_dir, 'resources.beta', 'config.properties')\n"
        "with open(config_path, 'r', encoding='utf-8') as handle:\n"
        "    VALUE = handle.read().strip()\n"
    )
    real_module.write_text(module_body, encoding="utf-8")
    mutant_module.write_text(module_body, encoding="utf-8")
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="common/utils.py",
    )

    script = tmp_path / "probe_mutant_resources.py"
    script.write_text(
        """
import importlib
import json

mod = importlib.import_module("sdk.util.properties")
print(json.dumps([mod.VALUE, mod.__file__]))
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{(repo / 'mutants').as_posix()}:{repo.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "common/utils.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "common.utils",
        "MUTANT_UNDER_TEST": "common.utils.x_to_int_display_value__mutmut_1",
    }

    result = subprocess.run(["python3", str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["answer=42", mutant_module.as_posix()]


def test_mutmut_import_compat_extends_mutant_package_to_real_siblings(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "chat_robot" / "models.py"
    service = repo / "chat_robot" / "service" / "prompt_optimization" / "operator_plan.py"
    mutant_source = repo / "mutants" / "chat_robot" / "models.py"
    source.parent.mkdir(parents=True)
    service.parent.mkdir(parents=True)
    mutant_source.parent.mkdir(parents=True)
    (repo / "chat_robot" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "chat_robot" / "service" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "chat_robot" / "service" / "prompt_optimization" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "mutants" / "chat_robot" / "__init__.py").write_text("", encoding="utf-8")
    source.write_text("def answer():\n    return 'real'\n", encoding="utf-8")
    service.write_text("VALUE = 'real-sibling'\n", encoding="utf-8")
    mutant_source.write_text(
        "def answer():\n"
        "    from chat_robot.service.prompt_optimization.operator_plan import VALUE\n"
        "    return VALUE\n",
        encoding="utf-8",
    )
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="chat_robot/models.py",
    )

    script = tmp_path / "probe_mutant_package_shadow.py"
    script.write_text(
        """
import importlib
import json

mod = importlib.import_module("chat_robot.models")
package = importlib.import_module("chat_robot")
print(json.dumps([mod.answer(), list(package.__path__)]))
""",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{(repo / 'mutants').as_posix()}:{repo.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "chat_robot/models.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "chat_robot.models",
        "MUTANT_UNDER_TEST": "chat_robot.models.x_answer__mutmut_1",
    }
    result = subprocess.run(["python3", str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload[0] == "real-sibling"
    assert payload[1] == [
        (repo / "mutants" / "chat_robot").as_posix(),
        (repo / "chat_robot").as_posix(),
    ]


def test_mutmut_import_compat_redirects_src_package_imports_to_stripped_mutant_copy(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "src" / "shidiao" / "app.py"
    mutant_source = repo / "mutants" / "shidiao" / "app.py"
    source.parent.mkdir(parents=True)
    mutant_source.parent.mkdir(parents=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "shidiao" / "__init__.py").write_text("", encoding="utf-8")
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    mutant_source.write_text("def answer():\n    return 99\n", encoding="utf-8")
    compat_dir = _write_mutmut_import_compat(
        repo / ".uta_cache" / "python" / "mutation",
        repo=repo,
        source_path="src/shidiao/app.py",
    )

    script = tmp_path / "probe_src_stripped_mutant.py"
    script.write_text(
        """
import importlib
import json

mod = importlib.import_module("src.shidiao.app")
print(json.dumps([mod.__name__, mod.__file__, mod.answer()]))
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PYTHONPATH": f"{compat_dir.as_posix()}:{repo.as_posix()}:{repo.parent.as_posix()}",
        "UTA_MUTMUT_TARGET_REL": "src/shidiao/app.py",
        "UTA_MUTMUT_CANONICAL_MODULE": "src.shidiao.app",
        "MUTANT_UNDER_TEST": "shidiao.app.x_answer__mutmut_1",
    }

    result = subprocess.run(["python3", str(script)], cwd=repo, env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload[0] == "src.shidiao.app"
    assert payload[1].endswith("/mutants/shidiao/app.py")
    assert payload[2] == 99


def test_verify_python_target_selects_python2_legacy_mutmut_lane(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    (repo / "jobs" / "legacy_job.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if cmd[:2] == ["/opt/python2/bin/python", "--version"]:
            return _completed(cmd, stdout="Python 2.7.18")
        if cmd[:3] == ["/opt/python2/bin/python", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 4.6.11")
        if cmd[:3] == ["/opt/python2/bin/python", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 5.5")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "/opt/python2/bin/python"):
            return _completed(cmd)
        if cmd[:4] == ["/opt/python2/bin/python", "-m", "coverage", "xml"]:
            assert "-i" in cmd
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/legacy_job.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["/opt/python2/bin/mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut 1.5.0")
        if cmd[:2] == ["/opt/python2/bin/mutmut", "run"]:
            return _completed(cmd, stdout="2 generated, 2 killed, 0 survived")
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/legacy_job.py")
    )
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_legacy_job.py"],
        syntax_version="python2",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(
            python2_bin="/opt/python2/bin/python",
            python2_mutmut_bin="/opt/python2/bin/mutmut",
        ),
        run_command=fake_run,
    )

    assert result.status == "passed"
    assert result.mutation.runtime_lane == "mutmut-legacy-py2"
    assert ["/opt/python2/bin/mutmut", "run"] in [cmd[:2] for cmd in calls]


def test_verify_python_target_skips_python2_only_target_in_python3_project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    (repo / "jobs" / "legacy_only.py").write_text(
        "store_sku_extend_df = None\n"
        "if __name__ == '__main__':\n"
        "    global store_sku_extend_df\n"
        "    store_sku_extend_df = 1\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if cmd[:3] == ["python3", "-m", "py_compile"]:
            return _completed(
                cmd,
                returncode=1,
                stderr="SyntaxError: name 'store_sku_extend_df' is assigned to before global declaration",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/legacy_only.py")
    )
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_legacy_only.py"],
        syntax_version="python3",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(python_bin="python3", mutmut_bin="mutmut"),
        run_command=fake_run,
    )

    assert result.status == "passed"
    assert result.reason_code == "python_runtime_incompatible_skipped"
    assert result.tests_pass is True
    assert result.coverage.scope == "runtime_incompatible"
    assert result.mutation.scope == "runtime_incompatible"
    assert [command.name for command in result.commands] == [
        # This repository declares no requirements at all, so the absent overlay
        # is recorded rather than left for a later ModuleNotFoundError to imply.
        "dependency_overlay_skipped",
        "python_version",
        "pytest_version",
        "coverage_version",
        "python_target_py_compile",
    ]
    assert not any(_is_coverage_run(cmd, "python3") for cmd in calls)


def test_verify_python_target_skips_python3_only_target_in_python2_project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    (repo / "jobs" / "modern_only.py").write_text(
        "def render(value):\n"
        "    return f'value={value}'\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if cmd[:2] == ["/opt/python2/bin/python", "--version"]:
            return _completed(cmd, stdout="Python 2.7.18")
        if cmd[:3] == ["/opt/python2/bin/python", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 4.6.11")
        if cmd[:3] == ["/opt/python2/bin/python", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 5.5")
        if cmd[:3] == ["/opt/python2/bin/python", "-m", "py_compile"]:
            return _completed(cmd, returncode=1, stderr="SyntaxError: invalid syntax")
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/modern_only.py")
    )
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_modern_only.py"],
        syntax_version="python2",
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=PythonRuntimeConfig(
            python2_bin="/opt/python2/bin/python",
            python2_mutmut_bin="/opt/python2/bin/mutmut",
        ),
        run_command=fake_run,
    )

    assert result.status == "passed"
    assert result.reason_code == "python_runtime_incompatible_skipped"
    assert result.tests_pass is True
    assert result.coverage.scope == "runtime_incompatible"
    assert result.mutation.runtime_lane == "mutmut-legacy-py2"
    assert result.mutation.scope == "runtime_incompatible"
    assert [command.name for command in result.commands] == [
        "python_version",
        "pytest_version",
        "coverage_version",
        "python_target_py_compile",
    ]
    assert not any(_is_coverage_run(cmd, "/opt/python2/bin/python") for cmd in calls)


def test_verify_python_target_reports_missing_mutmut(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            assert "-i" in cmd
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "--version"]:
            return _completed(cmd, returncode=127, stderr="mutmut: command not found")
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py")
    )
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_forecast.py"],
        coverage_gate=100.0,
        mutation_gate=100.0,
        run_command=fake_run,
    )

    assert result.status == "failed"
    assert result.reason_code == "missing_mutmut"
    assert result.mutation is None
    coverage_run = next(cmd for cmd in calls if _is_coverage_run(cmd, "python3"))
    coverage_xml_cmd = next(cmd for cmd in calls if cmd[:4] == ["python3", "-m", "coverage", "xml"])
    assert "--source" not in coverage_run
    assert "--source" not in coverage_xml_cmd
    assert coverage_run[coverage_run.index("--coverage-include") + 1] == "jobs/forecast.py,*/jobs/forecast.py"
    assert any(part.startswith("--include=jobs/forecast.py,*/jobs/forecast.py") for part in coverage_xml_cmd)
    assert "mutants/*" in coverage_run[coverage_run.index("--coverage-omit") + 1]
    assert any(part.startswith("--omit=") and "mutants/*" in part for part in coverage_xml_cmd)


def test_mutmut_metadata_adapter_uses_mutation_module_fallback():
    command = py_runner._mutmut_generate_metadata_command(
        "python3",
        max_children=2,
        policy_path=Path("/tmp/policy.json"),
    )

    script = command[2]
    assert "importlib.import_module(module_name)" in script
    assert '"mutmut.file_mutation"' in script
    assert '"mutmut.node_mutation"' in script
    assert '"mutmut.mutation.file_mutation"' in script
    assert 'hasattr(mutmut_main, "ensure_config_loaded")' in script
    assert "result = original_arrangement(function, materialized, class_name)" in script
    assert "return result" in script
    assert "file_mutation = _mutation_module()" in script


def test_python3_verify_rejects_mutmut_before_version_3(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "--version"]:
            return _completed(cmd, returncode=2, stderr="Error: No such option: --version")
        if cmd[:2] == ["mutmut", "version"]:
            return _completed(cmd, stdout="mutmut version 2.4.4")
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="jobs/forecast.py"))
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_forecast.py"],
        coverage_gate=100.0,
        mutation_gate=100.0,
        run_command=fake_run,
    )

    assert result.status == "failed"
    assert result.reason_code == "unsupported_mutmut_version"
    assert ["mutmut", "--version"] in calls
    assert ["mutmut", "version"] in calls
    assert not any(cmd[:2] == ["mutmut", "run"] for cmd in calls)


def test_resolve_python_runtime_config_applies_precedence_and_fingerprints(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".uta").mkdir(parents=True)
    (repo / "requirements.txt").write_text("pytest\ncoverage\n", encoding="utf-8")
    (repo / ".uta" / "python-enforce.toml").write_text(
        'python_bin = "repo-python"\n'
        'mutmut_bin = "repo-mutmut"\n'
        'setup_command = "repo-bootstrap --fast"\n'
        'environment_profile = "repo-venv"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("UTA_PYTHON_BIN", "env-python")
    monkeypatch.setenv("UTA_PYTHON_MUTMUT_BIN", "env-mutmut")
    monkeypatch.setenv("UTA_PYTHON_GATE_TIMEOUT_SECONDS", "77")

    config = resolve_python_runtime_config(repo, overrides={"python_bin": "cli-python"})

    assert config.python_bin == "cli-python"
    assert config.mutmut_bin == "env-mutmut"
    assert config.setup_command == ("repo-bootstrap", "--fast")
    assert config.timeout_seconds == 77
    assert config.environment_profile == "repo-venv"
    assert config.config_sources["python_bin"] == "cli"
    assert config.config_sources["mutmut_bin"] == "env:UTA_PYTHON_MUTMUT_BIN"
    assert config.config_sources["setup_command"] == ".uta/python-enforce.toml"
    assert config.dependency_fingerprints["requirements.txt"]
    assert config.cache_key.startswith("python-env:")

    repo_only_config = resolve_python_runtime_config(repo, environ={})

    assert repo_only_config.python_bin == "repo-python"
    assert repo_only_config.mutmut_bin == "mutmut"
    assert repo_only_config.config_sources["python_bin"] == ".uta/python-enforce.toml"
    assert repo_only_config.config_sources["mutmut_bin"] == "default"


def test_ensure_uta_owned_mutmut_version_installs_pin_when_setup_changes_version(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    commands = []
    calls = []

    config = resolve_python_runtime_config(
        repo,
        environ={"UTA_PYTHON_BIN": "python3", "UTA_PYTHON_MUTMUT_BIN": "uta-mutmut"},
    )

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if cmd == ["uta-mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut, version 3.6.0")
        if cmd == ["python3", "-m", "pip", "install", "-q", "mutmut==3.5.0"]:
            return _completed(cmd, stdout="installed")
        raise AssertionError(f"unexpected command: {cmd}")

    result = runtime_setup._ensure_uta_owned_mutmut_version(
        "uta-mutmut",
        "python3",
        repo,
        30,
        fake_run,
        commands,
        config,
        "mutmut-modern",
    )

    assert result is None
    assert calls == [
        ["uta-mutmut", "--version"],
        ["python3", "-m", "pip", "install", "-q", "mutmut==3.5.0"],
    ]
    assert [command.name for command in commands] == ["mutmut_version_preflight", "mutmut_pin_install"]


def test_ensure_uta_owned_mutmut_version_skips_when_already_pinned(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    commands = []

    config = resolve_python_runtime_config(repo, environ={"UTA_PYTHON_MUTMUT_BIN": "uta-mutmut"})

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        assert cmd == ["uta-mutmut", "--version"]
        return _completed(cmd, stdout="mutmut, version 3.5.0")

    result = runtime_setup._ensure_uta_owned_mutmut_version(
        "uta-mutmut",
        "python3",
        repo,
        30,
        fake_run,
        commands,
        config,
        "mutmut-modern",
    )

    assert result is None
    assert [command.name for command in commands] == ["mutmut_version_preflight"]


def test_verify_python_target_records_setup_and_dependency_evidence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd == ["bootstrap", "deps"]:
            return _completed(cmd, stdout="setup ok")
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            assert "-i" in cmd
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="jobs/forecast.py"))
    config = resolve_python_runtime_config(
        repo,
        overrides={"setup_command": "bootstrap deps", "environment_profile": "ci-prepared"},
    )

    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_forecast.py"],
        coverage_gate=100.0,
        mutation_gate=0.0,
        config=config,
        run_command=fake_run,
    )

    assert result.status == "passed"
    assert result.setup_status == "executed"
    assert result.environment_profile == "ci-prepared"
    assert result.dependency_fingerprints["requirements.txt"]
    assert result.cache_key == config.cache_key
    assert result.commands[0].name == "setup"


def test_verify_python_target_installs_nearest_requirements_manifest_in_isolated_overlay(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pipecat" / "api").mkdir(parents=True)
    (repo / "pipecat" / "router").mkdir(parents=True)
    (repo / "pipecat" / "tests").mkdir(parents=True)
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    (repo / "pipecat" / "requirements.txt").write_text(
        "pipecat-ai>=0.0.47\nfastapi>=0.110\nloguru>=0.7\nredis[asyncio]>=5.0\n",
        encoding="utf-8",
    )
    (repo / "pipecat" / "router" / "session_store.py").write_text(
        "from redis.asyncio import Redis\n",
        encoding="utf-8",
    )
    (repo / "pipecat" / "api" / "app.py").write_text(
        "from fastapi import FastAPI\nfrom loguru import logger\nfrom router.session_store import Redis\n\ndef run():\n    logger.info('run')\n    return 1\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append((list(cmd), dict(env or {})))
        if cmd[:2] == ["python3", "-c"]:
            return _completed(cmd, stdout='["loguru", "redis.asyncio"]')
        if cmd[:5] == ["python3", "-m", "pip", "install", "--disable-pip-version-check"]:
            return _completed(cmd, stdout="installed")
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            assert ".uta_cache/python/dependencies/" in env["PYTHONPATH"]
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pipecat/api/app.py'><lines>"
                "<line number='1' hits='1'/><line number='3' hits='1'/>"
                "<line number='4' hits='1'/><line number='5' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="pipecat/api/app.py")
    )
    result = verify_python_target(
        repo,
        target,
        test_paths=["pipecat/tests/test_app.py"],
        coverage_gate=100.0,
        mutation_gate=0.0,
        run_mutation=False,
        run_command=fake_run,
    )

    assert result.status == "passed"
    assert result.setup_status == "executed"
    assert "pipecat/requirements.txt" in result.dependency_fingerprints
    install = next(cmd for cmd, _ in calls if cmd[:3] == ["python3", "-m", "pip"])
    assert install[-2] == "-r"
    # Pip resolves the whole manifest, extras included; UTA selects nothing.
    assert Path(install[-1]).read_text(encoding="utf-8").splitlines() == [
        "pipecat-ai>=0.0.47",
        "fastapi>=0.110",
        "loguru>=0.7",
        "redis[asyncio]>=5.0",
    ]
    assert "--no-deps" not in install
    assert "--index-url" not in install
    assert "--target" in install


def test_verify_python_target_uses_compat_overlay_after_legacy_manifest_failure(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("import numpy\ndef run():\n    return numpy.array([1])\n", encoding="utf-8")
    test_file = repo / "tests" / "test_forecast.py"
    test_file.parent.mkdir()
    test_file.write_text("import numpy\n", encoding="utf-8")
    (repo / "requirements.txt").write_text(
        "absl-py==0.9.0\nnumpy==1.18.1\nlegacy-unrelated==1.0\n",
        encoding="utf-8",
    )
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:3] == ["python3", "-m", "pip"]:
            if "-r" in cmd:
                return _completed(cmd, returncode=1, stderr="Failed to build 'absl-py'")
            assert cmd[-1] == "numpy"
            return _completed(cmd, stdout="installed compatible numpy")
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8")
        if cmd[:4] == ["python3", "-m", "coverage", "--version"]:
            return _completed(cmd, stdout="Coverage.py 7")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            assert ".uta_cache/python/dependencies/" in env["PYTHONPATH"]
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='1'/><line number='3' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py")
    )
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/test_forecast.py"],
        coverage_gate=100.0,
        mutation_gate=0.0,
        run_mutation=False,
        run_command=fake_run,
    )

    assert result.status == "passed"
    assert result.setup_status == "compat"
    assert any(command.name == "dependency_overlay_compat_install" for command in result.commands)


def test_dependency_overlay_uses_final_fallback_runtime(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "pipecat" / "api" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("import pydantic_core\nVALUE = 1\n", encoding="utf-8")
    (repo / "pipecat" / "requirements.txt").write_text(
        "pydantic-core>=2.0\n",
        encoding="utf-8",
    )
    test_file = repo / "tests" / "test_app.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_app(): assert True\n", encoding="utf-8")
    service_python = "/service/python3.11"
    monkeypatch.setenv("UTA_SERVICE_PYTHON_BIN", service_python)
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.9.25")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8")
        if cmd[:3] == ["python3", "-m", "coverage"]:
            return _completed(cmd, returncode=1, stderr="No module named coverage")
        if cmd[:2] == [service_python, "--version"]:
            return _completed(cmd, stdout="Python 3.11.15")
        if cmd[:3] == [service_python, "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8")
        if cmd[:4] == [service_python, "-m", "coverage", "--version"]:
            return _completed(cmd, stdout="Coverage.py 7")
        if cmd[:2] == [service_python, "-c"]:
            return _completed(cmd, stdout='["pydantic_core"]')
        if cmd[:3] == [service_python, "-m", "pip"]:
            return _completed(cmd, stdout="installed cp311 overlay")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, service_python):
            return _completed(cmd)
        if cmd[:4] == [service_python, "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='pipecat/api/app.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="pipecat/api/app.py")
    )
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/test_app.py"],
        coverage_gate=100.0,
        mutation_gate=0.0,
        run_mutation=False,
        run_command=fake_run,
    )

    assert result.status == "passed"
    install = next(cmd for cmd in calls if cmd[1:3] == ["-m", "pip"])
    assert install[0] == service_python
    coverage_run = next(cmd for cmd in calls if _is_coverage_run(cmd, cmd[0]))
    assert coverage_run[0] == service_python


def test_coverage_failure_reports_pytest_error_before_secondary_warning(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_file = repo / "tests" / "test_forecast.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_value(): assert True\n", encoding="utf-8")

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8")
        if cmd[:4] == ["python3", "-m", "coverage", "--version"]:
            return _completed(cmd, stdout="Coverage.py 7")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(
                cmd,
                returncode=2,
                stdout="E ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'",
                stderr="CoverageWarning: No data was collected.",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py")
    )
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/test_forecast.py"],
        run_mutation=False,
        run_command=fake_run,
    )

    assert result.reason_code == "test_failed"
    assert "ModuleNotFoundError" in result.message
    assert "CoverageWarning" in result.message


def test_dependency_overlay_runner_preserves_target_pytest_pythonpath():
    captured = {}

    def base_runner(cmd, cwd=None, timeout=None, env=None):
        captured.update(env or {})
        return _completed(cmd)

    wrapped = py_runner._runner_with_env(base_runner, {"PYTHONPATH": "/deps"})
    wrapped(["pytest"], env={"PYTHONPATH": "/repo/pipecat:/repo"})

    assert captured["PYTHONPATH"] == f"/deps{os.pathsep}/repo/pipecat:/repo"


def test_nested_pytest_context_precedes_repo_root_without_root_package(tmp_path):
    repo = tmp_path / "repo"
    test_file = repo / "pipecat" / "tests" / "test_app.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_app(): pass\n", encoding="utf-8")

    context = py_runner._prepare_pytest_execution_context(repo, ["pipecat/tests/test_app.py"])
    pythonpath = context.env_overrides["PYTHONPATH"].split(os.pathsep)

    assert pythonpath.index((repo / "pipecat").as_posix()) < pythonpath.index(repo.as_posix())
    plugin_roots = context.env_overrides["UTA_PYTEST_IMPORT_ROOTS"].split(os.pathsep)
    assert (repo / "pipecat").as_posix() in plugin_roots
    assert context.env_overrides["PYTEST_PLUGINS"].split(",") == [
        "uta_py_enforce.pytest_import_roots_plugin",
        "uta_py_enforce.optional_plugins",
    ]


def test_isolated_pytest_rewrites_named_repo_root_constant(tmp_path):
    repo = tmp_path / "repo"
    test_file = repo / "tests" / "uta_generated" / "test_router.py"
    test_file.parent.mkdir(parents=True)
    (repo / "__init__.py").write_text("", encoding="utf-8")
    test_file.write_text(
        "from pathlib import Path\n"
        "REPO_ROOT = Path(__file__).resolve().parents[2]\n",
        encoding="utf-8",
    )

    context = py_runner._prepare_pytest_execution_context(
        repo,
        ["tests/uta_generated/test_router.py"],
    )

    isolated_source = Path(context.test_paths[0]).read_text(encoding="utf-8")
    assert 'REPO_ROOT = Path(__import__("os").environ.get("UTA_REPO_ROOT"' in isolated_source


def test_pytest_context_includes_source_package_root_for_top_level_tests(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pipecat" / "api" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("from wechatlet_ai import access_guard\n", encoding="utf-8")
    (repo / "pipecat" / "wechatlet_ai").mkdir()
    (repo / "pipecat" / "wechatlet_ai" / "access_guard.py").write_text("", encoding="utf-8")
    test_file = repo / "tests" / "uta_generated" / "test_pipecat_api_app.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_app(): pass\n", encoding="utf-8")

    context = py_runner._prepare_pytest_execution_context(
        repo,
        ["tests/uta_generated/test_pipecat_api_app.py"],
        source_path="pipecat/api/app.py",
    )

    pythonpath = context.env_overrides["PYTHONPATH"].split(os.pathsep)
    assert (repo / "pipecat").as_posix() not in pythonpath
    assert (repo / "pipecat").as_posix() in context.env_overrides[
        "UTA_PYTEST_IMPORT_ROOTS"
    ].split(os.pathsep)
    assert context.env_overrides["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_pytest_context_loads_declared_async_plugin_without_global_autoload(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "service.py"
    source.write_text("async def run(): pass\n", encoding="utf-8")
    test_file = repo / "tests" / "test_service.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "import pytest\n@pytest.mark.asyncio\nasync def test_run(): pass\n",
        encoding="utf-8",
    )

    context = py_runner._prepare_pytest_execution_context(
        repo,
        ["tests/test_service.py"],
        source_path="service.py",
    )

    plugins = context.env_overrides["PYTEST_PLUGINS"].split(",")
    assert "uta_py_enforce.optional_plugins" in plugins
    assert context.env_overrides["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_failed_verification_result_persists_concrete_error_fields():
    result = PythonVerificationResult(
        status="failed",
        reason_code="test_failed",
        message="ModuleNotFoundError: No module named 'wechatlet_ai.access_guard'",
    )

    fields = result.as_result_fields()

    assert fields["error"] == result.message
    assert fields["last_error"] == result.message


def test_dependency_overlay_rebuilds_invalid_cache_and_keys_by_runtime(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "service.py"
    source.parent.mkdir(parents=True)
    source.write_text("import loguru\n", encoding="utf-8")
    (repo / "pkg" / "requirements.txt").write_text("loguru>=0.7\n", encoding="utf-8")

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if "-c" in cmd:
            return _completed(cmd, stdout='["loguru"]')
        if cmd[:3] == ["python3", "-m", "pip"]:
            target = Path(cmd[cmd.index("--target") + 1])
            assert not (target / "stale-extension.so").exists()
            return _completed(cmd, stdout="installed")
        raise AssertionError(f"unexpected command: {cmd}")

    config_a = PythonRuntimeConfig(cache_key="python-env:runtime-a")
    _evidence, dependency_dir_a, _ = runtime_setup._prepare_nested_requirements_overlay(
        repo,
        "pkg/service.py",
        "python3",
        config_a,
        fake_run,
    )
    (dependency_dir_a / "stale-extension.so").write_text("old ABI", encoding="utf-8")
    (dependency_dir_a / ".uta-installed").write_text("invalid", encoding="utf-8")

    runtime_setup._prepare_nested_requirements_overlay(
        repo,
        "pkg/service.py",
        "python3",
        config_a,
        fake_run,
    )
    config_b = PythonRuntimeConfig(cache_key="python-env:runtime-b")
    _evidence, dependency_dir_b, _ = runtime_setup._prepare_nested_requirements_overlay(
        repo,
        "pkg/service.py",
        "python3",
        config_b,
        fake_run,
    )

    assert not (dependency_dir_a / "stale-extension.so").exists()
    assert dependency_dir_b != dependency_dir_a


def test_pytest_import_root_plugin_restores_nested_root_after_pytest_path_setup(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    nested = repo / "pipecat"
    nested.mkdir(parents=True)
    (repo / "main.py").write_text("VALUE = 'root'\n", encoding="utf-8")
    (nested / "main.py").write_text("VALUE = 'nested'\n", encoding="utf-8")
    monkeypatch.setenv("UTA_PYTEST_IMPORT_ROOTS", nested.as_posix())
    monkeypatch.syspath_prepend(repo.as_posix())

    from uta.language.python.verification.pytest_import_roots_plugin import _prioritize_pytest_import_roots

    _prioritize_pytest_import_roots()

    import importlib
    import sys

    sys.modules.pop("main", None)
    assert importlib.import_module("main").VALUE == "nested"


def test_pytest_import_root_plugin_reapplies_root_before_each_test(tmp_path, monkeypatch):
    from uta.language.python.verification.pytest_import_roots_plugin import pytest_runtest_setup

    repo = tmp_path / "repo"
    nested = repo / "pipecat"
    nested.mkdir(parents=True)
    monkeypatch.setenv("UTA_PYTEST_IMPORT_ROOTS", nested.as_posix())
    monkeypatch.syspath_prepend(nested.as_posix())
    monkeypatch.syspath_prepend(repo.as_posix())

    pytest_runtest_setup(None)

    assert os.path.realpath(sys.path[0]) == os.path.realpath(nested)


def test_verify_python_target_reports_missing_runtime_pytest_coverage_and_python2_mutmut(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    target = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="jobs/forecast.py"))

    def run_missing_python(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, returncode=127, stderr="python missing")
        raise AssertionError(f"unexpected command: {cmd}")

    assert verify_python_target(repo, target, test_paths=["tests/t.py"], run_command=run_missing_python).reason_code == "missing_python_runtime"

    def run_missing_pytest(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, returncode=1, stderr="No module named pytest")
        if cmd[1:] == ["--version"]:
            return _completed(cmd, stdout="Python 3.11")
        if cmd[1:] == ["-m", "pytest", "--version"]:
            return _completed(cmd, returncode=1, stderr="No module named pytest")
        raise AssertionError(f"unexpected command: {cmd}")

    assert verify_python_target(repo, target, test_paths=["tests/t.py"], run_command=run_missing_pytest).reason_code == "missing_pytest"

    def run_missing_coverage(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, returncode=1, stderr="No module named coverage")
        if cmd[1:] == ["-m", "coverage", "--version"]:
            return _completed(cmd, returncode=1, stderr="No module named coverage")
        raise AssertionError(f"unexpected command: {cmd}")

    assert verify_python_target(repo, target, test_paths=["tests/t.py"], run_command=run_missing_coverage).reason_code == "missing_coverage"

    def run_missing_python2(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python2", "--version"]:
            return _completed(cmd, returncode=127, stderr="python2 missing")
        raise AssertionError(f"unexpected command: {cmd}")

    assert verify_python_target(repo, target, test_paths=["tests/t.py"], syntax_version="python2", run_command=run_missing_python2).reason_code == "missing_python2_runtime"

    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def run_missing_python2_mutmut(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python2", "--version"]:
            return _completed(cmd, stdout="Python 2.7.18")
        if cmd[:3] == ["python2", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 4.6.11")
        if cmd[:3] == ["python2", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 5.5")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python2"):
            return _completed(cmd)
        if cmd[:4] == ["python2", "-m", "coverage", "xml"]:
            assert "-i" in cmd
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut 2.4.4")
        raise AssertionError(f"unexpected command: {cmd}")

    assert (
        verify_python_target(
            repo,
            target,
            test_paths=["tests/t.py"],
            syntax_version="python2",
            coverage_gate=100.0,
            run_command=run_missing_python2_mutmut,
        ).reason_code
        == "missing_python2_mutmut"
    )


def test_verify_python_target_uses_changed_line_coverage_and_skips_mutation_when_uncovered(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if _is_py_compile(cmd):
            return _completed(cmd)
        if _is_coverage_run(cmd, "python3"):
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='0'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="jobs/forecast.py"))
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_forecast.py"],
        coverage_gate=100.0,
        mutation_gate=100.0,
        changed_lines={"jobs/forecast.py": {2}},
        run_command=fake_run,
    )

    assert result.status == "failed"
    assert result.reason_code == "coverage_gate_failed"
    assert result.coverage.covered == 0
    assert result.coverage.total == 1
    assert result.coverage.uncovered_lines == {"jobs/forecast.py": [2]}
    assert "mutmut_run" not in [command.name for command in result.commands]


def test_verification_subprocess_kills_a_tree_over_the_memory_limit(monkeypatch):
    """Generated code that allocates without limit is killed, not left to the kernel.

    The CI lane has held this ceiling since it was written; generation and
    repair build their own runners and never saw it, so a model-written test
    that allocated unboundedly ran free. On a node with no swap the kernel
    answers that by OOM-killing whatever it likes, which is how three
    unrelated processes died in one hour.
    """
    monkeypatch.setattr(py_process, "_memory_limit_bytes", lambda: 256 * 1024 * 1024)
    # Stand in for the /proc walk so the test is not timing- or platform-bound.
    monkeypatch.setattr(
        "uta.enforcement.enforcement._linux_process_tree_rss_bytes",
        lambda pid: (512 * 1024 * 1024, {pid}),
    )

    result = py_process._subprocess_run(
        [sys.executable, "-c", "import time; time.sleep(60); print('REACHED END')"],
        timeout=120,
    )

    assert result.returncode == 137
    assert "UTA_RESOURCE_EXHAUSTED" in result.stderr
    assert "REACHED END" not in (result.stdout or "")


def test_verification_subprocess_leaves_a_command_under_the_limit_alone(monkeypatch):
    """The bound must not touch a command that stays inside it."""
    monkeypatch.setattr(py_process, "_memory_limit_bytes", lambda: 256 * 1024 * 1024)
    monkeypatch.setattr(
        "uta.enforcement.enforcement._linux_process_tree_rss_bytes",
        lambda pid: (1024 * 1024, {pid}),
    )

    result = py_process._subprocess_run([sys.executable, "-c", "print('ok')"], timeout=60)

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
    assert "UTA_RESOURCE_EXHAUSTED" not in (result.stderr or "")


def test_verification_memory_limit_can_be_disabled(monkeypatch):
    """0 disables the bound, for an operator who would rather risk the node."""
    monkeypatch.setattr(py_process, "_memory_limit_bytes", lambda: 0)
    polled = []
    monkeypatch.setattr(
        "uta.enforcement.enforcement._linux_process_tree_rss_bytes",
        lambda pid: polled.append(pid) or (512 * 1024 * 1024, {pid}),
    )

    result = py_process._subprocess_run([sys.executable, "-c", "print('ok')"], timeout=60)

    assert result.returncode == 0
    assert not polled, "guard polled despite being disabled"

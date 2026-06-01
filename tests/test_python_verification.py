import subprocess
from configparser import ConfigParser
from pathlib import Path

import pytest

from uta.engine.languages import RawTargetSelection, default_registry
from uta.language.python.verification.runner import (
    PythonRuntimeConfig,
    parse_coverage_xml,
    parse_mutmut_summary,
    resolve_python_runtime_config,
    verify_python_target,
)


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


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


def test_subprocess_runner_decodes_legacy_tool_bytes_with_replacement(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    script = repo / "emit_bytes.py"
    script.write_text("import sys\nsys.stdout.buffer.write(b'bad: ' + bytes([0xe2]))\n", encoding="utf-8")

    from uta.language.python.verification.runner import _subprocess_run

    result = _subprocess_run(["python3", str(script)], cwd=repo, timeout=30)

    assert result.returncode == 0
    assert "bad:" in result.stdout


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
        if cmd[:4] == ["/opt/python2/bin/python", "-m", "coverage", "run"]:
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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
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
    coverage_run = next(cmd for cmd in calls if cmd[:4] == ["python3", "-m", "coverage", "run"])
    coverage_xml_cmd = next(cmd for cmd in calls if cmd[:4] == ["python3", "-m", "coverage", "xml"])
    assert any(part.startswith("--omit=") and "mutants/*" in part for part in coverage_run)
    assert any(part.startswith("--omit=") and "mutants/*" in part for part in coverage_xml_cmd)


def test_verify_python_target_accepts_mutmut_version_subcommand(tmp_path):
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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
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
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(cmd, stdout="2 generated, 2 killed, 0 survived")
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

    assert result.status == "passed"
    assert ["mutmut", "--version"] in calls
    assert ["mutmut", "version"] in calls
    mutmut_run = next(cmd for cmd in calls if cmd[:2] == ["mutmut", "run"])
    assert mutmut_run[:4] == ["mutmut", "run", "--paths-to-mutate", "jobs/forecast.py"]
    assert "--tests-dir" in mutmut_run
    assert "tests/uta_generated" in mutmut_run
    assert "--runner" in mutmut_run
    runner = mutmut_run[mutmut_run.index("--runner") + 1]
    assert runner.startswith(f"PYTHONPATH={repo.as_posix()}:$PYTHONPATH ")
    assert "python3 -m pytest -x --assert=plain tests/uta_generated/test_jobs_forecast.py" in runner


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


def test_verify_python_target_falls_back_to_service_python_when_default_lacks_pytest(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "uta_generated").mkdir(parents=True)
    (repo / "tests" / "uta_generated" / "test_jobs_forecast.py").write_text(
        "def test_run():\n    assert True\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UTA_SERVICE_PYTHON_BIN", "service-python")
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if cmd == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.9")
        if cmd == ["python3", "-m", "pytest", "--version"]:
            return _completed(cmd, returncode=1, stderr="No module named pytest")
        if cmd == ["service-python", "--version"]:
            return _completed(cmd, stdout="Python 3.9")
        if cmd == ["service-python", "-m", "pytest", "--version"]:
            return _completed(cmd, stdout="pytest 8")
        if cmd == ["service-python", "-m", "coverage", "--version"]:
            return _completed(cmd, stdout="Coverage.py")
        if cmd[:4] == ["service-python", "-m", "coverage", "run"]:
            return _completed(cmd)
        if cmd[:4] == ["service-python", "-m", "coverage", "xml"]:
            xml_path = Path(cmd[-1])
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            xml_path.write_text(
                """<?xml version="1.0" ?>
<coverage><packages><package name="jobs"><classes><class filename="jobs/forecast.py"><lines>
<line number="1" hits="1"/><line number="2" hits="1"/>
</lines></class></classes></package></packages></coverage>
""",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut version 2.4.4")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(cmd, stdout="2 generated, 2 killed, 0 survived")
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

    assert result.status == "passed"
    assert ["pytest_version_fallback", ["service-python", "-m", "pytest", "--version"]] in [
        [command.name, command.command] for command in result.commands
    ]
    mutmut_run = next(cmd for cmd in calls if cmd[:2] == ["mutmut", "run"])
    runner = mutmut_run[mutmut_run.index("--runner") + 1]
    assert runner.startswith(f"PYTHONPATH={repo.as_posix()}:$PYTHONPATH ")
    assert "service-python -m pytest -x --assert=plain tests/uta_generated/test_jobs_forecast.py" in runner


def test_verify_python_target_uses_mutmut_next_to_fallback_python(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "uta_generated").mkdir(parents=True)
    (repo / "tests" / "uta_generated" / "test_jobs_forecast.py").write_text(
        "def test_run():\n    assert True\n",
        encoding="utf-8",
    )
    service_python = tmp_path / "venv" / "bin" / "python"
    service_mutmut = tmp_path / "venv" / "bin" / "mutmut"
    service_python.parent.mkdir(parents=True)
    service_python.write_text("", encoding="utf-8")
    service_mutmut.write_text("", encoding="utf-8")
    monkeypatch.setenv("UTA_SERVICE_PYTHON_BIN", service_python.as_posix())
    calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if cmd == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.9")
        if cmd == ["python3", "-m", "pytest", "--version"]:
            return _completed(cmd, returncode=1, stderr="No module named pytest")
        if cmd == [service_python.as_posix(), "--version"]:
            return _completed(cmd, stdout="Python 3.12")
        if cmd == [service_python.as_posix(), "-m", "pytest", "--version"]:
            return _completed(cmd, stdout="pytest 9")
        if cmd == [service_python.as_posix(), "-m", "coverage", "--version"]:
            return _completed(cmd, stdout="Coverage.py")
        if cmd[:4] == [service_python.as_posix(), "-m", "coverage", "run"]:
            return _completed(cmd)
        if cmd[:4] == [service_python.as_posix(), "-m", "coverage", "xml"]:
            xml_path = Path(cmd[-1])
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            xml_path.write_text(
                """<?xml version="1.0" ?>
<coverage><packages><package name="jobs"><classes><class filename="jobs/forecast.py"><lines>
<line number="1" hits="1"/><line number="2" hits="1"/>
</lines></class></classes></package></packages></coverage>
""",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd == [service_mutmut.as_posix(), "--version"]:
            return _completed(cmd, stdout="mutmut version 2.4.4")
        if cmd[:2] == [service_mutmut.as_posix(), "run"]:
            return _completed(cmd, stdout="2 generated, 2 killed, 0 survived")
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

    assert result.status == "passed"
    assert [service_mutmut.as_posix(), "--version"] in calls
    assert any(cmd[:2] == [service_mutmut.as_posix(), "run"] for cmd in calls)


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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
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
        if cmd[:4] == ["python2", "-m", "coverage", "run"]:
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


def test_verify_python_target_cleans_mutation_state_and_records_survivor_artifacts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".mutmut-cache").mkdir()
    (repo / ".mutmut-cache" / "stale").write_text("old", encoding="utf-8")
    (repo / ".coverage").write_text("stale coverage", encoding="utf-8")
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "config_schema.py").write_text("VALUE = 1\n", encoding="utf-8")
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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
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
            return _completed(cmd, stdout="mutmut 3.0.0")
        if cmd[:2] == ["mutmut", "run"]:
            setup_cfg = (repo / "setup.cfg").read_text(encoding="utf-8")
            parser = ConfigParser()
            parser.read_string(setup_cfg)
            assert "paths_to_mutate = jobs/forecast.py" in setup_cfg
            assert "tests_dir = tests/uta_generated/test_jobs_forecast.py" in setup_cfg
            assert f"runner = PYTHONPATH={repo.as_posix()}:$PYTHONPATH " in setup_cfg
            assert "python3 -m pytest -x --assert=plain tests/uta_generated/test_jobs_forecast.py" in setup_cfg
            also_copy = [line for line in parser.get("mutmut", "also_copy").splitlines() if line]
            assert also_copy == ["config_schema.py", "jobs"]
            return _completed(cmd, returncode=1, stdout="2 generated, 1 killed, 1 survived, 0 no coverage")
        if cmd[:2] == ["mutmut", "results"]:
            return _completed(cmd, stdout="SURVIVED jobs/forecast.py:2 replace return value")
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
    assert result.reason_code == "mutation_gate_failed"
    assert not (repo / ".mutmut-cache").exists()
    assert not (repo / ".coverage").exists()
    assert not (repo / "setup.cfg").exists()
    assert ["mutmut", "run"] in calls
    assert ["mutmut", "run", "--paths-to-mutate", "jobs/forecast.py"] not in calls
    assert (repo / ".uta_cache" / "python" / "mutation" / "mutmut-output.txt").is_file()
    assert (repo / ".uta_cache" / "python" / "mutation" / "survivors.json").is_file()
    assert result.mutation.survivors[0]["file"] == "jobs/forecast.py"
    assert result.mutation.survivors[0]["line"] == 2


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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
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
    assert "mutmut_run" not in [command.name for command in result.commands]


def test_verify_python_target_fails_only_for_survivors_on_changed_lines(tmp_path):
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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='1'/><line number='5' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut 3.0.0")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(cmd, returncode=1, stdout="3 generated, 1 killed, 2 survived, 0 no coverage")
        if cmd[:2] == ["mutmut", "results"]:
            return _completed(
                cmd,
                stdout=(
                    "SURVIVED jobs/forecast.py:2 changed line mutant\n"
                    "SURVIVED jobs/forecast.py:5 existing line mutant\n"
                ),
            )
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
    assert result.reason_code == "mutation_gate_failed"
    assert result.mutation.scope == "changed_lines"
    assert result.mutation.generated == 3
    assert result.mutation.changed_line_mutants_generated == 3
    assert result.mutation.survived == 1
    assert result.mutation.rate == 0.0
    assert result.mutation.diff_survivors[0]["line"] == 2
    assert len(result.mutation.survivors) == 2


def test_verify_python_target_passes_when_survivors_are_outside_changed_lines(tmp_path):
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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='1' hits='1'/><line number='2' hits='1'/><line number='5' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut 3.0.0")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(cmd, returncode=1, stdout="3 generated, 1 killed, 2 survived, 0 no coverage")
        if cmd[:2] == ["mutmut", "results"]:
            return _completed(cmd, stdout="SURVIVED jobs/forecast.py:5 existing line mutant")
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

    assert result.status == "passed"
    assert result.mutation.scope == "changed_lines"
    assert result.mutation.generated == 3
    assert result.mutation.changed_line_mutants_generated == 3
    assert result.mutation.changed_line_mutants_killed == 3
    assert result.mutation.survived == 0
    assert result.mutation.rate == 100.0
    assert result.mutation.passed is True
    assert result.mutation.survivors[0]["line"] == 5


def test_verify_python_target_rejects_mutation_without_generated_evidence(tmp_path):
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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut 3.0.0")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(cmd, stdout="0 generated, 0 killed, 0 survived, 0 no coverage")
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
    assert result.reason_code == "mutation_backend_failed"


def test_verify_python_target_reports_missing_mutmut_patch_dependency(tmp_path):
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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut version 2.5.1")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(
                cmd,
                returncode=1,
                stderr='The --use-patch feature requires the whatthepatch library. '
                'Run "pip install --force-reinstall mutmut[patch]"',
            )
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
    assert result.reason_code == "missing_mutmut_patch_dependency"


def test_verify_python_target_masks_non_changed_lines_during_mutation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "jobs").mkdir()
    source = repo / "jobs" / "forecast.py"
    source.write_text("def run():\n    value = 1\n    return value\n", encoding="utf-8")
    coverage_xml = repo / ".uta_cache" / "python" / "coverage" / "coverage.xml"
    source_during_mutation = ""

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        nonlocal source_during_mutation
        if cmd[:2] == ["python3", "--version"]:
            return _completed(cmd, stdout="Python 3.11.8")
        if cmd[:3] == ["python3", "-m", "pytest"]:
            return _completed(cmd, stdout="pytest 8.0.0")
        if cmd[:3] == ["python3", "-m", "coverage"] and cmd[3] == "--version":
            return _completed(cmd, stdout="Coverage.py 7.0")
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
            return _completed(cmd)
        if cmd[:4] == ["python3", "-m", "coverage", "xml"]:
            coverage_xml.parent.mkdir(parents=True, exist_ok=True)
            coverage_xml.write_text(
                "<coverage><packages><package><classes><class filename='jobs/forecast.py'><lines>"
                "<line number='2' hits='1'/>"
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            return _completed(cmd)
        if cmd[:2] == ["mutmut", "--version"]:
            return _completed(cmd, stdout="mutmut 3.0.0")
        if cmd[:2] == ["mutmut", "run"]:
            source_during_mutation = source.read_text(encoding="utf-8")
            return _completed(cmd, stdout="2 generated, 2 killed, 0 survived, 0 no coverage")
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

    assert result.status == "passed"
    assert "def run():  # pragma: no mutate" in source_during_mutation
    assert "    value = 1\n" in source_during_mutation
    assert "    return value  # pragma: no mutate" in source_during_mutation
    assert source.read_text(encoding="utf-8") == "def run():\n    value = 1\n    return value\n"
    assert result.mutation.changed_line_mutants_generated == 2


def test_verify_python_target_classifies_real_mutmut_progress_as_gate_result(tmp_path):
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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
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
            return _completed(cmd, stdout="mutmut version 2.4.4")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(
                cmd,
                returncode=2,
                stdout="⠴ 804/804  🎉 127  ⏰ 0  🤔 0  🙁 677  🔇 0\n",
            )
        if cmd[:2] == ["mutmut", "results"]:
            return _completed(cmd, stdout="SURVIVED jobs/forecast.py:2 replace return value")
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="jobs/forecast.py"))
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_forecast.py"],
        coverage_gate=100.0,
        mutation_gate=20.0,
        run_command=fake_run,
    )

    assert result.status == "failed"
    assert result.reason_code == "mutation_gate_failed"
    assert result.mutation.generated == 804
    assert result.mutation.killed == 127
    assert result.mutation.survived == 677


def test_verify_python_target_rejects_crashed_mutmut_even_with_progress(tmp_path):
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
        if cmd[:4] == ["python3", "-m", "coverage", "run"]:
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
            return _completed(cmd, stdout="mutmut version 2.4.4")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(cmd, returncode=139, stdout="⠴ 4/4  🎉 4  ⏰ 0  🤔 0  🙁 0\n")
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="jobs/forecast.py"))
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_forecast.py"],
        coverage_gate=100.0,
        mutation_gate=1.0,
        run_command=fake_run,
    )

    assert result.status == "failed"
    assert result.reason_code == "mutation_backend_failed"

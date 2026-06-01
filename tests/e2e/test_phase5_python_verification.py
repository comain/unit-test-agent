import os
import subprocess

import pytest

from uta.engine.languages import RawTargetSelection, default_registry
from uta.language.python.verification.runner import resolve_python_runtime_config, verify_python_target


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def test_phase5_fixture_python3_verification_loop_records_gates(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytest\ncoverage\nmutmut\n", encoding="utf-8")
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
            return _completed(cmd, stdout="mutmut 3.0.0")
        if cmd[:2] == ["mutmut", "run"]:
            return _completed(cmd, stdout="1 generated, 1 killed, 0 survived, 0 no coverage")
        raise AssertionError(f"unexpected command: {cmd}")

    target = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target="jobs/forecast.py"))
    result = verify_python_target(
        repo,
        target,
        test_paths=["tests/uta_generated/test_jobs_forecast.py"],
        coverage_gate=100.0,
        mutation_gate=100.0,
        config=resolve_python_runtime_config(repo, environ={}),
        run_command=fake_run,
    )

    fields = result.as_result_fields()
    assert result.status == "passed"
    assert fields["status"] == "PASS"
    assert fields["coverage"] == 100.0
    assert fields["mutation_score"] == 100.0
    assert fields["dependency_fingerprints"]["requirements.txt"]
    assert fields["verification_cache_key"].startswith("python-env:")


def test_phase5_real_python3_repo_stage2_when_configured():
    repo = os.environ.get("UTA_E2E_PY3_REPO")
    target_value = os.environ.get("UTA_E2E_PY3_TARGET")
    test_paths = os.environ.get("UTA_E2E_PY3_TEST_PATHS")
    if not repo or not target_value or not test_paths:
        pytest.skip("set UTA_E2E_PY3_REPO, UTA_E2E_PY3_TARGET, and UTA_E2E_PY3_TEST_PATHS to run real repo stage 2")

    target = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target=target_value))
    result = verify_python_target(
        repo,
        target,
        test_paths=[path for path in test_paths.split(os.pathsep) if path],
        syntax_version=os.environ.get("UTA_E2E_PY3_SYNTAX_VERSION", "python3"),
        coverage_gate=float(os.environ.get("UTA_E2E_PY3_COVERAGE_GATE", "80")),
        mutation_gate=float(os.environ.get("UTA_E2E_PY3_MUTATION_GATE", "70")),
        config=resolve_python_runtime_config(repo),
    )

    assert result.status == "passed", result.as_result_fields()

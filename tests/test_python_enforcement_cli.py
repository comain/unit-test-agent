import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from uta.cli import main
from uta.language.python.verification.runner import CoverageSummary, MutationSummary, PythonVerificationResult


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo_with_python_change(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_forecast.py").write_text("from jobs.forecast import run\n\n\ndef test_run():\n    assert run() == 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")


def test_python_enforce_cli_executes_core_and_prints_json(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)

    def fake_verify(*args, **kwargs):
        return PythonVerificationResult(
            status="passed",
            reason_code="passed",
            tests_pass=True,
            coverage=CoverageSummary(covered=2, total=2, rate=100.0, gate=95.0, passed=True, xml_path=".uta_cache/python/coverage/coverage.xml"),
            mutation=MutationSummary(
                runtime_lane="mutmut-modern",
                generated=4,
                killed=4,
                survived=0,
                no_coverage=0,
                rate=100.0,
                gate=100.0,
                passed=True,
            ),
        )

    monkeypatch.setattr("uta.language.python.enforcement.verify_python_target", fake_verify)

    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--test-path",
            "tests/test_forecast.py",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "100",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed"
    assert payload["reasonCode"] == "passed"
    assert payload["targets"][0]["target_id"] == "pyfile:jobs/forecast.py"


def test_python_enforce_cli_prints_test_enforcer_markers(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)

    def fake_verify(*args, **kwargs):
        return PythonVerificationResult(
            status="passed",
            reason_code="passed",
            tests_pass=True,
            coverage=CoverageSummary(covered=1, total=1, rate=100.0, gate=95.0, passed=True, xml_path=".uta_cache/python/coverage/coverage.xml"),
            mutation=MutationSummary(
                runtime_lane="mutmut-modern",
                generated=1,
                killed=1,
                survived=0,
                no_coverage=0,
                rate=100.0,
                gate=100.0,
                passed=True,
            ),
        )

    monkeypatch.setattr("uta.language.python.enforcement.verify_python_target", fake_verify)

    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--test-path",
            "tests/test_forecast.py",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "[test-enforcer] python enforcement passed" in result.output
    assert "UTA_PYTHON_ENFORCEMENT_EVIDENCE=" in result.output

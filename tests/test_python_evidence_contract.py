import subprocess
from pathlib import Path

from uta.language.python.enforcement import (
    PYTHON_ENFORCEMENT_SCHEMA_VERSION,
    PythonEnforcementStatus,
    run_python_enforcement,
    validate_python_enforcement_evidence,
)
from uta.language.python.verification.runner import CoverageSummary, MutationSummary, PythonVerificationResult


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
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


def _passing_verification(*args, **kwargs):
    return PythonVerificationResult(
        status="passed",
        reason_code="passed",
        tests_pass=True,
        coverage=CoverageSummary(
            covered=1,
            total=1,
            rate=100.0,
            gate=95.0,
            passed=True,
            xml_path=".uta_cache/python/coverage/coverage.xml",
            scope="changed_lines",
            changed_lines={"jobs/forecast.py": [2]},
        ),
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=4,
            killed=4,
            survived=0,
            no_coverage=0,
            rate=100.0,
            gate=100.0,
            passed=True,
            scope="changed_lines",
            changed_lines={"jobs/forecast.py": [2]},
            diff_survivors=[],
            changed_line_mutants_generated=4,
            changed_line_mutants_killed=4,
        ),
        message="Python pytest, coverage, and mutation gates passed",
        environment_profile="test",
        cache_key="python-env:test",
    )


def test_python_enforcement_evidence_is_schema_versioned_and_commit_bound(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    runner_calls = []

    def passing_with_changed_lines(*args, **kwargs):
        runner_calls.append(kwargs)
        return _passing_verification(*args, **kwargs)

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=["jobs/forecast.py"],
        test_paths=["tests/test_forecast.py"],
        base_ref="origin/master",
        coverage_gate=95.0,
        mutation_gate=100.0,
        verification_runner=passing_with_changed_lines,
    )

    assert evidence["schemaVersion"] == PYTHON_ENFORCEMENT_SCHEMA_VERSION
    assert evidence["language"] == "python"
    assert evidence["backend"] == "python_enforcer"
    assert evidence["status"] == PythonEnforcementStatus.passed.value
    assert evidence["reasonCode"] == "passed"
    assert evidence["baseRef"] == "origin/master"
    assert evidence["baseCommit"]
    assert evidence["headCommit"]
    assert evidence["changedProductionFiles"] == ["jobs/forecast.py"]
    assert evidence["changedLines"] == {"jobs/forecast.py": [2]}
    assert evidence["coverage"]["passed"] is True
    assert evidence["coverage"]["scope"] == "changed_lines"
    assert evidence["coverage"]["changed_lines"] == {"jobs/forecast.py": [2]}
    assert evidence["mutation"]["passed"] is True
    assert evidence["mutation"]["scope"] == "changed_lines"
    assert evidence["mutation"]["changed_lines"] == {"jobs/forecast.py": [2]}
    assert evidence["targets"][0]["target_id"] == "pyfile:jobs/forecast.py"
    assert runner_calls[0]["changed_lines"] == {"jobs/forecast.py": [2]}

    verdict = validate_python_enforcement_evidence(evidence, expected_head=evidence["headCommit"])

    assert verdict.passed is True
    assert verdict.reason_code == "passed"


def test_python_enforcement_rejects_unknown_schema_and_stale_head(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=["jobs/forecast.py"],
        test_paths=["tests/test_forecast.py"],
        base_ref="origin/master",
        coverage_gate=95.0,
        mutation_gate=100.0,
        verification_runner=_passing_verification,
    )

    unknown_schema = dict(evidence, schemaVersion=999)
    stale = dict(evidence, headCommit="0" * 40)

    assert validate_python_enforcement_evidence(unknown_schema, expected_head=evidence["headCommit"]).reason_code == "unknown_schema_version"
    assert validate_python_enforcement_evidence(stale, expected_head=evidence["headCommit"]).reason_code == "stale_head"

    missing_mutants = dict(evidence, mutation={**evidence["mutation"], "changedLineMutantsGenerated": 0})
    assert validate_python_enforcement_evidence(missing_mutants, expected_head=evidence["headCommit"]).reason_code == "missing_mutation_evidence"

    no_executable_lines = dict(
        evidence,
        coverage={**evidence["coverage"], "covered": 0, "total": 0, "no_executable_changed_lines": True},
        mutation=None,
    )
    assert validate_python_enforcement_evidence(no_executable_lines, expected_head=evidence["headCommit"]).passed is True


def test_python_enforcement_no_target_is_explicit_pass(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs")

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests/test_forecast.py"],
        base_ref="origin/master",
        coverage_gate=95.0,
        mutation_gate=100.0,
        verification_runner=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("verification should not run")),
    )

    assert evidence["status"] == PythonEnforcementStatus.passed.value
    assert evidence["reasonCode"] == "no_changed_python_targets"
    assert evidence["targets"] == []
    assert evidence["changedProductionFiles"] == []

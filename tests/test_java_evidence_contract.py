import subprocess
from pathlib import Path

from uta.language.java.enforcement import (
    JAVA_ENFORCEMENT_SCHEMA_VERSION,
    JavaEnforcementStatus,
    format_evidence_markers,
    run_java_enforcement,
    validate_java_enforcement_evidence,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_java_repo(repo: Path, *, base_ref: str = "origin/master") -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    prod = repo / "biz/src/main/java/com/demo/FooService.java"
    test = repo / "biz/src/test/java/com/demo/FooServiceTest.java"
    prod.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    test.write_text("package com.demo; class FooServiceTest {}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", f"refs/remotes/{base_ref}", "HEAD")
    prod.write_text("package com.demo; class FooService { int v; }\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")


def test_java_enforcement_core_wraps_maven_runner_with_schema_and_markers(tmp_path):
    repo = tmp_path / "repo"
    _init_java_repo(repo)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff line coverage 100.00% passed\nPIT generated=2 killed=2 survived=0 test-strength=100%",
            stderr="",
        )

    evidence = run_java_enforcement(
        repo_path=repo,
        command="mvn -Dtest.enforcement.enabled=true verify",
        base_ref="origin/master",
        run_command=fake_run,
    )

    assert evidence["schemaVersion"] == JAVA_ENFORCEMENT_SCHEMA_VERSION
    assert evidence["language"] == "java"
    assert evidence["backend"] == "maven_enforcer"
    assert evidence["status"] == JavaEnforcementStatus.passed.value
    assert evidence["reasonCode"] == "passed"
    assert evidence["passed"] is True
    assert evidence["headCommit"]
    assert evidence["evidenceId"].startswith("uta-java-enforcement:")
    assert "-DtargetTests=com.demo.FooServiceTest" in calls[0]
    assert "-Dtest=FooServiceTest" in calls[0]
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in calls[0]
    assert validate_java_enforcement_evidence(evidence, expected_head=evidence["headCommit"]).passed is True
    assert "UTA_JAVA_ENFORCEMENT_EVIDENCE=" in format_evidence_markers(evidence)


def test_java_enforcement_core_honors_configured_base_ref(tmp_path):
    repo = tmp_path / "repo"
    _init_java_repo(repo, base_ref="origin/main")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\nDiff mutation score 100%",
            stderr="",
        )

    evidence = run_java_enforcement(
        repo_path=repo,
        command="mvn -Dtest.enforcement.enabled=true verify",
        base_ref="origin/main",
        run_command=fake_run,
    )

    assert evidence["passed"] is True
    assert evidence["baseRef"] == "origin/main"
    assert calls, "Maven should run because origin/main detects the changed Java file"


def test_java_enforcement_validation_rejects_wrong_schema_and_stale_head(tmp_path):
    repo = tmp_path / "repo"
    _init_java_repo(repo)
    evidence = run_java_enforcement(
        repo_path=repo,
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\nDiff mutation score 100%",
            stderr="",
        ),
    )

    assert validate_java_enforcement_evidence({**evidence, "schemaVersion": 999}).reason_code == "unknown_schema_version"
    assert validate_java_enforcement_evidence({**evidence, "headCommit": "0" * 40}, expected_head=evidence["headCommit"]).reason_code == "stale_head"
    assert validate_java_enforcement_evidence({**evidence, "status": "failed", "passed": False, "reasonCode": "failed"}).reason_code == "failed"

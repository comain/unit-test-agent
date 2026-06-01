import subprocess
import json
from pathlib import Path

import pytest

from uta.ci_plugin.enforcement import (
    EnforcementResultStatus,
    MavenEnforcementRunner,
    MISSING_EVIDENCE_SUMMARY,
    PythonEnforcementRunner,
    TEST_ENFORCEMENT_USAGE_GUIDE,
)
from uta.ci_plugin.models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.ci_plugin.service import CiPluginService
from uta.ci_plugin.workspace import GitWorkspaceManager
from uta.config import Settings
from uta.language.java.maven_project import test_enforcement_tooling_status as maven_tooling_status


def test_workspace_manager_prepares_isolated_branch_checkout(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    manager = GitWorkspaceManager(workspace_root=tmp_path, run_command=fake_run)

    workspace = manager.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch="feature/EXAMPLE-1",
        task_id="task-1",
    )

    assert workspace == tmp_path / "task-1" / "demo"
    assert calls[0][0] == ["git", "clone", "git@git.example.com:group/demo.git", str(workspace)]
    assert calls[1][0] == [
        "git",
        "-C",
        str(workspace),
        "fetch",
        "origin",
        "refs/heads/feature/EXAMPLE-1:refs/remotes/origin/feature/EXAMPLE-1",
        "--prune",
    ]
    assert calls[2][0] == [
        "git",
        "-C",
        str(workspace),
        "checkout",
        "--force",
        "-B",
        "feature/EXAMPLE-1",
        "origin/feature/EXAMPLE-1",
    ]
    assert calls[3][0] == ["git", "-C", str(workspace), "clean", "-fd"]
    assert not any(
        call[0][:5] == ["git", "-C", str(workspace), "config", "core.sshCommand"]
        for call in calls
    )


def test_workspace_manager_uses_configured_git_ssh_key(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    manager = GitWorkspaceManager(
        workspace_root=tmp_path,
        run_command=fake_run,
        git_ssh_key_path="/opt/app/uta-ci-data/runner/ssh/uta ci key",
    )

    manager.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch="feature/EXAMPLE-1",
        task_id="task-1",
    )

    git_env = calls[0][1]["env"]
    assert git_env is not None
    assert "GIT_SSH_COMMAND" in git_env
    assert "ssh -F /dev/null" in git_env["GIT_SSH_COMMAND"]
    assert "-o IdentitiesOnly=yes" in git_env["GIT_SSH_COMMAND"]
    assert "-o PreferredAuthentications=publickey" in git_env["GIT_SSH_COMMAND"]
    assert "'/opt/app/uta-ci-data/runner/ssh/uta ci key'" in git_env["GIT_SSH_COMMAND"]
    assert calls[1][0] == [
        "git",
        "-C",
        str(tmp_path / "task-1" / "demo"),
        "config",
        "core.sshCommand",
        git_env["GIT_SSH_COMMAND"],
    ]


def test_workspace_manager_retries_transient_git_timeout(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"], output="partial")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    manager = GitWorkspaceManager(
        workspace_root=tmp_path,
        run_command=fake_run,
        command_timeout_seconds=3,
        command_retry_times=1,
        command_retry_delay_seconds=0,
    )

    manager.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch="feature/EXAMPLE-1",
        task_id="task-1",
    )

    assert calls[0][0] == ["git", "clone", "git@git.example.com:group/demo.git", str(tmp_path / "task-1" / "demo")]
    assert calls[1][0] == calls[0][0]
    assert calls[0][1]["timeout"] == 3


def test_workspace_manager_fails_after_git_timeout_retries(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"], output="partial", stderr="hung")

    manager = GitWorkspaceManager(
        workspace_root=tmp_path,
        run_command=fake_run,
        command_timeout_seconds=3,
        command_retry_times=1,
        command_retry_delay_seconds=0,
    )

    with pytest.raises(RuntimeError, match="git command timed out during clone after 3s"):
        manager.prepare(
            git_url="git@git.example.com:group/demo.git",
            branch="feature/EXAMPLE-1",
            task_id="task-1",
        )

    assert len(calls) == 2


def test_enforcement_runner_rejects_plain_mvn_test(tmp_path):
    runner = MavenEnforcementRunner(command="mvn test")

    with pytest.raises(ValueError, match="docs/test-enforce-usage.md"):
        runner.run(tmp_path)


def test_default_ci_enforcement_command_forces_test_execution():
    command = Settings(_env_file=None).ci_enforcement_command

    assert "mvn -U" in command
    assert "-DskipTests=false" in command
    assert "-Dmaven.test.skip=false" in command
    assert "-Dmaven.test.failure.ignore=true" in command


def _effective_pom_run(effective_pom: str, calls=None):
    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        if "help:effective-pom" not in cmd:
            raise AssertionError(f"Only Maven metadata should run without targetTests: {cmd}")
        output_arg = next(item for item in cmd if str(item).startswith("-Doutput="))
        Path(str(output_arg).split("=", 1)[1]).write_text(effective_pom, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return fake_run


def test_maven_tooling_status_requires_resolved_build_plugin(tmp_path):
    (tmp_path / "pom.xml").write_text(
        """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>demo</artifactId>
  <build>
    <pluginManagement>
      <plugins>
        <plugin>
          <groupId>io.example</groupId>
          <artifactId>test-enforcer</artifactId>
          <version>1.0.12</version>
        </plugin>
      </plugins>
    </pluginManagement>
  </build>
</project>
""",
        encoding="utf-8",
    )

    status = maven_tooling_status(tmp_path)

    assert status.available is False
    assert status.reason == "No resolved test-enforcer Maven plugin was found"


def test_enforcement_runner_classifies_success_with_required_evidence(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\nPIT generated=2 killed=2 survived=0 test-strength=100%",
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.passed
    assert result.passed is True


def test_enforcement_runner_passes_without_maven_when_no_changed_production_java(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    docs = repo / "doc"
    docs.mkdir()
    (docs / "note.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    (docs / "note.md").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "docs only"], check=True, capture_output=True, text=True)

    def fail_if_maven_runs(cmd, **kwargs):
        raise AssertionError(f"Maven should not run for docs-only changes: {cmd}")

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fail_if_maven_runs)

    result = runner.run(repo)

    assert result.status == EnforcementResultStatus.passed
    assert result.passed is True
    assert result.returncode is None
    assert "no changed production Java" in result.summary


def test_enforcement_runner_adds_target_tests_for_changed_java(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    prod = repo / "biz/src/main/java/com/demo/FooService.java"
    existing_test = repo / "biz/src/test/java/com/demo/FooServiceExistingTest.java"
    prod.parent.mkdir(parents=True)
    existing_test.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    existing_test.write_text("package com.demo; class FooServiceExistingTest {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    generated_test = repo / "biz/src/test/java/com/demo/FooServiceGeneratedTest.java"
    prod.write_text("package com.demo; class FooService { int v; }\n", encoding="utf-8")
    generated_test.write_text("package com.demo; class FooServiceGeneratedTest {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\nPIT generated=2 killed=2 survived=0 test-strength=100%",
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(repo)

    assert result.status == EnforcementResultStatus.passed
    assert result.passed is True
    assert "-DtargetTests=com.demo.FooServiceGeneratedTest" in calls[0]
    assert "-Dtest=FooServiceGeneratedTest" in calls[0]
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in calls[0]


def test_enforcement_runner_uses_existing_matching_test_when_no_changed_test(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    prod = repo / "biz/src/main/java/com/demo/FooService.java"
    existing_test = repo / "biz/src/test/java/com/demo/FooServiceExistingTest.java"
    prod.parent.mkdir(parents=True)
    existing_test.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    existing_test.write_text("package com.demo; class FooServiceExistingTest {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    prod.write_text("package com.demo; class FooService { int v; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\nPIT generated=2 killed=2 survived=0 test-strength=100%",
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(repo)

    assert result.status == EnforcementResultStatus.passed
    assert result.passed is True
    assert "-DtargetTests=com.demo.FooServiceExistingTest" in calls[0]
    assert "-Dtest=FooServiceExistingTest" in calls[0]
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in calls[0]


def test_enforcement_runner_preserves_configured_surefire_test_selector(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    prod = repo / "biz/src/main/java/com/demo/FooService.java"
    test = repo / "biz/src/test/java/com/demo/FooServiceTest.java"
    prod.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    test.write_text("package com.demo; class FooServiceTest {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    prod.write_text("package com.demo; class FooService { int v; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\nPIT generated=2 killed=2 survived=0 test-strength=100%",
            stderr="",
        )

    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true -Dtest=CustomSmokeTest verify",
        run_command=fake_run,
    )

    result = runner.run(repo)

    assert result.status == EnforcementResultStatus.passed
    assert "-Dtest=CustomSmokeTest" in calls[0]
    assert "-Dtest=FooServiceTest" not in calls[0]
    assert "-DtargetTests=com.demo.FooServiceTest" in calls[0]


def test_enforcement_runner_fails_when_no_target_tests_for_changed_java(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    (repo / "pom.xml").write_text(
        """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>demo</artifactId>
</project>
""",
        encoding="utf-8",
    )
    prod = repo / "src/main/java/com/demo/UntestedService.java"
    prod.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class UntestedService {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    prod.write_text("package com.demo; class UntestedService { int v; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)

    calls = []
    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=_effective_pom_run(
            """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <build>
    <plugins>
      <plugin>
        <groupId>io.example</groupId>
        <artifactId>test-enforcer</artifactId>
        <version>1.0.12</version>
      </plugin>
    </plugins>
  </build>
</project>
""",
            calls,
        ),
    )

    result = runner.run(repo)

    assert result.status == EnforcementResultStatus.missing_evidence
    assert result.passed is False
    assert "targetTests" in result.summary
    assert result.evidence and result.evidence["coverage"]["rate"] == 0.0
    assert result.evidence["tooling"]["available"] is True
    assert result.evidence["tooling"]["artifactId"] == "test-enforcer"
    assert any("help:effective-pom" in call for call in calls)


def test_enforcement_runner_blocks_repair_when_no_target_tests_and_tooling_too_old(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    (repo / "pom.xml").write_text(
        """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.example.service_a</groupId>
    <artifactId>service-parent</artifactId>
    <version>0.9.0</version>
  </parent>
  <artifactId>demo</artifactId>
</project>
""",
        encoding="utf-8",
    )
    prod = repo / "src/main/java/com/demo/DtoOnlyChange.java"
    prod.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class DtoOnlyChange {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    prod.write_text("package com.demo; class DtoOnlyChange { String value; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=_effective_pom_run(
            """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.example.service_a</groupId>
    <artifactId>service-parent</artifactId>
    <version>0.9.0</version>
  </parent>
  <artifactId>demo</artifactId>
</project>
""",
        ),
    ).run(repo)

    assert result.status == EnforcementResultStatus.missing_evidence
    assert result.summary == MISSING_EVIDENCE_SUMMARY
    assert result.evidence and result.evidence["tooling"]["available"] is False
    assert result.evidence["tooling"]["artifactId"] == "service-parent"
    assert result.evidence["tooling"]["version"] == "0.9.0"


def test_enforcement_runner_adds_default_profile_for_profile_active_projects(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    (repo / "pom.xml").write_text(
        """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.example.service_a</groupId>
    <artifactId>service-parent</artifactId>
    <version>1.0.0</version>
  </parent>
  <artifactId>demo</artifactId>
  <profiles>
    <profile><id>dev</id><properties><profile.active>dev</profile.active></properties></profile>
  </profiles>
  <build>
    <resources>
      <resource><directory>src/main/resources.${profile.active}</directory></resource>
    </resources>
  </build>
</project>
""",
        encoding="utf-8",
    )
    prod = repo / "src/main/java/com/demo/ProfiledService.java"
    test = repo / "src/test/java/com/demo/ProfiledServiceTest.java"
    prod.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class ProfiledService {}\n", encoding="utf-8")
    test.write_text("package com.demo; class ProfiledServiceTest {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    prod.write_text("package com.demo; class ProfiledService { int v; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo (1/1)\n"
                "[info] [test-enforcer] diff mutation score 100.00% passed for demo (1/1 detected)\n"
            ),
            stderr="",
        )

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
    ).run(repo)

    assert result.status == EnforcementResultStatus.passed
    assert "-Pdev" in calls[0]


def test_enforcement_runner_ignores_unrelated_maven_test_failures_after_gate_evidence(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="Diff coverage: 100%\nPIT generated=2 killed=2 survived=0 test-strength=100%",
            stderr="[ERROR] There are test failures in unrelated existing tests.",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.passed
    assert result.passed is True
    assert result.returncode == 1
    assert "non-zero" in result.summary


def test_enforcement_runner_keeps_gate_failure_blocking_even_with_evidence(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=(
                "Diff coverage: 80%\n"
                "PIT generated=2 killed=1 survived=1 test-strength=50%\n"
                "mutation gate failed"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.failed
    assert result.passed is False


def test_enforcement_runner_accepts_scoped_pit_summary_without_diff_mutation_marker(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.demo.ChangedService*]\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
                ">> Generated 51 mutations Killed 0 (0%)\n"
                ">> Mutations with no coverage 51. Test strength 100%\n"
                "[INFO] BUILD SUCCESS"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.passed
    assert result.passed is True


def test_enforcement_runner_rejects_unscoped_raw_pit_summary(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
                ">> Generated 51 mutations Killed 0 (0%)\n"
                ">> Mutations with no coverage 51. Test strength 100%\n"
                "[INFO] BUILD SUCCESS"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.missing_evidence
    assert result.passed is False


def test_enforcement_runner_accepts_zero_pitest_targets_with_coverage_evidence(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, pitest.targets=0 []\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
                "[INFO] BUILD SUCCESS"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.passed
    assert result.passed is True


def test_enforcement_runner_blocks_pitest_baseline_failure_when_no_test_class_isolated(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=(
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (109/109)\n"
                "[INFO] --- pitest:1.15.0:mutationCoverage (pitest) @ demo.biz ---\n"
                "[ERROR] Mutation testing requires a green suite."
            ),
            stderr="PIT >> SEVERE : Tests failing without mutation:",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.failed
    assert result.passed is False
    assert "PIT baseline tests were not green" in result.summary


def test_enforcement_runner_blocks_coverage_gate_failure_after_filter_diff(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=(
                "[INFO] --- test-enforcer:1.0.10:filter-diff ---\n"
                "[ERROR] Failed to execute goal io.example:test-enforcer:1.0.10:check-coverage\n"
                "[ERROR] test-enforcer check-coverage failed: "
                "diff line coverage 87.50% is below required 95.00% (126/144)\n"
                "PIT generated=2 killed=2 survived=0 test-strength=100%"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.failed
    assert result.passed is False


def test_enforcement_runner_blocks_dependency_resolution_failure_after_gate_keywords(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=(
                "[INFO] --- jacoco-maven-plugin:prepare-agent ---\n"
                "[INFO] --- test-enforcer:1.0.10:filter-diff ---\n"
                "[INFO] pitest.targets=com.demo.*\n"
                "[ERROR] Could not resolve dependencies for project com.demo:demo-service:jar:1.0\n"
                "[ERROR] Could not find artifact com.demo:demo-api:jar:EXAMPLE-123-SNAPSHOT\n"
                "[ERROR] org.apache.maven.project.DependencyResolutionException"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.failed
    assert result.passed is False
    assert "compile or resolve" in result.summary


def test_enforcement_runner_blocks_compilation_failure_after_gate_keywords(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=(
                "Diff coverage: 100%\n"
                "PIT generated=2 killed=2 survived=0 test-strength=100%\n"
                "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.8.1:compile\n"
                "[ERROR] Compilation failure\n"
                "[ERROR] cannot find symbol"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.failed
    assert result.passed is False


def test_enforcement_runner_classifies_missing_evidence_on_green_command(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="BUILD SUCCESS", stderr="")

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == EnforcementResultStatus.missing_evidence
    assert result.passed is False
    assert result.summary == MISSING_EVIDENCE_SUMMARY
    assert "test-enforcer >= 1.0.12" in result.summary
    assert "project-local test-enforcement profile" in result.summary
    assert result.usage_guide == TEST_ENFORCEMENT_USAGE_GUIDE


def test_enforcement_runner_classifies_failure_timeout_and_command_error(tmp_path):
    failure = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="test failed"),
    ).run(tmp_path)
    assert failure.status == EnforcementResultStatus.failed

    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=1)

    timeout = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=timeout_run,
    ).run(tmp_path)
    assert timeout.status == EnforcementResultStatus.timeout

    def command_error_run(cmd, **kwargs):
        raise OSError("mvn missing")

    command_error = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=command_error_run,
    ).run(tmp_path)
    assert command_error.status == EnforcementResultStatus.command_error


def _init_python_repo(repo: Path) -> str:
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    source.write_text("def run():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "change"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _python_evidence(head: str, *, passed: bool = True, reason: str = "passed") -> dict:
    return {
        "schemaVersion": 1,
        "language": "python",
        "backend": "python_enforcer",
        "status": "passed" if passed else "failed",
        "passed": passed,
        "reasonCode": reason,
        "summary": "Python enforcement passed" if passed else "Python enforcement failed",
        "baseRef": "origin/master",
        "baseCommit": "base",
        "headCommit": head,
        "changedProductionFiles": ["jobs/forecast.py"],
        "targets": [{"language": "python", "target_id": "pyfile:jobs/forecast.py", "target": "jobs/forecast.py"}],
        "coverage": {"covered": 2, "total": 2, "rate": 100.0, "gate": 95.0, "passed": True},
        "mutation": {"generated": 4, "killed": 4, "survived": 0, "rate": 100.0, "gate": 100.0, "passed": True},
    }


def test_python_enforcement_runner_accepts_uta_json_evidence(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence(head)), stderr="")

    runner = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run)

    result = runner.run(repo)

    assert result.status == EnforcementResultStatus.passed
    assert result.passed is True
    assert result.summary == "Python enforcement passed"
    assert result.command[:2] == ["uta", "python-enforce"]
    assert "--json-output" in result.command
    assert result.evidence["headCommit"] == head
    assert calls[0] == result.command


def test_python_enforcement_runner_preserves_equals_style_configured_options(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence(head)), stderr="")

    runner = PythonEnforcementRunner(
        command="uta python-enforce --repo=. --base-ref=origin/main --coverage-gate=99 --mutation-gate=88 --json-output",
        run_command=fake_run,
    )

    result = runner.run(repo)

    assert result.status == EnforcementResultStatus.passed
    assert calls[0].count("--repo") == 0
    assert "--repo=." in calls[0]
    assert calls[0].count("--base-ref") == 0
    assert "--base-ref=origin/main" in calls[0]
    assert calls[0].count("--json-output") == 1


def test_python_enforcement_runner_rejects_stale_evidence(tmp_path):
    repo = tmp_path / "repo"
    _init_python_repo(repo)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence("0" * 40)), stderr="")

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    assert result.status == EnforcementResultStatus.failed
    assert result.passed is False
    assert "stale_head" in result.summary


def test_python_enforcement_runner_fails_when_workspace_head_cannot_be_resolved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence("0" * 40)), stderr="")

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    assert result.status == EnforcementResultStatus.command_error
    assert result.passed is False
    assert "could not resolve workspace HEAD" in result.summary


def test_python_enforcement_runner_accepts_explicit_no_target_evidence(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    evidence = _python_evidence(head, passed=True, reason="no_changed_python_targets")
    evidence["changedProductionFiles"] = []
    evidence["targets"] = []
    evidence["coverage"] = None
    evidence["mutation"] = None

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="UTA_PYTHON_ENFORCEMENT_EVIDENCE=" + json.dumps(evidence), stderr="")

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    assert result.status == EnforcementResultStatus.passed
    assert result.passed is True
    assert result.summary == "Python enforcement passed"


def test_ci_service_runs_workspace_prepare_and_enforcement_with_mocked_commands(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%",
            stderr="",
        )

    service = CiPluginService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path, run_command=fake_run),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
    )
    request = CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/EXAMPLE-1",
        }
    )

    record = service.submit(request)

    assert record.status.value == "success"
    assert any(cmd[:2] == ["git", "clone"] for cmd, _ in calls)
    assert any(cmd[:2] == ["mvn", "-Dtest.enforcement.enabled=true"] for cmd, _ in calls)


def test_ci_service_routes_python_ci_request_to_python_enforcer(tmp_path):
    class Runner:
        def __init__(self, name):
            self.name = name
            self.calls = []

        def run(self, repo_path):
            self.calls.append(Path(repo_path))
            if self.name == "java":
                raise AssertionError("Java runner should not handle language=python")
            from uta.ci_plugin.enforcement import EnforcementResult

            return EnforcementResult(
                status=EnforcementResultStatus.passed,
                passed=True,
                command=["uta", "python-enforce"],
                summary="Python enforcement passed",
                language="python",
                backend="python_enforcer",
                evidence={"language": "python", "backend": "python_enforcer"},
            )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    java_runner = Runner("java")
    python_runner = Runner("python")
    service = CiPluginService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path, run_command=fake_run),
        enforcement_runner=java_runner,
        python_enforcement_runner=python_runner,
    )
    request = CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/EXAMPLE-1",
            "language": "python",
        }
    )

    record = service.submit(request)

    assert record.status.value == "success"
    assert record.enforcement_result["backend"] == "python_enforcer"
    assert len(python_runner.calls) == 1


def test_ci_service_can_route_through_custom_language_handler():
    class Runner:
        def run(self, repo_path):
            raise AssertionError("not called")

    class KotlinHandler:
        language = "kotlin"
        quality_gate_backend = "kotlin_enforcer"

        def __init__(self):
            self.runner = Runner()

        def matches(self, record):
            return (record.enforcement_result or {}).get("backend") == self.quality_gate_backend

        def create_repair_task(self, **kwargs):
            raise AssertionError("not called")

    handler = KotlinHandler()
    service = CiPluginService(language_handlers=[handler])
    record = CiTaskRecord(
        task_id="task-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/EXAMPLE-1",
            }
        ),
        enforcement_result={"backend": "kotlin_enforcer"},
    )

    assert service._runner_for_record(record) is handler.runner

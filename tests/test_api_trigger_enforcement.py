import subprocess
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from fake_maven_metadata import DIFF_MUTATION_OK, with_resolved_enforcer

from fake_git import calls as git_calls, envs, fake_git
from uta.enforcement.enforcement import (
    QualityGateResult,
    QualityGateStatus,
    TEST_ENFORCEMENT_USAGE_GUIDE,
    run_bounded_command,
)
from uta.language.java.enforcement_runner import MISSING_EVIDENCE_SUMMARY, MavenEnforcementRunner
from uta.language.java.enforcement_runner.execution import _with_jacoco_argline_bridge
from uta.language.python.enforcement_runner import PythonEnforcementRunner
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.app.reporting import CiReportRenderer
from uta.app.service import ApiTriggerService
from uta.app.store import JsonCiTaskStore
from agent_core.git import url_for_access_token
from agent_core.git import GitCommandError, GitTimeout
from uta.app.workspace import GitWorkspaceManager
from uta.shared.config import Settings
from uta.language.java.maven_project import test_enforcement_tooling_status as maven_tooling_status


def test_enforcement_command_spools_and_bounds_large_child_output(tmp_path):
    completed = run_bounded_command(
        subprocess.run,
        [
            sys.executable,
            "-c",
            "import sys; print('BEGIN'); sys.stdout.write('x' * (12 * 1024 * 1024)); print('END')",
        ],
        cwd=tmp_path,
        timeout=30,
        max_output_bytes=1024 * 1024,
    )

    assert completed.returncode == 0
    assert completed.stdout.startswith("BEGIN\n")
    assert completed.stdout.endswith("END\n")
    assert "output truncated" in completed.stdout
    assert len(completed.stdout.encode("utf-8")) < 2 * 1024 * 1024


def _manager(tmp_path, log=None, **kwargs):
    return GitWorkspaceManager(
        workspace_root=tmp_path,
        git_bin=fake_git(tmp_path, log=log, **kwargs.pop("fake", {})),
        **kwargs,
    )


def test_workspace_manager_prepares_isolated_branch_checkout(tmp_path):
    """One task, one tree, checked out as a local branch.

    The command sequence is agent-core's now, so this pins the properties the
    sequence has to produce rather than the exact lines: the tree is under the
    task's own directory, the branch is a *local* branch -- delivery pushes it
    by name -- and the tree is cleaned. It must not configure an ssh command
    when no key was given.
    """
    log = tmp_path / "git.jsonl"
    manager = _manager(tmp_path, log=log)

    workspace = manager.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch="feature/TASK-82767",
        task_id="task-1",
    )

    assert workspace == tmp_path / "task-1" / "demo"

    argvs = git_calls(log)
    clone = next(argv for argv in argvs if argv[0] == "clone")
    assert "git@git.example.com:group/demo.git" in clone
    assert str(workspace) == clone[-1]
    assert clone[clone.index("--branch") + 1] == "feature/TASK-82767"
    # Not shallow: enforcement and commit-message context read commit ranges.
    assert "--depth" not in clone

    checkout = next(argv for argv in argvs if "checkout" in argv)
    assert checkout[2:] == [
        "checkout", "--force", "-B", "feature/TASK-82767", "origin/feature/TASK-82767",
    ]
    assert any(argv[2:] == ["clean", "-fd"] for argv in argvs)
    assert not any("core.sshCommand" in argv for argv in argvs)


def test_a_prepared_workspace_starts_from_a_tree_git_would_not_have_cleaned(tmp_path):
    """`clean -fd` keeps ignored files, so the tree is rebuilt instead.

    Without this a task that compiles inherits the previous task's build
    output as its baseline, and the damage surfaces as an unexplained diff
    somewhere unrelated.
    """
    manager = _manager(tmp_path)
    workspace = manager.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch="feature/TASK-82767",
        task_id="task-1",
    )
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "target").mkdir()
    (workspace / "target" / "Stale.class").write_text("stale")

    manager.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch="feature/TASK-82767",
        task_id="task-1",
    )

    assert not (workspace / "target").exists()


def test_workspace_manager_refreshes_existing_branch_checkout(tmp_path):
    log = tmp_path / "git.jsonl"
    manager = _manager(tmp_path, log=log, fake={"rev": "abc123"})

    head = manager.refresh_branch(tmp_path / "repo", branch="feature/TASK-82767")

    assert head == "abc123"
    argvs = [argv[2:] for argv in git_calls(log) if argv[:2] == ["-C", str(tmp_path / "repo")]]
    fetch = next(argv for argv in argvs if argv[0] == "fetch")
    # Forced, because a repair workspace is a disposable mirror of a branch
    # that may have been rebased or force-pushed.
    assert "--force" in fetch
    assert "+refs/heads/feature/TASK-82767:refs/remotes/origin/feature/TASK-82767" in fetch
    assert ["checkout", "--force", "-B", "feature/TASK-82767", "origin/feature/TASK-82767"] in argvs
    assert ["reset", "--hard", "origin/feature/TASK-82767"] in argvs
    assert ["clean", "-fd"] in argvs


def test_workspace_manager_uses_configured_git_ssh_key(tmp_path):
    log = tmp_path / "git.jsonl"
    manager = _manager(
        tmp_path,
        log=log,
        git_ssh_key_path="/opt/app/uta-ci-data/runner/ssh/uta ci key",
    )

    manager.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch="feature/TASK-82767",
        task_id="task-1",
    )

    git_env = envs(log)[0]
    assert "GIT_SSH_COMMAND" in git_env
    assert "ssh -F /dev/null" in git_env["GIT_SSH_COMMAND"]
    assert "-o IdentitiesOnly=yes" in git_env["GIT_SSH_COMMAND"]
    assert "-o PreferredAuthentications=publickey" in git_env["GIT_SSH_COMMAND"]
    assert "'/opt/app/uta-ci-data/runner/ssh/uta ci key'" in git_env["GIT_SSH_COMMAND"]
    # Also pinned in the checkout's own config, for git run there by anything
    # else -- a build, a tool, the agent.
    assert [
        "-C", str(tmp_path / "task-1" / "demo"),
        "config", "core.sshCommand", git_env["GIT_SSH_COMMAND"],
    ] in git_calls(log)


def test_workspace_manager_uses_git_access_token_over_ssh_key(tmp_path):
    log = tmp_path / "git.jsonl"
    manager = _manager(
        tmp_path,
        log=log,
        git_ssh_key_path="/opt/app/uta-ci-data/runner/ssh/uta ci key",
        git_access_token="secret-token",
    )

    workspace = manager.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch="feature/TASK-82767",
        task_id="task-1",
    )

    clone = next(argv for argv in git_calls(log) if argv[0] == "clone")
    assert "https://git.example.com/group/demo.git" in clone
    assert clone[-1] == str(workspace)

    git_env = envs(log)[0]
    assert "GIT_SSH_COMMAND" not in git_env
    assert git_env["GIT_TERMINAL_PROMPT"] == "0"
    assert git_env["GIT_CONFIG_KEY_0"] == "http.https://git.example.com/.extraheader"
    assert git_env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "secret-token" not in git_env["GIT_CONFIG_VALUE_0"]
    assert not any("core.sshCommand" in argv for argv in git_calls(log))


def test_settings_reads_git_access_token_from_git_ac(monkeypatch):
    monkeypatch.setenv("GIT_AC", "secret-token")
    config = Settings(_env_file=None)

    assert config.ci_git_access_token == "secret-token"


def test_git_access_token_url_rewrite_strips_existing_url_credentials():
    assert url_for_access_token("https://oauth2:secret-token@git.example.com/group/demo.git") == (
        "https://git.example.com/group/demo.git"
    )


def test_workspace_manager_retries_a_transient_transport_failure(tmp_path):
    """A blip on the first clone recovers on the retry."""
    log = tmp_path / "git.jsonl"
    manager = _manager(
        tmp_path,
        log=log,
        fake={"fail_first": {"clone": "fatal: unable to access: Connection reset by peer"}},
        command_retry_times=1,
        command_retry_delay_seconds=0,
    )

    workspace = manager.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch="feature/TASK-82767",
        task_id="task-1",
    )

    assert workspace == tmp_path / "task-1" / "demo"
    assert len([argv for argv in git_calls(log) if argv[0] == "clone"]) == 2


def test_workspace_manager_fails_after_git_timeout_retries(tmp_path):
    """A command that outlives its budget is killed, retried, and then given up
    on -- rather than hanging for as long as the remote cares to."""
    log = tmp_path / "git.jsonl"
    manager = _manager(
        tmp_path,
        log=log,
        fake={"sleep": 5},
        command_retry_times=1,
        command_retry_delay_seconds=0,
    )
    manager.workspace.timeout = 0.8
    manager.workspace.poll_interval = 0.05

    with pytest.raises(GitTimeout, match="timed out after"):
        manager.prepare(
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
            task_id="task-1",
        )

    assert len([argv for argv in git_calls(log) if argv[0] == "clone"]) == 2


def test_a_missing_repository_is_not_retried(tmp_path):
    """Retrying it only makes the error slower, and makes it look like a
    network problem. The old loop repeated everything."""
    log = tmp_path / "git.jsonl"
    manager = _manager(
        tmp_path,
        log=log,
        fake={"fail_on": {"clone": "fatal: repository not found"}},
        command_retry_times=2,
        command_retry_delay_seconds=0,
    )

    with pytest.raises(GitCommandError):
        manager.prepare(
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
            task_id="task-1",
        )

    assert len(git_calls(log)) == 1


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


def test_enforcement_runner_forces_configured_java_home_for_maven(tmp_path, monkeypatch):
    inherited_java_home = tmp_path / "jdk21"
    configured_java_home = tmp_path / "jdk8"
    for java_home in (inherited_java_home, configured_java_home):
        (java_home / "bin").mkdir(parents=True)
        (java_home / "bin" / "java").write_text("#!/bin/sh\n", encoding="utf-8")
        (java_home / "bin" / "java").chmod(0o755)
    monkeypatch.setenv("JAVA_HOME", str(inherited_java_home))
    monkeypatch.setenv("PATH", os.pathsep.join([str(inherited_java_home / "bin"), "/usr/bin"]))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        java_home=str(configured_java_home),
        run_command=fake_run,
    )

    runner._run_command(["mvn", "-version"], tmp_path)

    env = calls[0][1]["env"]
    assert env["JAVA_HOME"] == str(configured_java_home)
    assert env["PATH"].split(os.pathsep)[0] == str(configured_java_home / "bin")
    assert str(inherited_java_home / "bin") not in env["PATH"].split(os.pathsep)


def test_enforcement_runner_routes_maven_central_through_configured_mirror(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
        maven_central_mirror_url="https://nexus.example.test/repository/public/",
    )

    runner._run_command(["mvn", "initialize"], tmp_path)

    command = calls[0][0]
    global_settings = Path(command[command.index("-gs") + 1])
    assert global_settings.is_file()
    assert "<mirrorOf>central</mirrorOf>" in global_settings.read_text(encoding="utf-8")
    assert "https://nexus.example.test/repository/public/" in global_settings.read_text(encoding="utf-8")


def test_enforcement_bridges_jacoco_when_pom_expands_argline_too_early(tmp_path):
    repo = tmp_path / "repo"
    local_repository = tmp_path / "maven-repository"
    agent = (
        local_repository
        / "org/jacoco/org.jacoco.agent/0.8.11/org.jacoco.agent-0.8.11-runtime.jar"
    )
    agent.parent.mkdir(parents=True)
    agent.write_bytes(b"agent")
    repo.mkdir()
    (repo / "pom.xml").write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <build><plugins><plugin><configuration>
    <argLine>${argLine} -javaagent:${settings.localRepository}/com/alibaba/testable/testable-agent/0.7.7/testable-agent-0.7.7.jar</argLine>
  </configuration></plugin></plugins></build>
</project>""",
        encoding="utf-8",
    )

    command = _with_jacoco_argline_bridge(
        [
            "mvn",
            "verify",
            "-Dtest.enforcement.enabled=true",
            f"-Dmaven.repo.local={local_repository}",
        ],
        repo,
    )

    assert (
        f"-DargLine=-javaagent:{agent}=destfile=target/jacoco.exec,append=true"
        in command
    )
    assert "testable-agent" in (repo / "pom.xml").read_text(encoding="utf-8")


def test_enforcement_does_not_bridge_correct_late_argline_or_override_caller(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pom = repo / "pom.xml"
    pom.write_text(
        """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <build><plugins><plugin><configuration>
    <argLine>@{argLine} -javaagent:testable-agent.jar</argLine>
  </configuration></plugin></plugins></build>
</project>""",
        encoding="utf-8",
    )
    original = ["mvn", "verify", "-Dtest.enforcement.enabled=true"]

    assert _with_jacoco_argline_bridge(original, repo) == original

    pom.write_text(pom.read_text(encoding="utf-8").replace("@{argLine}", "${argLine}"))
    explicit = [*original, "-DargLine=-Xmx512m"]
    assert _with_jacoco_argline_bridge(explicit, repo) == explicit


def test_enforcement_runner_applies_jacoco_argline_bridge_before_launch(tmp_path):
    local_repository = tmp_path / "maven-repository"
    agent = (
        local_repository
        / "org/jacoco/org.jacoco.agent/0.8.11/org.jacoco.agent-0.8.11-runtime.jar"
    )
    agent.parent.mkdir(parents=True)
    agent.write_bytes(b"agent")
    (tmp_path / "pom.xml").write_text(
        """<project><build><plugins><plugin><configuration>
  <argLine>${argLine} -javaagent:testable-agent.jar</argLine>
</configuration></plugin></plugins></build></project>""",
        encoding="utf-8",
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="build failed", stderr="")

    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
    )

    runner._run_command(
        [
            "mvn",
            "verify",
            "-Dtest.enforcement.enabled=true",
            f"-Dmaven.repo.local={local_repository}",
        ],
        tmp_path,
    )

    assert any(
        item.startswith(f"-DargLine=-javaagent:{agent}=") for item in calls[0]
    )


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
          <groupId>com.example</groupId>
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


def test_maven_tooling_status_rejects_test_enforcer_before_nested_module_fix(tmp_path):
    (tmp_path / "pom.xml").write_text(
        """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>demo</artifactId>
  <build>
    <plugins>
      <plugin>
        <groupId>com.example</groupId>
        <artifactId>test-enforcer</artifactId>
        <version>1.0.14</version>
      </plugin>
    </plugins>
  </build>
</project>
""",
        encoding="utf-8",
    )

    status = maven_tooling_status(tmp_path)

    assert status.available is False
    assert status.version == "1.0.14"
    assert "required 1.0.16" in status.reason


def test_maven_tooling_status_accepts_test_enforcer_1_0_16(tmp_path):
    (tmp_path / "pom.xml").write_text(
        """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>demo</artifactId>
  <build><plugins><plugin>
    <groupId>com.example</groupId>
    <artifactId>test-enforcer</artifactId>
    <version>1.0.16</version>
  </plugin></plugins></build>
</project>
""",
        encoding="utf-8",
    )

    status = maven_tooling_status(tmp_path)

    assert status.available is True
    assert status.version == "1.0.16"


def test_enforcement_runner_classifies_success_with_required_evidence(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[test-enforcer] Diff coverage: 100%\n"
                "[test-enforcer] diff mutation score 100.00% passed for demo "
                "(2/2 detected; 0 survived; 0 no coverage excluded)"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.passed
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

    assert result.status == QualityGateStatus.passed
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

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[test-enforcer] Diff coverage: 100%\n"
                "[test-enforcer] diff mutation score 100.00% passed for demo "
                "(2/2 detected; 0 survived; 0 no coverage excluded)"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(repo)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True
    assert "-DtargetTests=com.demo.FooServiceGeneratedTest" in calls[0]
    assert "-Dtest=FooServiceGeneratedTest" in calls[0]
    assert "-DfailIfNoTests=false" in calls[0]
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in calls[0]


def test_enforcement_runner_attaches_test_quality_for_selected_java_ci_tests(tmp_path):
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
    test.write_text(
        """
package com.demo;

import org.junit.Test;
import static org.junit.Assert.assertNotNull;

public class FooServiceTest {
    @Test
    public void returns_service() {
        assertNotNull(new FooService());
    }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    prod.write_text("package com.demo; class FooService { int value() { return 1; } }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(repo)

    assert result.status == QualityGateStatus.passed
    assert result.evidence["targetTests"] == ["com.demo.FooServiceTest"]
    assert result.evidence["testQuality"]["warningCount"] >= 1
    assert result.evidence["testQuality"]["topRuleIds"][0]["ruleId"] == "java-weak-not-null"
    assert result.evidence["testQuality"]["warnings"][0]["filePath"] == (
        "biz/src/test/java/com/demo/FooServiceTest.java"
    )


def test_enforcement_runner_scopes_maven_to_changed_modules(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    (repo / "pom.xml").write_text("<project><modules><module>common</module><module>service</module></modules></project>\n")
    (repo / "common/pom.xml").parent.mkdir()
    (repo / "common/pom.xml").write_text("<project><artifactId>common</artifactId></project>\n")
    (repo / "service/pom.xml").parent.mkdir()
    (repo / "service/pom.xml").write_text("<project><artifactId>service</artifactId></project>\n")
    prod = repo / "common/src/main/java/com/demo/FooService.java"
    test = repo / "common/src/test/java/com/demo/FooServiceTest.java"
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

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(repo)

    assert result.status == QualityGateStatus.passed
    assert "-pl" in calls[0]
    assert calls[0][calls[0].index("-pl") + 1] == "common"
    assert "-am" in calls[0]
    assert result.evidence["changedModules"] == ["common"]


def test_enforcement_runner_preserves_explicit_repair_target_source_scope(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    (repo / "pom.xml").write_text(
        "<project><modules><module>first</module><module>second</module></modules></project>\n",
        encoding="utf-8",
    )
    for module in ("first", "second"):
        (repo / module / "pom.xml").parent.mkdir()
        (repo / module / "pom.xml").write_text(
            f"<project><artifactId>{module}</artifactId></project>\n",
            encoding="utf-8",
        )
    first_prod = repo / "first/src/main/java/com/demo/FirstService.java"
    first_test = repo / "first/src/test/java/com/demo/FirstServiceTest.java"
    second_prod = repo / "second/src/main/java/com/demo/SecondService.java"
    second_test = repo / "second/src/test/java/com/demo/SecondServiceTest.java"
    for path in (first_prod, first_test, second_prod, second_test):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"package com.demo; class {path.stem} {{}}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    first_prod.write_text("package com.demo; class FirstService { int changed; }\n", encoding="utf-8")
    second_prod.write_text("package com.demo; class SecondService { int changed; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)
    calls = []

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "initialize" in cmd:
            assert (
                "-Dtest.enforcement.targetSource="
                "first/src/main/java/com/demo/FirstService.java"
            ) in cmd
            (repo / "target").mkdir(exist_ok=True)
            (repo / "target/filtered.diff").write_text(
                "diff --git a/first/src/main/java/com/demo/FirstService.java "
                "b/first/src/main/java/com/demo/FirstService.java\n"
                "+++ b/first/src/main/java/com/demo/FirstService.java\n"
                "@@ -1 +1 @@\n"
                "+package com.demo; class FirstService { int changed; }\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, target.sources=1 "
                    "[first/src/main/java/com/demo/FirstService.java], "
                    "pitest.targets=1 [com.demo.FirstService*]"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.demo.FirstService*]\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed\n"
                "[INFO] --- pitest:1.15.0:mutationCoverage (pitest) @ first ---\n"
                ">> Generated 1 mutations Killed 1 (100%)\n"
                ">> Mutations with no coverage 0. Test strength 100%\n"
                + DIFF_MUTATION_OK
                + "[INFO] BUILD SUCCESS\n"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(
        command=(
            "mvn -Dtest.enforcement.enabled=true verify "
            "-Dtest.enforcement.targetSource=first/src/main/java/com/demo/FirstService.java"
        ),
        run_command=fake_run,
        preserve_explicit_target_scope=True,
    )

    result = runner.run(repo)

    assert result.status == QualityGateStatus.passed
    assert "-DtargetTests=com.demo.FirstServiceTest" in calls[0]
    assert "-DtargetTests=com.demo.SecondServiceTest" not in calls[0]
    assert calls[0][calls[0].index("-pl") + 1] == "first"
    assert result.evidence["targetSources"] == ["first/src/main/java/com/demo/FirstService.java"]


def test_enforcement_runner_preserves_explicit_repair_target_source_scope_within_one_module(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    (repo / "pom.xml").write_text(
        "<project><modules><module>biz</module></modules></project>\n",
        encoding="utf-8",
    )
    (repo / "biz/pom.xml").parent.mkdir()
    (repo / "biz/pom.xml").write_text(
        "<project><artifactId>biz</artifactId></project>\n",
        encoding="utf-8",
    )
    first_prod = repo / "biz/src/main/java/com/demo/FirstService.java"
    first_test = repo / "biz/src/test/java/com/demo/FirstServiceTest.java"
    second_prod = repo / "biz/src/main/java/com/demo/SecondService.java"
    second_test = repo / "biz/src/test/java/com/demo/SecondServiceTest.java"
    for path in (first_prod, first_test, second_prod, second_test):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"package com.demo; class {path.stem} {{}}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    first_prod.write_text("package com.demo; class FirstService { int changed; }\n", encoding="utf-8")
    second_prod.write_text("package com.demo; class SecondService { int changed; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)
    calls = []

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "initialize" in cmd:
            assert (
                "-Dtest.enforcement.targetSource="
                "biz/src/main/java/com/demo/FirstService.java"
            ) in cmd
            (repo / "target").mkdir(exist_ok=True)
            (repo / "target/filtered.diff").write_text(
                "diff --git a/biz/src/main/java/com/demo/FirstService.java "
                "b/biz/src/main/java/com/demo/FirstService.java\n"
                "+++ b/biz/src/main/java/com/demo/FirstService.java\n"
                "@@ -1 +1 @@\n"
                "+package com.demo; class FirstService { int changed; }\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, target.sources=1 "
                    "[biz/src/main/java/com/demo/FirstService.java], "
                    "pitest.targets=1 [com.demo.FirstService*]"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.demo.FirstService*]\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed\n"
                "[INFO] --- pitest:1.15.0:mutationCoverage (pitest) @ biz ---\n"
                ">> Generated 1 mutations Killed 1 (100%)\n"
                ">> Mutations with no coverage 0. Test strength 100%\n"
                + DIFF_MUTATION_OK
                + "[INFO] BUILD SUCCESS\n"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(
        command=(
            "mvn -Dtest.enforcement.enabled=true verify "
            "-Dtest.enforcement.targetSource=biz/src/main/java/com/demo/FirstService.java"
        ),
        run_command=fake_run,
        preserve_explicit_target_scope=True,
    )

    result = runner.run(repo)

    # One call: the explicit scope is read from the command UTA wrote, not from
    # a preflight that asks Maven to echo it back.
    assert len(calls) == 1
    assert result.status == QualityGateStatus.passed
    assert "-DtargetTests=com.demo.FirstServiceTest" in calls[0]
    assert "-DtargetTests=com.demo.SecondServiceTest" not in calls[0]
    target_sources_arg = next(item for item in calls[0] if item.startswith("-Dtest.enforcement.targetSources="))
    assert target_sources_arg == "-Dtest.enforcement.targetSources=biz/src/main/java/com/demo/FirstService.java"
    assert calls[0][calls[0].index("-pl") + 1] == "biz"
    assert result.evidence["targetSources"] == ["biz/src/main/java/com/demo/FirstService.java"]


def test_enforcement_runner_keeps_filtered_target_without_matching_test_in_scope(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    (repo / "pom.xml").write_text("<project><modules><module>common</module><module>service</module></modules></project>\n")
    (repo / "common/pom.xml").parent.mkdir()
    (repo / "common/pom.xml").write_text("<project><artifactId>common</artifactId></project>\n")
    (repo / "service/pom.xml").parent.mkdir()
    (repo / "service/pom.xml").write_text("<project><artifactId>service</artifactId></project>\n")
    common_prod = repo / "common/src/main/java/com/demo/common/MissingCoverageTarget.java"
    service_prod = repo / "service/src/main/java/com/demo/service/LogisticsRiskProcessRecordServiceImpl.java"
    service_test = repo / "service/src/test/java/com/demo/service/LogisticsRiskProcessRecordServiceImplTest.java"
    common_prod.parent.mkdir(parents=True)
    service_prod.parent.mkdir(parents=True)
    service_test.parent.mkdir(parents=True)
    common_prod.write_text("package com.demo.common; class MissingCoverageTarget {}\n", encoding="utf-8")
    service_prod.write_text("package com.demo.service; class LogisticsRiskProcessRecordServiceImpl {}\n", encoding="utf-8")
    service_test.write_text("package com.demo.service; class LogisticsRiskProcessRecordServiceImplTest {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    common_prod.write_text("package com.demo.common; class MissingCoverageTarget { int v; }\n", encoding="utf-8")
    service_prod.write_text(
        "package com.demo.service; class LogisticsRiskProcessRecordServiceImpl { int v; }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)
    calls = []

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "initialize" in cmd:
            (repo / "target").mkdir()
            (repo / "target/filtered.diff").write_text(
                "diff --git a/common/src/main/java/com/demo/common/MissingCoverageTarget.java "
                "b/common/src/main/java/com/demo/common/MissingCoverageTarget.java\n"
                "+++ b/common/src/main/java/com/demo/common/MissingCoverageTarget.java\n"
                "@@ -1 +1 @@\n"
                "+package com.demo.common; class MissingCoverageTarget { int v; }\n"
                "diff --git a/service/src/main/java/com/demo/service/LogisticsRiskProcessRecordServiceImpl.java "
                "b/service/src/main/java/com/demo/service/LogisticsRiskProcessRecordServiceImpl.java\n"
                "+++ b/service/src/main/java/com/demo/service/LogisticsRiskProcessRecordServiceImpl.java\n"
                "@@ -1 +1 @@\n"
                "+package com.demo.service; class LogisticsRiskProcessRecordServiceImpl { int v; }\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                    "pitest.targets=2 [com.demo.common.MissingCoverageTarget*,com.demo.service.LogisticsRiskProcessRecordServiceImpl*]"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="diff line coverage 60.00% is below required 95.00%",
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(repo)

    assert result.status == QualityGateStatus.failed
    assert "-DtargetTests=com.demo.service.LogisticsRiskProcessRecordServiceImplTest" in calls[0]
    target_sources_arg = next(item for item in calls[0] if item.startswith("-Dtest.enforcement.targetSources="))
    assert "common/src/main/java/com/demo/common/MissingCoverageTarget.java" in target_sources_arg
    assert "service/src/main/java/com/demo/service/LogisticsRiskProcessRecordServiceImpl.java" in target_sources_arg
    assert calls[0][calls[0].index("-pl") + 1] == "common,service"
    assert result.evidence["changedModules"] == ["common", "service"]
    assert result.evidence["targetSources"] == [
        "common/src/main/java/com/demo/common/MissingCoverageTarget.java",
        "service/src/main/java/com/demo/service/LogisticsRiskProcessRecordServiceImpl.java",
    ]


def test_enforcement_runner_overrides_repo_local_java_gate_properties(tmp_path):
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

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
            stderr="",
        )

    runner = MavenEnforcementRunner(
        command=(
            "mvn -Dtest.enforcement.enabled=true "
            "-Dtest.enforcement.diff.coverage.minLines=0.50 "
            "-Dtest.enforcement.pitest.testStrengthThreshold=85 verify"
        ),
        run_command=fake_run,
        coverage_gate=95,
        mutation_gate=100,
    )

    result = runner.run(repo)

    assert result.status == QualityGateStatus.passed
    assert "-Dtest.enforcement.diff.coverage.minLines=0.50" not in calls[0]
    assert "-Dtest.enforcement.pitest.testStrengthThreshold=85" not in calls[0]
    assert "-Dtest.enforcement.diff.coverage.minLines=0.95" in calls[0]
    assert "-Dtest.enforcement.pitest.testStrengthThreshold=100" in calls[0]


def test_enforcement_runner_overrides_explicit_maven_project_selector(tmp_path):
    cmd = MavenEnforcementRunner._with_changed_modules(
        ["mvn", "-Dtest.enforcement.enabled=true", "verify", "-pl", "custom"],
        ["common"],
    )

    assert cmd == ["mvn", "-Dtest.enforcement.enabled=true", "verify", "-pl", "common", "-am"]


def test_enforcement_runner_ignores_unrelated_changed_tests_for_target_tests(tmp_path):
    repo = tmp_path / "repo"
    related_test = repo / "biz/src/test/java/com/demo/FooServiceGeneratedTest.java"
    unrelated_test = repo / "provider/src/test/java/com/demo/CameraSnapshotTaskTest.java"
    related_test.parent.mkdir(parents=True)
    unrelated_test.parent.mkdir(parents=True)
    related_test.write_text("package com.demo; class FooServiceGeneratedTest {}\n", encoding="utf-8")
    unrelated_test.write_text("package com.demo; class CameraSnapshotTaskTest {}\n", encoding="utf-8")

    target_tests = MavenEnforcementRunner._pitest_target_tests(
        repo,
        [
            "biz/src/main/java/com/demo/FooService.java",
            "biz/src/test/java/com/demo/FooServiceGeneratedTest.java",
            "provider/src/test/java/com/demo/CameraSnapshotTaskTest.java",
        ],
        ["biz/src/main/java/com/demo/FooService.java"],
    )

    assert target_tests == ["com.demo.FooServiceGeneratedTest"]


def test_enforcement_runner_uses_same_module_changed_behavior_named_test_with_target_reference(tmp_path):
    repo = tmp_path / "repo"
    pom = repo / "service/pom.xml"
    prod = repo / "service/src/main/java/com/demo/FooService.java"
    behavior_named_test = repo / "service/src/test/java/com/demo/FooFranchiseGuardTest.java"
    pom.parent.mkdir(parents=True)
    prod.parent.mkdir(parents=True)
    behavior_named_test.parent.mkdir(parents=True)
    pom.write_text("<project><artifactId>service</artifactId></project>\n", encoding="utf-8")
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    behavior_named_test.write_text(
        "package com.demo; class FooFranchiseGuardTest { "
        "void test() { new FooService(); } }\n",
        encoding="utf-8",
    )

    target_tests = MavenEnforcementRunner._pitest_target_tests(
        repo,
        [
            "service/src/main/java/com/demo/FooService.java",
            "service/src/test/java/com/demo/FooFranchiseGuardTest.java",
        ],
        ["service/src/main/java/com/demo/FooService.java"],
    )

    assert target_tests == ["com.demo.FooFranchiseGuardTest"]


def test_enforcement_runner_does_not_treat_comment_or_string_as_target_reference(tmp_path):
    repo = tmp_path / "repo"
    pom = repo / "service/pom.xml"
    prod = repo / "service/src/main/java/com/demo/FooService.java"
    behavior_named_test = repo / "service/src/test/java/com/demo/FooFranchiseGuardTest.java"
    pom.parent.mkdir(parents=True)
    prod.parent.mkdir(parents=True)
    behavior_named_test.parent.mkdir(parents=True)
    pom.write_text("<project><artifactId>service</artifactId></project>\n", encoding="utf-8")
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    behavior_named_test.write_text(
        "package com.demo; class FooFranchiseGuardTest { "
        "// new FooService()\n String hint = \"FooService.class\"; }\n",
        encoding="utf-8",
    )

    target_tests = MavenEnforcementRunner._pitest_target_tests(
        repo,
        [
            "service/src/main/java/com/demo/FooService.java",
            "service/src/test/java/com/demo/FooFranchiseGuardTest.java",
        ],
        ["service/src/main/java/com/demo/FooService.java"],
    )

    assert target_tests == []


def test_enforcement_runner_does_not_use_unrelated_changed_test_as_fallback(tmp_path):
    repo = tmp_path / "repo"
    unrelated_test = repo / "provider/src/test/java/com/demo/CameraSnapshotTaskTest.java"
    unrelated_test.parent.mkdir(parents=True)
    unrelated_test.write_text("package com.demo; class CameraSnapshotTaskTest {}\n", encoding="utf-8")

    target_tests = MavenEnforcementRunner._pitest_target_tests(
        repo,
        [
            "biz/src/main/java/com/demo/PunchBizImpl.java",
            "provider/src/test/java/com/demo/CameraSnapshotTaskTest.java",
        ],
        ["biz/src/main/java/com/demo/PunchBizImpl.java"],
    )

    assert target_tests == []


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

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(repo)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True
    assert "-DtargetTests=com.demo.FooServiceExistingTest" in calls[0]
    assert "-Dtest=FooServiceExistingTest" in calls[0]
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in calls[0]


def test_enforcement_runner_rejects_cross_module_matching_test_for_target_tests(tmp_path):
    repo = tmp_path / "repo"
    service_pom = repo / "service/pom.xml"
    provider_pom = repo / "provider/pom.xml"
    prod = repo / "service/src/main/java/com/demo/FooService.java"
    cross_module_test = repo / "provider/src/test/java/com/demo/FooServiceTest.java"
    service_pom.parent.mkdir(parents=True)
    provider_pom.parent.mkdir(parents=True)
    prod.parent.mkdir(parents=True)
    cross_module_test.parent.mkdir(parents=True)
    service_pom.write_text("<project><artifactId>service</artifactId></project>\n", encoding="utf-8")
    provider_pom.write_text("<project><artifactId>provider</artifactId></project>\n", encoding="utf-8")
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    cross_module_test.write_text("package com.demo; class FooServiceTest {}\n", encoding="utf-8")

    target_tests = MavenEnforcementRunner._pitest_target_tests(
        repo,
        [
            "service/src/main/java/com/demo/FooService.java",
            "provider/src/test/java/com/demo/FooServiceTest.java",
        ],
        ["service/src/main/java/com/demo/FooService.java"],
    )

    assert target_tests == []


def test_enforcement_runner_prefers_same_module_matching_test_over_cross_module_test(tmp_path):
    repo = tmp_path / "repo"
    service_pom = repo / "service/pom.xml"
    provider_pom = repo / "provider/pom.xml"
    prod = repo / "service/src/main/java/com/demo/FooService.java"
    same_module_test = repo / "service/src/test/java/com/demo/FooServiceTest.java"
    cross_module_test = repo / "provider/src/test/java/com/demo/FooServiceProviderTest.java"
    service_pom.parent.mkdir(parents=True)
    provider_pom.parent.mkdir(parents=True)
    prod.parent.mkdir(parents=True)
    same_module_test.parent.mkdir(parents=True)
    cross_module_test.parent.mkdir(parents=True)
    service_pom.write_text("<project><artifactId>service</artifactId></project>\n", encoding="utf-8")
    provider_pom.write_text("<project><artifactId>provider</artifactId></project>\n", encoding="utf-8")
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    same_module_test.write_text("package com.demo; class FooServiceTest {}\n", encoding="utf-8")
    cross_module_test.write_text("package com.demo; class FooServiceProviderTest {}\n", encoding="utf-8")

    target_tests = MavenEnforcementRunner._pitest_target_tests(
        repo,
        [
            "service/src/main/java/com/demo/FooService.java",
            "provider/src/test/java/com/demo/FooServiceProviderTest.java",
        ],
        ["service/src/main/java/com/demo/FooService.java"],
    )

    assert target_tests == ["com.demo.FooServiceTest"]


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

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
            stderr="",
        )

    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true -Dtest=CustomSmokeTest verify",
        run_command=fake_run,
    )

    result = runner.run(repo)

    assert result.status == QualityGateStatus.passed
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
        <groupId>com.example</groupId>
        <artifactId>test-enforcer</artifactId>
        <version>1.0.16</version>
      </plugin>
    </plugins>
  </build>
</project>
""",
            calls,
        ),
    )

    result = runner.run(repo)

    assert result.status == QualityGateStatus.missing_evidence
    assert result.passed is False
    assert "targetTests" in result.summary
    assert result.evidence and result.evidence["coverage"]["rate"] == 0.0
    assert result.evidence["baseRef"] == "origin/master"
    assert result.evidence["changedProductionFiles"] == ["src/main/java/com/demo/UntestedService.java"]
    assert result.evidence["changedClasses"] == ["com.demo.UntestedService"]
    assert result.evidence["targetTests"] == []
    assert result.evidence["tooling"]["available"] is True
    assert result.evidence["tooling"]["artifactId"] == "test-enforcer"
    assert any("help:effective-pom" in call for call in calls)


def test_enforcement_runner_detects_test_enforcer_in_multimodule_effective_pom(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.test"], check=True)
    (repo / "pom.xml").write_text(
        """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>demo-parent</artifactId>
</project>
""",
        encoding="utf-8",
    )
    prod = repo / "demo-biz/src/main/java/com/demo/UntestedService.java"
    prod.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class UntestedService {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    prod.write_text("package com.demo; class UntestedService { int v; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)

    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=_effective_pom_run(
            """
<projects>
  <project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <artifactId>demo-parent</artifactId>
  </project>
  <project xmlns="http://maven.apache.org/POM/4.0.0">
    <modelVersion>4.0.0</modelVersion>
    <artifactId>demo-biz</artifactId>
    <properties>
      <test-enforcer.version>1.0.16</test-enforcer.version>
    </properties>
    <build>
      <plugins>
        <plugin>
          <groupId>com.example</groupId>
          <artifactId>test-enforcer</artifactId>
          <version>${test-enforcer.version}</version>
        </plugin>
      </plugins>
    </build>
  </project>
</projects>
""",
        ),
    )

    result = runner.run(repo)

    assert result.status == QualityGateStatus.missing_evidence
    assert result.evidence is not None
    assert result.evidence["tooling"]["available"] is True
    assert result.evidence["tooling"]["artifactId"] == "test-enforcer"
    assert result.evidence["tooling"]["version"] == "1.0.16"
    assert "targetTests" in result.summary


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
    <groupId>com.example.platform</groupId>
    <artifactId>example-root</artifactId>
    <version>1.3.19</version>
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
    <groupId>com.example.platform</groupId>
    <artifactId>example-root</artifactId>
    <version>1.3.19</version>
  </parent>
  <artifactId>demo</artifactId>
</project>
""",
        ),
    ).run(repo)

    assert result.status == QualityGateStatus.missing_evidence
    # The message names what the project declares, not only the requirement:
    # whoever reads the report should not have to go and find the version.
    assert "example-root declares 1.3.19" in result.summary
    assert "1.3.97 or newer is required" in result.summary
    assert result.evidence and result.evidence["tooling"]["available"] is False
    assert result.evidence["tooling"]["artifactId"] == "example-root"
    assert result.evidence["tooling"]["version"] == "1.3.19"


def test_enforcement_runner_blocks_outdated_tooling_even_when_target_tests_exist(tmp_path):
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
    <groupId>com.example.common</groupId>
    <artifactId>example-parent-generic</artifactId>
    <version>1.0.20</version>
  </parent>
  <artifactId>demo</artifactId>
</project>
""",
        encoding="utf-8",
    )
    prod = repo / "src/main/java/com/demo/FooService.java"
    test = repo / "src/test/java/com/demo/FooServiceTest.java"
    prod.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class FooService { int value() { return 1; } }\n", encoding="utf-8")
    test.write_text("package com.demo; class FooServiceTest {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    prod.write_text("package com.demo; class FooService { int value() { return 2; } }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)

    calls = []
    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=_effective_pom_run(
            """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.example.common</groupId>
    <artifactId>example-parent-generic</artifactId>
    <version>1.0.20</version>
  </parent>
  <artifactId>demo</artifactId>
</project>
""",
            calls,
        ),
    )

    result = runner.run(repo)

    assert result.status == QualityGateStatus.missing_evidence
    assert "example-parent-generic declares 1.0.20" in result.summary
    assert "1.0.22 or newer is required" in result.summary
    assert result.evidence["targetTests"] == ["com.demo.FooServiceTest"]
    assert result.evidence["tooling"] == {
        "available": False,
        "artifactId": "example-parent-generic",
        "version": "1.0.20",
        "requiredVersion": "1.0.22",
        "reason": "example-parent-generic 1.0.20 is below the rollout version that introduces test-enforcer",
    }
    assert len(calls) == 1 and "help:effective-pom" in calls[0]


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
    <groupId>com.example.platform</groupId>
    <artifactId>example-root</artifactId>
    <version>1.3.97</version>
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

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo (1/1)\n"
                "[info] [test-enforcer] diff mutation score 100.00% passed for demo "
                "(1/1 detected; 0 survived; 0 no coverage excluded)\n"
            ),
            stderr="",
        )

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
    ).run(repo)

    assert result.status == QualityGateStatus.passed
    assert "-Pdev" in calls[0]


def test_enforcement_runner_ignores_unrelated_maven_test_failures_after_gate_evidence(tmp_path):
    from tests.fake_maven_metadata import with_completion
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return with_completion(cmd, subprocess.CompletedProcess(
            cmd,
            1,
            stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
            stderr="[ERROR] There are test failures in unrelated existing tests.",
        ))

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True
    assert result.returncode == 1
    assert "non-zero" in result.summary


def test_enforcement_runner_keeps_gate_failure_blocking_even_with_evidence(tmp_path):
    @with_resolved_enforcer
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

    assert result.status == QualityGateStatus.failed
    assert result.passed is False


def test_enforcement_runner_blocks_pit_test_strength_below_threshold(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=(
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.demo.InvoiceBiz*]\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (5/5)\n"
                ">> Generated 177 mutations Killed 1 (1%)\n"
                ">> Mutations with no coverage 173. Test strength 25%\n"
                "[ERROR] Failed to execute goal org.pitest:pitest-maven:1.15.0:mutationCoverage "
                "(pitest) on project demo.biz: Test strength score of 25 is below threshold of 100\n"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.failed
    assert result.passed is False
    assert result.summary == "UTA test-enforcement failed: Mutation gate failed: 25% < 100%"


def test_enforcement_runner_does_not_treat_raw_pit_strength_as_diff_gate(tmp_path):
    completed = subprocess.CompletedProcess(
        ["mvn"],
        0,
        stdout=(
            "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
            "pitest.targets=1 [com.demo.ChangedService*]\n"
            "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
            "PIT generated=46 killed=40 survived=6 test-strength=86.96%\n"
            "[INFO] BUILD SUCCESS"
        ),
        stderr="",
    )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", mutation_gate=100)

    result = runner._classify_completed(["mvn"], completed, tmp_path)

    assert result.status == QualityGateStatus.missing_evidence
    assert result.passed is False
    assert "Missing UTA test-enforcement" in result.summary


def test_enforcement_runner_rejects_incomplete_diff_mutation_marker(tmp_path):
    completed = subprocess.CompletedProcess(
        ["mvn"],
        0,
        stdout=(
            "[test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
            "[test-enforcer] diff mutation score 100.00% for demo.biz (4/4 detected)\n"
            "[INFO] BUILD SUCCESS\n"
        ),
        stderr="",
    )

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify"
    )._classify_completed(["mvn"], completed, tmp_path)

    assert result.status == QualityGateStatus.missing_evidence
    assert result.passed is False


def test_enforcement_runner_rejects_abbreviated_diff_mutation_pass_marker(tmp_path):
    completed = subprocess.CompletedProcess(
        ["mvn"],
        0,
        stdout=(
            "[test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
            "[test-enforcer] diff mutation score 100.00% passed for demo.biz (4/4 detected)\n"
            "[INFO] BUILD SUCCESS\n"
        ),
        stderr="",
    )

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify"
    )._classify_completed(["mvn"], completed, tmp_path)

    assert result.status == QualityGateStatus.missing_evidence
    assert result.passed is False


def test_enforcement_runner_accepts_explicit_diff_mutation_pass_over_raw_pit_strength(tmp_path):
    completed = subprocess.CompletedProcess(
        ["mvn"],
        0,
        stdout=(
            "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
            "pitest.targets=1 [com.demo.ChangedService*]\n"
            "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
            "PIT generated=46 killed=40 survived=6 test-strength=86.96%\n"
            "[info] [test-enforcer] diff mutation score 100.00% passed for demo.biz "
            "(40/40 detected; 0 survived; 6 no coverage excluded)\n"
            "[INFO] BUILD SUCCESS"
        ),
        stderr="",
    )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", mutation_gate=100)

    result = runner._classify_completed(["mvn"], completed, tmp_path)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True


def test_enforcement_runner_records_filtered_target_classes_from_pitest_targets(tmp_path):
    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify")
    evidence = {
        "changedClasses": [
            "com.wrompex.idss.mmc.horae.man.biz.PunchBiz",
            "com.wrompex.idss.mmc.horae.man.biz.impl.PunchBizImpl",
            "com.example.idss.mmc.horae.man.model.common.vo.RestPrecheckVO",
            "com.wrompex.idss.mmc.horae.man.service.face.FaceService",
            "com.wrompex.idss.mmc.horae.man.pad.PdaPunchController",
        ],
    }
    completed = subprocess.CompletedProcess(
        ["mvn"],
        1,
        stdout=(
            "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
            "pitest.targets=2 ["
            "com.wrompex.idss.mmc.horae.man.service.face.FaceService*,"
            "com.wrompex.idss.mmc.horae.man.biz.impl.PunchBizImpl*"
            "]\n"
            "test-enforcer check-coverage failed: diff line coverage 50.00% is below required 95.00%\n"
        ),
        stderr="",
    )

    result = runner._classify_completed(["mvn"], completed, tmp_path, evidence=evidence)

    assert result.status == QualityGateStatus.failed
    assert result.evidence["filteredTargetClasses"] == [
        "com.wrompex.idss.mmc.horae.man.service.face.FaceService",
        "com.wrompex.idss.mmc.horae.man.biz.impl.PunchBizImpl",
    ]
    assert result.evidence["changedClasses"] == evidence["changedClasses"]


def test_enforcement_runner_rejects_scoped_raw_pit_summary_without_diff_verdict(tmp_path):
    @with_resolved_enforcer
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

    assert result.status == QualityGateStatus.missing_evidence
    assert result.passed is False


def test_enforcement_runner_does_not_let_empty_reactor_modules_hide_missing_mutation_verdict(tmp_path):
    """A root-module no-lines marker cannot satisfy a scoped child-module gate."""

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.demo.ChangedService*]\n"
                "[info] [test-enforcer] no changed Java source lines for demo-root\n"
                "[info] [test-enforcer] no changed Java source lines for demo-domain\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo-biz (36/36)\n"
                ">> Generated 19 mutations Killed 14 (74%)\n"
                ">> Mutations with no coverage 5. Test strength 100%\n"
                "[INFO] BUILD SUCCESS\n"
            ),
            stderr="",
        )

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
    ).run(tmp_path)

    assert result.status == QualityGateStatus.missing_evidence
    assert result.passed is False


def test_enforcement_runner_accepts_explicit_diff_verdict_when_all_mutants_killed(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.demo.ChangedService*]\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
                ">> Generated 51 mutations Killed 51 (100%)\n"
                ">> Mutations with no coverage 0. Test strength 100%\n"
                + DIFF_MUTATION_OK
                + "[INFO] BUILD SUCCESS"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True
    assert result.summary == "UTA test-enforcement passed"


def test_enforcement_runner_does_not_hide_module_evidence_behind_root_no_lines(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[INFO] --- test-enforcer:1.0.12:check-coverage (check-coverage) @ demo-root ---\n"
                "[info] [test-enforcer] no changed Java source lines for demo-root\n"
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.demo.ChangedService*]\n"
                "[INFO] --- test-enforcer:1.0.12:check-coverage (check-coverage) @ demo-biz ---\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo-biz (2/2)\n"
                "[INFO] --- pitest:1.15.0:mutationCoverage (pitest) @ demo-biz ---\n"
                ">> Generated 95 mutations Killed 66 (69%)\n"
                ">> Mutations with no coverage 29. Test strength 100%\n"
                + DIFF_MUTATION_OK
                + "[INFO] BUILD SUCCESS\n"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True
    assert result.summary == "UTA test-enforcement passed"


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

    assert result.status == QualityGateStatus.missing_evidence
    assert result.passed is False


def test_enforcement_runner_accepts_zero_pitest_targets_with_no_scored_verdict(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                    "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, pitest.targets=0 []\n"
                    "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
                    "[info] [test-enforcer] no scored PIT mutations on changed lines for demo.biz "
                    "(0/0 scored; 0 no coverage excluded)\n"
                    "[INFO] BUILD SUCCESS"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True


def test_enforcement_runner_rejects_incomplete_no_scored_changed_line_marker(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (2/2)\n"
                "[info] [test-enforcer] no scored PIT mutations on changed lines for demo.biz "
                "(0 survived; 0 no coverage excluded)\n"
                "[INFO] BUILD SUCCESS\n"
            ),
            stderr="",
        )

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
    ).run(tmp_path)

    assert result.status == QualityGateStatus.missing_evidence
    assert result.passed is False


def test_enforcement_runner_passes_when_filter_diff_has_no_enforceable_java_lines(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[INFO] --- test-enforcer:1.0.12:filter-diff (filter-diff) @ demo-root ---\n"
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, pitest.targets=0\n"
                "[INFO] --- test-enforcer:1.0.12:check-coverage (check-coverage) @ demo-root ---\n"
                "[info] [test-enforcer] no changed Java source lines for demo-root\n"
                "[INFO] --- test-enforcer:1.0.12:check-coverage (check-coverage) @ demo-biz ---\n"
                "[info] [test-enforcer] no changed Java source lines for demo-biz\n"
                "[INFO] --- pitest:1.15.0:mutationCoverage (pitest) @ demo-biz ---\n"
                "[INFO] Skipping project because:\n"
                "[INFO]   - Execution of PIT should be skipped.\n"
                "[INFO] BUILD SUCCESS\n"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True
    assert "no changed Java source lines after filtering" in result.summary


def test_enforcement_runner_blocks_pitest_baseline_failure_when_no_test_class_isolated(tmp_path):
    @with_resolved_enforcer
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

    assert result.status == QualityGateStatus.failed
    assert result.passed is False
    assert "PIT baseline tests were not green" in result.summary


def test_enforcement_runner_blocks_coverage_gate_failure_after_filter_diff(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=(
                "[INFO] --- test-enforcer:1.0.10:filter-diff ---\n"
                "[ERROR] Failed to execute goal com.example:test-enforcer:1.0.10:check-coverage\n"
                "[ERROR] test-enforcer check-coverage failed: "
                "diff line coverage 87.50% is below required 95.00% (126/144)\n"
                "PIT generated=2 killed=2 survived=0 test-strength=100%"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.failed
    assert result.passed is False


def test_enforcement_runner_blocks_dependency_resolution_failure_after_gate_keywords(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=(
                "[INFO] --- jacoco-maven-plugin:prepare-agent ---\n"
                "[INFO] --- test-enforcer:1.0.10:filter-diff ---\n"
                "[INFO] pitest.targets=com.demo.*\n"
                "[ERROR] Could not resolve dependencies for project com.demo:demo-service:jar:1.0\n"
                "[ERROR] Could not find artifact com.demo:demo-api:jar:TASK-82768-SNAPSHOT\n"
                "[ERROR] org.apache.maven.project.DependencyResolutionException"
            ),
            stderr="",
        )

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.failed
    assert result.passed is False
    assert "compile or resolve" in result.summary


def test_enforcement_runner_blocks_compilation_failure_after_gate_keywords(tmp_path):
    @with_resolved_enforcer
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

    assert result.status == QualityGateStatus.failed
    assert result.passed is False


def test_enforcement_runner_classifies_missing_evidence_on_green_command(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="BUILD SUCCESS", stderr="")

    runner = MavenEnforcementRunner(command="mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run)

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.missing_evidence
    assert result.passed is False
    assert result.summary == MISSING_EVIDENCE_SUMMARY
    assert "test-enforcer >= 1.0.16" in result.summary
    assert "example-parent-generic >= 1.0.22" in result.summary
    assert "example-root >= 1.3.97" in result.summary
    assert result.usage_guide == TEST_ENFORCEMENT_USAGE_GUIDE


def test_enforcement_runner_attaches_tooling_mismatch_to_dependency_resolution_failure(tmp_path):
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
    <groupId>com.example.common</groupId>
    <artifactId>example-parent-generic</artifactId>
    <version>1.0.8</version>
  </parent>
  <artifactId>demo</artifactId>
</project>
""",
        encoding="utf-8",
    )
    prod = repo / "src/main/java/com/demo/FooService.java"
    test = repo / "src/test/java/com/demo/FooServiceTest.java"
    prod.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class FooService { int value() { return 1; } }\n", encoding="utf-8")
    test.write_text("package com.demo; class FooServiceTest {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    prod.write_text("package com.demo; class FooService { int value() { return 2; } }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "feature"], check=True, capture_output=True, text=True)

    def fake_run(cmd, **kwargs):
        if "help:effective-pom" in cmd:
            output_arg = next(item for item in cmd if str(item).startswith("-Doutput="))
            Path(str(output_arg).split("=", 1)[1]).write_text(
                """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>com.example.common</groupId>
    <artifactId>example-parent-generic</artifactId>
    <version>1.0.8</version>
  </parent>
  <artifactId>demo</artifactId>
</project>
""",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout=(
                "[ERROR] Failed to execute goal on project demo: Could not collect dependencies\n"
                "[ERROR] Failed to read artifact descriptor for com.example.inf:wmq-api:jar:2.1.5\n"
            ),
            stderr="",
        )

    result = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=fake_run,
    ).run(repo)

    assert result.status == QualityGateStatus.missing_evidence
    assert "example-parent-generic declares 1.0.8" in result.summary
    assert "1.0.22 or newer is required" in result.summary
    assert result.evidence["tooling"]["available"] is False
    assert result.evidence["tooling"]["artifactId"] == "example-parent-generic"
    assert result.evidence["tooling"]["version"] == "1.0.8"
    assert result.evidence["tooling"]["requiredVersion"] == "1.0.22"


def test_enforcement_runner_classifies_failure_timeout_and_command_error(tmp_path):
    failure = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=with_resolved_enforcer(lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="test failed")),
    ).run(tmp_path)
    assert failure.status == QualityGateStatus.failed

    @with_resolved_enforcer
    def timeout_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=1)

    timeout = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=timeout_run,
    ).run(tmp_path)
    assert timeout.status == QualityGateStatus.timeout

    @with_resolved_enforcer
    def command_error_run(cmd, **kwargs):
        raise OSError("mvn missing")

    command_error = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=command_error_run,
    ).run(tmp_path)
    assert command_error.status == QualityGateStatus.command_error


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


def _cli_option_values(cmd: list[str], option: str) -> list[str]:
    values = []
    for index, item in enumerate(cmd):
        if item == option and index + 1 < len(cmd):
            values.append(cmd[index + 1])
        elif item.startswith(f"{option}="):
            values.append(item.split("=", 1)[1])
    return values


def test_python_enforcement_runner_accepts_uta_json_evidence(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence(head)), stderr="")

    runner = PythonEnforcementRunner(command="uta python-enforce", timeout_seconds=7200, run_command=fake_run)

    result = runner.run(repo)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True
    assert result.summary == "Python enforcement passed"
    assert result.command[:2] == ["uta", "python-enforce"]
    assert "--json-output" in result.command
    assert result.evidence["headCommit"] == head
    assert calls[0][0] == result.command
    assert calls[0][1]["timeout"] == 7200
    assert calls[0][1]["env"]["UTA_PYTHON_GATE_TIMEOUT_SECONDS"] == "7200"
    assert calls[0][1]["env"]["UTA_INTERNAL_PYTHON_ENFORCEMENT_PROFILE"] == "ci_report_v1"
    assert "UTA_PYTHON_ENABLE_CI_MUTATION_SAMPLING" not in calls[0][1]["env"]


def test_python_enforcement_runner_reads_large_evidence_from_dedicated_file(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    evidence = _python_evidence(head, passed=False, reason="coverage_gate_failed")
    evidence["diagnostics"] = "x" * (9 * 1024 * 1024)

    def fake_run(cmd, **kwargs):
        output_paths = _cli_option_values(cmd, "--evidence-output")
        assert len(output_paths) == 1
        Path(output_paths[0]).write_text(json.dumps(evidence), encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="{\n... output truncated: 9000000 bytes omitted ...\n}",
            stderr="",
        )

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    assert result.status == QualityGateStatus.failed
    assert result.evidence["reasonCode"] == "coverage_gate_failed"
    assert "did not produce UTA evidence" not in result.summary


def test_python_enforcement_runner_in_process_adapter_enables_ci_sampling(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    calls = []

    def fake_enforcer(**kwargs):
        calls.append(kwargs)
        return _python_evidence(head)

    runner = PythonEnforcementRunner(
        command="uta python-enforce",
        timeout_seconds=7200,
        use_in_process_adapter=True,
        in_process_enforcer=fake_enforcer,
    )

    result = runner.run(repo)

    assert result.status == QualityGateStatus.passed
    assert calls[0]["repo_path"] == repo
    assert calls[0]["enable_ci_mutation_sampling"] is True
    assert calls[0]["base_ref"] == "origin/master"


def test_python_enforcement_runner_reports_process_group_memory_exhaustion(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _init_python_repo(repo)
    calls = []

    def fake_resource_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd,
            137,
            stdout="",
            stderr="UTA_RESOURCE_EXHAUSTED process-group-rss=3221225472 limit=3221225472",
        )

    monkeypatch.setattr(
        "uta.language.python.enforcement_runner.run_resource_bounded_command",
        fake_resource_run,
    )
    runner = PythonEnforcementRunner(
        command="uta python-enforce",
        memory_limit_mb=3072,
    )

    result = runner.run(repo)

    assert calls[0][1]["memory_limit_bytes"] == 3072 * 1024 * 1024
    assert result.status == QualityGateStatus.failed
    assert result.evidence["reasonCode"] == "mutation_resource_exhausted"
    assert "3.00 GiB" in result.summary


def test_python_enforcement_runner_restores_mutmut_pyproject_overlay(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    original_pyproject = (
        "[tool.mutmut]\n"
        "source_paths = [\"jobs/\"]\n"
        "pytest_add_cli_args_test_selection = [\"tests/test_old.py\", \"-x\", \"-q\"]\n"
    )
    (repo / "pyproject.toml").write_text(original_pyproject, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "pyproject.toml"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "add pyproject"], check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def fake_run(cmd, **kwargs):
        (repo / "pyproject.toml").write_text(
            "[tool.mutmut]\n"
            "paths_to_mutate = [\"jobs/forecast.py\"]\n"
            "pytest_add_cli_args_test_selection = [\"tests/uta_generated/test_jobs_forecast.py\", \"-x\", \"-q\"]\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence(head)), stderr="")

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    assert result.status == QualityGateStatus.passed
    assert (repo / "pyproject.toml").read_text(encoding="utf-8") == original_pyproject


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

    assert result.status == QualityGateStatus.passed
    assert calls[0].count("--repo") == 0
    assert "--repo=." in calls[0]
    assert calls[0].count("--base-ref") == 0
    assert "--base-ref=origin/main" in calls[0]
    assert calls[0].count("--json-output") == 1


def test_python_enforcement_runner_auto_adds_discovered_test_paths(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_forecast.py").write_text("def test_run():\n    assert True\n", encoding="utf-8")
    (repo / "pkg" / "test").mkdir(parents=True)
    (repo / "pkg" / "test" / "test_unit.py").write_text("def test_unit():\n    assert True\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence(head)), stderr="")

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    assert result.status == QualityGateStatus.passed
    assert _cli_option_values(calls[0], "--test-path") == ["tests", "pkg/test"]


def test_python_enforcement_runner_preserves_explicit_test_path(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_forecast.py").write_text("def test_run():\n    assert True\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence(head)), stderr="")

    result = PythonEnforcementRunner(
        command="uta python-enforce --test-path custom_tests",
        run_command=fake_run,
    ).run(repo)

    assert result.status == QualityGateStatus.passed
    assert _cli_option_values(calls[0], "--test-path") == ["custom_tests"]


def test_python_enforcement_runner_uses_pytest_configured_test_paths_first(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    (repo / "chat_robot" / "test").mkdir(parents=True)
    (repo / "chat_robot" / "test" / "test_views.py").write_text("def test_views():\n    assert True\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_fallback.py").write_text("def test_fallback():\n    assert True\n", encoding="utf-8")
    (repo / "setup.cfg").write_text("[tool:pytest]\ntestpaths = chat_robot/test\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence(head)), stderr="")

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    assert result.status == QualityGateStatus.passed
    assert _cli_option_values(calls[0], "--test-path") == ["chat_robot/test", "tests"]


def test_python_enforcement_runner_rejects_stale_evidence(tmp_path):
    repo = tmp_path / "repo"
    _init_python_repo(repo)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence("0" * 40)), stderr="")

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    assert result.status == QualityGateStatus.failed
    assert result.passed is False
    assert "stale_head" in result.summary


def test_python_enforcement_runner_fails_when_workspace_head_cannot_be_resolved(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence("0" * 40)), stderr="")

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    assert result.status == QualityGateStatus.command_error
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

    assert result.status == QualityGateStatus.passed
    assert result.passed is True
    assert result.summary == "Python enforcement passed"


def test_ci_service_runs_workspace_prepare_and_enforcement_with_mocked_commands(tmp_path):
    calls = []

    git_log = tmp_path / "git.jsonl"

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
            stderr="",
        )

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(
            workspace_root=tmp_path, git_bin=fake_git(tmp_path, log=git_log)
        ),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
    )
    request = CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
        }
    )

    record = service.submit(request)

    assert record.status.value == "success"
    assert any(argv[0] == "clone" for argv in git_calls(git_log))
    assert any(cmd[:2] == ["mvn", "-Dtest.enforcement.enabled=true"] for cmd, _ in calls)


def test_ci_service_routes_python_rdc_request_to_python_enforcer(tmp_path):
    class Runner:
        def __init__(self, name):
            self.name = name
            self.calls = []

        def run(self, repo_path):
            self.calls.append(Path(repo_path))
            if self.name == "java":
                raise AssertionError("Java runner should not handle language=python")
            from uta.enforcement.enforcement import QualityGateResult

            return QualityGateResult(
                status=QualityGateStatus.passed,
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
    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path, git_bin=fake_git(tmp_path)),
        enforcement_runner=java_runner,
        python_enforcement_runner=python_runner,
    )
    request = CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
            "language": "python",
        }
    )

    record = service.submit(request)

    assert record.status.value == "success"
    assert record.enforcement_result["backend"] == "python_enforcer"
    assert len(python_runner.calls) == 1


def test_ci_service_writes_inflight_marker_while_running_check(tmp_path):
    inflight_dir = tmp_path / "inflight"

    class Runner:
        def run(self, repo_path):
            markers = list(inflight_dir.glob("*.json"))
            assert len(markers) == 1
            payload = json.loads(markers[0].read_text(encoding="utf-8"))
            assert payload["pid"] == os.getpid()
            assert payload["task_id"]
            assert payload["app_name"] == "demo"
            return QualityGateResult(
                status=QualityGateStatus.passed,
                passed=True,
                command=["mvn", "verify"],
                summary="UTA test-enforcement passed",
            )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspace", git_bin=fake_git(tmp_path)),
        enforcement_runner=Runner(),
        inflight_dir=inflight_dir,
    )
    request = CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
        }
    )

    record = service.submit(request)

    assert record.status.value == "success"
    assert list(inflight_dir.glob("*.json")) == []


def test_ci_service_marks_orphaned_running_record_interrupted(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    inflight_dir = tmp_path / "inflight"
    inflight_dir.mkdir()
    request = CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
        }
    )
    record = CiTaskRecord(task_id="task-1", status=CiTaskStatus.running, request=request)
    store.save(record)
    (inflight_dir / "999999999-task-1.json").write_text(
        json.dumps({"pid": 999999999, "task_id": "task-1"}),
        encoding="utf-8",
    )
    service = ApiTriggerService(record_store=store, inflight_dir=inflight_dir)

    [summary] = service.recent_records(since=record.created_at, compact=True)
    loaded = service.get("task-1")

    assert summary.status == CiTaskStatus.failed
    assert loaded is not None
    assert loaded.status == CiTaskStatus.failed
    assert "interrupted" in (loaded.summary or "")


def test_ci_service_keeps_live_inflight_running_record(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    inflight_dir = tmp_path / "inflight"
    inflight_dir.mkdir()
    request = CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
        }
    )
    record = CiTaskRecord(task_id="task-1", status=CiTaskStatus.running, request=request)
    store.save(record)
    (inflight_dir / f"{os.getpid()}-task-1.json").write_text(
        json.dumps({"pid": os.getpid(), "task_id": "task-1"}),
        encoding="utf-8",
    )
    service = ApiTriggerService(record_store=store, inflight_dir=inflight_dir)

    [summary] = service.recent_records(since=record.created_at, compact=True)

    assert summary.status == CiTaskStatus.running


def test_ci_service_pends_second_report_until_single_ci_slot_is_free(tmp_path):
    first_started = threading.Event()
    release_first = threading.Event()

    class BlockingRunner:
        def __init__(self):
            self.calls = []
            self.lock = threading.Lock()

        def run(self, repo_path):
            with self.lock:
                self.calls.append(Path(repo_path))
                call_index = len(self.calls)
            if call_index == 1:
                first_started.set()
                assert release_first.wait(timeout=5)
            return QualityGateResult(
                status=QualityGateStatus.passed,
                passed=True,
                command=["mvn", "verify"],
                summary=f"UTA test-enforcement passed {call_index}",
            )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    runner = BlockingRunner()
    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspace", git_bin=fake_git(tmp_path)),
        enforcement_runner=runner,
        record_store=JsonCiTaskStore(tmp_path / "records"),
        ci_report_parallel_limit=1,
    )
    first = service.submit(
        CiTriggerRequest.model_validate(
            {
                "appName": "demo",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767-a",
            }
        ),
        public_base_url="http://uta",
        run_inline=False,
    )
    second = service.submit(
        CiTriggerRequest.model_validate(
            {
                "appName": "demo",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767-b",
            }
        ),
        public_base_url="http://uta",
        run_inline=False,
    )

    first_thread = threading.Thread(
        target=service.run_check,
        args=(first.task_id,),
        kwargs={"public_base_url": "http://uta"},
    )
    first_thread.start()
    assert first_started.wait(timeout=5)

    service.run_check(second.task_id, public_base_url="http://uta")
    pending_second = service.get(second.task_id)
    assert pending_second.status == CiTaskStatus.pending
    assert pending_second.summary == "CI test-enforcement report task is pending; another report task is running."
    pending_html = CiReportRenderer().status_html(pending_second)
    assert "pending" in pending_html
    assert "当前已有 CI 门禁报告任务运行中" in pending_html
    assert len(runner.calls) == 1

    release_first.set()
    first_thread.join(timeout=5)
    assert not first_thread.is_alive()

    assert service.get(first.task_id).status == CiTaskStatus.success
    completed_second = service.get(second.task_id)
    assert completed_second.status == CiTaskStatus.success
    assert completed_second.summary == "UTA test-enforcement passed 2"
    assert completed_second.report_url == f"http://uta/reports/{second.task_id}/index.html"
    assert len(runner.calls) == 2


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
    service = ApiTriggerService(language_handlers=[handler])
    record = CiTaskRecord(
        task_id="task-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
            }
        ),
        enforcement_result={"backend": "kotlin_enforcer"},
    )

    assert service._runner_for_record(record) is handler.runner


def test_python_enforcement_runner_full_profile_disables_ci_sampling(tmp_path):
    """The full-cap rerun must not inherit the parent's sampling profile.

    The profile travels by environment variable across the subprocess
    boundary, so leaving it set would make the "authoritative" evidence
    sampled -- and the repair targets derived from it just as arbitrary as
    the ones it was meant to replace.
    """
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    subprocess_calls = []

    def fake_run(cmd, **kwargs):
        subprocess_calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence(head)), stderr="")

    result = PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run_full(repo)

    assert result.passed is True
    assert "UTA_INTERNAL_PYTHON_ENFORCEMENT_PROFILE" not in subprocess_calls[0][1]["env"]


def test_python_enforcement_runner_in_process_full_profile_disables_ci_sampling(tmp_path):
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    calls = []

    def fake_enforcer(**kwargs):
        calls.append(kwargs)
        return _python_evidence(head)

    result = PythonEnforcementRunner(
        command="uta python-enforce",
        use_in_process_adapter=True,
        in_process_enforcer=fake_enforcer,
    ).run_full(repo)

    assert result.passed is True
    assert calls[0]["enable_ci_mutation_sampling"] is False


def test_python_enforcement_runner_default_run_keeps_ci_sampling(tmp_path):
    """`run` is still the sampled CI profile; only `run_full` opts out."""
    repo = tmp_path / "repo"
    head = _init_python_repo(repo)
    subprocess_calls = []

    def fake_run(cmd, **kwargs):
        subprocess_calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(_python_evidence(head)), stderr="")

    PythonEnforcementRunner(command="uta python-enforce", run_command=fake_run).run(repo)

    env = subprocess_calls[0][1]["env"]
    assert env["UTA_INTERNAL_PYTHON_ENFORCEMENT_PROFILE"] == "ci_report_v1"


_COMPAT_APPLIED = "[uta-pit-compat] verified module=demo skip=false targets=1 skipFailingTests=true\n"


def _scoped_pit_run(extra: str):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.demo.ChangedService*]\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (16/16)\n"
                + extra
                + "[INFO] BUILD SUCCESS"
            ),
            stderr="",
        )

    return fake_run


def test_all_mutants_uncovered_fails_once_skip_failing_tests_applied(tmp_path):
    """Test strength is killed/covered, so an empty denominator is not a pass.

    This is the vacuous score that made skipFailingTests a ship NO-GO: the only
    tests covering the changed class may be exactly the red ones PIT dropped.
    """
    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=_scoped_pit_run(
            _COMPAT_APPLIED
            + ">> Generated 51 mutations Killed 0 (0%)\n"
            + ">> Mutations with no coverage 51. Test strength 100%\n"
        ),
    )

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.failed
    assert result.passed is False
    assert "no covering test" in result.summary


def test_no_scored_changed_line_mutants_pass_with_explicit_verdict(tmp_path):
    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=_scoped_pit_run(
            ">> Generated 51 mutations Killed 0 (0%)\n"
            ">> Mutations with no coverage 51. Test strength 100%\n"
            "[test-enforcer] no scored PIT mutations on changed lines for demo.biz "
            "(0/0 scored; 51 no coverage excluded)\n"
        ),
    )

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True


def test_covered_mutants_pass_with_skip_failing_tests_applied(tmp_path):
    runner = MavenEnforcementRunner(
        command="mvn -Dtest.enforcement.enabled=true verify",
        run_command=_scoped_pit_run(
            _COMPAT_APPLIED
            + ">> Generated 51 mutations Killed 50 (98%)\n"
            + ">> Mutations with no coverage 1. Test strength 100%\n"
            + DIFF_MUTATION_OK
        ),
    )

    result = runner.run(tmp_path)

    assert result.status == QualityGateStatus.passed
    assert result.passed is True

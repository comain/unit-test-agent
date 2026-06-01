from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from uta.ci_plugin.auto_push import (
    AutoPushContext,
    AutoPushConflictError,
    AutoPushPolicyError,
    CiAutoPusher,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _create_remote_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True, text=True)
    _git(seed, "config", "user.name", "Seed User")
    _git(seed, "config", "user.email", "seed@example.test")
    test_dir = seed / "src" / "test" / "java" / "com" / "example"
    test_dir.mkdir(parents=True)
    (test_dir / "DemoTest.java").write_text("class DemoTest {}\n", encoding="utf-8")
    main_dir = seed / "src" / "main" / "java" / "com" / "example"
    main_dir.mkdir(parents=True)
    (main_dir / "Demo.java").write_text("class Demo {}\n", encoding="utf-8")
    (seed / "pom.xml").write_text("<project></project>\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "checkout", "-B", "feature/EXAMPLE-1")
    _git(seed, "push", "-u", "origin", "feature/EXAMPLE-1")
    return remote, seed


def _clone(remote: Path, target: Path) -> Path:
    subprocess.run(["git", "clone", str(remote), str(target)], check=True, capture_output=True, text=True)
    _git(target, "checkout", "feature/EXAMPLE-1")
    return target


def _context() -> AutoPushContext:
    return AutoPushContext(
        branch_name="feature/EXAMPLE-1",
        repo_task_id=17,
        ci_task_id="ci-task-1",
        ci_record_id="record-1",
        jira_key="EXAMPLE-1",
        class_fqns=["com.example.Demo"],
    )


def test_ci_auto_push_commits_test_only_changes_with_audit_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "uta.ci_plugin.auto_push.settings.ci_git_ssh_key_path",
        "/opt/app/uta-ci-data/runner/ssh/uta_ci_ed25519",
    )
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    test_file = repo / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    test_file.write_text("class DemoTest { void generated() {} }\n", encoding="utf-8")
    reports = repo / ".uta_reports"
    reports.mkdir()
    (reports / "status.html").write_text("<html></html>", encoding="utf-8")
    cache = repo / ".uta_cache" / "context"
    cache.mkdir(parents=True)
    (cache / "Demo.context.md").write_text("runtime context", encoding="utf-8")
    sisyphus = repo / ".sisyphus" / "run-continuation"
    sisyphus.mkdir(parents=True)
    (sisyphus / "ses_123.json").write_text("{}", encoding="utf-8")
    (repo / ".uta_summary.md").write_text("summary", encoding="utf-8")
    (repo / "opencode.json").write_text("{}", encoding="utf-8")
    (repo / "pom.xml").write_text("<project><!-- deterministic UTA test deps --></project>\n", encoding="utf-8")

    result = CiAutoPusher(
        repo,
        user_name="UTA Bot",
        user_email="unit-test-agent@example.test",
    ).commit_and_push(_context())

    assert result.commit_sha
    assert result.remote_ref == result.commit_sha
    assert result.changed_paths == ["src/test/java/com/example/DemoTest.java"]
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip() == ""

    assert _git(repo, "log", "-1", "--format=%an <%ae>").stdout.strip() == (
        "UTA Bot <unit-test-agent@example.test>"
    )
    assert _git(repo, "config", "core.sshCommand").stdout.strip() == (
        "ssh -F /dev/null -i /opt/app/uta-ci-data/runner/ssh/uta_ci_ed25519 "
        "-o IdentitiesOnly=yes -o PreferredAuthentications=publickey "
        "-o StrictHostKeyChecking=accept-new"
    )
    body = _git(repo, "log", "-1", "--format=%B").stdout
    assert "CI-Task-Id: ci-task-1" in body
    assert "CI-Record-Id: record-1" in body
    assert "Jira: EXAMPLE-1" in body
    assert "UTA-Repo-Task-Id: 17" in body


def test_ci_auto_push_without_configured_ssh_key_leaves_repo_ssh_config_unset(tmp_path, monkeypatch):
    monkeypatch.setattr("uta.ci_plugin.auto_push.settings.ci_git_ssh_key_path", "")
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    test_file = repo / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    test_file.write_text("class DemoTest { void generated() {} }\n", encoding="utf-8")

    result = CiAutoPusher(
        repo,
        user_name="UTA Bot",
        user_email="unit-test-agent@example.test",
    ).commit_and_push(_context())

    ssh_config = subprocess.run(
        ["git", "config", "--get", "core.sshCommand"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.commit_sha
    assert ssh_config.returncode == 1
    assert ssh_config.stdout == ""


def test_ci_auto_push_rejects_production_code_but_ignores_runtime_artifacts(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    (repo / "src" / "main" / "java" / "com" / "example" / "Demo.java").write_text(
        "class Demo { int changed; }\n",
        encoding="utf-8",
    )
    reports = repo / ".uta_reports"
    reports.mkdir()
    (reports / "summary.json").write_text("{}", encoding="utf-8")
    (repo / ".coverage").write_text("runtime coverage", encoding="utf-8")
    mutants = repo / "mutants"
    mutants.mkdir()
    (mutants / "cache.sqlite").write_text("runtime mutation cache", encoding="utf-8")

    with pytest.raises(AutoPushPolicyError) as exc:
        CiAutoPusher(repo).commit_and_push(_context())

    assert "src/main/java/com/example/Demo.java" in str(exc.value)
    assert ".uta_reports/summary.json" not in str(exc.value)
    assert ".coverage" not in str(exc.value)
    assert "mutants/cache.sqlite" not in str(exc.value)
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_ci_auto_push_allows_python_tests_directory_changes(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    test_file = repo / "tests" / "uta_generated" / "test_forecast.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_forecast():\n    assert True\n", encoding="utf-8")

    result = CiAutoPusher(
        repo,
        user_name="UTA Bot",
        user_email="unit-test-agent@example.test",
    ).commit_and_push(_context())

    assert result.commit_sha
    assert result.changed_paths == ["tests/uta_generated/test_forecast.py"]


def test_ci_auto_push_can_scope_python_passed_target_paths(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    passed = repo / "tests" / "uta_generated" / "test_passed.py"
    failed = repo / "tests" / "uta_generated" / "test_failed.py"
    passed.parent.mkdir(parents=True)
    passed.write_text("def test_passed():\n    assert True\n", encoding="utf-8")
    failed.write_text("def test_failed():\n    assert False\n", encoding="utf-8")

    context = AutoPushContext(
        branch_name="feature/EXAMPLE-1",
        repo_task_id=17,
        ci_task_id="ci-task-1",
        ci_record_id="record-1",
        jira_key="EXAMPLE-1",
        class_fqns=["pyfile:src/example/passed.py"],
        commit_paths=["tests/uta_generated/test_passed.py"],
    )
    result = CiAutoPusher(
        repo,
        user_name="UTA Bot",
        user_email="unit-test-agent@example.test",
    ).commit_and_push(context)

    assert result.commit_sha
    assert result.changed_paths == ["tests/uta_generated/test_passed.py"]
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip() == (
        "?? tests/uta_generated/test_failed.py"
    )


def test_ci_auto_push_fetch_rebase_conflict_is_visible_and_never_force_pushes(tmp_path):
    remote, seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")

    local_test = repo / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    local_test.write_text("class DemoTest { void localChange() {} }\n", encoding="utf-8")

    remote_test = seed / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    remote_test.write_text("class DemoTest { void remoteChange() {} }\n", encoding="utf-8")
    _git(seed, "add", "src/test/java/com/example/DemoTest.java")
    _git(seed, "commit", "-m", "remote conflicting test change")
    _git(seed, "push", "origin", "feature/EXAMPLE-1")

    with pytest.raises(AutoPushConflictError) as exc:
        CiAutoPusher(repo).commit_and_push(_context())

    assert "rebase failed" in str(exc.value)
    remote_head = _git(seed, "rev-parse", "HEAD").stdout.strip()
    assert _git(repo, "ls-remote", "origin", "refs/heads/feature/EXAMPLE-1").stdout.startswith(remote_head)


def test_ci_auto_push_verifies_rebased_commit_sha(tmp_path):
    remote, seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")

    remote_file = seed / "src" / "test" / "java" / "com" / "example" / "RemoteOnlyTest.java"
    remote_file.write_text("class RemoteOnlyTest {}\n", encoding="utf-8")
    _git(seed, "add", "src/test/java/com/example/RemoteOnlyTest.java")
    _git(seed, "commit", "-m", "remote non-conflicting test")
    _git(seed, "push", "origin", "feature/EXAMPLE-1")

    local_test = repo / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    local_test.write_text("class DemoTest { void localChange() {} }\n", encoding="utf-8")

    result = CiAutoPusher(
        repo,
        user_name="UTA Bot",
        user_email="unit-test-agent@example.test",
    ).commit_and_push(_context())

    assert result.commit_sha == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result.remote_ref == result.commit_sha

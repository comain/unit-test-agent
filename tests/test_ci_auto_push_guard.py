from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from uta.shared.delivery import (
    PushConflictError,
    PushPolicyError,
    RdcDeliveryContext,
    RdcRepairPublisher,
)


@pytest.fixture(autouse=True)
def _clear_git_credentials(monkeypatch):
    monkeypatch.setattr("uta.shared.delivery.settings.ci_git_ssh_key_path", "")
    monkeypatch.setattr("uta.shared.delivery.settings.ci_git_access_token", "")


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
    _git(seed, "checkout", "-B", "feature/TASK-82767")
    _git(seed, "push", "-u", "origin", "feature/TASK-82767")
    return remote, seed


def _clone(remote: Path, target: Path) -> Path:
    subprocess.run(["git", "clone", str(remote), str(target)], check=True, capture_output=True, text=True)
    _git(target, "checkout", "feature/TASK-82767")
    return target


def _context() -> RdcDeliveryContext:
    return RdcDeliveryContext(
        branch_name="feature/TASK-82767",
        repo_task_id=17,
        rdc_task_id="rdc-task-1",
        rdc_record_id="record-1",
        jira_key="TASK-82767",
        class_fqns=["com.example.Demo"],
    )


def test_rdc_auto_push_commits_test_only_changes_with_audit_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "uta.shared.delivery.settings.ci_git_ssh_key_path",
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

    result = RdcRepairPublisher(
        repo,
        user_name="UTA Unit Test Agent",
        user_email="unit-test-agent@example.test",
    ).publish(_context())

    assert result.commit_sha
    assert result.remote_ref == result.commit_sha
    assert result.changed_paths == ("pom.xml", "src/test/java/com/example/DemoTest.java")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip() == ""

    assert _git(repo, "log", "-1", "--format=%an <%ae>").stdout.strip() == (
        "UTA Unit Test Agent <unit-test-agent@example.test>"
    )
    assert subprocess.run(
        ["git", "config", "--get", "core.sshCommand"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    ).returncode == 1
    body = _git(repo, "log", "-1", "--format=%B").stdout
    assert "RDC-Task-Id: rdc-task-1" in body
    assert "RDC-Record-Id: record-1" in body
    assert "Jira: TASK-82767" in body
    assert "UTA-Repo-Task-Id: 17" in body


def test_rdc_auto_push_without_configured_ssh_key_leaves_repo_ssh_config_unset(tmp_path, monkeypatch):
    monkeypatch.setattr("uta.shared.delivery.settings.ci_git_ssh_key_path", "")
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    test_file = repo / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    test_file.write_text("class DemoTest { void generated() {} }\n", encoding="utf-8")

    result = RdcRepairPublisher(
        repo,
        user_name="UTA Unit Test Agent",
        user_email="unit-test-agent@example.test",
    ).publish(_context())

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


def test_rdc_delivery_uses_token_headers_without_rewriting_origin(tmp_path, monkeypatch):
    monkeypatch.setattr("uta.shared.delivery.settings.ci_git_access_token", "secret-token")
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    _git(repo, "remote", "set-url", "origin", "git@git.example.com:group/demo.git")
    test_file = repo / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    test_file.write_text("class DemoTest { void generated() {} }\n", encoding="utf-8")
    calls = []
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="stop before network")
        return real_run(cmd, cwd=repo, check=kwargs.get("check", True), capture_output=True, text=True, env=kwargs.get("env"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(PushConflictError, match="fetch before push failed"):
        RdcRepairPublisher(repo).publish(_context())

    assert not [cmd for cmd, _kwargs in calls if cmd[:4] == ["git", "remote", "set-url", "origin"]]
    assert "secret-token" not in " ".join(" ".join(cmd) for cmd, _kwargs in calls)
    fetch_env = next(kwargs["env"] for cmd, kwargs in calls if cmd[:2] == ["git", "fetch"])
    assert fetch_env["GIT_TERMINAL_PROMPT"] == "0"
    assert fetch_env["GIT_CONFIG_KEY_0"] == "http.https://git.example.com/.extraheader"
    assert fetch_env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "secret-token" not in fetch_env["GIT_CONFIG_VALUE_0"]


def test_rdc_auto_push_rejects_production_code_but_ignores_runtime_artifacts(tmp_path):
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

    with pytest.raises(PushPolicyError) as exc:
        RdcRepairPublisher(repo).publish(_context())

    assert "src/main/java/com/example/Demo.java" in str(exc.value)
    assert ".uta_reports/summary.json" not in str(exc.value)
    assert ".coverage" not in str(exc.value)
    assert "mutants/cache.sqlite" not in str(exc.value)
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_rdc_auto_push_ignores_quoted_mutants_and_python_temp_artifacts(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    test_file = repo / "tests" / "uta_generated" / "test_forecast.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_forecast():\n    assert True\n", encoding="utf-8")
    quoted_mutant = repo / "mutants" / "chat_robot" / "test" / "agent" / "提示词.txt"
    quoted_mutant.parent.mkdir(parents=True)
    quoted_mutant.write_text("runtime mutant copy", encoding="utf-8")
    temp_file = repo / ".tmp_train_server_test" / "cfg" / "train.yaml"
    temp_file.parent.mkdir(parents=True)
    temp_file.write_text("runtime config", encoding="utf-8")

    result = RdcRepairPublisher(
        repo,
        user_name="UTA Unit Test Agent",
        user_email="unit-test-agent@example.test",
    ).publish(_context())

    assert result.changed_paths == ("tests/uta_generated/test_forecast.py",)
    assert not (repo / "mutants").exists()
    assert not (repo / ".tmp_train_server_test").exists()


def test_rdc_auto_push_allows_python_tests_directory_changes(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    test_file = repo / "tests" / "uta_generated" / "test_forecast.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_forecast():\n    assert True\n", encoding="utf-8")

    result = RdcRepairPublisher(
        repo,
        user_name="UTA Unit Test Agent",
        user_email="unit-test-agent@example.test",
    ).publish(_context())

    assert result.commit_sha
    assert result.changed_paths == ("tests/uta_generated/test_forecast.py",)


def test_rdc_auto_push_allows_package_local_python_test_file(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    test_file = repo / "chat_robot" / "test" / "test_fine_tuning_control_plane.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_control_plane():\n    assert True\n", encoding="utf-8")

    result = RdcRepairPublisher(
        repo,
        user_name="UTA Unit Test Agent",
        user_email="unit-test-agent@example.test",
    ).publish(
        RdcDeliveryContext(
            branch_name="feature/TASK-82767",
            repo_task_id=17,
            rdc_task_id="rdc-task-1",
            rdc_record_id="record-1",
            jira_key="TASK-82767",
            class_fqns=["pyfile:chat_robot/service/fine_tuning_service.py"],
            commit_paths=["chat_robot/test/test_fine_tuning_control_plane.py"],
        )
    )

    assert result.commit_sha
    assert result.changed_paths == ("chat_robot/test/test_fine_tuning_control_plane.py",)


def test_rdc_auto_push_rejects_package_local_non_test_file(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    helper = repo / "chat_robot" / "test" / "fixtures.yaml"
    helper.parent.mkdir(parents=True)
    helper.write_text("runtime: fixture\n", encoding="utf-8")

    with pytest.raises(PushPolicyError) as exc:
        RdcRepairPublisher(repo).publish(
            RdcDeliveryContext(
                branch_name="feature/TASK-82767",
                repo_task_id=17,
                rdc_task_id="rdc-task-1",
                rdc_record_id="record-1",
                jira_key="TASK-82767",
                class_fqns=["pyfile:chat_robot/service/fine_tuning_service.py"],
                commit_paths=["chat_robot/test/fixtures.yaml"],
            )
        )

    assert "chat_robot/test/fixtures.yaml" in str(exc.value)


def test_rdc_auto_push_can_scope_python_passed_target_paths(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    passed = repo / "tests" / "uta_generated" / "test_passed.py"
    failed = repo / "tests" / "uta_generated" / "test_failed.py"
    passed.parent.mkdir(parents=True)
    passed.write_text("def test_passed():\n    assert True\n", encoding="utf-8")
    failed.write_text("def test_failed():\n    assert False\n", encoding="utf-8")

    context = RdcDeliveryContext(
        branch_name="feature/TASK-82767",
        repo_task_id=17,
        rdc_task_id="rdc-task-1",
        rdc_record_id="record-1",
        jira_key="TASK-82767",
        class_fqns=["pyfile:src/example/passed.py"],
        commit_paths=["tests/uta_generated/test_passed.py"],
    )
    result = RdcRepairPublisher(
        repo,
        user_name="UTA Unit Test Agent",
        user_email="unit-test-agent@example.test",
    ).publish(context)

    assert result.commit_sha
    assert result.changed_paths == ("tests/uta_generated/test_passed.py",)
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip() == ""


def test_rdc_auto_push_scoped_checkpoint_can_commit_requested_pom_change(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    passed = repo / "tests" / "uta_generated" / "test_passed.py"
    passed.parent.mkdir(parents=True)
    passed.write_text("def test_passed():\n    assert True\n", encoding="utf-8")
    (repo / "pom.xml").write_text("<project><!-- enable tests --></project>\n", encoding="utf-8")

    context = RdcDeliveryContext(
        branch_name="feature/TASK-82767",
        repo_task_id=17,
        rdc_task_id="rdc-task-1",
        rdc_record_id="record-1",
        jira_key="TASK-82767",
        class_fqns=["pyfile:src/example/passed.py"],
        commit_paths=["tests/uta_generated/test_passed.py", "pom.xml"],
    )
    result = RdcRepairPublisher(repo).publish(context)

    assert result.changed_paths == ("pom.xml", "tests/uta_generated/test_passed.py")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip() == ""


def test_rdc_auto_push_scoped_checkpoint_can_commit_requested_pyproject_change(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    passed = repo / "tests" / "uta_generated" / "test_passed.py"
    passed.parent.mkdir(parents=True)
    passed.write_text("def test_passed():\n    assert True\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        "[tool.mutmut]\npaths_to_mutate = ['src/example/passed.py']\n",
        encoding="utf-8",
    )

    context = RdcDeliveryContext(
        branch_name="feature/TASK-82767",
        repo_task_id=17,
        rdc_task_id="rdc-task-1",
        rdc_record_id="record-1",
        jira_key="TASK-82767",
        class_fqns=["pyfile:src/example/passed.py"],
        commit_paths=["tests/uta_generated/test_passed.py", "pyproject.toml"],
    )
    result = RdcRepairPublisher(repo).publish(context)

    assert result.changed_paths == ("pyproject.toml", "tests/uta_generated/test_passed.py")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip() == ""


def test_rdc_auto_push_scoped_checkpoint_discards_unrequested_config_residue(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    passed = repo / "tests" / "uta_generated" / "test_passed.py"
    passed.parent.mkdir(parents=True)
    passed.write_text("def test_passed():\n    assert True\n", encoding="utf-8")
    (repo / "setup.cfg").write_text("[mutmut]\npaths_to_mutate=src/example.py\n", encoding="utf-8")

    context = RdcDeliveryContext(
        branch_name="feature/TASK-82767",
        repo_task_id=17,
        rdc_task_id="rdc-task-1",
        rdc_record_id="record-1",
        jira_key="TASK-82767",
        class_fqns=["pyfile:src/example/passed.py"],
        commit_paths=["tests/uta_generated/test_passed.py"],
    )
    result = RdcRepairPublisher(repo).publish(context)

    assert result.changed_paths == ("tests/uta_generated/test_passed.py",)
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip() == ""


def test_rdc_auto_push_empty_scoped_paths_do_not_fall_back_to_all_dirty_files(tmp_path):
    remote, _seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")
    dirty_test = repo / "tests" / "uta_generated" / "test_unscoped.py"
    dirty_test.parent.mkdir(parents=True)
    dirty_test.write_text("def test_unscoped():\n    assert True\n", encoding="utf-8")

    context = RdcDeliveryContext(
        branch_name="feature/TASK-82767",
        repo_task_id=17,
        rdc_task_id="rdc-task-1",
        rdc_record_id="record-1",
        jira_key="TASK-82767",
        class_fqns=["pyfile:src/example/missing.py"],
        commit_paths=[],
    )

    with pytest.raises(PushPolicyError, match="found no test changes to commit"):
        RdcRepairPublisher(repo).publish(context)

    assert _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip() == (
        "?? tests/uta_generated/test_unscoped.py"
    )


def test_rdc_auto_push_fetch_rebase_conflict_is_visible_and_never_force_pushes(tmp_path):
    remote, seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")

    local_test = repo / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    local_test.write_text("class DemoTest { void localChange() {} }\n", encoding="utf-8")

    remote_test = seed / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    remote_test.write_text("class DemoTest { void remoteChange() {} }\n", encoding="utf-8")
    _git(seed, "add", "src/test/java/com/example/DemoTest.java")
    _git(seed, "commit", "-m", "remote conflicting test change")
    _git(seed, "push", "origin", "feature/TASK-82767")

    with pytest.raises(PushConflictError) as exc:
        RdcRepairPublisher(repo).publish(_context())

    assert "rebase onto" in str(exc.value)
    remote_head = _git(seed, "rev-parse", "HEAD").stdout.strip()
    assert _git(repo, "ls-remote", "origin", "refs/heads/feature/TASK-82767").stdout.startswith(remote_head)


def test_rdc_auto_push_verifies_rebased_commit_sha(tmp_path):
    remote, seed = _create_remote_repo(tmp_path)
    repo = _clone(remote, tmp_path / "work")

    remote_file = seed / "src" / "test" / "java" / "com" / "example" / "RemoteOnlyTest.java"
    remote_file.write_text("class RemoteOnlyTest {}\n", encoding="utf-8")
    _git(seed, "add", "src/test/java/com/example/RemoteOnlyTest.java")
    _git(seed, "commit", "-m", "remote non-conflicting test")
    _git(seed, "push", "origin", "feature/TASK-82767")

    local_test = repo / "src" / "test" / "java" / "com" / "example" / "DemoTest.java"
    local_test.write_text("class DemoTest { void localChange() {} }\n", encoding="utf-8")

    result = RdcRepairPublisher(
        repo,
        user_name="UTA Unit Test Agent",
        user_email="unit-test-agent@example.test",
    ).publish(_context())

    assert result.commit_sha == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result.remote_ref == result.commit_sha

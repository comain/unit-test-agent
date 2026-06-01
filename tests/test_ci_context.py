import json
import subprocess

from uta.ci_plugin.context import (
    RepairContextExporter,
    assemble_base_context,
    collect_git_commit_messages,
    render_context_markdown,
)
from uta.ci_plugin.models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.ci_plugin.protocols.github import GithubContextProvider
from uta.tasks.manager import TaskManager
from uta.tasks.models import json_loads


def _record() -> CiTaskRecord:
    request = CiTriggerRequest.model_validate(
        {
            "appName": "demo-app",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/EXAMPLE-122",
            "jiraId": "EXAMPLE-122",
            "taskId": "ci-task-1",
            "recordId": "record-1",
            "parentId": "parent-1",
            "taskTemplateId": "T_91_pre_unitTestAppTool",
            "pipelineId": "pipe-1",
            "sprintId": "sprint-1",
            "operator": "dev-user",
            "metadata": {"cookie": "must-not-leak", "authorization": "must-not-leak"},
        }
    )
    return CiTaskRecord(
        task_id="uta-check-1",
        status=CiTaskStatus.failed,
        request=request,
        enforcement_result={
            "status": "missing_evidence",
            "summary": "test-enforcement evidence is missing",
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
        },
    )


def test_ci_context_renders_sources_without_auth_material(tmp_path):
    context = assemble_base_context(
        _record(),
        issue={"id": "EXAMPLE-122", "description": "Issue description from CI", "source": "trigger_payload", "kind": "issue"},
        user_context="Please focus on service tests",
        commit_messages=["EXAMPLE-122 add checkout validation", "test: update fixture"],
    )
    text = render_context_markdown(context)

    assert "Priority order" in text
    assert "Issue description from CI" in text
    assert "EXAMPLE-122 add checkout validation" in text
    assert "missing_evidence" in text
    assert "Please focus on service tests" in text
    assert "must-not-leak" not in json.dumps(context)
    assert "cookie" not in text.lower()
    assert "authorization" not in text.lower()


def test_repair_context_export_is_run_scoped_and_not_reused(tmp_path):
    exporter = RepairContextExporter(tmp_path / "runtime")
    first = exporter.export("repair-1", {"pipeline": {"branch": "feature/a"}, "sources": []})
    second = exporter.export("repair-2", {"pipeline": {"branch": "feature/b"}, "sources": []})

    assert first != second
    assert ".uta_cache" not in first.parts
    assert first.read_text(encoding="utf-8") != second.read_text(encoding="utf-8")


def test_assemble_base_context_marks_missing_issue_and_commits():
    context = assemble_base_context(_record())

    assert "issue_description_unavailable" in context["missingReasons"]
    assert "git_commit_messages_unavailable" in context["missingReasons"]


def test_github_context_provider_uses_pull_request_as_issue():
    request = CiTriggerRequest.model_validate(
        {
            "appName": "octo/demo",
            "gitUrl": "https://github.com/octo/demo.git",
            "branch": "feature/x",
            "metadata": {"github": {"prNumber": 42, "prTitle": "Add checkout", "prBody": "Covers edge cases"}},
        }
    )
    record = CiTaskRecord(task_id="gh-1", status=CiTaskStatus.failed, request=request, protocol="github")

    context = GithubContextProvider().build_context(record)

    assert context["issue"]["id"] == 42
    assert context["issue"]["kind"] == "github_pr"
    assert "Add checkout" in context["issue"]["description"]
    assert "Covers edge cases" in context["issue"]["description"]
    assert "issue_description_unavailable" not in context["missingReasons"]


def test_collect_git_commit_messages_uses_diff_range_without_blocking_on_empty_repo(tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="feat: add tests\x1efix: cover branch\x1e", stderr="")

    messages = collect_git_commit_messages(tmp_path, "origin/main", run_command=fake_run)

    assert messages == ["feat: add tests", "fix: cover branch"]
    assert calls[0][0] == ["git", "-C", str(tmp_path), "log", "--format=%B%x1e", "origin/main..HEAD"]


def test_repo_task_persists_ci_context_json_and_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    manager = TaskManager(tmp_path / "tasks.db")
    context = {"pipeline": {"taskId": "ci-task-1"}, "sources": [{"name": "trigger_payload", "available": True}]}
    context_path = tmp_path / "runtime" / "repair-1" / "ci_context.md"

    task_id = manager.create_task(
        repo_path=str(repo),
        branch_name="feature/EXAMPLE-122",
        ci_context=context,
        ci_context_path=str(context_path),
    )
    row = manager.get_task(task_id)

    assert json_loads(row["ci_context_json"]) == context
    assert row["ci_context_path"] == str(context_path)


def test_db_migration_adds_ci_context_columns_to_existing_db(tmp_path):
    db_path = tmp_path / "tasks.db"
    manager = TaskManager(db_path)
    with manager.db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(repo_tasks)")}

    assert {"ci_context_json", "ci_context_path"}.issubset(columns)

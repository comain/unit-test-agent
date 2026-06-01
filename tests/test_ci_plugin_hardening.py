import subprocess

import pytest

from uta.ci_plugin.app import create_default_service
from uta.ci_plugin.context import RepairContextExporter, assemble_base_context
from uta.ci_plugin.enforcement import MavenEnforcementRunner
from uta.ci_plugin.fix_sessions import CreateFixSessionRequest
from uta.ci_plugin.models import CiTaskStatus, CiTriggerRequest
from uta.ci_plugin.protocols import ProtocolRegistry
from uta.ci_plugin.protocols.github import GithubContextProvider, GithubWebhookProtocol
from uta.ci_plugin.service import CiPluginService
from uta.ci_plugin.store import JsonCiTaskStore
from uta.ci_plugin.workspace import GitWorkspaceManager
from uta.config import Settings
from uta.tasks.manager import TaskManager


def _request():
    return CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/EXAMPLE-122",
            "jiraId": "EXAMPLE-122",
            "taskId": "ci-task-1",
            "recordId": "record-1",
            "parentId": "parent-1",
            "taskTemplateId": "T_91_pre_unitTestAppTool",
        }
    )


def test_default_service_is_wired_from_ci_settings(tmp_path):
    settings = Settings(
        ci_workspace_root=str(tmp_path / "workspaces"),
        ci_enforcement_command="mvn -Dtest.enforcement.enabled=true verify",
        ci_enforcement_timeout_seconds=123,
        ci_context_runtime_root=str(tmp_path / "runtime"),
        ci_record_store_root=str(tmp_path / "records"),
    )

    service = create_default_service(settings)

    health = service.health()
    assert health["runner"]["ready"] is True
    assert health["integrations"]["callbackConfigured"] is False
    assert health["integrations"]["taskManagerConfigured"] is True
    assert health["integrations"]["contextExporterConfigured"] is True
    assert health["integrations"]["recordStoreConfigured"] is True
    assert health["integrations"]["protocols"] == ["github"]


def test_ci_task_store_recovers_status_report_and_fix_sessions(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    service = CiPluginService(record_store=store)
    record = service.submit(_request(), public_base_url="http://uta", protocol="github")
    record.status = CiTaskStatus.failed
    record.report_url = "http://uta/reports/task/index.html"
    record.fix_sessions.append({"sessionId": "fix-1", "status": "created"})
    service.save(record)

    recovered = CiPluginService(record_store=store).get(record.task_id)

    assert recovered is not None
    assert recovered.status == CiTaskStatus.failed
    assert recovered.report_url == "http://uta/reports/task/index.html"
    assert recovered.fix_sessions == [{"sessionId": "fix-1", "status": "created"}]


def test_service_passes_configured_context_provider_to_repair_context(tmp_path):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="commit message\x1e", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="test-enforcer check-coverage failed: diff line coverage 87.50% is below required 95.00%",
            stderr="",
        )

    class FakeContextProvider(GithubContextProvider):
        def __init__(self):
            self.seen = []

        def build_context(self, record, *, user_context=None, commit_messages=None):
            self.seen.append(record.request.jira_id)
            return assemble_base_context(
                record,
                issue={"id": record.request.jira_id, "description": "Description from issue provider"},
                user_context=user_context,
                commit_messages=commit_messages,
            )

    context_provider = FakeContextProvider()
    service = CiPluginService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces", run_command=fake_run),
        enforcement_runner=MavenEnforcementRunner("mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([GithubWebhookProtocol(context_provider=context_provider)]),
    )
    record = service.submit(_request(), public_base_url="http://uta", protocol="github")

    service.create_fix_session(record, CreateFixSessionRequest(targetIds=["class:com.example.Foo"]))

    assert context_provider.seen == ["EXAMPLE-122"]
    assert record.fix_sessions[0]["repoTaskId"]
    task = service.task_manager.get_task(record.fix_sessions[0]["repoTaskId"])
    assert "Description from issue provider" in task["ci_context_json"]


def test_workspace_rejects_untrusted_git_url_and_bad_branch(tmp_path):
    manager = GitWorkspaceManager(workspace_root=tmp_path)

    with pytest.raises(ValueError, match="git_url"):
        manager.prepare(git_url="file:///tmp/repo.git", branch="feature/EXAMPLE-122", task_id="task-1")
    with pytest.raises(ValueError, match="branch"):
        manager.prepare(
            git_url="git@git.example.com:group/demo.git",
            branch="../master",
            task_id="task-2",
        )

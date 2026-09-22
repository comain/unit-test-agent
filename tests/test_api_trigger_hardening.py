import subprocess
from fake_maven_metadata import with_resolved_enforcer
import threading

import httpx
import pytest

from fake_git import fake_git
from uta.app.app import create_default_service
from uta.app.context import RepairContextExporter
from uta.app.protocols import ProtocolRegistry
from uta.app.protocols.rdc import JiraDescriptionClient, RdcContextProvider, RdcProtocol
from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.shared.ci_models import CiTaskStatus, CiTriggerRequest
from uta.app.service import ApiTriggerService
from uta.app.store import JsonCiTaskStore
from uta.app.workspace import GitWorkspaceManager
from uta.shared.config import Settings
from uta.tasks.manager import TaskManager


def _request():
    return CiTriggerRequest.model_validate(
        {
            "appName": "demo",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
            "jiraId": "TASK-82767",
            "taskId": "rdc-task-1",
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
        ci_python_enforcement_timeout_seconds=456,
        ci_python_enforcement_memory_limit_mb=2048,
        ci_python_diff_mutation_gate=91,
        rdc_ack_url="http://rdc/plugin/ack",
        ci_context_runtime_root=str(tmp_path / "runtime"),
        ci_record_store_root=str(tmp_path / "records"),
        jira_raw_url="http://jira/wcr/jira/issues/raw/v2",
    )

    service = create_default_service(settings)

    health = service.health()
    assert health["runner"]["ready"] is True
    assert health["integrations"]["callbackConfigured"] is True
    assert health["integrations"]["taskManagerConfigured"] is True
    assert health["integrations"]["contextExporterConfigured"] is True
    assert health["integrations"]["recordStoreConfigured"] is True
    assert health["integrations"]["protocols"] == ["rdc", "github"]
    assert service.protocols.get("rdc").context_provider._jira_client is not None
    python_handler = next(handler for handler in service.language_handlers.handlers if handler.language == "python")
    assert python_handler.runner.timeout_seconds == 456
    assert python_handler.runner.mutation_gate == 91
    assert python_handler.runner.use_in_process_adapter is False
    assert python_handler.runner.memory_limit_bytes == 2048 * 1024 * 1024


def test_ci_task_store_recovers_status_report_and_fix_sessions(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    service = ApiTriggerService(record_store=store)
    record = service.submit(_request(), public_base_url="http://uta")
    record.status = CiTaskStatus.failed
    record.report_url = "http://uta/reports/task/index.html"
    record.fix_sessions.append({"sessionId": "fix-1", "status": "created"})
    service.save(record)

    recovered = ApiTriggerService(record_store=store).get(record.task_id)

    assert recovered is not None
    assert recovered.status == CiTaskStatus.failed
    assert recovered.report_url == "http://uta/reports/task/index.html"
    assert recovered.fix_sessions == [{"sessionId": "fix-1", "status": "created"}]


def test_ci_task_store_concurrent_saves_use_isolated_temp_files(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    service = ApiTriggerService(record_store=store)
    record = service.submit(_request(), public_base_url="http://uta")
    start = threading.Barrier(8)
    errors = []

    def save_copy(index: int) -> None:
        try:
            start.wait(timeout=5)
            copy = record.model_copy(deep=True)
            copy.summary = f"summary-{index}"
            store.save(copy)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=save_copy, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    recovered = store.load(record.task_id)
    assert errors == []
    assert recovered is not None
    assert recovered.task_id == record.task_id
    assert not list((tmp_path / "records" / "tasks").glob("*.tmp"))


def test_service_passes_configured_jira_client_to_repair_context(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="commit message\x1e", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="test-enforcer check-coverage failed: diff line coverage 87.50% is below required 95.00%",
            stderr="",
        )

    class FakeJiraClient:
        def __init__(self):
            self.seen = []

        def fetch_description(self, jira_id):
            self.seen.append(jira_id)
            return "Description from Jira"

    jira_client = FakeJiraClient()
    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces", git_bin=fake_git(tmp_path)),
        enforcement_runner=MavenEnforcementRunner("mvn -Dtest.enforcement.enabled=true verify", run_command=fake_run),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([RdcProtocol(context_provider=RdcContextProvider(jira_client))]),
    )
    record = service.submit(_request(), public_base_url="http://uta")

    service.create_fix_session(record, CreateFixSessionRequest(targetIds=["class:com.example.Foo"]))

    assert jira_client.seen == ["TASK-82767"]
    assert record.fix_sessions[0]["repoTaskId"]
    task = service.task_manager.get_task(record.fix_sessions[0]["repoTaskId"])
    assert "Description from Jira" in task["rdc_context_json"]


def test_jira_client_posts_jira_key_contract():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"fields": {"description": "desc"}}})

    client = JiraDescriptionClient(
        endpoint="http://jira/wcr/jira/issues/raw/v2",
        transport=httpx.MockTransport(handler),
    )

    assert client.fetch_description("TASK-82767") == "desc"
    assert seen[0].read().decode("utf-8") == '{"jira_key":"TASK-82767"}'


def test_workspace_rejects_untrusted_git_url_and_bad_branch(tmp_path):
    manager = GitWorkspaceManager(workspace_root=tmp_path)

    with pytest.raises(ValueError, match="git_url"):
        manager.prepare(git_url="file:///tmp/repo.git", branch="feature/TASK-82767", task_id="task-1")
    with pytest.raises(ValueError, match="branch"):
        manager.prepare(
            git_url="git@git.example.com:group/demo.git",
            branch="../master",
            task_id="task-2",
        )

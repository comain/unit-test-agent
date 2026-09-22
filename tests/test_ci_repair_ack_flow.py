import subprocess
from fake_maven_metadata import DIFF_MUTATION_OK, with_resolved_enforcer

import httpx

from fake_git import fake_git
from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.shared.ci_models import CiTriggerRequest
from uta.app.protocols import ProtocolRegistry
from uta.app.protocols.rdc import RdcCallbackClient, RdcProtocol
from uta.app.service import ApiTriggerService
from uta.app.workspace import GitWorkspaceManager
from uta.tasks.manager import TaskManager


def _request() -> CiTriggerRequest:
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
            "operator": "dev-user",
        }
    )


def test_repair_green_rerun_sends_rdc_success_ack(tmp_path):
    callback_bodies = []

    def callback_handler(request: httpx.Request) -> httpx.Response:
        callback_bodies.append(request.read().decode("utf-8"))
        return httpx.Response(200, text="ok")

    maven_results = iter(
        [
            subprocess.CompletedProcess(["mvn"], 1, stdout="", stderr="initial red"),
            subprocess.CompletedProcess(
                ["mvn"],
                0,
                stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
                stderr="",
            ),
        ]
    )

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return next(maven_results)

    manager = TaskManager(tmp_path / "tasks.db")
    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspace", git_bin=fake_git(tmp_path)),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        protocols=ProtocolRegistry(
            [
                RdcProtocol(
                    callback_client=RdcCallbackClient(
                        ack_url="http://rdc/plugin/ack",
                        transport=httpx.MockTransport(callback_handler),
                        sleep=lambda _: None,
                    )
                )
            ]
        ),
        task_manager=manager,
    )
    record = service.submit(_request(), public_base_url="http://uta")
    session = service.create_fix_session(
        record,
        CreateFixSessionRequest(target_ids=["class:com.example.Demo"]),
    )["session"]
    repo_task_id = int(session["repoTaskId"])
    manager.mark_running(repo_task_id)
    manager.record_push_verified(
        repo_task_id,
        branch_name="feature/TASK-82767",
        local_head="def456",
        remote_head="def456",
    )
    manager.mark_completed(repo_task_id, message="repair finished")

    refreshed = service.get(record.task_id)

    assert refreshed.status.value == "success"
    assert refreshed.enforcement_result["status"] == "passed"
    assert session["status"] == "green"
    assert session["repoTaskId"] == repo_task_id
    assert session["rerunEnforcement"]["summary"] == "UTA test-enforcement passed"
    assert '"state":-1024' in callback_bodies[0].replace(" ", "")
    assert '"state":0' in callback_bodies[-1].replace(" ", "")


def test_repair_red_rerun_sends_rdc_failure_ack(tmp_path):
    callback_bodies = []

    def callback_handler(request: httpx.Request) -> httpx.Response:
        callback_bodies.append(request.read().decode("utf-8"))
        return httpx.Response(200, text="ok")

    # Two enforcement runs are exercised: the initial check and the post-repair
    # rerun. The repo-task precheck owns any pre-LLM Maven enforcer pass.
    maven_results = iter(
        [
            subprocess.CompletedProcess(["mvn"], 1, stdout="", stderr="initial red"),
            subprocess.CompletedProcess(["mvn"], 1, stdout="", stderr="still red after repair"),
        ]
    )

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return next(maven_results)

    manager = TaskManager(tmp_path / "tasks.db")
    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspace", git_bin=fake_git(tmp_path)),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        protocols=ProtocolRegistry(
            [
                RdcProtocol(
                    callback_client=RdcCallbackClient(
                        ack_url="http://rdc/plugin/ack",
                        transport=httpx.MockTransport(callback_handler),
                        sleep=lambda _: None,
                    )
                )
            ]
        ),
        task_manager=manager,
    )
    record = service.submit(_request(), public_base_url="http://uta")
    session = service.create_fix_session(
        record,
        CreateFixSessionRequest(target_ids=["class:com.example.Demo"]),
    )["session"]
    repo_task_id = int(session["repoTaskId"])
    manager.mark_running(repo_task_id)
    manager.record_push_verified(
        repo_task_id,
        branch_name="feature/TASK-82767",
        local_head="def456",
        remote_head="def456",
    )
    manager.mark_completed(repo_task_id, message="repair finished")

    refreshed = service.get(record.task_id)

    assert refreshed.status.value == "failed"
    assert refreshed.enforcement_result["status"] == "failed"
    assert session["status"] == "rerun_failed"
    assert len(callback_bodies) == 2
    assert '"state":-1024' in callback_bodies[0].replace(" ", "")
    assert '"state":-1024' in callback_bodies[1].replace(" ", "")

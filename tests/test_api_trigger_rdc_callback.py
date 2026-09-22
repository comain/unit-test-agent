import json
import subprocess
from fake_maven_metadata import DIFF_MUTATION_OK, with_resolved_enforcer

import httpx

from fake_git import fake_git
from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.app.protocols import ProtocolRegistry
from uta.app.protocols.base import CiResult
from uta.app.protocols.rdc import RdcCallbackClient, RdcCallbackPayload, RdcProtocol
from uta.app.service import ApiTriggerService
from uta.app.workspace import GitWorkspaceManager


def _rdc_request(app_name: str = "demo") -> CiTriggerRequest:
    return CiTriggerRequest.model_validate(
        {
            "appName": app_name,
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
            "taskId": "rdc-task-1",
            "recordId": "record-1",
            "parentId": "parent-1",
            "taskTemplateId": "T_91_pre_unitTestAppTool",
            "operator": "dev-user",
        }
    )


def test_rdc_protocol_temporarily_auto_passes_fd_wmonitor_default_store():
    callback_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        callback_bodies.append(json.loads(request.read()))
        return httpx.Response(200, text="ok")

    protocol = RdcProtocol(
        callback_client=RdcCallbackClient(
            ack_url="http://rdc/plugin/ack",
            retry_times=0,
            transport=httpx.MockTransport(handler),
        )
    )
    record = CiTaskRecord(
        task_id="internal-task-1",
        status=CiTaskStatus.failed,
        request=_rdc_request("fd_wmonitor_default_store"),
        summary="JDK 8 cannot load class version 69",
    )

    outcome = protocol.report_result(
        record,
        CiResult(
            passed=False,
            report_url="http://uta/report",
            summary="JDK 8 cannot load class version 69",
        ),
    )

    assert outcome.succeeded is True
    assert record.status == CiTaskStatus.failed
    assert record.summary == "JDK 8 cannot load class version 69"
    assert callback_bodies[0]["state"] == 0
    assert callback_bodies[0]["data"]["passed"] == "true"
    assert callback_bodies[0]["data"]["score"] == "100"
    callback_summary = callback_bodies[0]["data"]["summary"]
    assert "temporary compatibility override" in callback_summary.lower()
    assert "JDK 8 cannot load class version 69" in callback_bodies[0]["data"]["summary"]


def test_rdc_protocol_keeps_failure_for_other_applications():
    callback_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        callback_bodies.append(json.loads(request.read()))
        return httpx.Response(200, text="ok")

    protocol = RdcProtocol(
        callback_client=RdcCallbackClient(
            ack_url="http://rdc/plugin/ack",
            retry_times=0,
            transport=httpx.MockTransport(handler),
        )
    )
    record = CiTaskRecord(
        task_id="internal-task-2",
        status=CiTaskStatus.failed,
        request=_rdc_request("another_java_application"),
    )

    protocol.report_result(
        record,
        CiResult(
            passed=False,
            report_url="http://uta/report",
            summary="test enforcement failed",
        ),
    )

    assert callback_bodies[0]["state"] == -1024
    assert callback_bodies[0]["data"] == {
        "passed": "false",
        "score": "0",
        "summary": "test enforcement failed",
    }


def test_rdc_callback_body_matches_rdc_ack_contract():
    request = _rdc_request()
    payload = RdcCallbackPayload(
        passed=True,
        score=100,
        report_url="http://uta/reports/task-1/index.html",
        summary="UTA test-enforcement passed",
    )

    body = RdcCallbackClient(ack_url="http://rdc/plugin/ack").build_body(request, payload)

    assert body == {
        "state": 0,
        "attribute": {
            "taskId": "rdc-task-1",
            "recordId": "record-1",
            "taskTemplateId": "T_91_pre_unitTestAppTool",
            "parentId": "parent-1",
            "url": "http://uta/reports/task-1/index.html",
            "reportUrl": "http://uta/reports/task-1/index.html",
            "operator": "dev-user",
        },
        "data": {
            "passed": "true",
            "score": "100",
            "summary": "UTA test-enforcement passed",
        },
    }


def test_rdc_callback_retries_until_mock_server_success():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(500, text="temporary failure")
        return httpx.Response(200, text="ok")

    client = RdcCallbackClient(
        ack_url="http://rdc/plugin/ack",
        retry_times=2,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    result = client.send(
        _rdc_request(),
        RdcCallbackPayload(passed=False, score=0, report_url="http://uta/report", summary="missing evidence"),
    )

    assert result.succeeded is True
    assert [entry["status_code"] for entry in result.history] == [500, 200]
    assert len(seen) == 2


def test_rdc_callback_detects_ignored_terminal_state_update():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": 0,
                "data": {
                    "state": "SUCCESS",
                    "result": {
                        "summary": "previous success",
                        "score": "100",
                        "passed": "true",
                    },
                },
            },
        )

    client = RdcCallbackClient(
        ack_url="http://rdc/plugin/ack",
        retry_times=2,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )

    result = client.send(
        _rdc_request(),
        RdcCallbackPayload(
            passed=False,
            score=0,
            report_url="http://uta/report",
            summary="repair session ended (repair_stale)",
        ),
    )

    assert result.succeeded is False
    assert "did not apply requested passed=false" in result.error
    assert len(seen) == 1
    assert result.history[0]["request"]["passed"] == "false"


def test_ci_service_records_bounded_callback_retry_failure(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
            stderr="",
        )

    callback = RdcCallbackClient(
        ack_url="http://rdc/plugin/ack",
        retry_times=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(503, text="down")),
        sleep=lambda _: None,
    )
    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path, git_bin=fake_git(tmp_path)),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        protocols=ProtocolRegistry([RdcProtocol(callback_client=callback)]),
    )

    record = service.submit(_rdc_request(), public_base_url="http://uta")

    assert record.status.value == "success"
    assert record.callback_succeeded is False
    assert record.callback_error
    assert [entry["status_code"] for entry in record.callback_history] == [503, 503]

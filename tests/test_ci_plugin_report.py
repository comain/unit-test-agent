from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from uta.ci_plugin.app import create_app
from uta.ci_plugin.context import RepairContextExporter
from uta.ci_plugin.enforcement import MavenEnforcementRunner
from uta.ci_plugin.fix_sessions import CreateFixSessionRequest
from uta.ci_plugin.models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.ci_plugin.protocols import ProtocolRegistry
from uta.ci_plugin.protocols.github import GithubAppAuth, GithubChecksClient, GithubWebhookProtocol
from uta.ci_plugin.reporting import CiReportRenderer, format_gmt8
from uta.ci_plugin.service import CiPluginService
from uta.ci_plugin.store import JsonCiTaskStore
from uta.ci_plugin.workspace import GitWorkspaceManager
from uta.tasks.manager import TaskManager


def _github_payload(branch: str = "feature/EXAMPLE-122", title: str = "EXAMPLE-122 add coverage") -> dict:
    return {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "title": title,
            "body": "Covers checkout edge cases",
            "user": {"login": "dev-user"},
            "head": {"ref": branch, "sha": "abc123def"},
        },
        "repository": {
            "name": "demo-app",
            "full_name": "example/demo-app",
            "owner": {"login": "example"},
            "clone_url": "git@git.example.com:group/demo.git",
        },
        "installation": {"id": 555},
    }


def _trigger(client: TestClient, payload: dict | None = None) -> str:
    response = client.post(
        "/api/v1/github/webhook",
        content=json.dumps(payload or _github_payload()).encode("utf-8"),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 200
    return response.json()["taskId"]


def _github_service(**kwargs) -> CiPluginService:
    return CiPluginService(protocols=ProtocolRegistry([GithubWebhookProtocol()]), **kwargs)


def _request_with_issue() -> CiTriggerRequest:
    protocol = GithubWebhookProtocol()
    request = protocol.parse_trigger(
        json.dumps(_github_payload()).encode("utf-8"),
        {"X-GitHub-Event": "pull_request"},
    )
    assert request is not None
    return request


def test_task_status_page_and_data_for_queued_check():
    client = TestClient(create_app(_github_service()))
    task_id = _trigger(client)

    page = client.get(f"/task-status/{task_id}")
    data = client.get(f"/task-status/{task_id}/data")

    assert page.status_code == 200
    assert "demo-app" in page.text
    assert "queued" in page.text
    assert "创建时间" in page.text
    assert "GMT+8" in page.text
    assert data.status_code == 200
    assert data.json()["taskId"] == task_id
    assert data.json()["status"] == "queued"


def test_test_enforcement_usage_doc_route_is_browser_accessible():
    client = TestClient(create_app(CiPluginService()))

    response = client.get("/docs/test-enforce-usage.md")

    assert response.status_code == 200
    assert "Direct test-enforcer Fallback" in response.text
    assert "test-enforcer" in response.text


def test_recent_jobs_page_lists_persisted_jobs_from_last_day(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    now = datetime.now(timezone.utc)
    recent = CiTaskRecord(
        task_id="recent-python-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-demo",
            git_url="git@git.example.com:group/py-demo.git",
            branch="feature/EXAMPLE-122",
            task_id="ci-task-1",
            record_id="record-1",
            task_template_id="T_91_pre_unitTestAppTool",
            language="python",
        ),
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
        summary="Python enforcement failed",
        report_url="http://uta/reports/recent-python-1/index.html",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "evidence": {
                "coverage": {"covered": 1, "total": 3, "rate": 33.33, "passed": False},
                "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
            },
        },
    )
    old = CiTaskRecord(
        task_id="old-java-1",
        status=CiTaskStatus.success,
        request=CiTriggerRequest(app_name="old-java", git_url="git@git.example.com:group/old-java.git", branch="main"),
        created_at=now - timedelta(days=2),
    )
    store.save(recent)
    store.save(old)
    client = TestClient(create_app(CiPluginService(record_store=store)))

    data = client.get("/jobs/recent/data").json()
    page = client.get("/jobs/recent.html")

    assert data["total"] == 1
    assert data["statusCounts"]["failed"] == 1
    assert data["languageCounts"]["python"] == 1
    assert data["rows"][0]["taskId"] == "recent-python-1"
    assert page.status_code == 200
    assert "UTA 最近任务状态" in page.text
    assert "py-demo" in page.text
    assert "Python enforcement failed" in page.text
    assert "old-java" not in page.text


def test_report_page_and_detail_expose_enforcement_evidence(tmp_path):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.common (13/13)\n"
                "[info] [test-enforcer] diff line coverage 90.00% passed for demo.biz (9/10)\n"
                "[info] [test-enforcer] diff mutation score 100.00% passed for demo.biz (5/5 detected)\n"
                "[INFO] BUILD SUCCESS"
            ),
            stderr="",
        )

    service = CiPluginService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path, run_command=fake_run),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        protocols=ProtocolRegistry([GithubWebhookProtocol()]),
    )
    client = TestClient(create_app(service))
    task_id = _trigger(client)

    detail = client.get(f"/reports/{task_id}/detail")
    report = client.get(f"/reports/{task_id}/index.html")

    assert detail.status_code == 200
    body = detail.json()
    assert body["taskId"] == task_id
    assert body["status"] == "success"
    assert body["enforcement"]["status"] == "passed"
    assert body["evidence"]["coverage"]["formattedRate"] == "95.65%"
    assert body["evidence"]["mutation"]["source"] == "diff"
    assert body["evidence"]["mutation"]["formattedRate"] == "100.00%"
    assert report.status_code == 200
    assert "单元测试覆盖率报告" in report.text
    assert "95.65%" in report.text
    assert "Diff 变异得分" in report.text
    assert "100.00%" in report.text
    assert "GMT+8" in report.text


def test_report_mutation_evidence_falls_back_to_pit_when_diff_mutation_is_absent():
    detail = CiReportRenderer._mutation_detail(
        ">> Generated 11 mutations Killed 10 (90%)\n"
        ">> Generated 4 mutations Killed 4 (100%)\n"
    )

    assert detail["source"] == "pit"
    assert detail["formattedRate"] == "93.33%"
    assert detail["killed"] == 14
    assert detail["generated"] == 15


def test_report_evidence_uses_python_structured_evidence():
    detail = CiReportRenderer._evidence_detail(
        {
            "language": "python",
            "backend": "python_enforcer",
            "evidence": {
                "coverage": {"covered": 21, "total": 23, "rate": 91.3043, "passed": True},
                "mutation": {"generated": 19, "killed": 15, "survived": 4, "rate": 78.9474, "passed": True},
            },
        }
    )

    assert detail["coverage"]["formattedRate"] == "91.30%"
    assert detail["mutation"]["source"] == "python"
    assert detail["mutation"]["formattedRate"] == "78.95%"


def test_report_missing_plugin_shows_upgrade_requirements_and_zero_metrics():
    record = CiTaskRecord(
        task_id="missing-plugin-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(app_name="shelf-service", git_url="git@git.example.com:opc/shelf-service.git", branch="IDSS-40967"),
        summary="Missing test-enforcement plugin/profile",
        enforcement_result={
            "status": "missing_evidence",
            "passed": False,
            "command": ["mvn", "-U", "verify"],
            "summary": "Missing test-enforcement plugin/profile",
            "evidence": {
                "coverage": {"covered": 0, "total": 0, "rate": 0.0, "passed": False},
                "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
                "tooling": {"available": False, "artifactId": "service-parent", "version": "1.3.19"},
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["canCreateFixSession"] is False
    assert detail["evidence"]["coverage"]["formattedRate"] == "0.00%"
    assert "Maven effective-pom" in html
    assert "test-enforcer &gt;= 1.0.12" in html
    assert "&lt;artifactId&gt;service-parent&lt;/artifactId&gt;" in html
    assert "&lt;artifactId&gt;quality-parent&lt;/artifactId&gt;" in html
    assert "&lt;artifactId&gt;test-enforcer&lt;/artifactId&gt;" in html
    assert "一键修复" not in html


def test_ci_report_time_filter_formats_gmt8():
    assert format_gmt8(datetime(2026, 5, 18, 9, 48, 7, tzinfo=timezone.utc)) == "2026-05-18 17:48:07 GMT+8"


def test_report_detail_and_page_show_missing_context_and_callback_status(tmp_path):
    callback_bodies = []

    def callback_handler(request: httpx.Request) -> httpx.Response:
        callback_bodies.append(json.loads(request.read()))
        return httpx.Response(500, text="ci down")

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="test-enforcer check-coverage failed: diff line coverage 87.50% is below required 95.00%",
            stderr="",
        )

    checks = GithubChecksClient(
        auth=GithubAppAuth(token_provider=lambda inst: f"tok-{inst}"),
        transport=httpx.MockTransport(callback_handler),
        retry_times=1,
        sleep=lambda _: None,
    )
    service = CiPluginService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspace", run_command=fake_run),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        protocols=ProtocolRegistry([GithubWebhookProtocol(checks_client=checks)]),
    )
    client = TestClient(create_app(service))
    task_id = _trigger(client)

    detail = client.get(f"/reports/{task_id}/detail").json()
    report = client.get(f"/reports/{task_id}/index.html")

    assert callback_bodies
    assert detail["callback"]["succeeded"] is False
    assert detail["callback"]["error"]
    assert "git_commit_messages_unavailable" in detail["context"]["missingReasons"]
    assert report.status_code == 200
    assert "缺失上下文" in report.text
    assert "CI callback" in report.text
    assert "fix-cta" in report.text


def test_ci_service_logs_check_callback_and_preemption(tmp_path, caplog):
    def callback_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": 1})

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="test-enforcer check-coverage failed: diff line coverage 87.50% is below required 95.00%",
            stderr="",
        )

    checks = GithubChecksClient(
        auth=GithubAppAuth(token_provider=lambda inst: f"tok-{inst}"),
        transport=httpx.MockTransport(callback_handler),
        sleep=lambda _: None,
    )
    manager = TaskManager(tmp_path / "tasks.db")
    service = CiPluginService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspace", run_command=fake_run),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        protocols=ProtocolRegistry([GithubWebhookProtocol(checks_client=checks)]),
        task_manager=manager,
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
    )

    with caplog.at_level(logging.INFO, logger="uta.ci_plugin.service"):
        record = service.submit(_request_with_issue(), public_base_url="http://uta", protocol="github")
        batch_id = manager.create_task(repo_path=record.workspace_path, class_fqns=["com.example.Batch"], priority=100)
        manager.mark_running(batch_id, stage="generate", detail="large batch")
        service.create_fix_session(record, CreateFixSessionRequest(target_ids=["class:com.example.Demo"]))

    messages = "\n".join(item.getMessage() for item in caplog.records)
    assert "ci_check_started" in messages
    assert "ci_check_finished" in messages
    assert "ci_callback_finished" in messages
    assert "ci_repair_preempted" in messages

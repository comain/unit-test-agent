import logging
import subprocess
from fake_maven_metadata import with_resolved_enforcer
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from fake_git import fake_git
from uta.app.app import create_app
from uta.app.context import RepairContextExporter
from uta.language.java.ci_evidence import _mutation_detail, java_gate_failure_summary, output_evidence_detail
from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.app.protocols import ProtocolRegistry
from uta.app.protocols.rdc import RdcCallbackClient, RdcProtocol, parse_rdc_payload
from uta.enforcement.evidence import evidence_detail
from uta.app.reporting import CiReportRenderer, format_gmt8
from uta.app.service import ApiTriggerService
from uta.app.store import JsonCiTaskStore
from uta.app.workspace import GitWorkspaceManager
from uta.tasks.manager import TaskManager
from uta.shared.targets import TargetIdentity
from uta.app.repair import RepairSessions


def _rdc_payload():
    return {
        "attribute": {
            "appName": "demo-app",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
            "taskId": "rdc-task-1",
            "recordId": "record-1",
            "parentId": "parent-1",
            "taskTemplateId": "T_91_pre_unitTestAppTool",
        }
    }


def _rdc_service(**kwargs) -> ApiTriggerService:
    return ApiTriggerService(protocols=ProtocolRegistry([RdcProtocol()]), **kwargs)


def test_task_status_page_and_data_for_queued_check():
    client = TestClient(create_app(_rdc_service()))
    trigger = client.post("/api/v1/rdc/trigger", json=_rdc_payload()).json()
    task_id = trigger["data"]["taskId"]

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


def test_recent_jobs_marks_interrupted_record_without_enforcement_as_failed():
    record = CiTaskRecord(
        task_id="interrupted",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
        summary="CI test-enforcement report task was interrupted before completion.",
    )

    row = CiReportRenderer._recent_job_row(record)

    assert row["enforcementStatus"] == "failed"


def test_report_renders_canonical_task_link_for_superseded_fix_session():
    record = CiTaskRecord(
        task_id="old-report",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
        summary="old failed report",
        enforcement_result={"status": "failed", "passed": False, "summary": "failed"},
        fix_sessions=[
            {
                "sessionId": "stale-session",
                "status": "superseded",
                "retryCount": 0,
                "canonicalTaskId": "canonical-task",
                "canonicalTaskUrl": "/unit-test/task-status/canonical-task",
            }
        ],
    )

    html = CiReportRenderer().report_html(record)

    assert "查看幂等任务" in html
    assert "/unit-test/task-status/canonical-task" in html
    assert "已转到幂等任务" in html
    assert "fix-sessions/stale-session/progress" not in html


def test_java_gate_failure_summary_includes_gate_and_test_failure_reasons():
    summary = java_gate_failure_summary(
        {
            "passed": False,
            "summary": "UTA test-enforcement failed",
            "stdout": (
                "[ERROR] Tests run: 107, Failures: 0, Errors: 5, Skipped: 0\n"
                "[ERROR]   NoticeUtilTest.init:42 RuntimeException\n"
                "[ERROR] Failed to execute goal com.example.build.maven-plugins:test-enforcer:1.0.12:check-coverage "
                "on project svc: test-enforcer check-coverage failed: diff line coverage "
                "91.00% is below required 95.00% (1031/1133)\n"
            ),
            "stderr": "",
        }
    )

    assert summary is not None
    assert "Coverage gate failed: 91.00% < 95.00% (1031/1133)" in summary
    assert "Selected test run failed: 5 errors (NoticeUtilTest)" in summary


def test_report_renders_canonical_session_link_for_superseded_fix_session():
    record = CiTaskRecord(
        task_id="old-report",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
        summary="old failed report",
        enforcement_result={"status": "failed", "passed": False, "summary": "failed"},
        fix_sessions=[
            {
                "sessionId": "stale-session",
                "status": "superseded",
                "retryCount": 0,
                "canonicalSessionId": "canonical-session",
                "canonicalSessionUrl": "/unit-test/reports/new-report/fix-sessions/canonical-session/progress",
                "canonicalReportId": "new-report",
            }
        ],
    )

    html = CiReportRenderer().report_html(record)

    assert "查看幂等会话" in html
    assert "/unit-test/reports/new-report/fix-sessions/canonical-session/progress" in html
    assert "已转到幂等会话" in html
    assert "fix-sessions/stale-session/progress" not in html


def test_test_enforcement_usage_doc_route_is_browser_accessible():
    client = TestClient(create_app(_rdc_service()))

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
            branch="feature/TASK-82767",
            task_id="rdc-task-1",
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
        fix_sessions=[
            {
                "sessionId": "repair-session-1",
                "status": "repair_task_created",
                "repoTaskId": 96,
                "repoTaskStatus": "CREATED",
                "repoTaskStage": None,
                "budgetUsed": {
                    "actualCostUsd": 0.123456,
                    "estimatedCostUsd": 2.0,
                    "totalTokens": 151618,
                    "inputTokens": 37872,
                    "cacheReadTokens": 109568,
                    "outputTokens": 2334,
                    "reasoningTokens": 1844,
                },
                "retryCount": 0,
                "createdAt": (now - timedelta(minutes=30)).isoformat(),
                "updatedAt": (now - timedelta(minutes=10)).isoformat(),
            },
            {
                "sessionId": "repair-session-older-task",
                "status": "repair_failed",
                "repoTaskId": 95,
                "repoTaskStatus": "FAILED",
                "repoTaskStage": "finished",
                "retryCount": 0,
                "createdAt": (now - timedelta(minutes=20)).isoformat(),
                "updatedAt": (now - timedelta(minutes=5)).isoformat(),
            }
        ],
    )
    old = CiTaskRecord(
        task_id="old-java-1",
        status=CiTaskStatus.success,
        request=CiTriggerRequest(
            app_name="old-java",
            git_url="git@git.example.com:group/old-java.git",
            branch="main",
        ),
        created_at=now - timedelta(days=2),
    )
    store.save(recent)
    store.save(old)
    client = TestClient(create_app(ApiTriggerService(record_store=store)))

    data = client.get("/jobs/recent/data").json()
    page = client.get("/jobs/recent.html")

    assert data["total"] == 1
    assert data["statusCounts"]["failed"] == 1
    assert data["languageCounts"]["python"] == 1
    assert data["rows"][0]["taskId"] == "recent-python-1"
    assert data["repairSessions"][0]["sessionId"] == "repair-session-1"
    assert data["repairSessions"][0]["repoTaskId"] == 96
    assert data["repairSessions"][0]["budgetUsedText"] == "$0.1235 / $2.0000 · 151.6K tokens"
    assert data["repairSessions"][0]["budgetUsed"]["totalTokens"] == 151618
    assert data["repairSessions"][1]["sessionId"] == "repair-session-older-task"
    assert data["repairSessions"][1]["repoTaskId"] == 95
    assert data["repairSessions"][0]["progressUrl"].endswith(
        "../reports/recent-python-1/fix-sessions/repair-session-1/progress"
    )
    assert page.status_code == 200
    assert "UTA 最近任务状态" in page.text
    assert "最近修复会话" in page.text
    assert "py-demo" in page.text
    assert "Python enforcement failed" in page.text
    assert "repair-session-1" in page.text
    assert ">96<" in page.text
    assert "预算使用" in page.text
    assert "$0.1235 / $2.0000 · 151.6K tokens" in page.text
    assert "../reports/recent-python-1/fix-sessions/repair-session-1/progress" in page.text
    assert "old-java" not in page.text
    assert "T_91_pre_unitTestAppTool" in page.text
    assert 'data-operation="retry"' in page.text
    assert 'data-operation="rdc-callback"' in page.text
    assert 'data-operation="stop"' not in page.text


def test_recent_jobs_page_exposes_stop_only_for_active_jobs():
    request = CiTriggerRequest(
        app_name="demo-app",
        git_url="git@git.example.com:group/demo.git",
        branch="feature/TASK-82767",
        task_id="rdc-task-1",
        record_id="record-1",
    )
    active = CiTaskRecord(task_id="active", status=CiTaskStatus.running, request=request)
    terminal = CiTaskRecord(task_id="terminal", status=CiTaskStatus.failed, request=request)

    html = CiReportRenderer().recent_jobs_html(
        [active, terminal],
        since=datetime.now(timezone.utc) - timedelta(hours=1),
        generated_at=datetime.now(timezone.utc),
        hours=1,
    )

    active_row = html.split('<code>active</code>', 1)[1].split("</tr>", 1)[0]
    terminal_row = html.split('<code>terminal</code>', 1)[1].split("</tr>", 1)[0]
    assert 'data-operation="stop"' in active_row
    assert 'data-operation="retry"' not in active_row
    assert 'data-operation="rdc-callback"' in active_row
    assert 'data-operation="stop"' not in terminal_row
    assert 'data-operation="retry"' in terminal_row
    assert 'data-operation="rdc-callback"' in terminal_row


def test_recent_job_stop_marks_record_stopped_and_terminates_workspace(tmp_path):
    stopped_workspaces = []
    service = _rdc_service(
        record_store=JsonCiTaskStore(tmp_path / "records"),
        ci_process_stopper=lambda path: stopped_workspaces.append(path) or 2,
    )
    record = CiTaskRecord(
        task_id="running-job",
        status=CiTaskStatus.running,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
        workspace_path=str(tmp_path / "workspace"),
    )
    service._tasks[record.task_id] = record
    service.save(record)
    client = TestClient(create_app(service))

    response = client.post(f"/jobs/{record.task_id}/stop")

    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert stopped_workspaces == [tmp_path / "workspace"]
    assert service.get(record.task_id).status == CiTaskStatus.stopped


def test_recent_job_stop_can_terminate_clone_before_workspace_is_persisted(tmp_path):
    stopped_workspaces = []
    service = _rdc_service(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces"),
        record_store=JsonCiTaskStore(tmp_path / "records"),
        ci_process_stopper=lambda path: stopped_workspaces.append(path) or 1,
    )
    record = CiTaskRecord(
        task_id="cloning-job",
        status=CiTaskStatus.running,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
    )
    service._tasks[record.task_id] = record

    service.stop_ci_task(record.task_id)

    assert stopped_workspaces == [tmp_path / "workspaces" / "cloning-job"]


def test_recent_job_retry_creates_fresh_task_with_same_trigger(tmp_path):
    service = _rdc_service(record_store=JsonCiTaskStore(tmp_path / "records"))
    original = CiTaskRecord(
        task_id="failed-job",
        status=CiTaskStatus.failed,
        protocol="rdc",
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
            task_id="rdc-task-1",
            record_id="record-1",
            language="java",
            metadata={"source": "rdc"},
        ),
    )
    service.save(original)
    client = TestClient(create_app(service))

    response = client.post(f"/jobs/{original.task_id}/retry")

    assert response.status_code == 200
    body = response.json()
    assert body["taskId"] != original.task_id
    retried = service.get(body["taskId"])
    assert retried.status == CiTaskStatus.queued
    assert retried.protocol == original.protocol
    assert retried.request == original.request


def test_recent_job_rdc_callback_replays_terminal_result(tmp_path):
    callback_results = []

    class CallbackProtocol(RdcProtocol):
        def can_report(self, record):
            return True

        def report_result(self, record, result):
            from uta.app.protocols import CiCallbackOutcome

            callback_results.append(result)
            return CiCallbackOutcome(succeeded=True, history=[{"status_code": 200}])

    service = ApiTriggerService(
        record_store=JsonCiTaskStore(tmp_path / "records"),
        protocols=ProtocolRegistry([CallbackProtocol()]),
    )
    record = CiTaskRecord(
        task_id="failed-job",
        status=CiTaskStatus.failed,
        protocol="rdc",
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
            task_id="rdc-task-1",
            record_id="record-1",
        ),
        summary="gate failed",
        report_url="http://uta/reports/failed-job/index.html",
    )
    service.save(record)
    client = TestClient(create_app(service))

    response = client.post(f"/jobs/{record.task_id}/rdc-callback")

    assert response.status_code == 200
    assert response.json()["callbackSucceeded"] is True
    assert callback_results[0].passed is True
    assert "operator" in callback_results[0].summary.lower()
    persisted = service.get(record.task_id)
    assert persisted.callback_override["passed"] is True


def test_stopped_ci_worker_cannot_overwrite_terminal_state(tmp_path):
    class LateRunner:
        def run(self, repo_path):
            from uta.enforcement.enforcement import QualityGateResult, QualityGateStatus

            service._ci_stop_requested.add("running-job")
            service._tasks["running-job"].status = CiTaskStatus.stopped
            return QualityGateResult(
                status=QualityGateStatus.failed,
                passed=False,
                command=["mvn", "verify"],
                summary="late failure",
            )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = _rdc_service(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path, git_bin=fake_git(tmp_path)),
        enforcement_runner=LateRunner(),
        record_store=JsonCiTaskStore(tmp_path / "records"),
    )
    record = CiTaskRecord(
        task_id="running-job",
        status=CiTaskStatus.running,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
    )
    service._tasks[record.task_id] = record

    service._run_check(record)

    assert record.status == CiTaskStatus.stopped
    assert record.enforcement_result is None


def test_compact_ci_record_preserves_manual_callback_override(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    record = CiTaskRecord(
        task_id="callback-override",
        status=CiTaskStatus.running,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
        callback_override={"passed": True, "source": "recent_jobs_operator"},
    )
    store.save(record)

    compact = store.list_record_summaries(limit=1)[0]

    assert compact.callback_override == {
        "passed": True,
        "source": "recent_jobs_operator",
    }


def test_recent_jobs_page_uses_compact_records_for_large_enforcement_payloads(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    now = datetime.now(timezone.utc)
    record = CiTaskRecord(
        task_id="large-python-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-large",
            git_url="git@git.example.com:group/py-large.git",
            branch="feature/TASK-82767",
            language="python",
        ),
        created_at=now - timedelta(minutes=15),
        updated_at=now - timedelta(minutes=5),
        summary="Python enforcement failed with large output",
        report_url="http://uta/reports/large-python-1/index.html",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "stdout": "x" * (1024 * 1024),
            "stderr": "y" * (1024 * 1024),
        },
        fix_sessions=[
            {
                "sessionId": "repair-large-1",
                "status": "repair_task_created",
                "repoTaskId": 101,
                "repoTaskStatus": "CREATED",
                "retryCount": 0,
                "createdAt": (now - timedelta(minutes=10)).isoformat(),
                "updatedAt": (now - timedelta(minutes=2)).isoformat(),
            }
        ],
    )
    store.save(record)
    client = TestClient(create_app(ApiTriggerService(record_store=store)))

    data = client.get("/jobs/recent/data").json()
    page = client.get("/jobs/recent.html")

    assert data["total"] == 1
    assert data["rows"][0]["taskId"] == "large-python-1"
    assert data["rows"][0]["enforcementStatus"] == "failed"
    assert data["repairSessions"][0]["sessionId"] == "repair-large-1"
    assert data["repairSessions"][0]["repoTaskId"] == 101
    assert page.status_code == 200
    assert "py-large" in page.text
    assert "repair-large-1" in page.text


def test_recent_jobs_page_refreshes_repair_task_status_from_task_db_without_full_record_load(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    task_manager = TaskManager(tmp_path / "tasks.db")
    now = datetime.now(timezone.utc)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    target = TargetIdentity(
        language="python",
        target_id="pyfile:src/app.py",
        display_name="src/app.py",
        source_path="src/app.py",
        granularity="file",
    )
    repo_task_id = task_manager.create_task_targets(
        repo_path=str(repo_path),
        targets=[target],
        branch_name="feature/TASK-82767",
        coverage_gate=95.0,
        mutation_gate=95.0,
        quality_gate_backend="python_enforcer",
        language="python",
    )
    task_manager.sync_target_results(
        repo_task_id,
        {
            target: {
                "status": "PASS",
                "language": "python",
                "coverage": 100.0,
                "mutation_score": 100.0,
                "surviving_mutants": 0,
                "total_mutants": 3,
                "test_file_path": "tests/uta_generated/test_app.py",
            }
        },
        targets=[target],
    )
    task_manager.mark_completed(repo_task_id, message="repair finished")
    record = CiTaskRecord(
        task_id="stale-repair-status",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-stale",
            git_url="git@git.example.com:group/py-stale.git",
            branch="feature/TASK-82767",
            language="python",
        ),
        created_at=now - timedelta(minutes=15),
        updated_at=now - timedelta(minutes=5),
        summary="Python enforcement failed",
        report_url="http://uta/reports/stale-repair-status/index.html",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "stdout": "x" * (1024 * 1024),
        },
        fix_sessions=[
            {
                "sessionId": "repair-stale-created",
                "status": "repair_task_created",
                "repoTaskId": repo_task_id,
                "repoTaskStatus": "CREATED",
                "createdAt": (now - timedelta(minutes=10)).isoformat(),
                "updatedAt": (now - timedelta(minutes=2)).isoformat(),
            }
        ],
    )
    store.save(record)
    client = TestClient(create_app(ApiTriggerService(record_store=store, task_manager=task_manager)))

    data = client.get("/jobs/recent/data").json()
    page = client.get("/jobs/recent.html")
    persisted = store.load(record.task_id)

    assert data["repairSessions"][0]["sessionId"] == "repair-stale-created"
    assert data["repairSessions"][0]["status"] == "green"
    assert data["repairSessions"][0]["repoTaskStatus"] == "COMPLETED"
    assert "repair-stale-created" in page.text
    assert "COMPLETED" in page.text
    assert persisted.fix_sessions[0]["repoTaskStatus"] == "CREATED"
    assert len(persisted.enforcement_result["stdout"]) == 1024 * 1024


def test_recent_jobs_page_lists_repair_session_when_fix_session_payload_exceeds_tail_window(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    task_manager = TaskManager(tmp_path / "tasks.db")
    now = datetime.now(timezone.utc)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    target = TargetIdentity(
        language="python",
        target_id="pyfile:src/app.py",
        display_name="src/app.py",
        source_path="src/app.py",
        granularity="file",
    )
    repo_task_id = task_manager.create_task_targets(
        repo_path=str(repo_path),
        targets=[target],
        branch_name="feature/TASK-82767",
        coverage_gate=95.0,
        mutation_gate=95.0,
        quality_gate_backend="python_enforcer",
        language="python",
    )
    task_manager.mark_failed(repo_task_id, "repair failed")
    record = CiTaskRecord(
        task_id="large-fix-session",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-large-session",
            git_url="git@git.example.com:group/py-large-session.git",
            branch="feature/TASK-82767",
            language="python",
        ),
        created_at=now - timedelta(minutes=15),
        updated_at=now - timedelta(minutes=5),
        summary="Python enforcement failed",
        report_url="http://uta/reports/large-fix-session/index.html",
        fix_sessions=[
            {
                "sessionId": "repair-large-session",
                "status": "repair_task_created",
                # Retry metadata is appended after the persisted heavy context
                # in production records. The compact reader must still retain
                # the task id without loading this value into memory.
                "rdcContext": {"large": "x" * (1024 * 1024)},
                "repoTaskId": repo_task_id,
                "repoTaskStatus": "CREATED",
                "repoTaskStage": "python_verify",
                "createdAt": (now - timedelta(minutes=10)).isoformat(),
                "updatedAt": (now - timedelta(minutes=2)).isoformat(),
            }
        ],
    )
    store.save(record)
    client = TestClient(create_app(ApiTriggerService(record_store=store, task_manager=task_manager)))

    data = client.get("/jobs/recent/data").json()

    assert data["repairSessions"][0]["sessionId"] == "repair-large-session"
    assert data["repairSessions"][0]["repoTaskId"] == repo_task_id
    assert data["repairSessions"][0]["repoTaskStatus"] == "FAILED"
    assert data["repairSessions"][0]["status"] == "repair_failed"


def test_repair_budget_used_summary_uses_repo_task_metrics():
    summary = RepairSessions._repair_budget_used_from_repo_task(
        {
            "provider_cost_usd": 0.234567,
            "estimated_cost_usd": 3.0,
            "input_tokens": 40000,
            "cache_read_tokens": 100000,
            "output_tokens": 3000,
            "reasoning_tokens": 2000,
            "total_tokens": 145000,
        },
        None,
    )

    assert summary == {
        "actualCostUsd": 0.234567,
        "estimatedCostUsd": 3.0,
        "inputTokens": 40000,
        "cacheReadTokens": 100000,
        "cacheWriteTokens": 0,
        "outputTokens": 3000,
        "reasoningTokens": 2000,
        "totalTokens": 145000,
    }


def test_report_page_and_detail_expose_enforcement_evidence(tmp_path):
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.common (13/13)\n"
                "[info] [test-enforcer] diff line coverage 90.00% passed for demo.biz (9/10)\n"
                "[info] [test-enforcer] diff mutation score 100.00% passed for demo.biz "
                "(5/5 detected; 0 survived; 0 no coverage excluded)\n"
                ">> Generated 1 Killed 1 (100%)\n"
                ">> Generated 11 mutations Killed 10 (90%)\n"
                ">> Generated 4 mutations Killed 4 (100%)\n"
                "[INFO] BUILD SUCCESS"
            ),
            stderr="",
        )

    service = _rdc_service(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path, git_bin=fake_git(tmp_path)),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
    )
    client = TestClient(create_app(service))
    trigger = client.post("/api/v1/rdc/trigger", json=_rdc_payload()).json()
    task_id = trigger["data"]["taskId"]

    detail = client.get(f"/reports/{task_id}/detail")
    report = client.get(f"/reports/{task_id}/index.html")

    assert trigger["data"]["status"] == "queued"
    assert trigger["data"]["reportUrl"].endswith(f"/task-status/{task_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["taskId"] == task_id
    assert body["status"] == "success"
    assert body["enforcement"]["status"] == "passed"
    assert body["evidence"]["coverage"]["formattedRate"] == "95.65%"
    assert body["evidence"]["coverage"]["covered"] == 22
    assert body["evidence"]["coverage"]["total"] == 23
    assert body["evidence"]["mutation"]["source"] == "diff"
    assert body["evidence"]["mutation"]["formattedRate"] == "100.00%"
    assert body["evidence"]["mutation"]["killed"] == 5
    assert body["evidence"]["mutation"]["generated"] == 5
    assert report.status_code == 200
    assert "单元测试覆盖率报告" in report.text
    assert "覆盖率" in report.text
    assert "95.65%" in report.text
    assert "Diff 变异得分" in report.text
    assert "100.00%" in report.text
    assert "本地启用 test-enforcement" in report.text
    assert "plugins/dev-skills/README.zh-CN.md" in report.text
    assert "BUILD SUCCESS" in report.text
    assert "GMT+8" in report.text


def test_large_enforcement_output_is_bounded_in_report_without_mutating_record():
    large_stdout = "HEAD\n" + ("x" * (1024 * 1024)) + "\nTAIL"
    large_stderr = "ERR-HEAD\n" + ("y" * (1024 * 1024)) + "\nERR-TAIL"
    record = CiTaskRecord(
        task_id="large-report",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
        summary="Python enforcement failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "python",
            "command": ["python", "-m", "uta", "python-enforce"],
            "returncode": 1,
            "stdout": large_stdout,
            "stderr": large_stderr,
            "summary": "Python enforcement failed",
        },
    )

    renderer = CiReportRenderer()
    detail = renderer.detail(record)
    html = renderer.report_html(record)

    assert len(html.encode("utf-8")) < 400 * 1024
    assert "output truncated" in html
    assert "HEAD" in html and "TAIL" in html
    assert "ERR-HEAD" in html and "ERR-TAIL" in html
    assert 'fetch("fix-sessions"' in html
    assert len(detail["enforcement"]["stdout"].encode("utf-8")) < 200 * 1024
    assert len(detail["enforcement"]["stderr"].encode("utf-8")) < 200 * 1024
    assert record.enforcement_result["stdout"] == large_stdout
    assert record.enforcement_result["stderr"] == large_stderr


def test_report_mutation_evidence_falls_back_to_pit_when_diff_mutation_is_absent():
    detail = _mutation_detail(
        ">> Generated 11 mutations Killed 10 (90%)\n"
        ">> Generated 4 mutations Killed 4 (100%)\n"
    )

    assert detail["source"] == "pit"
    assert detail["formattedRate"] == "93.33%"
    assert detail["killed"] == 14
    assert detail["generated"] == 15


def test_raw_pit_output_is_diagnostic_even_without_test_enforcer_marker():
    detail = output_evidence_detail(
        ">> Generated 11 mutations Killed 10 (90%)\n"
        ">> Mutations with no coverage 1. Test strength 100%\n"
    )

    assert detail["mutation"] is None
    assert detail["pitMutation"]["source"] == "pit"
    assert detail["pitMutation"]["formattedRate"] == "100.00%"


def test_report_parses_released_changed_line_mutation_marker():
    detail = output_evidence_detail(
        "[test-enforcer] diff line coverage 100.00% passed for demo.biz (38/38)\n"
        "[test-enforcer] diff mutation score 100.00% passed for demo.biz "
        "(25/25 detected; 0 survived; 4 no coverage excluded)\n"
    )

    assert detail["mutation"] == {
        "rate": 100.0,
        "formattedRate": "100.00%",
        "killed": 25,
        "generated": 25,
        "modules": 1,
        "source": "diff",
    }


def test_failed_java_report_shows_diff_mutation_score():
    detail = evidence_detail(
        {
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "stdout": (
                ">> Generated 41 mutations Killed 31 (76%)\n"
                "[ERROR] test-enforcer check-mutation failed: diff mutation score 90.00% "
                "for flagpayment (9/10 detected; 1 survived; 0 no coverage excluded) "
                "is below required 100.00%\n"
            ),
            "stderr": "",
            "evidence": {
                "coverage": {"covered": 10, "total": 10, "rate": 100.0},
            },
        }
    )

    assert detail["mutation"] == {
        "rate": 90.0,
        "formattedRate": "90.00%",
        "killed": 9,
        "generated": 10,
        "modules": 1,
        "source": "diff",
    }


def test_java_report_aggregates_passed_and_failed_diff_mutation_modules():
    detail = output_evidence_detail(
        "[test-enforcer] diff mutation score 100.00% passed for demo.common "
        "(4/4 detected; 0 survived; 0 no coverage excluded)\n"
        "[ERROR] test-enforcer check-mutation failed: diff mutation score 80.00% "
        "for demo.biz (8/10 detected; 2 survived; 0 no coverage excluded) "
        "is below required 100.00%\n"
    )

    assert detail["mutation"]["formattedRate"] == "85.71%"
    assert detail["mutation"]["killed"] == 12
    assert detail["mutation"]["generated"] == 14
    assert detail["mutation"]["modules"] == 2


def test_report_does_not_show_unscoped_raw_pit_metrics_as_java_ci_gate_result():
    detail = evidence_detail(
        {
            "status": "passed",
            "passed": True,
            "stdout": (
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.service (1/1)\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (15/15)\n"
                ">> Generated 51 mutations Killed 0 (0%)\n"
                ">> Mutations with no coverage 51. Test strength 100%\n"
            ),
            "stderr": "",
        }
    )

    assert detail["coverage"]["formattedRate"] == "100.00%"
    assert detail["coverage"]["covered"] == 16
    assert detail["coverage"]["total"] == 16
    assert detail["mutation"] is None
    assert detail["pitMutation"]["source"] == "pit"
    assert detail["pitMutation"]["formattedRate"] == "0.00%"
    assert detail["pitMutation"]["killed"] == 0
    assert detail["pitMutation"]["generated"] == 0
    assert detail["pitMutation"]["rawGenerated"] == 51
    assert detail["pitMutation"]["noCoverage"] == 51
    assert detail["pitMutation"]["formattedTestStrength"] == "100.00%"


def test_report_keeps_scoped_pit_metrics_diagnostic_without_diff_marker():
    record = CiTaskRecord(
        task_id="pit-diagnostic-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
        summary="UTA test-enforcement failed before diff mutation evidence was produced",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "returncode": 1,
            "stdout": (
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=4 [com.demo.ChangedService*]\n"
                "[info] [test-enforcer] diff line coverage 98.44% passed for demo.biz (63/64)\n"
                ">> Generated 70 mutations Killed 64 (91%)\n"
                ">> Mutations with no coverage 2. Test strength 94%\n"
            ),
            "stderr": "",
        },
    )

    html = CiReportRenderer().report_html(record)

    assert "变异得分: <strong>94.12%</strong>" not in html
    assert "PIT 全量变异统计" in html


def test_report_marks_mutation_blocked_when_pit_baseline_tests_are_not_green():
    record = CiTaskRecord(
        task_id="pit-baseline-failed",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
        summary="UTA test-enforcement failed because PIT baseline tests were not green",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "returncode": 1,
            "stdout": (
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=4 [com.demo.ChangedService*]\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.common (124/124)\n"
                ">> Generated 36 mutations Killed 36 (100%)\n"
                ">> Mutations with no coverage 0. Test strength 100%\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.biz (76/76)\n"
                "[ERROR] Tests run: 30, Failures: 1, Errors: 0, Skipped: 0\n"
                "[ERROR]   EleMeSalePromotionChangeBizTest.onShopMappingChangeShouldHandleEmptyMapping:631 "
                "expected [null] but found [2099047791]\n"
                "[ERROR] Failed to execute goal org.pitest:pitest-maven:1.15.0:mutationCoverage "
                "(pitest) on project demo.biz: Execution pitest of goal "
                "org.pitest:pitest-maven:1.15.0:mutationCoverage failed: "
                "1 tests did not pass without mutation when calculating line coverage. "
                "Mutation testing requires a green suite.\n"
            ),
            "stderr": "",
            "evidence": {
                "coverage": {"covered": 200, "total": 200, "rate": 100.0, "modules": 4},
                "mutation": {"killed": 36, "generated": 36, "rate": 100.0, "modules": 2},
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["evidence"]["coverage"]["formattedRate"] == "100.00%"
    assert detail["evidence"]["mutation"] is None
    assert detail["evidence"]["mutationBlocked"]["type"] == "pit_baseline_not_green"
    assert "覆盖率: <strong>100.00%</strong>" in html
    assert "变异得分: <strong>N/A</strong>" in html
    assert "变异得分: <strong>100.00%</strong>" not in html
    assert "PIT baseline tests failed before mutation scoring completed" in html
    assert "EleMeSalePromotionChangeBizTest.onShopMappingChangeShouldHandleEmptyMapping" in html


def test_pit_baseline_diagnostic_ignores_expected_exception_stack_frames():
    from uta.language.java.ci_evidence import pit_baseline_failure_detail

    output = """
[INFO] Running com.example.AfterCommitExecutorTest
java.lang.RuntimeException: ignored
    at com.example.AfterCommitExecutorTest.lambda$shouldSwallowCallbackException$0(AfterCommitExecutorTest.java:53)
[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0
[ERROR] com.example.ImsSkuRemoteAdapterLocalTest.oldRuleShouldUseCurrentDate -- Time elapsed: 0.022 s <<< FAILURE!
java.lang.AssertionError: expected same:<supported> was not:<null>
    at com.example.ImsSkuRemoteAdapterLocalTest.oldRuleShouldUseCurrentDate(ImsSkuRemoteAdapterLocalTest.java:105)
[ERROR]   ImsSkuRemoteAdapterLocalTest.oldRuleShouldUseCurrentDate:105 expected same:<supported> was not:<null>
[ERROR] com.example.ImsSkuRemoteAdapterLocalTest.oldRuleShouldUseCurrentDate(com.example.ImsSkuRemoteAdapterLocalTest)
[ERROR] Failed to execute goal org.pitest:pitest-maven:mutationCoverage on project inventory-service:
1 tests did not pass without mutation when calculating line coverage. Mutation testing requires a green suite.
"""

    detail = pit_baseline_failure_detail(output)

    assert detail is not None
    assert detail["failingTests"] == [
        "com.example.ImsSkuRemoteAdapterLocalTest.oldRuleShouldUseCurrentDate"
    ]
    assert "AfterCommitExecutorTest" not in detail["errorExcerpt"]


def test_report_scoped_pit_aggregate_excludes_no_coverage_mutants():
    detail = output_evidence_detail(
        "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
        "pitest.targets=2 [com.demo.FaceService*,com.demo.PunchBizImpl*]\n"
        "[info] [test-enforcer] diff line coverage 100.00% passed for demo.service (6/6)\n"
        "[info] [test-enforcer] diff line coverage 95.83% passed for demo.biz (46/48)\n"
        ">> Generated 15 mutations Killed 2 (13%)\n"
        ">> Mutations with no coverage 13. Test strength 100%\n"
        ">> Generated 25 mutations Killed 25 (100%)\n"
        ">> Mutations with no coverage 0. Test strength 100%\n"
    )

    assert detail["coverage"]["formattedRate"] == "96.30%"
    assert detail["mutation"] is None
    assert detail["pitMutation"]["source"] == "pit"
    assert detail["pitMutation"]["formattedRate"] == "100.00%"
    assert detail["pitMutation"]["killed"] == 27
    assert detail["pitMutation"]["generated"] == 27
    assert detail["pitMutation"]["rawGenerated"] == 40
    assert detail["pitMutation"]["noCoverage"] == 13
    assert detail["pitMutation"]["modules"] == 2


def test_passed_java_report_merges_stdout_metrics_when_evidence_is_diff_metadata_only():
    record = CiTaskRecord(
        task_id="java-metadata-only-pass",
        status=CiTaskStatus.success,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
        summary="UTA test-enforcement passed",
        enforcement_result={
            "status": "passed",
            "passed": True,
            "language": "java",
            "backend": "maven_enforcer",
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "returncode": 0,
            "stdout": (
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.demo.ChangedService*]\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for demo.service (14/14)\n"
                ">> Generated 10 mutations Killed 10 (100%)\n"
                ">> Mutations with no coverage 0. Test strength 100%\n"
                "[info] [test-enforcer] diff mutation score 100.00% passed for demo.service "
                "(10/10 detected; 0 survived; 0 no coverage excluded)\n"
                "[INFO] BUILD SUCCESS\n"
            ),
            "stderr": "",
            "evidence": {
                "baseRef": "origin/master",
                "changedJavaFiles": [
                    "service/src/main/java/com/demo/ChangedService.java",
                    "service/src/test/java/com/demo/ChangedServiceTest.java",
                ],
                "changedProductionFiles": ["service/src/main/java/com/demo/ChangedService.java"],
                "changedClasses": ["com.demo.ChangedService"],
                "targetTests": ["com.demo.ChangedServiceTest"],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["evidence"]["coverage"]["formattedRate"] == "100.00%"
    assert detail["evidence"]["coverage"]["covered"] == 14
    assert detail["evidence"]["coverage"]["total"] == 14
    assert detail["evidence"]["mutation"]["formattedRate"] == "100.00%"
    assert detail["evidence"]["mutation"]["source"] == "diff"
    assert "覆盖率: <strong>100.00%</strong>" in html
    assert "变异得分: <strong>100.00%</strong>" in html
    assert "覆盖率: <strong>N/A</strong>" not in html
    assert "变异得分: <strong>N/A</strong>" not in html


def test_report_evidence_uses_python_structured_evidence():
    detail = evidence_detail(
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
    assert detail["coverage"]["covered"] == 21
    assert detail["coverage"]["total"] == 23
    assert detail["mutation"]["source"] == "python"
    assert detail["mutation"]["formattedRate"] == "78.95%"
    assert detail["mutation"]["killed"] == 15
    assert detail["mutation"]["generated"] == 19


def test_report_shows_test_quality_warnings():
    record = CiTaskRecord(
        task_id="quality-warnings-1",
        status=CiTaskStatus.success,
        request=CiTriggerRequest(
            app_name="py-demo",
            git_url="git@git.example.com:group/py-demo.git",
            branch="feature/TASK-1",
            language="python",
        ),
        summary="Python enforcement passed",
        enforcement_result={
            "status": "passed",
            "passed": True,
            "language": "python",
            "backend": "python_enforcer",
            "evidence": {
                "coverage": {"covered": 2, "total": 2, "rate": 100.0, "passed": True},
                "mutation": {"generated": 1, "killed": 1, "survived": 0, "rate": 100.0, "passed": True},
                "testQuality": {
                    "warningCount": 1,
                    "topRuleIds": [{"ruleId": "python-weak-assert-not-none", "count": 1}],
                    "warnings": [
                        {
                            "language": "python",
                            "filePath": "tests/uta_generated/test_demo.py",
                            "ruleId": "python-weak-assert-not-none",
                            "category": "weak_assertion",
                            "severity": "warning",
                            "message": "Primary assertions only check existence.",
                            "line": 7,
                            "evidence": "assert result is not None",
                        }
                    ],
                },
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["evidence"]["testQuality"]["warningCount"] == 1
    assert detail["evidence"]["testQuality"]["topRuleIds"][0]["ruleId"] == "python-weak-assert-not-none"
    assert "Test quality signals" in html
    assert "python-weak-assert-not-none" in html
    assert "assert result is not None" in html


def test_java_report_shows_missing_pom_as_warning_without_overriding_gate_failure():
    warning = (
        "[WARNING] The POM for com.example:legacy:jar:1 is missing, "
        "no dependency information available"
    )
    record = CiTaskRecord(
        task_id="java-pom-warning-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="java-demo",
            git_url="git@git.example.com:group/java-demo.git",
            branch="feature/TASK-1",
            language="java",
        ),
        summary="Coverage gate failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "stdout": warning,
            "evidence": {
                "coverage": {"covered": 8, "total": 10, "rate": 80.0, "passed": False},
                "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["enforcement"]["status"] == "failed"
    assert detail["evidence"]["dependencyWarnings"] == [warning]
    assert "依赖 POM 缺失警告" in html
    assert "com.example:legacy:jar:1" in html


def test_python_report_shows_aggregate_and_target_coverage_failures():
    record = CiTaskRecord(
        task_id="python-coverage-failed-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-demo",
            git_url="git@git.example.com:group/py-demo.git",
            branch="feature/TASK-1",
            language="python",
        ),
        summary="Python enforcement failed: coverage_gate_failed: Python coverage 16.45% is below gate 95.00%",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["uta", "python-enforce"],
            "summary": "Python enforcement failed: coverage_gate_failed: Python coverage 16.45% is below gate 95.00%",
            "evidence": {
                "coverage": {
                    "covered": 177,
                    "total": 433,
                    "rate": 40.8776,
                    "gate": 95.0,
                    "passed": False,
                    "scope": "changed_lines",
                },
                "mutation": {
                    "generated": 261,
                    "killed": 261,
                    "survived": 0,
                    "rate": 100.0,
                    "gate": 100.0,
                    "passed": True,
                    "scope": "changed_lines",
                },
                "targetResults": [
                    {
                        "target": {"source_path": "chat_robot/apps.py", "display_name": "chat_robot/apps.py"},
                        "status": "passed",
                        "coverage": {"covered": 19, "total": 20, "rate": 95.0, "gate": 95.0, "passed": True, "scope": "changed_lines"},
                        "mutation": {"generated": 57, "killed": 57, "rate": 100.0, "passed": True},
                    },
                    {
                        "target": {
                            "source_path": "chat_robot/service/fine_tuning_service.py",
                            "display_name": "chat_robot/service/fine_tuning_service.py",
                        },
                        "status": "failed",
                        "reasonCode": "coverage_gate_failed",
                        "message": "Python coverage 16.45% is below gate 95.00%",
                        "coverage": {"covered": 50, "total": 304, "rate": 16.4474, "gate": 95.0, "passed": False, "scope": "changed_lines"},
                    },
                ],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["evidence"]["coverage"]["formattedRate"] == "40.88%"
    assert detail["evidence"]["coverage"]["modules"] == 2
    assert detail["evidence"]["coverageFailures"][0]["target"] == "chat_robot/service/fine_tuning_service.py"
    assert detail["evidence"]["coverageFailures"][0]["formattedRate"] == "16.45%"
    assert detail["evidence"]["mutation"]["modules"] == 1
    assert detail["evidence"]["mutationSkipped"]["count"] == 1
    assert detail["evidence"]["mutationSkipped"]["targets"][0]["target"] == "chat_robot/service/fine_tuning_service.py"
    assert "Diff 覆盖率（聚合）" in html
    assert "40.88%" in html
    assert "2 个目标" in html
    assert "Diff 变异得分" in html
    assert "1 个已执行目标；1 个目标因覆盖率未通过未执行变异" in html
    assert "未通过覆盖率门禁的目标" in html
    assert "chat_robot/service/fine_tuning_service.py" in html
    assert "16.45%" in html
    assert "Python test-enforcement 标准" in html
    assert "UTA_PYTHON_ENFORCE_SCRIPT" in html
    assert "mutmut 变异测试" in html
    assert "batch/operator 过滤证据" in html
    assert "Python enforcement 命令" in html
    assert "Maven 命令复现" not in html
    assert "PIT 变异测试通过" not in html


def test_python_report_shows_target_mutation_failures():
    record = CiTaskRecord(
        task_id="python-mutation-failed-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-demo",
            git_url="git@git.example.com:group/py-demo.git",
            branch="feature/TASK-1",
            language="python",
        ),
        summary="Python enforcement failed: mutation_gate_failed: Python mutation score 0.00% is below gate 100.00%",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["uta", "python-enforce"],
            "summary": "Python enforcement failed: mutation_gate_failed: Python mutation score 0.00% is below gate 100.00%",
            "evidence": {
                "coverage": {
                    "covered": 419,
                    "total": 433,
                    "rate": 96.7667,
                    "gate": 95.0,
                    "passed": True,
                    "scope": "changed_lines",
                    "modules": 4,
                },
                "mutation": {
                    "generated": 1067,
                    "killed": 261,
                    "survived": 0,
                    "changedLineMutantsGenerated": 261,
                    "changedLineMutantsKilled": 261,
                    "rate": 100.0,
                    "gate": 100.0,
                    "passed": True,
                    "scope": "changed_lines",
                    "modules": 4,
                },
                "targetResults": [
                    {
                        "target": {"source_path": "chat_robot/apps.py", "display_name": "chat_robot/apps.py"},
                        "status": "passed",
                        "coverage": {"covered": 19, "total": 20, "rate": 95.0, "gate": 95.0, "passed": True, "scope": "changed_lines"},
                        "mutation": {
                            "generated": 57,
                            "killed": 57,
                            "survived": 0,
                            "changedLineMutantsGenerated": 57,
                            "changedLineMutantsKilled": 57,
                            "rate": 100.0,
                            "gate": 100.0,
                            "passed": True,
                            "scope": "changed_lines",
                        },
                    },
                    {
                        "target": {
                            "source_path": "chat_robot/service/fine_tuning_service.py",
                            "display_name": "chat_robot/service/fine_tuning_service.py",
                        },
                        "status": "failed",
                        "reasonCode": "mutation_gate_failed",
                        "message": "Python mutation score 0.00% is below gate 100.00%",
                        "coverage": {"covered": 292, "total": 304, "rate": 96.0526, "gate": 95.0, "passed": True, "scope": "changed_lines"},
                        "mutation": {
                            "generated": 806,
                            "killed": 0,
                            "survived": 0,
                            "changedLineMutantsGenerated": 0,
                            "changedLineMutantsKilled": 0,
                            "rate": 0.0,
                            "gate": 100.0,
                            "passed": False,
                            "scope": "changed_lines",
                        },
                    },
                ],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["evidence"]["mutation"]["formattedRate"] == "100.00%"
    assert detail["evidence"]["mutation"]["killed"] == 261
    assert detail["evidence"]["mutation"]["generated"] == 261
    assert detail["evidence"]["mutation"]["passed"] is False
    assert detail["evidence"]["mutation"]["formattedWorstTargetRate"] == "0.00%"
    assert detail["evidence"]["mutationFailures"]["count"] == 1
    assert detail["evidence"]["mutationFailures"]["targets"][0]["target"] == "chat_robot/service/fine_tuning_service.py"
    assert "Diff 变异得分" in html
    assert "Diff 变异得分（聚合）" in html
    assert "100.00%" in html
    assert "0.00%" in html
    assert "261/261 计分变异；1 个目标未通过变异门禁" in html
    assert "未通过变异门禁的目标" in html
    assert "chat_robot/service/fine_tuning_service.py" in html
    assert "mutmut 变异测试" in html
    assert "UTA_PYTHON_ENFORCE_SCRIPT" in html
    assert "Maven 命令复现" not in html


def test_python_report_does_not_mix_worst_rate_with_aggregate_mutation_counts():
    record = CiTaskRecord(
        task_id="python-mutation-mixed-rate-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-demo",
            git_url="git@git.example.com:group/py-demo.git",
            branch="feature/TASK-1",
            language="python",
        ),
        summary="Python enforcement failed: mutation_gate_failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["uta", "python-enforce"],
            "summary": "Python enforcement failed: mutation_gate_failed",
            "evidence": {
                "coverage": {
                    "covered": 1585,
                    "total": 1617,
                    "rate": 98.021,
                    "gate": 95.0,
                    "passed": True,
                    "scope": "changed_lines",
                    "modules": 15,
                },
                "mutation": {
                    "generated": 8501,
                    "killed": 8305,
                    "survived": 0,
                    "noTests": 187,
                    "changedLineMutantsGenerated": 8492,
                    "changedLineMutantsKilled": 8305,
                    # Older stored evidence used the failed target rate here.
                    "rate": 42.9878,
                    "gate": 100.0,
                    "passed": False,
                    "scope": "changed_lines",
                    "modules": 15,
                    "sampled": True,
                    "sampledTargets": 1,
                    "sampling": {
                        "strategy": "deterministic_changed_line_hash",
                        "totalChangedLines": 1032,
                        "sampledChangedLines": 200,
                    },
                },
                "targetResults": [
                    {
                        "target": {"source_path": "src/shidiao/runners/mac_wechat_runner.py"},
                        "status": "failed",
                        "reasonCode": "mutation_gate_failed",
                        "message": "Python mutation score 42.99% is below gate 100.00%",
                        "coverage": {"covered": 9, "total": 9, "rate": 100.0, "passed": True, "scope": "changed_lines"},
                        "mutation": {
                            "generated": 328,
                            "killed": 141,
                            "survived": 0,
                            "no_tests": 187,
                            "changedLineMutantsGenerated": 328,
                            "changedLineMutantsKilled": 141,
                            "rate": 42.9878,
                            "gate": 100.0,
                            "passed": False,
                            "scope": "changed_lines",
                            "sampling": {
                                "enabled": True,
                                "strategy": "deterministic_changed_line_hash",
                                "sourcePath": "src/shidiao/runners/mac_wechat_runner.py",
                                "totalChangedLines": 1032,
                                "sampledChangedLines": 200,
                                "sampledLines": [1, 2],
                            },
                        },
                    }
                ],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["evidence"]["mutation"]["formattedRate"] == "100.00%"
    assert detail["evidence"]["mutation"]["killed"] == 8305
    assert detail["evidence"]["mutation"]["generated"] == 8492
    assert detail["evidence"]["mutation"]["scored"] == 8305
    assert detail["evidence"]["mutation"]["formattedWorstTargetRate"] == "100.00%"
    assert detail["evidence"]["mutation"]["sampled"] is True
    assert detail["evidence"]["mutation"]["sampling"]["sampledChangedLines"] == 200
    assert "Diff 变异得分（聚合）" in html
    assert "100.00%" in html
    assert "8305/8305 计分变异，187 个未匹配测试已排除；采样 200/1032 行；1 个目标未通过变异门禁" in html
    assert "src/shidiao/runners/mac_wechat_runner.py" in html
    assert "42.99%" in html
    assert "141/141 计分变异，187 个未匹配测试已排除，采样 200/1032 行" in html


def test_python_report_summarizes_candidate_plan_funnel():
    record = CiTaskRecord(
        task_id="python-candidate-plan-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-demo",
            git_url="git@git.example.com:group/py-demo.git",
            branch="feature/TASK-1",
            language="python",
        ),
        summary="Python enforcement failed: mutation_gate_failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["uta", "python-enforce"],
            "summary": "Python enforcement failed: mutation_gate_failed",
            "evidence": {
                "mutationBudget": {
                    "scope": "report",
                    "configuredCandidates": 300,
                    "allocatedCandidates": 1,
                    "selectedCandidates": 1,
                    "runMutants": 1,
                },
                "coverage": {"covered": 1, "total": 1, "rate": 100.0, "passed": True, "scope": "changed_lines"},
                "mutation": {
                    "generated": 2,
                    "killed": 1,
                    "survived": 1,
                    "changedLineMutantsGenerated": 2,
                    "changedLineMutantsKilled": 1,
                    "changedLineMutantsScored": 2,
                    "rate": 50.0,
                    "gate": 95.0,
                    "passed": False,
                    "scope": "changed_lines",
                    "candidatePlan": {
                        "filterMechanism": "mutmut3_metadata_selected_execution",
                        "eligibleMutationOpportunities": [{"opportunityId": "opp-1"}, {"opportunityId": "opp-2"}],
                        "suppressed": [{"reasonCode": "low_value_logging"}],
                        "omittedByOnePerLine": [{"opportunityId": "opp-3"}],
                        "reportFullSelected": [{"candidateId": "c1"}, {"candidateId": "c2"}],
                        "activeSelected": [{"candidateId": "c1"}],
                        "exactToolCandidateKeys": ["pkg.worker.x_run__mutmut_1", "pkg.worker.x_run__mutmut_2"],
                        "changedLines": [2, 3],
                        "coveredChangedLines": [2],
                        "runMutants": 1,
                        "scoredMutants": 1,
                        "killed": 1,
                        "survived": 0,
                        "noTests": 0,
                        "timeout": 0,
                        "suspicious": 0,
                        "comparable": False,
                        "changedFingerprints": ["headCommit"],
                        "samplingLayer": {"enabled": True, "threshold": 1, "limit": 1},
                    },
                },
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    candidate_plan = detail["evidence"]["mutation"]["candidatePlan"]
    assert candidate_plan["filterMechanism"] == "mutmut3_metadata_selected_execution"
    assert candidate_plan["eligibleOpportunities"] == 2
    assert candidate_plan["suppressedOpportunities"] == 1
    assert candidate_plan["omittedByOnePerLine"] == 1
    assert candidate_plan["reportFullCandidates"] == 2
    assert candidate_plan["activeCandidates"] == 1
    assert candidate_plan["exactToolCandidateKeys"] == 2
    assert candidate_plan["changedLines"] == 2
    assert candidate_plan["coveredChangedLines"] == 1
    assert candidate_plan["runMutants"] == 1
    assert candidate_plan["scoredMutants"] == 1
    assert candidate_plan["sampled"] is True
    assert "Python 变异候选计划" in html
    assert "候选漏斗" in html
    assert "候选计划不可与原报告直接比较" in html
    assert "headCommit" in html
    assert "mutmut3_metadata_selected_execution" in html
    assert "候选 2 个，实际执行 1 个" in html
    assert "CI 上限裁剪已启用" in html
    assert "CI 报告级变异预算" in html
    assert "已分配 1/300 个候选" in html
    assert "该上限由全部 Python 目标共享" in html
    assert "CI 采样已启用" not in html


def test_report_missing_target_tests_shows_zero_metrics_without_upgrade_requirements():
    record = CiTaskRecord(
        task_id="missing-evidence-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="inventory_store",
            git_url="git@git.example.com:inf/inventory-store.git",
            branch="TASK-11324",
        ),
        summary=(
            "UTA test-enforcement cannot run PIT safely because no related "
            "targetTests were found for changed production Java files"
        ),
        enforcement_result={
            "status": "missing_evidence",
            "passed": False,
            "command": ["mvn", "-U", "verify"],
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "summary": (
                "UTA test-enforcement cannot run PIT safely because no related "
                "targetTests were found for changed production Java files"
            ),
            "evidence": {
                "coverage": {"covered": 0, "total": 0, "rate": 0.0, "passed": False},
                "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
                "tooling": {"available": True, "artifactId": "example-root", "version": "1.3.97"},
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["canCreateFixSession"] is True
    assert detail["evidence"]["coverage"]["formattedRate"] == "0.00%"
    assert detail["evidence"]["mutation"]["formattedRate"] == "0.00%"
    assert "test-enforcer &gt;= 1.0.16" not in html
    assert "覆盖率: <strong>0.00%</strong>" in html
    assert "变异得分: <strong>0.00%</strong>" in html


def test_report_surfaces_missing_jacoco_xml_and_target_mismatch():
    record = CiTaskRecord(
        task_id="missing-jacoco-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="cvs_product_user",
            git_url="git@git.example.com:cvs/cvs-product-user.git",
            branch="TASK-11327",
        ),
        summary="UTA test-enforcement failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "returncode": 1,
            "stdout": (
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.example.cvs.product.user.biz.TT*]\n"
                "[ERROR] Failed to execute goal com.example.build.maven-plugins:test-enforcer:1.0.11:check-coverage "
                "(check-coverage) on project cvs-product-user.biz: Execution check-coverage of goal failed: "
                "test-enforcer check-coverage failed: JaCoCo XML not found at "
                "/opt/app/uta-ci-data/workspaces/61/cvs-product-user/biz/target/site/jacoco/jacoco.xml; "
                "run jacoco:report before check-coverage or disable the coverage gate -> [Help 1]\n"
            ),
            "stderr": "",
            "evidence": {
                "changedClasses": [
                    "com.example.cvs.product.user.biz.TT",
                    "com.example.cvs.product.user.provider.http.SearchController",
                ],
                "targetTests": ["com.example.cvs.product.user.provider.http.SearchControllerTest"],
                "filteredTargetClasses": ["com.example.cvs.product.user.biz.TT"],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    diagnostic = detail["failureDiagnostics"][0]
    assert diagnostic["type"] == "missing_jacoco_xml"
    assert diagnostic["module"] == "biz"
    assert diagnostic["project"] == "cvs-product-user.biz"
    assert diagnostic["filteredTargetClasses"] == ["com.example.cvs.product.user.biz.TT"]
    assert diagnostic["targetTests"] == ["com.example.cvs.product.user.provider.http.SearchControllerTest"]
    assert diagnostic["testsSkipped"] is False
    assert diagnostic["targetMismatch"] is True
    assert "失败原因" in html
    assert "JaCoCo coverage report missing" in html
    assert "Maven 过滤后的目标类" in html
    assert "com.example.cvs.product.user.biz.TT" in html
    assert "本次选择的测试" in html
    assert "SearchControllerTest" in html
    assert "目标类与测试选择不匹配" in html


def test_report_surfaces_cross_module_selected_test_hint_for_missing_jacoco():
    record = CiTaskRecord(
        task_id="missing-jacoco-cross-module-test",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="platform_sample_core_provider",
            git_url="git@git.example.com:demo/sample-order-core.git",
            branch="20260703-TASK-11330",
        ),
        summary="UTA test-enforcement failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": [
                "mvn",
                "-Dtest.enforcement.enabled=true",
                "verify",
                "-DtargetTests=com.example.platform.sample.core.test.service.LogisticsRiskProcessRecordServiceImplTest",
                "-Dtest=LogisticsRiskProcessRecordServiceImplTest",
                "-pl",
                "sample-platform-core.common,sample-platform-core.service",
                "-am",
            ],
            "returncode": 1,
            "stdout": (
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.example.platform.sample.core.service.logistics.risk.impl.LogisticsRiskProcessRecordServiceImpl*]\n"
                "[ERROR] Failed to execute goal com.example.build.maven-plugins:test-enforcer:1.0.13:check-coverage "
                "(check-coverage) on project sample-platform-core.common: Execution check-coverage of goal failed: "
                "test-enforcer check-coverage failed: JaCoCo XML not found at "
                "/opt/app/uta-ci-data/workspaces/e37/sample-order-core/sample-platform-core.common/target/site/jacoco/jacoco.xml; "
                "run jacoco:report before check-coverage or disable the coverage gate -> [Help 1]\n"
            ),
            "stderr": "",
            "evidence": {
                "changedModules": ["sample-platform-core.common", "sample-platform-core.service"],
                "changedJavaFiles": [
                    "sample-platform-core.common/src/main/java/com/example/platform/sample/core/common/constant/LogisticsRiskConstant.java",
                    "sample-platform-core.service/src/main/java/com/example/platform/sample/core/service/logistics/risk/impl/LogisticsRiskProcessRecordServiceImpl.java",
                    "sample-platform-core.provider/src/test/java/com/example/platform/sample/core/test/service/LogisticsRiskProcessRecordServiceImplTest.java",
                ],
                "changedClasses": [
                    "com.example.platform.sample.core.service.logistics.risk.impl.LogisticsRiskProcessRecordServiceImpl",
                ],
                "targetTests": [
                    "com.example.platform.sample.core.test.service.LogisticsRiskProcessRecordServiceImplTest",
                ],
                "filteredTargetClasses": [
                    "com.example.platform.sample.core.service.logistics.risk.impl.LogisticsRiskProcessRecordServiceImpl",
                ],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    diagnostic = detail["failureDiagnostics"][0]
    assert diagnostic["title"] == "Selected test is outside Maven-scoped target modules"
    assert diagnostic["targetMismatch"] is False
    assert diagnostic["targetTestModuleMismatch"] is True
    assert diagnostic["scopedModules"] == ["sample-platform-core.common", "sample-platform-core.service"]
    assert diagnostic["targetTestModules"] == ["sample-platform-core.provider"]
    assert "same Maven module as the filtered target class" in diagnostic["hint"]
    assert "所选测试在 Maven 本次验证范围之外" in html
    assert "Maven 本次验证模块" in html
    assert "sample-platform-core.service" in html
    assert "所选测试所在模块" in html
    assert "sample-platform-core.provider" in html


def test_report_surfaces_skipped_tests_before_missing_jacoco_xml():
    record = CiTaskRecord(
        task_id="missing-jacoco-skipped-tests",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="opc_roster_provider",
            git_url="git@git.example.com:IDSS/ipes-roster.git",
            branch="20260604-TASK-40970",
        ),
        summary="UTA test-enforcement failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": [
                "mvn",
                "-Dtest.enforcement.enabled=true",
                "verify",
                "-DtargetTests=com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBizTest",
                "-Dtest=GiveWorkflowBizTest",
            ],
            "returncode": 1,
            "stdout": (
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBiz*]\n"
                "[INFO] --- compiler:3.6.0:testCompile (default-testCompile) @ roster-domain ---\n"
                "[INFO] Compiling 1 source file to target/test-classes\n"
                "[INFO] --- surefire:2.12.4:test (default-test) @ roster-domain ---\n"
                "[INFO] Tests are skipped.\n"
                "[INFO] --- jacoco:0.8.11:report (rep) @ roster-domain ---\n"
                "[INFO] Skipping JaCoCo execution due to missing execution data file.\n"
                "[ERROR] Failed to execute goal com.example.build.maven-plugins:test-enforcer:1.0.12:check-coverage "
                "(check-coverage) on project roster-domain: Execution check-coverage of goal failed: "
                "test-enforcer check-coverage failed: JaCoCo XML not found at "
                "/opt/app/uta-ci-data/workspaces/61/ipes-roster/roster-domain/target/site/jacoco/jacoco.xml; "
                "run jacoco:report before check-coverage or disable the coverage gate -> [Help 1]\n"
            ),
            "stderr": "",
            "evidence": {
                "changedClasses": [
                    "com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBiz",
                ],
                "targetTests": [
                    "com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBizTest",
                ],
                "filteredTargetClasses": [
                    "com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBiz",
                ],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    diagnostic = detail["failureDiagnostics"][0]
    assert diagnostic["title"] == "JaCoCo coverage report missing because tests were skipped"
    assert diagnostic["module"] == "roster-domain"
    assert diagnostic["testsSkipped"] is True
    assert diagnostic["targetMismatch"] is False
    assert "Maven/Surefire skipped test execution" in diagnostic["message"]
    assert "Maven/Surefire 跳过了测试执行" in html
    assert "目标类与测试选择不匹配" not in html
    assert "Check module/profile-level Maven test skip settings" in html


def test_report_surfaces_jacoco_argline_agent_hint_when_tests_ran_without_exec_data():
    record = CiTaskRecord(
        task_id="missing-jacoco-argline",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="opc_roster_provider",
            git_url="git@git.example.com:IDSS/ipes-roster.git",
            branch="20260604-TASK-40970",
        ),
        summary="UTA test-enforcement failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": [
                "mvn",
                "-Dtest.enforcement.enabled=true",
                "verify",
                "-DtargetTests=com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBizTest",
                "-Dtest=GiveWorkflowBizTest",
            ],
            "returncode": 1,
            "stdout": (
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=1 [com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBiz*]\n"
                "[INFO] --- surefire:2.12.4:test (default-test) @ roster-domain ---\n"
                "Running com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBizTest\n"
                "Tests run: 2, Failures: 0, Errors: 0, Skipped: 0\n"
                "[INFO] --- jacoco:0.8.11:report (rep) @ roster-domain ---\n"
                "[INFO] Skipping JaCoCo execution due to missing execution data file.\n"
                "[ERROR] Failed to execute goal com.example.build.maven-plugins:test-enforcer:1.0.12:check-coverage "
                "(check-coverage) on project roster-domain: Execution check-coverage of goal failed: "
                "test-enforcer check-coverage failed: JaCoCo XML not found at "
                "/opt/app/uta-ci-data/workspaces/cd/ipes-roster/roster-domain/target/site/jacoco/jacoco.xml; "
                "run jacoco:report before check-coverage or disable the coverage gate -> [Help 1]\n"
            ),
            "stderr": "",
            "evidence": {
                "changedClasses": [
                    "com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBiz",
                ],
                "targetTests": [
                    "com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBizTest",
                ],
                "filteredTargetClasses": [
                    "com.example.idss.ipes.roster.domain.service.give.GiveWorkflowBiz",
                ],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    diagnostic = detail["failureDiagnostics"][0]
    assert diagnostic["title"] == "JaCoCo coverage agent did not run"
    assert diagnostic["module"] == "roster-domain"
    assert diagnostic["testsSkipped"] is False
    assert diagnostic["targetMismatch"] is False
    assert "Surefire argLine" in diagnostic["hint"]
    assert "@{argLine}" in diagnostic["hint"]
    assert "${argLine}" in diagnostic["hint"]
    assert "JaCoCo coverage agent did not run" in html
    assert "Surefire argLine" in html
    assert "@{argLine}" in html


def test_report_surfaces_python_test_import_failure_cleanly():
    pytest_output = (
        "============================= test session starts ==============================\n"
        "tests/uta_generated/test_src_shidiao_routes.py::test_start_task_request_defaults_and_validators ERROR [  2%]\n"
        "____________________________________ ERROR ____________________________________\n"
        "E       ImportError: cannot import name 'Query' from 'fastapi' (unknown location)\n"
    )
    record = CiTaskRecord(
        task_id="python-import-failure",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="mmc_app_react_agent",
            git_url="git@git.example.com:mmc/react_agent.git",
            branch="20260604-TASK-40970",
            language="python",
        ),
        summary=f"Python enforcement failed: test_failed: {pytest_output}",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "python",
            "backend": "python_enforcer",
            "command": ["uta", "python-enforce", "--json-output"],
            "returncode": 1,
            "stdout": pytest_output,
            "stderr": "",
            "evidence": {
                "language": "python",
                "backend": "python_enforcer",
                "status": "failed",
                "passed": False,
                "reasonCode": "test_failed",
                "summary": pytest_output,
                "targetResults": [
                    {
                        "target": {
                            "display_name": "src/shidiao/routes.py",
                            "source_path": "src/shidiao/routes.py",
                        },
                        "status": "failed",
                        "reasonCode": "test_failed",
                        "testsPass": False,
                        "message": pytest_output,
                        "selectedTestPaths": ["tests/uta_generated/test_src_shidiao_routes.py"],
                        "candidateTestPaths": [
                            "tests/uta_generated/test_src_shidiao_routes.py",
                            "tests/unit/test_tool_tree_routes.py",
                        ],
                        "candidateResults": [
                            {
                                "testPaths": ["tests/uta_generated/test_src_shidiao_routes.py"],
                                "status": "failed",
                                "reasonCode": "test_failed",
                                "message": pytest_output,
                            }
                        ],
                    }
                ],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    diagnostic = detail["failureDiagnostics"][0]
    assert diagnostic["type"] == "python_test_failed"
    assert diagnostic["target"] == "src/shidiao/routes.py"
    assert diagnostic["failingTestPaths"] == ["tests/uta_generated/test_src_shidiao_routes.py"]
    assert "cannot import name 'Query' from 'fastapi'" in diagnostic["errorExcerpt"]
    assert "Python enforcement failed: test_failed for src/shidiao/routes.py" in detail["displaySummary"]
    assert "full command output is available below" not in detail["displaySummary"]
    assert "Python selected unit test failed before coverage/mutation" in html
    assert "目标: <code>src/shidiao/routes.py</code>" in html
    assert "错误摘要" in html
    assert "fastapi" in html


def test_report_java_compile_failure_shows_exact_blocking_test():
    compile_output = (
        "[ERROR] COMPILATION ERROR :\n"
        "[ERROR] /workspace/provider/src/test/java/com/demo/ChatRobotServiceTest.java:[56,37] "
        "error: cannot find symbol\n"
        "  symbol:   method buildReason(AuditType,String)\n"
        "  location: variable chatRobotService of type ChatRobotService\n"
    )
    record = CiTaskRecord(
        task_id="java-compile-blocker-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="java-app",
            git_url="git@git.example.com:group/java-app.git",
            branch="feature/TASK-40996",
        ),
        summary="UTA test-enforcement failed because Maven build did not compile or resolve",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "stdout": compile_output,
            "stderr": "",
            "summary": "UTA test-enforcement failed because Maven build did not compile or resolve",
            "evidence": {},
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    diagnostic = detail["failureDiagnostics"][0]
    assert diagnostic["type"] == "java_test_compile_failed"
    assert diagnostic["failingTestPaths"] == [
        "provider/src/test/java/com/demo/ChatRobotServiceTest.java"
    ]
    assert "buildReason(AuditType,String)" in diagnostic["errorExcerpt"]
    assert "ChatRobotServiceTest.java" in html
    assert "buildReason(AuditType,String)" in html


def test_report_missing_plugin_shows_upgrade_requirements_and_zero_metrics():
    record = CiTaskRecord(
        task_id="missing-plugin-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="shelf-hermes",
            git_url="git@git.example.com:opc/shelf-hermes.git",
            branch="TASK-40967",
        ),
        summary="Missing UTA test-enforcement plugin/profile",
        enforcement_result={
            "status": "missing_evidence",
            "passed": False,
            "command": ["mvn", "-U", "verify"],
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "summary": "Missing UTA test-enforcement plugin/profile",
            "evidence": {
                "coverage": {"covered": 0, "total": 0, "rate": 0.0, "passed": False},
                "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
                "tooling": {"available": False, "artifactId": "example-root", "version": "1.3.19"},
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["canCreateFixSession"] is False
    assert detail["evidence"]["coverage"]["formattedRate"] == "0.00%"
    assert "Maven effective-pom" in html
    assert "test-enforcer &gt;= 1.0.16" in html
    assert "如果项目继承 example-root" in html
    assert "&lt;artifactId&gt;example-root&lt;/artifactId&gt;" in html
    assert "如果项目直接继承 example-parent-generic" in html
    assert "&lt;artifactId&gt;example-parent-generic&lt;/artifactId&gt;" in html
    assert "无法升级父 POM 时的兜底方案" in html
    assert "jacoco:prepare-agent" in html
    assert "pitest:mutationCoverage" in html
    assert "check-mutation" in html
    assert "原始 PIT Test strength 仅用于诊断" in html
    assert "&lt;artifactId&gt;test-enforcer&lt;/artifactId&gt;" in html
    assert "../../docs/test-enforce-usage.md" in html
    assert "example-root 1.3.19" in html
    assert "一键修复" not in html


def test_report_build_failure_shows_parent_version_mismatch_requirements():
    record = CiTaskRecord(
        task_id="dependency-resolution-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="tempoon_cigarette_man",
            git_url="git@git.example.com:tempoon/cigarette.git",
            branch="20260622-TASK-40974",
        ),
        summary="UTA test-enforcement failed because Maven build did not compile or resolve",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "returncode": 1,
            "stdout": (
                "[ERROR] Failed to read artifact descriptor for "
                "com.example.inf:wmq-api:jar:2.1.5"
            ),
            "stderr": "",
            "summary": "UTA test-enforcement failed because Maven build did not compile or resolve",
            "evidence": {
                "tooling": {
                    "available": False,
                    "artifactId": "example-parent-generic",
                    "version": "1.0.8",
                    "requiredVersion": "1.0.22",
                    "reason": (
                        "example-parent-generic 1.0.8 is below the rollout version "
                        "that introduces test-enforcer"
                    ),
                }
            },
        },
    )

    html = CiReportRenderer().report_html(record)

    assert "当前版本不满足 test-enforcement 要求" in html
    assert "example-parent-generic 1.0.8" in html
    assert "要求版本: &gt;= 1.0.22" in html
    assert "test-enforcer &gt;= 1.0.16" in html
    assert "一键修复" not in html


def test_report_build_failure_derives_parent_version_mismatch_from_workspace(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pom.xml").write_text(
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
    record = CiTaskRecord(
        task_id="dependency-resolution-old-record",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="tempoon_cigarette_man",
            git_url="git@git.example.com:tempoon/cigarette.git",
            branch="20260622-TASK-40974",
        ),
        workspace_path=str(workspace),
        summary="UTA test-enforcement failed because Maven build did not compile or resolve",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "returncode": 1,
            "stdout": "[ERROR] Could not collect dependencies",
            "stderr": "",
            "summary": "UTA test-enforcement failed because Maven build did not compile or resolve",
            "evidence": {},
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["canCreateFixSession"] is False
    assert detail["testEnforcementRequirements"]["detected"]["artifactId"] == "example-parent-generic"
    assert detail["testEnforcementRequirements"]["detected"]["version"] == "1.0.8"
    assert detail["testEnforcementRequirements"]["detected"]["requiredVersion"] == "1.0.22"
    assert "当前版本不满足 test-enforcement 要求" in html
    assert "要求版本: &gt;= 1.0.22" in html
    assert "一键修复" not in html


def test_report_generic_gate_failure_backfills_declared_parent_version_mismatch(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "pom.xml").write_text(
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
    record = CiTaskRecord(
        task_id="generic-gate-old-record",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="data_order_strategy_man_web",
            git_url="git@example.test:smart-order/strategy.git",
            branch="feature",
        ),
        workspace_path=str(workspace),
        summary="UTA test-enforcement failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "command": ["mvn", "verify"],
            "returncode": 1,
            "stdout": "JaCoCo XML not found",
            "stderr": "",
            "summary": "UTA test-enforcement failed",
            "evidence": {"targetTests": ["com.demo.FooServiceTest"]},
        },
    )

    detail = CiReportRenderer().detail(record)

    assert detail["canCreateFixSession"] is False
    assert detail["testEnforcementRequirements"]["detected"]["artifactId"] == "example-parent-generic"
    assert detail["testEnforcementRequirements"]["detected"]["version"] == "1.0.20"
    assert detail["testEnforcementRequirements"]["detected"]["requiredVersion"] == "1.0.22"


def test_ci_report_time_filter_formats_gmt8():
    assert format_gmt8(datetime(2026, 5, 18, 9, 48, 7, tzinfo=timezone.utc)) == (
        "2026-05-18 17:48:07 GMT+8"
    )
    assert format_gmt8("2026-05-18T09:48:07+00:00") == "2026-05-18 17:48:07 GMT+8"


def test_health_endpoint_reports_service_and_runner_readiness(tmp_path):
    unconfigured = TestClient(create_app(ApiTriggerService()))

    assert unconfigured.get("/healthcheck.html").status_code == 200

    unconfigured_body = unconfigured.get("/healthz").json()
    unconfigured_ready = unconfigured.get("/readyz")

    assert unconfigured_body["service"]["status"] == "ok"
    assert unconfigured_body["runner"]["ready"] is False
    assert unconfigured_body["runner"]["workspaceManager"] is False
    assert unconfigured_body["runner"]["enforcementRunner"] is False
    assert unconfigured_ready.status_code == 503

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspace"),
        enforcement_runner=MavenEnforcementRunner(command="mvn verify"),
    )
    configured = TestClient(create_app(service))

    configured_body = configured.get("/healthz").json()
    configured_ready = configured.get("/readyz")

    assert configured_body["service"]["status"] == "ok"
    assert configured_body["runner"]["ready"] is True
    assert configured_body["runner"]["workspaceManager"] is True
    assert configured_body["runner"]["enforcementRunner"] is True
    assert configured_ready.status_code == 200


def test_report_detail_and_page_show_missing_context_and_callback_status(tmp_path):
    callback_bodies = []

    def callback_handler(request: httpx.Request) -> httpx.Response:
        callback_bodies.append(request.read().decode("utf-8"))
        return httpx.Response(500, text="rdc down")

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="test-enforcer check-coverage failed: diff line coverage 87.50% is below required 95.00%",
            stderr="",
        )

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
                        retry_times=1,
                    )
                )
            ]
        ),
    )
    client = TestClient(create_app(service))
    trigger = client.post("/api/v1/rdc/trigger", json=_rdc_payload()).json()
    task_id = trigger["data"]["taskId"]

    detail = client.get(f"/reports/{task_id}/detail").json()
    report = client.get(f"/reports/{task_id}/index.html")

    assert callback_bodies
    assert detail["callback"]["succeeded"] is False
    assert detail["callback"]["error"]
    assert "issue_description_unavailable" in detail["context"]["missingReasons"]
    assert "git_commit_messages_unavailable" in detail["context"]["missingReasons"]
    assert report.status_code == 200
    assert "缺失上下文" in report.text
    assert "issue_description_unavailable" in report.text
    assert "RDC callback" in report.text
    assert "fix-cta" in report.text
    assert "自动生成或补强单元测试" in report.text


def test_ci_service_logs_check_callback_and_preemption(tmp_path, caplog):
    def callback_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="test-enforcer check-coverage failed: diff line coverage 87.50% is below required 95.00%",
            stderr="",
        )

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
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
    )

    with caplog.at_level(logging.INFO, logger="uta.app.service"):
        record = service.submit(_request_with_jira(), public_base_url="http://uta")
        batch_id = manager.create_task(repo_path=record.workspace_path, class_fqns=["com.example.Batch"], priority=100)
        manager.mark_running(batch_id, stage="generate", detail="large batch")
        service.create_fix_session(record, CreateFixSessionRequest(target_ids=["class:com.example.Demo"]))

    messages = "\n".join(item.getMessage() for item in caplog.records)
    assert "ci_check_started" in messages
    assert "ci_check_finished" in messages
    assert "ci_callback_finished" in messages
    assert "ci_repair_preempted" in messages


def _request_with_jira():
    payload = _rdc_payload()
    payload["attribute"]["jiraId"] = "TASK-82767"
    return parse_rdc_payload(payload)


def test_report_shows_java_test_quality_from_fix_sessions():
    record = CiTaskRecord(
        task_id="quality-java-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="java-demo",
            git_url="git@git.example.com:group/java-demo.git",
            branch="feature/TASK-2",
            language="java",
        ),
        summary="UTA test-enforcement failed",
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "evidence": {
                "coverage": {"covered": 1, "total": 2, "rate": 50.0, "passed": False},
            },
        },
    )
    record.fix_sessions.append(
        {
            "sessionId": "fix-1",
            "status": "repair_completed",
            "repoTaskId": 7,
            "testQuality": {
                "warningCount": 3,
                "topRuleIds": [
                    {"ruleId": "java-weak-not-null", "count": 2},
                    {"ruleId": "java-happy-path-only-hint", "count": 1},
                ],
            },
        }
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["evidence"]["testQuality"]["warningCount"] == 3
    assert detail["evidence"]["testQuality"]["source"] == "fix_sessions"
    assert detail["evidence"]["testQuality"]["topRuleIds"][0]["ruleId"] == "java-weak-not-null"
    assert "Test quality signals" in html
    assert "java-weak-not-null" in html


def test_repair_session_refresh_aggregates_test_quality_from_task_payload():

    task_payload = {
        "classes": [
            {
                "class_fqn": "com.demo.A",
                "test_quality": {
                    "warningCount": 2,
                    "topRuleIds": [{"ruleId": "java-weak-not-null", "count": 2}],
                },
            },
            {
                "class_fqn": "com.demo.B",
                "test_quality": {
                    "warningCount": 1,
                    "topRuleIds": [{"ruleId": "java-happy-path-only-hint", "count": 1}],
                },
            },
            {"class_fqn": "com.demo.C"},
        ]
    }

    quality = RepairSessions._repair_test_quality_from_task_payload(task_payload)

    assert quality == {
        "warningCount": 3,
        "topRuleIds": [
            {"ruleId": "java-weak-not-null", "count": 2},
            {"ruleId": "java-happy-path-only-hint", "count": 1},
        ],
    }
    assert RepairSessions._repair_test_quality_from_task_payload({"classes": []}) is None


def test_python_report_uses_report_aggregate_mutation_gate_for_ci_sampling():
    """A diagnostic per-target rate must not resurface as a report failure.

    The enforcement lane stops gating on a sampled target's score; if the
    report still took the worst target rate as the verdict, the gate would be
    back -- just one layer further out.
    """
    from uta.enforcement.evidence import evidence_detail

    target_results = [
        {
            "target": {"source_path": "jobs/forecast.py", "display_name": "jobs/forecast.py"},
            "status": "passed",
            "reasonCode": "sampled_mutation_diagnostic",
            "mutation": {
                "generated": 1,
                "killed": 0,
                "survived": 1,
                "changedLineMutantsGenerated": 1,
                "changedLineMutantsKilled": 0,
                "changedLineMutantsScored": 1,
                "rate": 0.0,
                "gate": 40.0,
                "passed": False,
                "scope": "changed_lines",
            },
        },
        {
            "target": {"source_path": "jobs/pricing.py", "display_name": "jobs/pricing.py"},
            "status": "passed",
            "reasonCode": "passed",
            "mutation": {
                "generated": 1,
                "killed": 1,
                "survived": 0,
                "changedLineMutantsGenerated": 1,
                "changedLineMutantsKilled": 1,
                "changedLineMutantsScored": 1,
                "rate": 100.0,
                "gate": 40.0,
                "passed": True,
                "scope": "changed_lines",
            },
        },
    ]
    detail = evidence_detail(
        {
            "status": "passed",
            "language": "python",
            "evidence": {
                "mutation": {
                    "generated": 2,
                    "killed": 1,
                    "survived": 1,
                    "changedLineMutantsGenerated": 2,
                    "changedLineMutantsKilled": 1,
                    "changedLineMutantsScored": 2,
                    "rate": 50.0,
                    "gate": 40.0,
                    "passed": True,
                    "scope": "changed_lines",
                    "gateScope": "report",
                    "modules": 2,
                },
                "targetResults": target_results,
            },
        }
    )

    assert detail["mutation"]["passed"] is True
    assert detail["mutation"]["gateScope"] == "report"
    assert detail["mutation"]["formattedWorstTargetRate"] is None
    assert detail["mutationFailures"] == {"count": 0, "targets": []}


def test_passing_report_renders_no_failure_reason_for_warning_noise():
    """Regression for CI task f51a849c.

    The Maven build exited 0 with 100% diff coverage and 66/66 mutants killed,
    but javac `[WARNING]` lines on two test files were read as compile errors
    and rendered a "test compilation blocker" under 失败原因.
    """
    record = CiTaskRecord(
        task_id="passing-warning-noise-1",
        status=CiTaskStatus.success,
        request=CiTriggerRequest(
            app_name="wtrace",
            git_url="git@git.example.com:fd/wtrace.git",
            branch="TASK-4001",
        ),
        summary="UTA test-enforcement passed",
        enforcement_result={
            "status": "passed",
            "passed": True,
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "returncode": 0,
            "stdout": (
                "[WARNING] /ws/wtrace/wtrace-api/src/test/java/com/example/fd/trace/BinaryAnnotationTest.java:"
                "[11,15] Unsafe is internal proprietary API and may be removed in a future release\n"
                "[info] [test-enforcer] diff line coverage 100.00% passed for wtrace-common (112/112)\n"
                "[INFO] BUILD SUCCESS\n"
            ),
            "stderr": "",
            "evidence": {
                "changedClasses": ["com.example.trace.task.kafka.SpanKafkaConsumer"],
                "filteredTargetClasses": ["com.example.trace.task.kafka.SpanKafkaConsumer"],
            },
        },
    )

    detail = CiReportRenderer().detail(record)
    html = CiReportRenderer().report_html(record)

    assert detail["failureDiagnostics"] == []
    assert "失败原因" not in html
    assert "test compilation blocker" not in html

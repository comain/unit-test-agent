from __future__ import annotations

import subprocess
import json

from fastapi.testclient import TestClient

from uta.ci_plugin.app import create_app
from uta.ci_plugin.enforcement import MavenEnforcementRunner
from uta.ci_plugin.fix_sessions import CreateFixSessionRequest
from uta.ci_plugin.service import CiPluginService
from uta.ci_plugin.workspace import GitWorkspaceManager
from uta.ci_plugin.context import RepairContextExporter, assemble_base_context
from uta.ci_plugin.models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.ci_plugin.protocols import ProtocolRegistry
from uta.ci_plugin.protocols.github import GithubContextProvider, GithubWebhookProtocol
from uta.tasks.manager import TaskManager
from uta.tasks.models import json_loads
from uta.tasks.targets import TargetIdentity
from uta.ci_plugin.store import JsonCiTaskStore

FIXABLE_GATE_FAILURE = (
    "test-enforcer check-coverage failed: "
    "diff line coverage 87.50% is below required 95.00%"
)


def _payload():
    return {
        "action": "opened",
        "pull_request": {
            "number": 7,
            "title": "EXAMPLE-122 add coverage",
            "body": "Covers checkout edge cases",
            "user": {"login": "dev-user"},
            "head": {"ref": "feature/EXAMPLE-122", "sha": "abc123def"},
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
        content=json.dumps(payload or _payload()).encode("utf-8"),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 200
    return response.json()["taskId"]


def _client_with_enforcement(tmp_path, stdout: str) -> TestClient:
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    service = CiPluginService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path, run_command=fake_run),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        protocols=ProtocolRegistry([GithubWebhookProtocol()]),
    )
    return TestClient(create_app(service))


def _service_with_repair(tmp_path, stdout: str = FIXABLE_GATE_FAILURE) -> CiPluginService:
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return CiPluginService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces", run_command=fake_run),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([GithubWebhookProtocol()]),
        repair_priority=1,
    )


def test_report_exposes_fix_entry_only_for_failed_reports(tmp_path):
    failed_client = _client_with_enforcement(tmp_path / "failed", FIXABLE_GATE_FAILURE)
    failed_task = _trigger(failed_client)

    passed_client = _client_with_enforcement(
        tmp_path / "passed",
        "Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%",
    )
    passed_task = _trigger(passed_client)

    assert "一键修复" in failed_client.get(f"/reports/{failed_task}/index.html").text
    assert "一键修复" not in passed_client.get(f"/reports/{passed_task}/index.html").text


def test_missing_plugin_report_does_not_expose_fix_session(tmp_path):
    client = _client_with_enforcement(tmp_path, "BUILD SUCCESS")
    task_id = _trigger(client)

    html = client.get(f"/reports/{task_id}/index.html").text
    response = client.post(f"/reports/{task_id}/fix-sessions", json={})

    assert "Missing test-enforcement plugin/profile" in html
    assert "一键修复" not in html
    assert 'id="open-fix-session"' not in html
    assert response.status_code == 409
    assert response.json()["detail"] == "fix sessions are not available for this report"


def test_failed_report_renders_fix_session_submit_form(tmp_path):
    client = _client_with_enforcement(tmp_path, FIXABLE_GATE_FAILURE)
    task_id = _trigger(client)

    html = client.get(f"/reports/{task_id}/index.html").text

    assert 'id="open-fix-session"' in html
    assert 'id="fix-session-form"' in html
    assert 'id="fix-user-context"' in html
    assert "创建修复任务" in html
    assert 'fetch("fix-sessions"' in html


def test_fix_session_create_message_retry_and_detail(tmp_path):
    client = _client_with_enforcement(tmp_path, FIXABLE_GATE_FAILURE)
    task_id = _trigger(client)

    created = client.post(
        f"/reports/{task_id}/fix-sessions",
        json={"targetIds": ["enforcement:missing_evidence"], "userContext": "focus on changed service tests"},
    )
    session_id = created.json()["session"]["sessionId"]
    message = client.post(
        f"/reports/{task_id}/fix-sessions/{session_id}/messages",
        json={"message": "also cover mutation survivor"},
    )
    retry = client.post(f"/reports/{task_id}/fix-sessions/{session_id}/retry", json={"message": "try again"})
    detail = client.get(f"/reports/{task_id}/detail").json()

    assert created.status_code == 200
    assert created.json()["session"]["selectedTargets"][0]["id"] == "enforcement:missing_evidence"
    assert message.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["session"]["retryCount"] == 1
    session = detail["fixSessions"][0]
    assert session["sessionId"] == session_id
    assert session["selectedTargets"][0]["label"] == "missing_evidence"
    assert [item["content"] for item in session["messages"]] == [
        "focus on changed service tests",
        "also cover mutation survivor",
        "try again",
    ]


def test_fix_session_message_and_retry_are_persisted(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    service = CiPluginService(record_store=store)
    record = CiTaskRecord(
        task_id="task-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/EXAMPLE-122",
            }
        ),
        fix_sessions=[
            {
                "sessionId": "fix-1",
                "status": "repair_failed",
                "messages": [],
                "retryCount": 0,
            }
        ],
    )
    service.save(record)
    client = TestClient(create_app(service))

    message = client.post(
        "/reports/task-1/fix-sessions/fix-1/messages",
        json={"message": "persist this follow-up"},
    )
    after_message = JsonCiTaskStore(tmp_path / "records").load("task-1")
    retry = client.post(
        "/reports/task-1/fix-sessions/fix-1/retry",
        json={"message": "persist this retry"},
    )
    after_retry = JsonCiTaskStore(tmp_path / "records").load("task-1")

    assert message.status_code == 200
    assert after_message.fix_sessions[0]["messages"][0]["content"] == "persist this follow-up"
    assert retry.status_code == 200
    assert after_retry.fix_sessions[0]["status"] == "retry_requested"
    assert after_retry.fix_sessions[0]["retryCount"] == 1
    assert [item["content"] for item in after_retry.fix_sessions[0]["messages"]] == [
        "persist this follow-up",
        "persist this retry",
    ]


def test_fix_session_rejects_success_report_and_bad_session(tmp_path):
    client = _client_with_enforcement(
        tmp_path,
        "Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%",
    )
    task_id = _trigger(client)

    response = client.post(f"/reports/{task_id}/fix-sessions", json={"targetIds": ["enforcement:passed"]})
    missing = client.post(f"/reports/{task_id}/fix-sessions/missing/messages", json={"message": "x"})

    assert response.status_code == 409
    assert missing.status_code == 404


def test_fix_session_creates_urgent_repair_task_with_context(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = _trigger(client)

    response = client.post(
        f"/reports/{task_id}/fix-sessions",
        json={"targetIds": ["class:com.example.Foo"], "userContext": "add branch tests"},
    )

    session = response.json()["session"]
    repo_task = service.task_manager.get_task(session["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert response.status_code == 200
    assert session["status"] == "repair_task_created"
    assert 0 <= repo_task["priority"] <= 9
    assert repo_task["branch_name"] == "feature/EXAMPLE-122"
    assert repo_task["coverage_gate"] == 95
    assert repo_task["mutation_gate"] == 100
    assert selection["class_fqns"] == ["com.example.Foo"]
    assert selection["quality_mode"] == "ci_incremental"
    assert selection["quality_gate_backend"] == "maven_enforcer"
    assert json_loads(repo_task["ci_context_json"])["user"]["context"] == "add branch tests"
    assert ".uta_cache" not in repo_task["ci_context_path"]


def test_fix_session_creates_python_repair_task_with_target_refs(tmp_path):
    service = _service_with_repair(tmp_path)
    record = CiTaskRecord(
        task_id="task-python",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "py-app",
                "gitUrl": "git@git.example.com:group/py-app.git",
                "branch": "feature/EXAMPLE-122",
                "language": "python",
            }
        ),
        workspace_path=str(tmp_path / "workspace" / "repo"),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "python",
            "backend": "python_enforcer",
            "command": ["uta", "python-enforce"],
            "evidence": {
                "targetResults": [
                    {"target": {"target": "jobs/forecast.py::run", "language": "python"}}
                ]
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest())

    session = response["session"]
    repo_task = service.task_manager.get_task(session["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert session["status"] == "repair_task_created"
    assert selection["language"] == "python"
    assert selection["targets"][0]["source_path"] == "jobs/forecast.py"
    assert selection["targets"][0]["symbol"] == "run"
    assert selection["targets"][0]["target_id"] == "pysymbol:jobs/forecast.py::run"
    assert selection["quality_gate_backend"] == "python_enforcer"
    assert repo_task["language"] == "python"


def test_report_links_to_fix_session_progress_page(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = _trigger(client)
    session = client.post(
        f"/reports/{task_id}/fix-sessions",
        json={"targetIds": ["class:com.example.Foo"]},
    ).json()["session"]
    record = service.get(task_id)
    record.fix_sessions[0]["status"] = "green"
    record.fix_sessions[0]["rerunEnforcement"] = {
        "passed": True,
        "status": "passed",
        "summary": "test-enforcement passed",
        "command": ["mvn", "-U", "-Dtest.enforcement.enabled=true", "verify"],
        "stdout": (
            "[INFO] diff line coverage 97.50% passed for fd-maven-plugins (39/40)\n"
            "[INFO] PIT generated=4 killed=4 survived=0 test-strength=100%"
        ),
        "stderr": "",
    }
    service.save(record)

    report_html = client.get(f"/reports/{task_id}/index.html").text
    progress_response = client.get(f"/reports/{task_id}/fix-sessions/{session['sessionId']}/progress")
    progress_data = client.get(f"/reports/{task_id}/fix-sessions/{session['sessionId']}/progress/data").json()

    assert f"fix-sessions/{session['sessionId']}/progress" in report_html
    assert "查看修复进度" in report_html
    assert 'id="fix-session-panel" hidden' not in report_html
    assert progress_response.status_code == 200
    assert 'http-equiv="refresh"' in progress_response.text
    assert "基线编译" in progress_response.text
    assert "GMT+8" in progress_response.text
    assert "Detail" in progress_response.text
    assert "Backend Coverage" not in progress_response.text
    assert "覆盖率/变异门禁由 Maven test-enforcement 在修复过程中执行" not in progress_response.text
    assert "等待所有类任务完成后执行" not in progress_response.text
    assert "完成确认" in progress_response.text
    assert "最终覆盖率" in progress_response.text
    assert "97.50%" in progress_response.text
    assert "最终变异得分" in progress_response.text
    assert "100.00%" in progress_response.text
    assert progress_data["session"]["repoTaskId"] == session["repoTaskId"]
    assert progress_data["repoTask"]["task"]["id"] == session["repoTaskId"]
    assert progress_data["repoTask"]["classes"][0]["class_fqn"] == "com.example.Foo"


def test_report_shows_fix_sessions_near_top_with_rate_limit_issue(tmp_path):
    service = CiPluginService()
    record = CiTaskRecord(
        task_id="task-rate-limit",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/EXAMPLE-122",
            }
        ),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "stdout": FIXABLE_GATE_FAILURE,
            "stderr": "",
        },
        fix_sessions=[
            {
                "sessionId": "fix-rate-limit",
                "status": "repair_failed",
                "repoTaskId": 18,
                "repoTaskStatus": "POISONED",
                "retryCount": 1,
                "repairIssues": [
                    {
                        "type": "provider_rate_limit",
                        "stage": "plan",
                        "message": "Planning hit provider/model rate limit for batch ['com.example.Foo']",
                    }
                ],
            }
        ],
    )
    service._tasks[record.task_id] = record
    service.save(record)
    client = TestClient(create_app(service))

    html = client.get("/reports/task-rate-limit/index.html").text

    assert html.index("修复会话") < html.index("检查结论")
    assert "Provider/model rate limit" in html
    assert "Planning hit provider/model rate limit" in html
    assert "fix-sessions/fix-rate-limit/progress" in html


def test_repair_session_refresh_extracts_rate_limit_from_run_log(tmp_path):
    run_log = tmp_path / "run.log"
    run_log.write_text(
        "13:20:45 [uta] WARNING: Planning hit provider/model rate limit for batch ['com.example.Foo']\n"
        "13:46:36 [uta] WARNING: Generation timed out after 3600s for batch ['com.example.Foo']\n"
        "13:20:45 [uta] ERROR: CI repair auto-push found no test changes to commit\n",
        encoding="utf-8",
    )

    issues = CiPluginService._repair_issues_from_repo_task(
        {
            "run_log_path": str(run_log),
            "current_stage": "quarantined",
            "error": "Auto-quarantined after 2 failures. Last: CI repair auto-push found no test changes to commit",
            "last_error": "Auto-quarantined after 2 failures. Last: CI repair auto-push found no test changes to commit",
            "current_detail": "Auto-quarantined after 2 failures. Last: CI repair auto-push found no test changes to commit",
        },
        {
            "latest_events": [
                {
                    "stage": "push",
                    "message": "20260526-IDSS-40967: CI repair auto-push found no test changes to commit",
                }
            ]
        },
    )

    assert any(issue["type"] == "provider_rate_limit" for issue in issues)
    assert any("Planning hit provider/model rate limit" in issue["message"] for issue in issues)
    assert any(issue["type"] == "generation_timeout" for issue in issues)
    assert any(issue["type"] == "no_test_changes" for issue in issues)


def test_repair_progress_stages_ignore_events_before_latest_resume():
    stages = CiPluginService._repair_progress_stages(
        {"status": "repair_task_created", "repoTaskId": 7},
        {
            "task": {"status": "RUNNING", "current_stage": "plan_tests"},
            "latest_events": [
                {"event_type": "stage_started", "stage": "plan_tests", "message": "current"},
                {"event_type": "task_resumed", "stage": "queued", "message": "resume"},
                {"event_type": "stage_started", "stage": "coverage_fix", "message": "old run"},
            ],
        },
    )

    statuses = {stage["key"]: stage["status"] for stage in stages}
    assert statuses["generate"] == "active"
    assert statuses["coverage_fix"] == "pending"


def test_repair_progress_marks_prior_stages_done_when_current_stage_advanced():
    stages = CiPluginService._repair_progress_stages(
        {"status": "repair_task_created", "repoTaskId": 7},
        {
            "task": {"status": "RUNNING", "current_stage": "test_execution"},
            "latest_events": [
                {"event_type": "stage_started", "stage": "test_execution", "message": "com.example.Foo"},
            ],
        },
    )

    statuses = {stage["key"]: stage["status"] for stage in stages}
    assert statuses["queued"] == "done"
    assert statuses["baseline_compile"] == "done"
    assert statuses["generate"] == "active"


def test_repair_progress_uses_latest_llm_stage_when_current_stage_is_generic():
    stages = CiPluginService._repair_progress_stages(
        {"status": "repair_task_created", "repoTaskId": 7},
        {
            "task": {"status": "RUNNING", "current_stage": "test_execution"},
            "latest_events": [
                {"event_type": "llm_progress", "stage": "coverage_fix", "message": "Starting LLM phase coverage_fix"},
                {"event_type": "stage_started", "stage": "test_execution", "message": "com.example.Foo"},
            ],
        },
    )

    statuses = {stage["key"]: stage["status"] for stage in stages}
    assert statuses["baseline_compile"] == "done"
    assert statuses["generate"] == "done"
    assert statuses["coverage_fix"] == "active"


def test_report_context_uses_injected_repair_context(tmp_path):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git" and "log" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="EXAMPLE-123 implement feature flow\x1e", stderr="")
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=FIXABLE_GATE_FAILURE, stderr="")

    class FakeContextProvider(GithubContextProvider):
        def build_context(self, record, *, user_context=None, commit_messages=None):
            assert record.request.jira_id == "EXAMPLE-123"
            return assemble_base_context(
                record,
                issue={"id": "EXAMPLE-123", "description": "Issue describes feature selection changes"},
                user_context=user_context,
                commit_messages=commit_messages,
            )

    service = CiPluginService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces", run_command=fake_run),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([GithubWebhookProtocol(context_provider=FakeContextProvider())]),
    )
    client = TestClient(create_app(service))
    payload = _payload()
    payload["pull_request"]["head"]["ref"] = "EXAMPLE-123-20260515"
    payload["pull_request"]["title"] = "EXAMPLE-123 add coverage"
    task_id = _trigger(client, payload)

    client.post(f"/reports/{task_id}/fix-sessions", json={"targetIds": ["class:com.example.Foo"]})
    detail = client.get(f"/reports/{task_id}/detail").json()

    assert detail["jiraId"] == "EXAMPLE-123"
    assert detail["context"]["missingReasons"] == []
    sources = {source["name"]: source for source in detail["context"]["sources"]}
    assert sources["issue_description"]["available"] is True
    assert sources["git_commit_messages"]["available"] is True


def test_terminal_fix_session_report_enriches_saved_ci_context(tmp_path):
    class FakeContextProvider(GithubContextProvider):
        def enrich_repair_context(self, record, context, repo_task):
            issue = context.setdefault("issue", {})
            issue.update({"id": "EXAMPLE-123", "description": "Issue description fetched during report refresh"})
            context["missingReasons"] = []
            for source in context.get("sources") or []:
                if source.get("name") in {"issue_description", "git_commit_messages"}:
                    source["available"] = True
            return context

    service = CiPluginService(
        task_manager=TaskManager(tmp_path / "tasks.db"),
        protocols=ProtocolRegistry([GithubWebhookProtocol(context_provider=FakeContextProvider())]),
    )
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="EXAMPLE-123-20260515",
        ci_context={
            "pipeline": {"branch": "EXAMPLE-123-20260515"},
            "git": {"commitMessages": ["EXAMPLE-123 update unit test gate"]},
            "issue": {"id": None, "description": None, "source": "unavailable", "kind": "jira"},
            "sources": [
                {"name": "git_commit_messages", "available": False, "priority": 3},
                {"name": "issue_description", "available": False, "priority": 4, "source": "unavailable"},
            ],
            "missingReasons": ["issue_description_unavailable", "git_commit_messages_unavailable"],
        },
    )
    record = CiTaskRecord(
        task_id="task-old",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "EXAMPLE-123-20260515",
            }
        ),
        protocol="github",
        fix_sessions=[{"sessionId": "fix-old", "status": "rerun_failed", "repoTaskId": repo_task_id}],
    )
    service._tasks[record.task_id] = record
    client = TestClient(create_app(service))

    detail = client.get("/reports/task-old/detail").json()

    assert detail["context"]["missingReasons"] == []
    sources = {source["name"]: source for source in detail["context"]["sources"]}
    assert sources["issue_description"]["available"] is True
    assert sources["git_commit_messages"]["available"] is True


def test_fix_session_recovered_repair_failed_reruns_enforcement(tmp_path):
    service = _service_with_repair(
        tmp_path,
        stdout="Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%",
    )
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="EXAMPLE-123-20260515",
    )
    service.task_manager.mark_completed(repo_task_id, message="resumed task completed")
    record = CiTaskRecord(
        task_id="task-old",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "EXAMPLE-123-20260515",
            }
        ),
        fix_sessions=[{"sessionId": "fix-old", "status": "repair_failed", "repoTaskId": repo_task_id}],
    )
    service._tasks[record.task_id] = record

    refreshed = service.get("task-old")

    assert refreshed.fix_sessions[0]["status"] == "green"
    assert refreshed.fix_sessions[0]["repoTaskStatus"] == "COMPLETED"
    assert refreshed.fix_sessions[0]["rerunEnforcement"]["status"] == "passed"
    assert refreshed.status == CiTaskStatus.success


def test_completed_python_repair_task_replaces_stale_rerun_failed_report(tmp_path):
    service = CiPluginService(task_manager=TaskManager(tmp_path / "tasks.db"))
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    target = TargetIdentity(
        language="python",
        target_id="pyfile:src/app.py",
        display_name="src/app.py",
        source_path="src/app.py",
        granularity="file",
    )
    repo_task_id = service.task_manager.create_task_targets(
        repo_path=str(repo_path),
        targets=[target],
        branch_name="feature/EXAMPLE-122",
        coverage_gate=95.0,
        mutation_gate=100.0,
        quality_gate_backend="python_enforcer",
        language="python",
    )
    service.task_manager.sync_target_results(
        repo_task_id,
        {
            target: {
                "status": "PASS",
                "language": "python",
                "coverage": 100.0,
                "mutation_score": 100.0,
                "surviving_mutants": 0,
                "total_mutants": 2,
                "test_file_path": "tests/uta_generated/test_app.py",
            }
        },
        targets=[target],
    )
    service.task_manager.record_push_verified(
        repo_task_id,
        branch_name="feature/EXAMPLE-122",
        local_head="abc123",
        remote_head="abc123",
    )
    service.task_manager.mark_completed(repo_task_id, message="repair finished")
    record = CiTaskRecord(
        task_id="task-python-old",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "py-app",
                "gitUrl": "git@git.example.com:group/py-app.git",
                "branch": "feature/EXAMPLE-122",
                "language": "python",
            }
        ),
        enforcement_result={
            "status": "missing_evidence",
            "passed": False,
            "language": "python",
            "backend": "python_enforcer",
            "summary": "Python enforcement failed: missing_test_paths",
            "command": ["uta", "python-enforce", "--json-output"],
            "evidence": {
                "baseRef": "origin/master",
                "baseCommit": "base123",
                "changedProductionFiles": ["src/app.py"],
                "changedLines": {"src/app.py": [1]},
            },
        },
        fix_sessions=[
            {
                "sessionId": "fix-old",
                "status": "rerun_failed",
                "repoTaskId": repo_task_id,
                "rerunEnforcement": {
                    "status": "missing_evidence",
                    "passed": False,
                    "summary": "Python enforcement failed: missing_test_paths",
                },
            }
        ],
    )
    service._tasks[record.task_id] = record

    refreshed = service.get(record.task_id)

    session = refreshed.fix_sessions[0]
    assert refreshed.status == CiTaskStatus.success
    assert refreshed.enforcement_result["status"] == "passed"
    assert refreshed.enforcement_result["backend"] == "python_enforcer"
    assert refreshed.enforcement_result["evidence"]["coverage"]["passed"] is True
    assert refreshed.enforcement_result["evidence"]["mutation"]["passed"] is True
    assert session["status"] == "green"
    assert session["repoTaskStatus"] == "COMPLETED"
    assert session["rerunEnforcement"]["summary"] == "Python enforcement passed from completed repair task evidence"


def test_fix_session_refresh_preserves_failed_status_for_poisoned_repo_task(tmp_path):
    service = CiPluginService(task_manager=TaskManager(tmp_path / "tasks.db"))
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="EXAMPLE-123-20260515",
    )
    service.task_manager.mark_poisoned(
        repo_task_id,
        "Auto-quarantined after 2 failures. Last: provider/model rate limit",
    )
    record = CiTaskRecord(
        task_id="task-poisoned",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "EXAMPLE-123-20260515",
            }
        ),
        fix_sessions=[{"sessionId": "fix-poisoned", "status": "repair_failed", "repoTaskId": repo_task_id}],
    )
    service._tasks[record.task_id] = record

    refreshed = service.get("task-poisoned")

    assert refreshed.fix_sessions[0]["status"] == "repair_failed"
    assert refreshed.fix_sessions[0]["repoTaskStatus"] == "POISONED"


def test_fix_session_rerun_running_does_not_start_duplicate_enforcement(tmp_path):
    class ExplodingRunner:
        def run(self, repo_path):
            raise AssertionError("duplicate enforcement rerun")

    service = CiPluginService(
        enforcement_runner=ExplodingRunner(),
        task_manager=TaskManager(tmp_path / "tasks.db"),
    )
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="EXAMPLE-123-20260515",
    )
    service.task_manager.mark_completed(repo_task_id, message="resumed task completed")
    record = CiTaskRecord(
        task_id="task-old",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "EXAMPLE-123-20260515",
            }
        ),
        fix_sessions=[{"sessionId": "fix-old", "status": "rerun_running", "repoTaskId": repo_task_id}],
    )
    service._tasks[record.task_id] = record

    refreshed = service.get("task-old")

    assert refreshed.fix_sessions[0]["status"] == "rerun_running"
    assert "rerunEnforcement" not in refreshed.fix_sessions[0]


def test_repair_progress_marks_rerun_enforcement_active():
    stages = CiPluginService._repair_progress_stages(
        {"status": "rerun_running"},
        {
            "task": {"status": "COMPLETED", "current_stage": "finished"},
            "latest_events": [
                {"event_type": "stage_started", "stage": "store_and_push", "message": "save and push"},
            ],
        },
    )

    by_key = {stage["key"]: stage["status"] for stage in stages}
    assert by_key["push"] == "done"
    assert by_key["rerun_enforcement"] == "active"


def test_fix_session_duplicate_fingerprint_returns_existing_task(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = _trigger(client)
    body = {"targetIds": ["class:com.example.Foo"], "userContext": "first"}

    first = client.post(f"/reports/{task_id}/fix-sessions", json=body).json()["session"]
    duplicate = client.post(f"/reports/{task_id}/fix-sessions", json=body).json()

    assert duplicate["alreadyRunning"] is True
    assert duplicate["session"]["sessionId"] == first["sessionId"]
    assert duplicate["session"]["repoTaskId"] == first["repoTaskId"]


def test_fix_session_duplicate_after_rerun_failed_creates_new_task(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = _trigger(client)
    body = {"targetIds": ["class:com.example.Foo"], "userContext": "first"}

    first = client.post(f"/reports/{task_id}/fix-sessions", json=body).json()["session"]
    record = service.get(task_id)
    record.fix_sessions[0]["status"] = "rerun_failed"
    service.save(record)

    second = client.post(f"/reports/{task_id}/fix-sessions", json=body).json()

    assert second.get("alreadyRunning") is not True
    assert second["session"]["sessionId"] != first["sessionId"]
    assert second["session"]["repoTaskId"] != first["repoTaskId"]
    assert len(record.fix_sessions) == 2


def test_fix_session_prior_green_returns_successful_result_without_new_task(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = _trigger(client)
    first = client.post(
        f"/reports/{task_id}/fix-sessions",
        json={"targetIds": ["class:com.example.Foo"]},
    ).json()["session"]
    record = service.get(task_id)
    record.fix_sessions[0]["status"] = "green"

    second = client.post(
        f"/reports/{task_id}/fix-sessions",
        json={"targetIds": ["class:com.example.Foo"]},
    ).json()

    assert second["alreadyGreen"] is True
    assert second["session"]["sessionId"] == first["sessionId"]
    assert len(record.fix_sessions) == 1


def test_fix_session_rate_limit_blocks_new_fingerprints(tmp_path):
    service = _service_with_repair(tmp_path)
    service.repair_rate_limit_per_task = 1
    client = TestClient(create_app(service))
    task_id = _trigger(client)

    first = client.post(f"/reports/{task_id}/fix-sessions", json={"targetIds": ["class:com.example.Foo"]})
    second = client.post(f"/reports/{task_id}/fix-sessions", json={"targetIds": ["class:com.example.Bar"]})

    assert first.status_code == 200
    assert second.status_code == 429

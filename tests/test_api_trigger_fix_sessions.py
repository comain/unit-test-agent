import json
import subprocess
import threading
from pathlib import Path
from fake_maven_metadata import DIFF_MUTATION_OK, with_resolved_enforcer

from fastapi.testclient import TestClient
import pytest

from fake_git import calls, fake_git
from uta.app.app import create_app
from uta.enforcement.enforcement import QualityGateResult, QualityGateStatus
from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.app.service import ApiTriggerService, FixSessionUnsupportedError
from uta.app.workspace import GitWorkspaceManager
from uta.app.context import RepairContextExporter, render_context_markdown
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.app.protocols import ProtocolRegistry
from uta.app.protocols.rdc import RdcContextProvider, RdcProtocol
from uta.app.reporting import CiReportRenderer
from uta.language.python.ci import PythonCiLanguageHandler
from uta.tasks.manager import TaskManager
from uta.tasks.models import json_loads
from uta.shared.targets import TargetIdentity
from uta.app.store import JsonCiTaskStore
from uta.app.repair import RepairSessions

FIXABLE_GATE_FAILURE = (
    "test-enforcer check-coverage failed: "
    "diff line coverage 87.50% is below required 95.00%"
)


def test_python_ci_repair_detects_passed_target_with_unexecuted_candidate_plan():
    target_result = {
        "status": "passed",
        "reasonCode": "passed",
        "testsPass": True,
        "coverage": {"passed": True, "rate": 100.0},
        "mutation": {
            "passed": True,
            "rate": 100.0,
            "changedLineMutantsGenerated": 0,
            "candidatePlan": {
                "eligibleMutationOpportunities": [{"opportunityId": "op-1"}],
                "exactToolCandidateKeys": ["pkg.mod.x_target__mutmut_1"],
                "activeSelected": [{"toolCandidateKey": "pkg.mod.x_target__mutmut_1"}],
                "reportFullSelected": [{"toolCandidateKey": "pkg.mod.x_target__mutmut_1"}],
            },
        },
    }

    assert PythonCiLanguageHandler._target_result_needs_repair(target_result) is True


def test_python_ci_repair_skips_advisory_skipped_targets():
    """A skipped target has no failing gate, so repair must not be opened for it.

    Both skips report `status: passed` with an empty, passing mutation summary.
    That shape also trips the unexecuted-candidate-plan check above, so the
    reason code has to be consulted before it -- otherwise UTA declines to
    verify a target as too expensive and then pays an LLM to write tests for it
    that the next run will skip again.
    """
    def skipped(reason_code):
        return {
            "status": "passed",
            "reasonCode": reason_code,
            "testsPass": True,
            "coverage": {"passed": True, "rate": 100.0, "scope": "large_change_skipped"},
            "mutation": {
                "passed": True,
                "rate": 100.0,
                "generated": 0,
                "changedLineMutantsGenerated": 0,
                "scope": "large_change_skipped",
            },
        }

    needs_repair = PythonCiLanguageHandler._target_result_needs_repair
    assert needs_repair(skipped("python_large_change_skipped")) is False
    assert needs_repair(skipped("python_runtime_incompatible_skipped")) is False
    # A real gate failure is still repaired.
    assert needs_repair(
        {"status": "failed", "reasonCode": "mutation_failed",
         "coverage": {"passed": True}, "mutation": {"passed": False}}
    ) is True


def _payload():
    return {
        "attribute": {
            "appName": "demo-app",
            "gitUrl": "git@git.example.com:group/demo.git",
            "branch": "feature/TASK-82767",
        }
    }


def _client_with_enforcement(tmp_path, stdout: str) -> TestClient:
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path, git_bin=fake_git(tmp_path)),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        protocols=ProtocolRegistry([RdcProtocol()]),
    )
    return TestClient(create_app(service))


def _service_with_repair(tmp_path, stdout: str = FIXABLE_GATE_FAILURE) -> ApiTriggerService:
    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces", git_bin=fake_git(tmp_path)),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([RdcProtocol()]),
        repair_priority=1,
    )


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_java_diff_repo(repo):
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    prod = repo / "biz/src/main/java/com/demo/PunchBizImpl.java"
    prod.parent.mkdir(parents=True)
    prod.write_text("package com.demo; class PunchBizImpl {}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    prod.write_text("package com.demo; class PunchBizImpl { int v; }\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feature")


def test_report_exposes_fix_entry_only_for_failed_reports(tmp_path):
    failed_client = _client_with_enforcement(tmp_path / "failed", FIXABLE_GATE_FAILURE)
    failed_task = failed_client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]

    passed_client = _client_with_enforcement(
        tmp_path / "passed",
        "Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%",
    )
    passed_task = passed_client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]

    assert "一键修复" in failed_client.get(f"/reports/{failed_task}/index.html").text
    assert "一键修复" not in passed_client.get(f"/reports/{passed_task}/index.html").text


def test_missing_plugin_report_does_not_expose_fix_session(tmp_path):
    client = _client_with_enforcement(tmp_path, "BUILD SUCCESS")
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]

    html = client.get(f"/reports/{task_id}/index.html").text
    response = client.post(f"/reports/{task_id}/fix-sessions", json={})

    assert "Missing UTA test-enforcement plugin/profile" in html
    assert "一键修复" not in html
    assert 'id="open-fix-session"' not in html
    assert response.status_code == 409
    assert response.json()["detail"] == "fix sessions are not available for this report"


def test_failed_report_renders_fix_session_submit_form(tmp_path):
    client = _client_with_enforcement(tmp_path, FIXABLE_GATE_FAILURE)
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]

    html = client.get(f"/reports/{task_id}/index.html").text

    assert 'id="open-fix-session"' in html
    assert 'id="fix-session-form"' in html
    assert 'id="fix-user-context"' in html
    assert "创建修复任务" in html
    assert 'fetch("fix-sessions"' in html


def test_fix_session_create_message_retry_and_detail(tmp_path):
    client = _client_with_enforcement(tmp_path, FIXABLE_GATE_FAILURE)
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]

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
    service = ApiTriggerService(record_store=store)
    record = CiTaskRecord(
        task_id="task-1",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
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


def test_fix_session_retry_creates_fresh_repair_task_after_failure(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]
    created = client.post(
        f"/reports/{task_id}/fix-sessions",
        json={"targetIds": ["class:com.example.Foo"], "userContext": "first repair context"},
    ).json()["session"]
    old_repo_task_id = created["repoTaskId"]
    service.task_manager.mark_failed(old_repo_task_id, "OpenCode did not produce a test file", stage="finished")
    record = service.get(task_id)
    record.fix_sessions[0]["status"] = "repair_failed"
    record.fix_sessions[0]["repoTaskStatus"] = "FAILED"
    record.fix_sessions[0]["repoTaskStage"] = "finished"
    service.save(record)

    retry = client.post(
        f"/reports/{task_id}/fix-sessions/{created['sessionId']}/retry",
        json={"message": "retry after deploy"},
    )

    session = retry.json()["session"]
    repo_task = service.task_manager.get_task(session["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert retry.status_code == 200
    assert session["status"] == "repair_task_created"
    assert session["repoTaskId"] != old_repo_task_id
    assert session["repoTaskHistory"][0]["repoTaskId"] == old_repo_task_id
    assert session["retryCount"] == 1
    assert selection["class_fqns"] == ["com.example.Foo"]
    assert json_loads(repo_task["rdc_context_json"])["user"]["context"] == (
        "first repair context\n\nretry after deploy"
    )


def test_fix_session_rejects_success_report_and_bad_session(tmp_path):
    client = _client_with_enforcement(
        tmp_path,
        "Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%",
    )
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]

    response = client.post(f"/reports/{task_id}/fix-sessions", json={"targetIds": ["enforcement:passed"]})
    missing = client.post(f"/reports/{task_id}/fix-sessions/missing/messages", json={"message": "x"})

    assert response.status_code == 409
    assert missing.status_code == 404


def test_fix_session_creates_urgent_repair_task_with_context(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]

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
    assert repo_task["branch_name"] == "feature/TASK-82767"
    assert repo_task["coverage_gate"] == 95
    assert repo_task["mutation_gate"] == 100
    assert selection["class_fqns"] == ["com.example.Foo"]
    assert selection["quality_mode"] == "ci_incremental"
    assert selection["quality_gate_backend"] == "maven_enforcer"
    assert json_loads(repo_task["rdc_context_json"])["user"]["context"] == "add branch tests"
    assert ".uta_cache" not in repo_task["rdc_context_path"]


def test_default_java_fix_session_rejects_missing_filtered_target_evidence(tmp_path):
    service = _service_with_repair(tmp_path)
    repo = tmp_path / "workspace" / "repo"
    _init_java_diff_repo(repo)
    record = CiTaskRecord(
        task_id="task-java-missing-evidence",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(repo),
        enforcement_result={
            "status": "missing_evidence",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "summary": "UTA test-enforcement cannot run PIT safely because no related targetTests were found",
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "evidence": {
                "coverage": {"covered": 0, "total": 0, "rate": 0.0, "passed": False},
                "mutation": {"generated": 0, "killed": 0, "rate": 0.0, "passed": False},
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    with pytest.raises(FixSessionUnsupportedError) as exc:
        service.create_fix_session(record, CreateFixSessionRequest())

    assert "Maven-filtered target classes" in str(exc.value)


def test_java_fix_session_strips_stale_target_tests_from_report_command(tmp_path):
    service = _service_with_repair(tmp_path)
    repo = tmp_path / "workspace" / "repo"
    _init_java_diff_repo(repo)
    record = CiTaskRecord(
        task_id="task-java-stale-target-tests",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(repo),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "command": [
                "mvn",
                "-Dtest.enforcement.enabled=true",
                "verify",
                "-DtargetTests=com.demo.UnrelatedTaskTest",
                "-Dtest=UnrelatedTaskTest",
                "-Dsurefire.failIfNoSpecifiedTests=false",
            ],
            "evidence": {
                "changedClasses": ["com.demo.PunchBizImpl"],
                "filteredTargetClasses": ["com.demo.PunchBizImpl"],
                "targetTests": ["com.demo.UnrelatedTaskTest"],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest())

    repo_task = service.task_manager.get_task(response["session"]["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert selection["class_fqns"] == ["com.demo.PunchBizImpl"]
    assert selection["quality_gate_command"] == "mvn -Dtest.enforcement.enabled=true verify"


def test_java_fix_session_replaces_filter_diff_preflight_command(tmp_path):
    service = _service_with_repair(tmp_path)
    repo = tmp_path / "workspace" / "repo"
    _init_java_diff_repo(repo)
    record = CiTaskRecord(
        task_id="task-java-filter-diff-command",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(repo),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "summary": "UTA test-enforcement filter-diff preflight failed",
            "command": [
                "mvn",
                "-U",
                "-DskipTests=false",
                "-Dmaven.test.skip=false",
                "-Dtest.enforcement.enabled=true",
                "-Dmaven.test.failure.ignore=true",
                "-Dsurefire.timeout=900",
                "-DskipPitest=true",
                "initialize",
            ],
            "evidence": {
                "changedClasses": ["com.demo.PunchBizImpl"],
                "filteredTargetClasses": ["com.demo.PunchBizImpl"],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest())

    repo_task = service.task_manager.get_task(response["session"]["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert selection["class_fqns"] == ["com.demo.PunchBizImpl"]
    assert "verify" in selection["quality_gate_command"]
    assert "initialize" not in selection["quality_gate_command"]
    assert "-DskipPitest=true" not in selection["quality_gate_command"]


def test_java_fix_session_includes_compile_failing_selected_test_owner(tmp_path):
    service = _service_with_repair(tmp_path)
    repo = tmp_path / "workspace" / "repo"
    _init_java_diff_repo(repo)
    record = CiTaskRecord(
        task_id="task-java-selected-test-compile-failure",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(repo),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "summary": "UTA test-enforcement failed because Maven build did not compile or resolve",
            "command": [
                "mvn",
                "-Dtest.enforcement.enabled=true",
                "verify",
                "-DtargetTests=com.demo.DemandOrderBizTest,com.demo.OrderQueryServiceTest",
                "-Dtest=DemandOrderBizTest,OrderQueryServiceTest",
            ],
            "stdout": (
                "[ERROR] COMPILATION ERROR :\n"
                "[ERROR] /workspace/biz/src/test/java/com/demo/DemandOrderBizTest.java:[84,41] "
                "error: cannot find symbol\n"
                "  symbol:   method queryAiCallInfo(String,String)\n"
                "  location: variable biz of type DemandOrderBiz\n"
                "Running com.demo.OrderQueryServiceTest\n"
                "Tests run: 8, Failures: 0, Errors: 0, Skipped: 0\n"
            ),
            "evidence": {
                "changedClasses": [
                    "com.demo.DemandOrderBiz",
                    "com.demo.OrderQueryService",
                ],
                "filteredTargetClasses": ["com.demo.OrderQueryService"],
                "targetTests": [
                    "com.demo.DemandOrderBizTest",
                    "com.demo.OrderQueryServiceTest",
                ],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest())

    repo_task = service.task_manager.get_task(response["session"]["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert selection["class_fqns"] == [
        "com.demo.DemandOrderBiz",
        "com.demo.OrderQueryService",
    ]


def test_java_fix_session_uses_filtered_maven_targets_before_raw_changed_classes(tmp_path):
    service = _service_with_repair(tmp_path)
    repo = tmp_path / "workspace" / "repo"
    _init_java_diff_repo(repo)
    record = CiTaskRecord(
        task_id="task-java-filtered-targets",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(repo),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "evidence": {
                "changedClasses": [
                    "com.demo.PunchBiz",
                    "com.demo.PunchBizImpl",
                    "com.demo.RestPrecheckVO",
                    "com.demo.FaceService",
                    "com.demo.PdaPunchController",
                ],
                "filteredTargetClasses": [
                    "com.demo.FaceService",
                    "com.demo.PunchBizImpl",
                ],
                "targetTests": ["com.demo.PunchBizImplTest"],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest())

    repo_task = service.task_manager.get_task(response["session"]["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert selection["class_fqns"] == ["com.demo.FaceService", "com.demo.PunchBizImpl"]


def test_java_fix_session_targets_failing_pit_baseline_test_owner(tmp_path):
    service = _service_with_repair(tmp_path)
    repo = tmp_path / "workspace" / "repo"
    _init_java_diff_repo(repo)
    record = CiTaskRecord(
        task_id="task-java-pit-baseline-failure",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(repo),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "summary": "UTA test-enforcement failed because PIT baseline tests were not green",
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "stdout": (
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=2 [com.demo.FaceService*,com.demo.PunchBizImpl*]\n"
                "[ERROR] com.demo.PunchBizImplTest.handlesEmptyMapping:631 expected [null] but found [x]\n"
                "[ERROR] Failed to execute goal org.pitest:pitest-maven:1.15.0:mutationCoverage "
                "(pitest) on project demo.biz: 1 tests did not pass without mutation when calculating "
                "line coverage. Mutation testing requires a green suite.\n"
            ),
            "stderr": "PIT >> SEVERE : Tests failing without mutation:",
            "evidence": {
                "changedClasses": [
                    "com.demo.PunchBiz",
                    "com.demo.PunchBizImpl",
                    "com.demo.FaceService",
                ],
                "filteredTargetClasses": [
                    "com.demo.FaceService",
                    "com.demo.PunchBizImpl",
                ],
                "targetTests": ["com.demo.PunchBizImplTest"],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest())

    repo_task = service.task_manager.get_task(response["session"]["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert selection["class_fqns"] == ["com.demo.PunchBizImpl"]


def test_java_fix_session_targets_failing_pit_baseline_from_surefire_evidence(tmp_path):
    service = _service_with_repair(tmp_path)
    repo = tmp_path / "workspace" / "repo"
    _init_java_diff_repo(repo)
    record = CiTaskRecord(
        task_id="task-java-pit-baseline-surefire-evidence",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(repo),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "summary": "UTA test-enforcement failed because PIT baseline tests were not green",
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "stdout": (
                "[ERROR] 1 tests did not pass without mutation when calculating line coverage. "
                "Mutation testing requires a green suite.\n"
            ),
            "evidence": {
                "changedClasses": ["com.demo.PunchBizImpl", "com.demo.FaceService"],
                "filteredTargetClasses": ["com.demo.FaceService", "com.demo.PunchBizImpl"],
                "failedSurefireTests": [
                    {
                        "className": "com.demo.PunchBizImplTest",
                        "testName": "com.demo.PunchBizImplTest.handlesEmptyMapping",
                        "message": "java.lang.AssertionError: expected [null] but found [x]",
                    }
                ],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest())

    repo_task = service.task_manager.get_task(response["session"]["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert selection["class_fqns"] == ["com.demo.PunchBizImpl"]


def test_repair_context_renders_failed_surefire_evidence():
    rendered = render_context_markdown(
        {
            "pipeline": {"appName": "demo", "branch": "feature/TASK-1"},
            "enforcement": {
                "status": "failed",
                "summary": "UTA test-enforcement failed because PIT baseline tests were not green",
                "command": ["mvn", "verify"],
                "evidence": {
                    "filteredTargetClasses": ["com.demo.PunchBizImpl"],
                    "targetTests": ["com.demo.PunchBizImplTest"],
                    "failedSurefireTests": [
                        {
                            "className": "com.demo.PunchBizImplTest",
                            "testName": "com.demo.PunchBizImplTest.handlesEmptyMapping",
                            "summary": "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0",
                            "message": "java.lang.AssertionError: expected [null] but found [x]",
                            "sourceLocation": "PunchBizImplTest.java:631",
                            "reportPath": "biz/target/surefire-reports/com.demo.PunchBizImplTest.txt",
                        }
                    ],
                },
            },
            "git": {},
            "issue": {},
            "user": {},
            "sources": [],
            "missingReasons": [],
        }
    )

    assert "### Failed Surefire tests" in rendered
    assert "com.demo.PunchBizImplTest.handlesEmptyMapping" in rendered
    assert "expected [null] but found [x]" in rendered
    assert "PunchBizImplTest.java:631" in rendered
    assert "com.demo.PunchBizImpl" in rendered


def test_java_fix_session_derives_filtered_targets_from_legacy_maven_stdout(tmp_path):
    service = _service_with_repair(tmp_path)
    repo = tmp_path / "workspace" / "repo"
    _init_java_diff_repo(repo)
    record = CiTaskRecord(
        task_id="task-java-legacy-filtered-targets",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(repo),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "java",
            "backend": "maven_enforcer",
            "command": ["mvn", "-Dtest.enforcement.enabled=true", "verify"],
            "stdout": (
                "[info] [test-enforcer] refs/remotes/origin/master -> target/filtered.diff, "
                "pitest.targets=2 [com.demo.FaceService*,com.demo.PunchBizImpl*]\n"
            ),
            "evidence": {
                "changedClasses": [
                    "com.demo.PunchBiz",
                    "com.demo.PunchBizImpl",
                    "com.demo.RestPrecheckVO",
                    "com.demo.FaceService",
                    "com.demo.PdaPunchController",
                ],
                "targetTests": ["com.demo.PunchBizImplTest"],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest())

    repo_task = service.task_manager.get_task(response["session"]["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert selection["class_fqns"] == ["com.demo.FaceService", "com.demo.PunchBizImpl"]


def test_fix_session_creates_python_repair_task_with_target_refs(tmp_path, monkeypatch):
    monkeypatch.setattr("uta.app.service.uta_settings.ci_python_diff_mutation_gate", 95)
    service = _service_with_repair(tmp_path)
    record = CiTaskRecord(
        task_id="task-python",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "py-app",
                "gitUrl": "git@git.example.com:group/py-app.git",
                "branch": "feature/TASK-82767",
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
                "changedLines": {"jobs/forecast.py": [4, 7]},
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
    assert repo_task["mutation_gate"] == 95


def test_python_fix_session_rejects_missing_changed_lines_context(tmp_path):
    service = _service_with_repair(tmp_path)
    record = CiTaskRecord(
        task_id="task-python-no-changed-lines",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "py-app",
                "gitUrl": "git@git.example.com:group/py-app.git",
                "branch": "feature/TASK-82767",
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
                    {
                        "target": {
                            "target_id": "pyfile:jobs/forecast.py",
                            "source_path": "jobs/forecast.py",
                        },
                        "status": "failed",
                        "reasonCode": "mutation_gate_failed",
                        "mutation": {"passed": False, "rate": 87.0},
                    }
                ],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    with pytest.raises(FixSessionUnsupportedError, match="changedLines evidence"):
        service.create_fix_session(record, CreateFixSessionRequest())

    assert record.fix_sessions == []
    assert service.task_manager.list_tasks(limit=10) == []


def test_java_fix_session_refreshes_remote_branch_without_precreation_enforcer_rerun(tmp_path):
    git_log = tmp_path / "git-calls.jsonl"
    git = fake_git(tmp_path, log=git_log, rev="fresh-head")
    runner_paths = []

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    class PassingRunner:
        def run(self, repo_path):
            runner_paths.append(repo_path)
            return QualityGateResult(
                status=QualityGateStatus.passed,
                passed=True,
                command=["mvn", "verify"],
                stdout="Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%",
                summary="UTA test-enforcement passed after branch refresh",
            )

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces", git_bin=git),
        enforcement_runner=PassingRunner(),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([RdcProtocol()]),
    )
    record = CiTaskRecord(
        task_id="task-stale",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(tmp_path / "workspace" / "repo"),
        enforcement_result={"status": "failed", "passed": False, "summary": "stale failed report"},
    )
    service._tasks[record.task_id] = record

    response = service.create_fix_session(record, CreateFixSessionRequest(target_ids=["class:com.example.Foo"]))

    session = response["session"]
    repo_task = service.task_manager.get_task(session["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert session["status"] == "repair_task_created"
    assert session["refreshedHead"] == "fresh-head"
    assert runner_paths == []
    assert record.status == CiTaskStatus.failed
    assert selection["quality_gate_backend"] == "maven_enforcer"
    assert selection["class_fqns"] == ["com.example.Foo"]
    refreshed = [argv for argv in calls(git_log) if argv[:2] == ["-C", str(tmp_path / "workspace" / "repo")]]
    assert any(argv[2] == "fetch" for argv in refreshed)
    assert any(argv[2] == "reset" for argv in refreshed)


def test_fix_session_creation_defers_slow_workspace_refresh(tmp_path):
    refresh_started = threading.Event()
    release_refresh = threading.Event()

    class SlowRefreshWorkspace:
        #: No git workspace: this double never prepares a checkout, so
        #: commit-message context has nothing to read and says so.
        workspace = None

        def refresh_branch(self, repo_path, *, branch):
            refresh_started.set()
            assert release_refresh.wait(timeout=5)
            return "fresh-head"

    service = ApiTriggerService(
        workspace_manager=SlowRefreshWorkspace(),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd,
                1,
                stdout=FIXABLE_GATE_FAILURE,
                stderr="",
            ),
        ),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([RdcProtocol()]),
        async_repair_task_creation=True,
    )
    record = CiTaskRecord(
        task_id="task-slow-create",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "java-app",
                "gitUrl": "git@git.example.com:group/java-app.git",
                "branch": "feature/TASK-82767",
                "language": "java",
            }
        ),
        workspace_path=str(tmp_path / "workspace" / "repo"),
        enforcement_result={"status": "failed", "passed": False, "summary": FIXABLE_GATE_FAILURE},
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest(target_ids=["class:com.example.Foo"]))

    session = response["session"]
    assert session["status"] == "creating_repair_task"
    assert session["repoTaskStage"] == "refresh_workspace"
    assert "repoTaskId" not in session
    assert refresh_started.wait(timeout=5)

    progress = service.repair_progress(record, session["sessionId"])
    assert progress["repoTask"] is None
    assert progress["stages"][0]["status"] == "active"
    html = CiReportRenderer().repair_progress_html(progress)
    assert "正在创建修复任务" in html
    assert "刷新修复工作区" in html

    release_refresh.set()
    service.wait_for_deferred_repair_tasks(timeout=5)
    assert session["status"] == "repair_task_created"
    assert session["repoTaskId"]


def test_python_fix_session_refreshes_remote_branch_without_precreation_enforcer_rerun(tmp_path):
    git_log = tmp_path / "git-calls.jsonl"
    git = fake_git(tmp_path, log=git_log, rev="fresh-python-head")
    runner_paths = []

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    class FailedPythonRunner:
        def run(self, repo_path):
            runner_paths.append(repo_path)
            return QualityGateResult(
                status=QualityGateStatus.failed,
                passed=False,
                command=["uta", "python-enforce"],
                returncode=1,
                summary="Python enforcement failed after branch refresh",
                language="python",
                backend="python_enforcer",
                evidence={
                    "language": "python",
                    "backend": "python_enforcer",
                    "status": "failed",
                    "passed": False,
                    "targetResults": [
                        {
                            "status": "failed",
                            "reasonCode": "mutation_gate_failed",
                            "target": {
                                "language": "python",
                                "target_id": "pyfile:pipecat/security/output_guard.py",
                                "source_path": "pipecat/security/output_guard.py",
                            },
                            "coverage": {"passed": True, "rate": 100.0},
                            "mutation": {"passed": False, "rate": 86.0},
                        }
                    ],
                },
            )

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces", git_bin=git),
        python_enforcement_runner=FailedPythonRunner(),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([RdcProtocol()]),
    )
    record = CiTaskRecord(
        task_id="task-python-stale",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "py-app",
                "gitUrl": "git@git.example.com:group/py-app.git",
                "branch": "feature/TASK-82767",
                "language": "python",
            }
        ),
        workspace_path=str(tmp_path / "workspace" / "repo"),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "python",
            "backend": "python_enforcer",
            "returncode": 1,
            "summary": "Original Python enforcement failed",
            "evidence": {
                "language": "python",
                "backend": "python_enforcer",
                "status": "failed",
                "passed": False,
                "changedLines": {"pipecat/security/output_guard.py": [12, 13]},
                "targetResults": [
                    {
                        "status": "failed",
                        "reasonCode": "mutation_gate_failed",
                        "target": {
                            "language": "python",
                            "target_id": "pyfile:pipecat/security/output_guard.py",
                            "source_path": "pipecat/security/output_guard.py",
                        },
                        "coverage": {"passed": True, "rate": 100.0},
                        "mutation": {"passed": False, "rate": 86.0},
                    }
                ],
            },
        },
    )
    service._tasks[record.task_id] = record

    response = service.create_fix_session(record, CreateFixSessionRequest())

    session = response["session"]
    repo_task = service.task_manager.get_task(session["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert session["status"] == "repair_task_created"
    assert session["refreshedHead"] == "fresh-python-head"
    assert "refreshRerunEnforcement" not in session
    assert runner_paths == []
    assert record.enforcement_result["summary"] == "Original Python enforcement failed"
    assert selection["targets"][0]["target_id"] == "pyfile:pipecat/security/output_guard.py"
    assert selection["targets"][0]["source_path"] == "pipecat/security/output_guard.py"
    assert selection["quality_gate_backend"] == "python_enforcer"
    refreshed = [argv for argv in calls(git_log) if argv[:2] == ["-C", str(tmp_path / "workspace" / "repo")]]
    assert any(argv[2] == "fetch" for argv in refreshed)
    assert any(argv[2] == "reset" for argv in refreshed)


def test_python_fix_session_rejects_empty_repair_targets_after_refresh(tmp_path):
    runner_paths = []
    git = fake_git(tmp_path, rev="fresh-python-head")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    class FailedPythonRunnerWithoutEvidence:
        def run(self, repo_path):
            runner_paths.append(repo_path)
            return QualityGateResult(
                status=QualityGateStatus.failed,
                passed=False,
                command=["uta", "python-enforce"],
                returncode=-15,
                summary="Python enforcement did not produce UTA evidence",
                language="python",
                backend="python_enforcer",
                evidence=None,
            )

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces", git_bin=git),
        python_enforcement_runner=FailedPythonRunnerWithoutEvidence(),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([RdcProtocol()]),
    )
    record = CiTaskRecord(
        task_id="task-python-empty",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "py-app",
                "gitUrl": "git@git.example.com:group/py-app.git",
                "branch": "feature/TASK-82767",
                "language": "python",
            }
        ),
        workspace_path=str(tmp_path / "workspace" / "repo"),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "python",
            "backend": "python_enforcer",
            "evidence": None,
        },
    )
    service._tasks[record.task_id] = record

    with pytest.raises(FixSessionUnsupportedError, match="no safe repair target files"):
        service.create_fix_session(record, CreateFixSessionRequest())

    assert runner_paths == []
    assert record.fix_sessions == []
    assert service.task_manager.list_tasks(limit=10) == []


def test_python_fix_session_repairs_only_failed_target_results(tmp_path):
    service = _service_with_repair(tmp_path)
    record = CiTaskRecord(
        task_id="task-python-failed-only",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "py-app",
                "gitUrl": "git@git.example.com:group/py-app.git",
                "branch": "feature/TASK-82767",
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
                "changedLines": {
                    "chat_robot/service/fine_tuning_service.py": [8, 9],
                    "chat_robot/views.py": [20],
                },
                "targetResults": [
                    {
                        "target": {"target_id": "pyfile:chat_robot/apps.py", "source_path": "chat_robot/apps.py"},
                        "status": "passed",
                        "reasonCode": "passed",
                        "coverage": {"passed": True, "rate": 100.0},
                        "mutation": {"passed": True, "rate": 100.0},
                    },
                    {
                        "target": {
                            "target_id": "pyfile:chat_robot/service/fine_tuning_service.py",
                            "source_path": "chat_robot/service/fine_tuning_service.py",
                        },
                        "status": "failed",
                        "reasonCode": "mutation_gate_failed",
                        "coverage": {"passed": True, "rate": 96.05},
                        "mutation": {"passed": False, "rate": 36.35},
                    },
                    {
                        "target": {"target_id": "pyfile:chat_robot/views.py", "source_path": "chat_robot/views.py"},
                        "status": "failed",
                        "reasonCode": "coverage_gate_failed",
                        "coverage": {"passed": False, "rate": 77.92},
                    },
                ],
                "changedProductionFiles": [
                    "chat_robot/apps.py",
                    "chat_robot/service/fine_tuning_service.py",
                    "chat_robot/views.py",
                ],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(record, CreateFixSessionRequest())

    repo_task = service.task_manager.get_task(response["session"]["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert [target["target_id"] for target in selection["targets"]] == [
        "pyfile:chat_robot/service/fine_tuning_service.py",
        "pyfile:chat_robot/views.py",
    ]


def test_python_explicit_fix_target_does_not_expand_to_other_gate_failures(tmp_path):
    service = _service_with_repair(tmp_path)
    record = CiTaskRecord(
        task_id="task-python-explicit-scope",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-app",
            git_url="git@example.com:group/py-app.git",
            branch="feature/TASK-1",
            language="python",
        ),
        workspace_path=str(tmp_path / "workspace" / "repo"),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "python",
            "backend": "python_enforcer",
            "evidence": {
                "changedLines": {"src/selected.py": [3], "src/other.py": [7]},
                "targetResults": [
                    {
                        "target": {"target_id": "pyfile:src/selected.py", "source_path": "src/selected.py"},
                        "coverage": {"passed": True},
                        "mutation": {"passed": False},
                    },
                    {
                        "target": {"target_id": "pyfile:src/other.py", "source_path": "src/other.py"},
                        "coverage": {"passed": False},
                        "mutation": {"passed": True},
                    },
                ],
            },
        },
    )
    service._tasks[record.task_id] = record
    service.save(record)

    response = service.create_fix_session(
        record,
        CreateFixSessionRequest(target_ids=["pyfile:src/selected.py"]),
    )

    repo_task = service.task_manager.get_task(response["session"]["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert [target["target_id"] for target in selection["targets"]] == ["pyfile:src/selected.py"]


def test_report_links_to_fix_session_progress_page(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]
    session = client.post(
        f"/reports/{task_id}/fix-sessions",
        json={"targetIds": ["class:com.example.Foo"]},
    ).json()["session"]
    record = service.get(task_id)
    record.fix_sessions[0]["status"] = "green"
    record.fix_sessions[0]["rerunEnforcement"] = {
        "passed": True,
        "status": "passed",
        "summary": "UTA test-enforcement passed",
        "command": ["mvn", "-U", "-Dtest.enforcement.enabled=true", "verify"],
            "stdout": (
                "[INFO] diff line coverage 97.50% passed for example-maven-plugins (39/40)\n"
                "[INFO] PIT generated=4 killed=4 survived=0 test-strength=100%\n"
                "[INFO] [test-enforcer] diff mutation score 100.00% passed for example-maven-plugins "
                "(4/4 detected; 0 survived; 0 no coverage excluded)"
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
    assert 'http-equiv="refresh"' not in progress_response.text
    # The SSE panel was removed: it carried only agent progress, so it read as
    # a thinner duplicate of the events table beneath it. The table is now the
    # single feed and polls for updates.
    assert 'id="events-body"' in progress_response.text
    assert "setInterval(refresh" in progress_response.text
    assert "进度更新: 实时" in progress_response.text
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
    service = ApiTriggerService()
    record = CiTaskRecord(
        task_id="task-rate-limit",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
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
        "13:20:45 [uta] ERROR: RDC repair auto-push found no test changes to commit\n",
        encoding="utf-8",
    )

    issues = RepairSessions._repair_issues_from_repo_task(
        {
            "run_log_path": str(run_log),
            "current_stage": "quarantined",
            "error": "Auto-quarantined after 2 failures. Last: RDC repair auto-push found no test changes to commit",
            "last_error": "Auto-quarantined after 2 failures. Last: RDC repair auto-push found no test changes to commit",
            "current_detail": "Auto-quarantined after 2 failures. Last: RDC repair auto-push found no test changes to commit",
        },
        {
            "latest_events": [
                {
                    "stage": "push",
                    "message": "20260526-TASK-40967: RDC repair auto-push found no test changes to commit",
                }
            ]
        },
    )

    assert any(issue["type"] == "provider_rate_limit" for issue in issues)
    assert any("Planning hit provider/model rate limit" in issue["message"] for issue in issues)
    assert any(issue["type"] == "generation_timeout" for issue in issues)
    assert any(issue["type"] == "no_test_changes" for issue in issues)


def test_repair_progress_stages_ignore_events_before_latest_resume():
    stages = RepairSessions._repair_progress_stages(
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
    stages = RepairSessions._repair_progress_stages(
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
    stages = RepairSessions._repair_progress_stages(
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
    # Commit-message context reads the checkout through the workspace, so the
    # fake git answers `log` for it.
    git = fake_git(tmp_path, responses={"log": "TASK-82768 implement QGP flow\x1e"})

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=FIXABLE_GATE_FAILURE, stderr="")

    class FakeJiraClient:
        def fetch_description(self, jira_id):
            assert jira_id == "TASK-82768"
            return "Jira describes QGP selection changes"

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(workspace_root=tmp_path / "workspaces", git_bin=git),
        enforcement_runner=MavenEnforcementRunner(
            command="mvn -Dtest.enforcement.enabled=true verify",
            run_command=fake_run,
        ),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([RdcProtocol(context_provider=RdcContextProvider(FakeJiraClient()))]),
    )
    client = TestClient(create_app(service))
    payload = _payload()
    payload["attribute"]["branch"] = "TASK-82768-20260515"
    task_id = client.post("/api/v1/rdc/trigger", json=payload).json()["data"]["taskId"]

    client.post(f"/reports/{task_id}/fix-sessions", json={"targetIds": ["class:com.example.Foo"]})
    detail = client.get(f"/reports/{task_id}/detail").json()

    assert detail["jiraId"] == "TASK-82768"
    assert detail["context"]["missingReasons"] == []
    sources = {source["name"]: source for source in detail["context"]["sources"]}
    assert sources["issue_description"]["available"] is True
    assert sources["git_commit_messages"]["available"] is True


def test_terminal_fix_session_report_enriches_saved_rdc_context(tmp_path):
    class FakeJiraClient:
        def fetch_description(self, jira_id):
            assert jira_id == "TASK-82768"
            return "Jira description fetched during report refresh"

    service = ApiTriggerService(
        task_manager=TaskManager(tmp_path / "tasks.db"),
        protocols=ProtocolRegistry([RdcProtocol(context_provider=RdcContextProvider(FakeJiraClient()))]),
    )
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="TASK-82768-20260515",
        rdc_context={
            "pipeline": {"branch": "TASK-82768-20260515"},
            "git": {"commitMessages": ["TASK-82768 update unit test gate"]},
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
                "branch": "TASK-82768-20260515",
            }
        ),
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
        stdout="Diff coverage: 100%\n" + DIFF_MUTATION_OK,
    )
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="TASK-82768-20260515",
    )
    service.task_manager.mark_completed(repo_task_id, message="resumed task completed")
    record = CiTaskRecord(
        task_id="task-old",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "TASK-82768-20260515",
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
    service = ApiTriggerService(task_manager=TaskManager(tmp_path / "tasks.db"))
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
        branch_name="feature/TASK-82767",
        coverage_gate=95.0,
        mutation_gate=95.0,
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
                "mutation_score": 95.4545,
                "surviving_mutants": 8,
                "total_mutants": 1321,
                "test_file_path": "tests/uta_generated/test_app.py",
            }
        },
        targets=[target],
    )
    service.task_manager.record_push_verified(
        repo_task_id,
        branch_name="feature/TASK-82767",
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
                "branch": "feature/TASK-82767",
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
    assert refreshed.enforcement_result["evidence"]["mutation"]["survived"] == 8
    assert refreshed.enforcement_result["evidence"]["mutation"]["rate"] == 95.4545
    assert session["status"] == "green"
    assert session["repoTaskStatus"] == "COMPLETED"
    assert session["rerunEnforcement"]["summary"] == "Python enforcement passed from completed repair task evidence"


def test_green_python_repair_task_remains_green_when_branch_moved_later(tmp_path):
    remote_head_checks: list[tuple[object, str]] = []

    class MovedBranchWorkspace:
        def remote_branch_head(self, repo_path, *, branch):
            remote_head_checks.append((repo_path, branch))
            return "new-head"

    service = ApiTriggerService(
        workspace_manager=MovedBranchWorkspace(),
        task_manager=TaskManager(tmp_path / "tasks.db"),
    )
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
        branch_name="feature/TASK-82767",
        coverage_gate=95.0,
        mutation_gate=95.0,
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
                "total_mutants": 3,
                "test_file_path": "tests/uta_generated/test_app.py",
            }
        },
        targets=[target],
    )
    service.task_manager.record_push_verified(
        repo_task_id,
        branch_name="feature/TASK-82767",
        local_head="old-head",
        remote_head="old-head",
    )
    service.task_manager.mark_completed(repo_task_id, message="repair finished")
    record = CiTaskRecord(
        task_id="task-python-green",
        status=CiTaskStatus.success,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "py-app",
                "gitUrl": "git@git.example.com:group/py-app.git",
                "branch": "feature/TASK-82767",
                "language": "python",
            }
        ),
        enforcement_result={
            "status": "passed",
            "passed": True,
            "language": "python",
            "backend": "python_enforcer",
            "summary": "Python enforcement passed from completed repair task evidence",
        },
        fix_sessions=[
            {
                "sessionId": "fix-stale",
                "status": "green",
                "repoTaskId": repo_task_id,
                "rerunEnforcement": {
                    "status": "passed",
                    "passed": True,
                    "summary": "Python enforcement passed from completed repair task evidence",
                },
            }
        ],
    )
    service._tasks[record.task_id] = record

    refreshed = service.get(record.task_id)

    session = refreshed.fix_sessions[0]
    assert refreshed.status == CiTaskStatus.success
    assert refreshed.summary is None
    assert refreshed.enforcement_result["status"] == "passed"
    assert refreshed.enforcement_result["passed"] is True
    assert session["status"] == "green"
    assert session["repoTaskStatus"] == "COMPLETED"
    assert session["rerunEnforcement"]["status"] == "passed"
    assert remote_head_checks == []


def test_completed_python_repair_task_is_accepted_when_branch_moved_before_refresh(monkeypatch, tmp_path):
    remote_head_checks: list[tuple[object, str]] = []

    class MovedBranchWorkspace:
        def remote_branch_head(self, repo_path, *, branch):
            remote_head_checks.append((repo_path, branch))
            return "new-head"

    service = ApiTriggerService(
        workspace_manager=MovedBranchWorkspace(),
        task_manager=TaskManager(tmp_path / "tasks.db"),
    )
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
        branch_name="feature/TASK-82767",
        coverage_gate=95.0,
        mutation_gate=95.0,
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
                "total_mutants": 3,
                "test_file_path": "tests/uta_generated/test_app.py",
            }
        },
        targets=[target],
    )
    service.task_manager.record_push_verified(
        repo_task_id,
        branch_name="feature/TASK-82767",
        local_head="old-head",
        remote_head="old-head",
    )
    service.task_manager.mark_completed(repo_task_id, message="repair finished")
    record = CiTaskRecord(
        task_id="task-python-stale",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "py-app",
                "gitUrl": "git@git.example.com:group/py-app.git",
                "branch": "feature/TASK-82767",
                "language": "python",
            }
        ),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "python",
            "backend": "python_enforcer",
            "summary": "Python enforcement failed before repair",
        },
        fix_sessions=[
            {
                "sessionId": "fix-stale",
                "status": "repair_completed",
                "repoTaskId": repo_task_id,
            }
        ],
    )
    service._tasks[record.task_id] = record
    callbacks = []

    def fake_report(record, passed, summary):
        callbacks.append((record.task_id, passed, summary))

    monkeypatch.setattr(service, "_report_result", fake_report)

    refreshed = service.get(record.task_id)

    assert refreshed.status == CiTaskStatus.success
    assert refreshed.fix_sessions[0]["status"] == "green"
    assert refreshed.summary == "Python enforcement passed from completed repair task evidence"
    assert refreshed.enforcement_result["status"] == "passed"
    assert refreshed.enforcement_result["passed"] is True
    assert refreshed.fix_sessions[0]["repoTaskStatus"] == "COMPLETED"
    assert callbacks == [
        (
            "task-python-stale",
            True,
            "Python enforcement passed from completed repair task evidence",
        )
    ]
    assert refreshed.fix_sessions[0]["terminalCallbackReportedKey"] == "success"
    assert remote_head_checks == []


def test_repair_callback_marker_is_written_after_report_attempt(monkeypatch):
    service = ApiTriggerService()
    record = CiTaskRecord(
        task_id="task-callback-retry",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
                "language": "python",
            }
        ),
    )
    session = {"sessionId": "fix-callback-retry"}

    def fail_report(record, passed, summary):
        raise RuntimeError("callback store failed")

    monkeypatch.setattr(service, "_report_result", fail_report)

    with pytest.raises(RuntimeError, match="callback store failed"):
        service.repair._report_repair_result_once(record, session, False, "repair session ended (repair_stale)")

    assert "terminalCallbackReportedKey" not in session
    assert "terminalCallbackReportedAt" not in session


def test_fix_session_refresh_preserves_failed_status_for_poisoned_repo_task(tmp_path):
    service = ApiTriggerService(task_manager=TaskManager(tmp_path / "tasks.db"))
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="TASK-82768-20260515",
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
                "branch": "TASK-82768-20260515",
            }
        ),
        fix_sessions=[{"sessionId": "fix-poisoned", "status": "repair_failed", "repoTaskId": repo_task_id}],
    )
    service._tasks[record.task_id] = record

    refreshed = service.get("task-poisoned")

    assert refreshed.fix_sessions[0]["status"] == "repair_failed"
    assert refreshed.fix_sessions[0]["repoTaskStatus"] == "POISONED"


def test_fix_session_terminal_failure_reports_callback_once(monkeypatch, tmp_path):
    service = ApiTriggerService(task_manager=TaskManager(tmp_path / "tasks.db"))
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="TASK-82768-20260515",
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
                "branch": "TASK-82768-20260515",
            }
        ),
        fix_sessions=[{"sessionId": "fix-poisoned", "status": "repairing", "repoTaskId": repo_task_id}],
    )
    service._tasks[record.task_id] = record
    calls = []

    def fake_report(record, passed, summary):
        calls.append((record.task_id, passed, summary))

    monkeypatch.setattr(service, "_report_result", fake_report)

    first = service.get("task-poisoned")
    second = service.get("task-poisoned")

    assert calls == [("task-poisoned", False, "repair session ended (repair_failed)")]
    assert first.fix_sessions[0]["terminalCallbackReportedAt"]
    assert second.fix_sessions[0]["terminalCallbackReportedAt"] == first.fix_sessions[0]["terminalCallbackReportedAt"]


def test_fix_session_refresh_ends_stopped_duplicate_repo_task(tmp_path):
    service = ApiTriggerService(task_manager=TaskManager(tmp_path / "tasks.db"))
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        class_fqns=["com.example.Foo"],
        branch_name="TASK-82768-20260515",
    )
    service.task_manager.mark_stopped(
        repo_task_id,
        reason="duplicate stale repair task",
        stage="stopped",
    )
    record = CiTaskRecord(
        task_id="task-stopped",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "TASK-82768-20260515",
            }
        ),
        fix_sessions=[{"sessionId": "fix-stopped", "status": "repairing", "repoTaskId": repo_task_id}],
    )
    service._tasks[record.task_id] = record

    refreshed = service.get("task-stopped")

    assert refreshed.fix_sessions[0]["status"] == "repair_failed"
    assert refreshed.fix_sessions[0]["repoTaskStatus"] == "STOPPED"
    assert refreshed.fix_sessions[0]["repoTaskDetail"] == "duplicate stale repair task"


def test_fix_session_rerun_running_completed_task_does_not_launch_duplicate_rerun(tmp_path):
    calls = []

    class PassingRunner:
        def run(self, repo_path):
            calls.append(repo_path)
            return QualityGateResult(
                status=QualityGateStatus.passed,
                passed=True,
                command=["mvn", "verify"],
                stdout="Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%",
                summary="UTA test-enforcement passed",
            )

    service = ApiTriggerService(
        enforcement_runner=PassingRunner(),
        task_manager=TaskManager(tmp_path / "tasks.db"),
    )
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = service.task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="TASK-82768-20260515",
    )
    service.task_manager.mark_completed(repo_task_id, message="resumed task completed")
    record = CiTaskRecord(
        task_id="task-old",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "TASK-82768-20260515",
            }
        ),
        fix_sessions=[{"sessionId": "fix-old", "status": "rerun_running", "repoTaskId": repo_task_id}],
    )
    service._tasks[record.task_id] = record

    refreshed = service.get("task-old")

    assert refreshed.status == CiTaskStatus.failed
    assert refreshed.fix_sessions[0]["status"] == "rerun_running"
    assert refreshed.fix_sessions[0]["repoTaskStatus"] == "COMPLETED"
    assert calls == []


def test_concurrent_completed_repair_refresh_runs_final_enforcement_once(tmp_path):
    calls = []
    entered = threading.Event()
    release = threading.Event()

    class SlowRunner:
        def run(self, repo_path):
            calls.append(repo_path)
            entered.set()
            assert release.wait(timeout=5)
            return QualityGateResult(
                status=QualityGateStatus.passed,
                passed=True,
                command=["mvn", "verify"],
                stdout="Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%",
                summary="UTA test-enforcement passed",
            )

    task_manager = TaskManager(tmp_path / "tasks.db")
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo_task_id = task_manager.create_task(
        repo_path=str(repo_path),
        branch_name="TASK-82768-20260515",
    )
    task_manager.mark_completed(repo_task_id, message="repair completed")

    def make_record():
        return CiTaskRecord(
            task_id="task-concurrent",
            status=CiTaskStatus.failed,
            request=CiTriggerRequest.model_validate(
                {
                    "appName": "demo-app",
                    "gitUrl": "git@git.example.com:group/demo.git",
                    "branch": "TASK-82768-20260515",
                }
            ),
            fix_sessions=[{"sessionId": "fix-concurrent", "status": "repair_completed", "repoTaskId": repo_task_id}],
        )

    runner = SlowRunner()
    first_service = ApiTriggerService(enforcement_runner=runner, task_manager=task_manager)
    second_service = ApiTriggerService(enforcement_runner=runner, task_manager=task_manager)
    first_record = make_record()
    second_record = make_record()
    first_service._tasks[first_record.task_id] = first_record
    second_service._tasks[second_record.task_id] = second_record

    first_thread = threading.Thread(target=lambda: first_service.get(first_record.task_id))
    first_thread.start()
    assert entered.wait(timeout=5)

    second = second_service.get(second_record.task_id)
    release.set()
    first_thread.join(timeout=5)

    assert calls == [repo_path]
    assert second.fix_sessions[0]["status"] == "repair_completed"


def test_repair_progress_marks_rerun_enforcement_active():
    stages = RepairSessions._repair_progress_stages(
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
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]
    body = {"targetIds": ["class:com.example.Foo"], "userContext": "first"}

    first = client.post(f"/reports/{task_id}/fix-sessions", json=body).json()["session"]
    duplicate = client.post(f"/reports/{task_id}/fix-sessions", json=body).json()

    assert duplicate["alreadyRunning"] is True
    assert duplicate["session"]["sessionId"] == first["sessionId"]
    assert duplicate["session"]["repoTaskId"] == first["repoTaskId"]


def test_fix_session_retry_does_not_create_new_task_while_current_task_is_live(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]
    created = client.post(
        f"/reports/{task_id}/fix-sessions",
        json={"targetIds": ["class:com.example.Foo"]},
    ).json()["session"]
    old_repo_task_id = created["repoTaskId"]

    retry = client.post(
        f"/reports/{task_id}/fix-sessions/{created['sessionId']}/retry",
        json={"message": "clicked retry while still running"},
    )

    session = retry.json()["session"]
    assert retry.status_code == 200
    assert session["status"] == "repair_task_created"
    assert session["repoTaskId"] == old_repo_task_id
    assert "repoTaskHistory" not in session
    assert service.task_manager.get_task(old_repo_task_id)["status"] == "CREATED"


def test_fix_session_reuses_active_repair_task_for_duplicate_rdc_record(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    first_payload = _payload()
    first_payload["attribute"].update({"taskId": "rdc-task-1", "recordId": "record-1"})
    second_payload = _payload()
    second_payload["attribute"].update({"taskId": "rdc-task-1", "recordId": "record-2"})

    first_task_id = client.post("/api/v1/rdc/trigger", json=first_payload).json()["data"]["taskId"]
    second_task_id = client.post("/api/v1/rdc/trigger", json=second_payload).json()["data"]["taskId"]
    first = client.post(
        f"/reports/{first_task_id}/fix-sessions",
        json={"targetIds": ["class:com.example.Foo"]},
    ).json()["session"]
    repo_tasks_before_duplicate = len(service.task_manager.list_tasks(limit=10))
    second = client.post(
        f"/reports/{second_task_id}/fix-sessions",
        json={"targetIds": ["class:com.example.Foo"]},
    ).json()["session"]

    assert second["repoTaskId"] == first["repoTaskId"]
    assert second["reusedRepoTaskId"] == first["repoTaskId"]
    assert "deduplicatedRepoTaskId" not in second
    assert len(service.task_manager.list_tasks(limit=10)) == repo_tasks_before_duplicate


def test_fix_session_duplicate_after_rerun_failed_creates_new_task(tmp_path):
    service = _service_with_repair(tmp_path)
    client = TestClient(create_app(service))
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]
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
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]
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
    task_id = client.post("/api/v1/rdc/trigger", json=_payload()).json()["data"]["taskId"]

    first = client.post(f"/reports/{task_id}/fix-sessions", json={"targetIds": ["class:com.example.Foo"]})
    second = client.post(f"/reports/{task_id}/fix-sessions", json={"targetIds": ["class:com.example.Bar"]})

    assert first.status_code == 200
    assert second.status_code == 429


def test_python_completed_repair_result_preserves_coverage_counts_from_latest_report(tmp_path):
    report_path = tmp_path / "summary_python.json"
    report_path.write_text(
        json.dumps(
            {
                "results": {
                    "pyfile:configure/tasks.py": {
                        "coverage_summary": {
                            "covered": 80,
                            "total": 80,
                            "rate": 100.0,
                            "gate": 95.0,
                            "passed": True,
                            "scope": "changed_lines",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeTaskManager:
        def list_class_tasks(self, repo_task_id):
            assert repo_task_id == 159
            return [
                {
                    "status": "PASS",
                    "class_fqn": "pyfile:configure/tasks.py",
                    "target_id": "pyfile:configure/tasks.py",
                    "display_name": "configure/tasks.py",
                    "source_path": "configure/tasks.py",
                    "target_granularity": "file",
                    "coverage_line": 100.0,
                    "mutation_score": 100.0,
                    "total_mutants": 32,
                    "surviving_mutants": 0,
                    "test_file_path": "tests/uta_generated/test_configure_tasks.py",
                }
            ]

    record = CiTaskRecord(
        task_id="addec7bb53154bfc94b0b2c16cedc17f",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="w_ais_gaia",
            git_url="git@git.example.com:aistore/gaia.git",
            branch="feature/TASK-1",
            language="python",
        ),
    )
    result = PythonCiLanguageHandler(None).completed_task_enforcement_result(
        record=record,
        task_manager=FakeTaskManager(),
        repo_task={
            "id": 159,
            "status": "COMPLETED",
            "language": "python",
            "coverage_gate": 95.0,
            "mutation_gate": 95.0,
            "latest_report_path": str(report_path),
        },
    )

    assert result is not None
    coverage = result.evidence["coverage"]
    assert coverage["rate"] == 100.0
    assert coverage["covered"] == 80
    assert coverage["total"] == 80

    html = CiReportRenderer().report_html(record.model_copy(update={"enforcement_result": result.model_dump()}))
    assert "覆盖率: <strong>100.00%</strong>" in html
    assert "(80/80, 1 个目标)" in html
    assert "(0/0, 1 个目标)" not in html


def test_python_completed_scoped_repair_does_not_hide_other_failed_gate_targets():
    class FakeTaskManager:
        def list_class_tasks(self, repo_task_id):
            assert repo_task_id == 160
            return [
                {
                    "status": "PASS",
                    "target_id": "pyfile:src/repaired.py",
                    "display_name": "src/repaired.py",
                    "source_path": "src/repaired.py",
                    "target_granularity": "file",
                    "coverage_line": 100.0,
                    "mutation_score": 100.0,
                    "total_mutants": 4,
                    "surviving_mutants": 0,
                }
            ]

    record = CiTaskRecord(
        task_id="task-python-scoped-repair",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-app",
            git_url="git@example.com:group/py-app.git",
            branch="feature/TASK-1",
            language="python",
        ),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "python",
            "backend": "python_enforcer",
            "evidence": {
                "changedLines": {"src/repaired.py": [1], "src/still_failing.py": [2]},
                "targetResults": [
                    {
                        "target": {"target_id": "pyfile:src/repaired.py", "source_path": "src/repaired.py"},
                        "coverage": {"passed": True},
                        "mutation": {"passed": False},
                    },
                    {
                        "target": {"target_id": "pyfile:src/still_failing.py", "source_path": "src/still_failing.py"},
                        "coverage": {"passed": False},
                        "mutation": {"passed": True},
                    },
                ],
            },
        },
    )

    result = PythonCiLanguageHandler(None).completed_task_enforcement_result(
        record=record,
        task_manager=FakeTaskManager(),
        repo_task={
            "id": 160,
            "status": "COMPLETED",
            "language": "python",
            "coverage_gate": 95.0,
            "mutation_gate": 95.0,
        },
    )

    assert result is None


def test_python_sampled_failure_recalculates_full_evidence_before_creating_repair_targets(tmp_path):
    """Sampled diagnostics cannot name repair targets.

    Under CI sampling every target's score comes from a one- or two-mutant
    sample and is reported as diagnostic, so the report-aggregate failure says
    the report missed the gate without saying which files are weak. Deriving
    targets from that sends the agent at whichever file drew an unlucky
    mutant; only the full-cap rerun knows that `src/b.py` is the real one.
    """
    full_runs = []

    class FullProfilePythonRunner:
        def run(self, repo_path):
            raise AssertionError("sampled CI runner must not derive repair targets")

        def run_full(self, repo_path):
            full_runs.append(repo_path)
            return QualityGateResult(
                status=QualityGateStatus.failed,
                passed=False,
                command=["uta", "python-enforce", "--profile", "report_full"],
                returncode=1,
                summary="Full Python mutation verification failed",
                language="python",
                backend="python_enforcer",
                evidence={
                    "language": "python",
                    "backend": "python_enforcer",
                    "status": "failed",
                    "passed": False,
                    "changedLines": {"src/a.py": [3], "src/b.py": [7]},
                    "mutation": {"passed": False, "rate": 90.0, "gate": 95.0},
                    "targetResults": [
                        {
                            "target": {"target_id": "pyfile:src/a.py", "source_path": "src/a.py"},
                            "status": "passed",
                            "reasonCode": "passed",
                            "coverage": {"passed": True, "rate": 100.0},
                            "mutation": {"passed": True, "rate": 100.0},
                        },
                        {
                            "target": {"target_id": "pyfile:src/b.py", "source_path": "src/b.py"},
                            "status": "failed",
                            "reasonCode": "mutation_gate_failed",
                            "coverage": {"passed": True, "rate": 100.0},
                            "mutation": {"passed": False, "rate": 80.0},
                        },
                    ],
                },
            )

    service = ApiTriggerService(
        workspace_manager=GitWorkspaceManager(
            workspace_root=tmp_path / "workspaces", git_bin=fake_git(tmp_path)
        ),
        python_enforcement_runner=FullProfilePythonRunner(),
        task_manager=TaskManager(tmp_path / "tasks.db"),
        context_exporter=RepairContextExporter(tmp_path / "runtime"),
        protocols=ProtocolRegistry([RdcProtocol()]),
    )
    record = CiTaskRecord(
        task_id="task-python-sampled-repair",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-app",
            git_url="git@example.com:group/py-app.git",
            branch="feature/TASK-1",
            language="python",
        ),
        workspace_path=str(tmp_path / "workspace" / "repo"),
        enforcement_result={
            "status": "failed",
            "passed": False,
            "language": "python",
            "backend": "python_enforcer",
            "command": ["uta", "python-enforce"],
            "evidence": {
                "changedLines": {"src/a.py": [3], "src/b.py": [7]},
                "mutation": {
                    "passed": False,
                    "rate": 50.0,
                    "gate": 95.0,
                    "gateScope": "report",
                },
                "mutationBudget": {"scope": "report", "configuredCandidates": 300},
                "targetResults": [
                    {
                        "target": {"target_id": "pyfile:src/a.py", "source_path": "src/a.py"},
                        "status": "passed",
                        "reasonCode": "sampled_mutation_diagnostic",
                        "coverage": {"passed": True, "rate": 100.0},
                        "mutation": {"passed": False, "rate": 0.0},
                    },
                    {
                        "target": {"target_id": "pyfile:src/b.py", "source_path": "src/b.py"},
                        "status": "passed",
                        "reasonCode": "sampled_mutation_diagnostic",
                        "coverage": {"passed": True, "rate": 100.0},
                        "mutation": {"passed": False, "rate": 0.0},
                    },
                ],
            },
        },
    )
    service._tasks[record.task_id] = record

    response = service.create_fix_session(record, CreateFixSessionRequest())

    session = response["session"]
    repo_task = service.task_manager.get_task(session["repoTaskId"])
    selection = json_loads(repo_task["selection_json"])
    assert full_runs == [Path(record.workspace_path)]
    assert session["refreshRerunEnforcement"]["command"][-2:] == ["--profile", "report_full"]
    assert [target["target_id"] for target in selection["targets"]] == ["pyfile:src/b.py"]
    assert record.enforcement_result["summary"] == "Full Python mutation verification failed"


def test_python_unsampled_failure_does_not_trigger_a_full_rerun(tmp_path):
    """A target-scoped failure already names its targets; rerunning is waste."""
    handler = PythonCiLanguageHandler(runner=None)
    record = CiTaskRecord(
        task_id="task-python-target-scoped",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest(
            app_name="py-app",
            git_url="git@example.com:group/py-app.git",
            branch="feature/TASK-2",
            language="python",
        ),
        enforcement_result={
            "evidence": {"mutation": {"passed": False, "rate": 50.0, "gate": 95.0}},
        },
    )

    request = CreateFixSessionRequest()
    assert handler.should_rerun_after_repair_workspace_refresh(record=record, request=request) is False
    assert handler.run_repair_preflight(record=record, request=request, repo_path=Path(".")) is None


def test_rerun_running_session_is_retried_once_the_rerun_is_stale():
    """`rerun_running` must not be a one-way door.

    The state is written before a rerun that can run as long as a full
    enforcement build and cleared only when that call returns, while the poll
    loop skips any session already in it. An interrupted rerun therefore
    stranded the session: never retried, never reported. One sat there 13
    hours. Past the staleness bound the rerun is assumed dead.
    """
    from datetime import datetime, timedelta, timezone
    from uta.app.repair.session import RepairSessionMixin

    is_stale = RepairSessionMixin._rerun_is_stale
    now = datetime.now(timezone.utc)

    fresh = {"rerunStartedAt": (now - timedelta(seconds=30)).isoformat()}
    dead = {"rerunStartedAt": (now - timedelta(hours=13)).isoformat()}

    assert is_stale(fresh) is False, "a live rerun must be left alone"
    assert is_stale(dead) is True
    # Unstamped is not judged: every site that starts a rerun stamps it, so a
    # missing stamp means some other path set the state, and guessing "dead"
    # would launch the duplicate rerun the guard exists to prevent.
    assert is_stale({}) is False
    assert is_stale({"rerunStartedAt": "not-a-timestamp"}) is False

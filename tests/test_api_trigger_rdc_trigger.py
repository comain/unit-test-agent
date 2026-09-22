from fastapi.testclient import TestClient

from uta.app.app import create_app
from uta.app.protocols import ProtocolRegistry
from uta.app.protocols.rdc import RdcProtocol, parse_rdc_payload
from uta.app.service import ApiTriggerService
from uta.app.store import JsonCiTaskStore
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest


def _rdc_service() -> ApiTriggerService:
    return ApiTriggerService(protocols=ProtocolRegistry([RdcProtocol()]))


def test_app_startup_recovers_the_persisted_ci_queue():
    class RecoveringService(ApiTriggerService):
        recovered = False

        def recover_ci_queue(self):
            self.recovered = True

    service = RecoveringService()

    with TestClient(create_app(service)):
        assert service.recovered is True


def test_queue_claim_includes_records_persisted_before_background_start(tmp_path):
    store = JsonCiTaskStore(tmp_path / "records")
    record = CiTaskRecord(
        task_id="persisted-queued",
        status=CiTaskStatus.queued,
        request=CiTriggerRequest(
            app_name="demo-app",
            git_url="git@git.example.com:group/demo.git",
            branch="feature/TASK-82767",
        ),
    )
    store.save(record)
    store.list_records = lambda **_: (_ for _ in ()).throw(
        AssertionError("queue recovery must not load full persisted records")
    )
    service = ApiTriggerService(record_store=store, ci_report_parallel_limit=1)

    claimed = service._claim_next_pending_ci_check()

    assert claimed is not None
    assert claimed.task_id == record.task_id
    assert claimed.status == CiTaskStatus.running


def test_rdc_trigger_request_parses_attribute_and_top_level_fields():
    request = parse_rdc_payload(
        {
            "appName": "ignored-top-app",
            "operator": "top-user",
            "attribute": {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
                "taskId": 91,
                "recordId": 1001,
                "parentId": 2002,
                "taskTemplateId": "T_91_pre_unitTestAppTool",
                "jiraId": "TASK-82767",
            },
        }
    )

    assert request.app_name == "demo-app"
    assert request.git_url == "git@git.example.com:group/demo.git"
    assert request.branch == "feature/TASK-82767"
    assert request.operator == "top-user"
    assert request.task_id == "91"
    assert request.record_id == "1001"
    assert request.parent_id == "2002"
    assert request.task_template_id == "T_91_pre_unitTestAppTool"
    assert request.jira_id == "TASK-82767"


def test_rdc_trigger_request_infers_jira_from_branch_when_missing():
    request = parse_rdc_payload(
        {
            "attribute": {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "TASK-82768-20260515",
            },
        }
    )

    assert request.jira_id == "TASK-82768"


def test_rdc_trigger_request_parses_language_from_attribute():
    request = parse_rdc_payload(
        {
            "attribute": {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
                "language": "python",
            },
        }
    )

    assert request.language == "python"


def test_rdc_trigger_request_infers_python_language_from_python_app_type():
    request = parse_rdc_payload(
        {
            "attribute": {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
                "appType": "python",
            },
        }
    )

    assert request.app_type == "python"
    assert request.language == "python"


def test_rdc_trigger_request_python_app_type_overrides_java_default_language():
    request = parse_rdc_payload(
        {
            "language": "java",
            "attribute": {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
                "appType": "python",
            },
        }
    )

    assert request.language == "python"


def test_rdc_trigger_request_defaults_to_java_when_language_is_missing():
    request = parse_rdc_payload(
        {
            "attribute": {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
            },
        }
    )

    assert request.language == "java"


def test_rdc_trigger_endpoint_returns_rdc_compatible_running_response():
    client = TestClient(create_app(_rdc_service()))

    response = client.post(
        "/api/v1/rdc/trigger",
        json={
            "attribute": {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
                "taskId": "rdc-task-1",
                "recordId": "record-1",
                "taskTemplateId": "T_91_pre_unitTestAppTool",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == 0
    assert body["msg"] == "处理中"
    assert body["data"]["taskId"]
    assert body["data"]["status"] in {"queued", "running"}
    assert body["data"]["url"].endswith(f"/task-status/{body['data']['taskId']}")
    assert body["data"]["reportUrl"].endswith(f"/task-status/{body['data']['taskId']}")


def test_rdc_trigger_endpoint_supports_prefixed_deployment_urls():
    client = TestClient(
            create_app(
                _rdc_service(),
                public_base_url="http://ci.example.com/unit-test",
            )
        )

    response = client.post(
        "/unit-test/api/v1/rdc/trigger",
        json={
            "attribute": {
                "appName": "demo-app",
                "gitUrl": "git@git.example.com:group/demo.git",
                "branch": "feature/TASK-82767",
                "taskId": "rdc-task-1",
                "recordId": "record-1",
                "taskTemplateId": "T_91_pre_unitTestAppTool",
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == 0
    assert body["data"]["url"].startswith("http://ci.example.com/unit-test/task-status/")
    assert client.get("/unit-test/healthcheck.html").status_code == 200


def test_rdc_trigger_endpoint_returns_failure_json_for_invalid_payload():
    client = TestClient(create_app(_rdc_service()))

    response = client.post("/api/v1/rdc/trigger", json={"attribute": {"branch": "feature/TASK-82767"}})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == -1
    assert body["msg"]
    assert body["data"] == {}

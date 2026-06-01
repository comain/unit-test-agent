from __future__ import annotations

import hashlib
import hmac
import json

import httpx
from fastapi.testclient import TestClient

from uta.ci_plugin.app import create_app
from uta.ci_plugin.models import CiTaskRecord, CiTaskStatus, CiTriggerRequest
from uta.ci_plugin.protocols import ProtocolRegistry
from uta.ci_plugin.protocols.base import CiResult
from uta.ci_plugin.protocols.github import (
    GithubAppAuth,
    GithubChecksClient,
    GithubWebhookProtocol,
)
from uta.ci_plugin.service import CiPluginService

SECRET = "shhh"


def _pr_payload(action: str = "opened") -> dict:
    return {
        "action": action,
        "pull_request": {
            "number": 7,
            "title": "EXAMPLE-100 add coverage",
            "body": "Covers the checkout edge cases",
            "user": {"login": "octocat"},
            "head": {"ref": "feature/x", "sha": "abc123def"},
        },
        "repository": {
            "name": "demo",
            "full_name": "octo/demo",
            "owner": {"login": "octo"},
            "clone_url": "https://github.com/octo/demo.git",
        },
        "installation": {"id": 555},
    }


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _client(checks_client: GithubChecksClient | None = None, secret: str = SECRET):
    service = CiPluginService(
        protocols=ProtocolRegistry([GithubWebhookProtocol(webhook_secret=secret, checks_client=checks_client)])
    )
    return service, TestClient(create_app(service))


# --------------------------------------------------------------------------- #
# Inbound: signature + parsing
# --------------------------------------------------------------------------- #
def test_signed_pull_request_webhook_creates_github_task():
    service, client = _client()
    body = json.dumps(_pr_payload()).encode("utf-8")

    response = client.post(
        "/api/v1/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(body)},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    task_id = data["taskId"]
    record = service.get(task_id)
    assert record.protocol == "github"
    assert record.request.branch == "feature/x"
    assert record.request.commit_id == "abc123def"
    assert record.request.metadata["github"]["owner"] == "octo"
    assert record.request.metadata["github"]["installationId"] == 555


def test_invalid_signature_is_rejected_and_creates_no_task():
    service, client = _client()
    body = json.dumps(_pr_payload()).encode("utf-8")

    response = client.post(
        "/api/v1/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=deadbeef"},
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert service._tasks == {}


def test_non_pull_request_event_is_ignored():
    service, client = _client()
    body = json.dumps({"zen": "ping"}).encode("utf-8")

    response = client.post(
        "/api/v1/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": _sign(body)},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True}
    assert service._tasks == {}


def test_closed_pull_request_action_is_ignored():
    service, client = _client()
    body = json.dumps(_pr_payload(action="closed")).encode("utf-8")

    response = client.post(
        "/api/v1/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign(body)},
    )

    assert response.json() == {"ok": True, "ignored": True}
    assert service._tasks == {}


def test_parse_trigger_maps_pull_request_fields():
    protocol = GithubWebhookProtocol(webhook_secret=SECRET)
    body = json.dumps(_pr_payload()).encode("utf-8")

    request = protocol.parse_trigger(body, {"X-GitHub-Event": "pull_request"})

    assert request.app_name == "octo/demo"
    assert request.git_url == "https://github.com/octo/demo.git"
    assert request.branch == "feature/x"
    assert request.operator == "octocat"
    assert request.jira_id == "EXAMPLE-100"  # inferred from the PR title


def test_verify_skipped_when_no_secret_configured():
    protocol = GithubWebhookProtocol(webhook_secret="")
    # Must not raise even without a signature header.
    protocol.verify(b"{}", {})


# --------------------------------------------------------------------------- #
# Outbound: Checks API
# --------------------------------------------------------------------------- #
def _github_record() -> CiTaskRecord:
    request = CiTriggerRequest.model_validate(
        {
            "appName": "octo/demo",
            "gitUrl": "https://github.com/octo/demo.git",
            "branch": "feature/x",
            "metadata": {
                "github": {"owner": "octo", "repo": "demo", "headSha": "abc123def", "installationId": 555}
            },
        }
    )
    return CiTaskRecord(task_id="gh-1", status=CiTaskStatus.failed, request=request, protocol="github")


def test_report_result_posts_check_run_with_failure_conclusion():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.read())
        return httpx.Response(201, json={"id": 1})

    checks = GithubChecksClient(
        auth=GithubAppAuth(token_provider=lambda inst: f"tok-{inst}"),
        transport=httpx.MockTransport(handler),
    )
    protocol = GithubWebhookProtocol(checks_client=checks, check_name="uta/unit-test-enforcement")

    outcome = protocol.report_result(
        _github_record(),
        CiResult(passed=False, summary="coverage 72% < 95%", report_url="http://uta/reports/gh-1/index.html"),
    )

    assert outcome.succeeded is True
    assert captured["url"].endswith("/repos/octo/demo/check-runs")
    assert captured["auth"] == "Bearer tok-555"
    assert captured["body"]["head_sha"] == "abc123def"
    assert captured["body"]["conclusion"] == "failure"
    assert captured["body"]["details_url"] == "http://uta/reports/gh-1/index.html"
    assert captured["body"]["output"]["summary"] == "coverage 72% < 95%"


def test_report_result_posts_success_conclusion_when_passed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return httpx.Response(201, json={"id": 2})

    checks = GithubChecksClient(
        auth=GithubAppAuth(token_provider=lambda inst: "tok"),
        transport=httpx.MockTransport(handler),
    )
    protocol = GithubWebhookProtocol(checks_client=checks)

    protocol.report_result(_github_record(), CiResult(passed=True, summary="all gates green", report_url="http://uta/r"))

    assert seen["body"]["conclusion"] == "success"


def test_can_report_requires_github_metadata_and_client():
    record = _github_record()
    assert GithubWebhookProtocol(checks_client=object()).can_report(record) is True
    # Missing checks client -> cannot report.
    assert GithubWebhookProtocol(checks_client=None).can_report(record) is False
    # Missing github metadata -> cannot report.
    bare = CiTaskRecord(
        task_id="x",
        status=CiTaskStatus.failed,
        request=CiTriggerRequest.model_validate(
            {"appName": "a", "gitUrl": "https://github.com/o/r.git", "branch": "b"}
        ),
        protocol="github",
    )
    assert GithubWebhookProtocol(checks_client=object()).can_report(bare) is False

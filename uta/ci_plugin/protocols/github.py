"""GitHub webhook protocol adapter — the open-source CI integration.

Inbound: a ``pull_request`` webhook (signed with ``X-Hub-Signature-256``).
Outbound: a GitHub Checks API ``check-run`` against the PR head SHA. The Checks
API can only be written by a GitHub App, so reporting mints an installation
token from the App's id + private key (RS256 JWT). ``jwt`` is imported lazily so
this module loads even where the optional crypto dependency is absent (tests
inject a token provider instead).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

import httpx

from uta.ci_plugin.context import assemble_base_context
from uta.ci_plugin.models import CiTaskRecord, CiTriggerRequest, infer_jira_id
from uta.ci_plugin.protocols.base import (
    CiCallbackOutcome,
    CiContextProvider,
    CiProtocol,
    CiResult,
    ProtocolResponse,
)

_TRIGGER_ACTIONS = {"opened", "synchronize", "reopened"}
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _github_meta(record: CiTaskRecord) -> Dict[str, Any]:
    meta = (record.request.metadata or {}).get("github")
    return meta if isinstance(meta, dict) else {}


# --------------------------------------------------------------------------- #
# GitHub App authentication
# --------------------------------------------------------------------------- #
class GithubAppAuth:
    """Mints short-lived installation tokens for the GitHub App.

    Tests pass ``token_provider`` to bypass JWT signing entirely; production
    leaves it unset and supplies ``app_id`` + ``private_key_pem``.
    """

    def __init__(
        self,
        app_id: str = "",
        private_key_pem: str = "",
        *,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: int = 10,
        transport: Optional[httpx.BaseTransport] = None,
        token_provider: Optional[Callable[[Any], str]] = None,
    ) -> None:
        self.app_id = app_id
        self.private_key_pem = private_key_pem
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self._token_provider = token_provider

    def installation_token(self, installation_id: Any) -> str:
        if self._token_provider is not None:
            return self._token_provider(installation_id)
        app_jwt = self._app_jwt()
        url = f"{self.api_base_url}/app/installations/{installation_id}/access_tokens"
        headers = {"Authorization": f"Bearer {app_jwt}", **_GITHUB_HEADERS}
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.post(url, headers=headers)
        response.raise_for_status()
        token = response.json().get("token")
        if not token:
            raise ValueError("GitHub installation token response missing 'token'")
        return token

    def _app_jwt(self) -> str:
        if not self.app_id or not self.private_key_pem:
            raise ValueError("GitHub App id and private key are required to mint a token")
        import jwt  # lazy: optional dependency, only needed for real App auth

        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self.app_id}
        return jwt.encode(payload, self.private_key_pem, algorithm="RS256")


# --------------------------------------------------------------------------- #
# GitHub Checks API client
# --------------------------------------------------------------------------- #
class GithubChecksClient:
    def __init__(
        self,
        auth: GithubAppAuth,
        *,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: int = 10,
        retry_times: int = 3,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.auth = auth
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retry_times = retry_times
        self.transport = transport
        self.sleep = sleep

    def create_check_run(
        self,
        *,
        owner: str,
        repo: str,
        head_sha: str,
        installation_id: Any,
        name: str,
        conclusion: str,
        title: str,
        summary: str,
        details_url: str,
    ) -> CiCallbackOutcome:
        history: List[Dict[str, Any]] = []
        try:
            token = self.auth.installation_token(installation_id)
        except Exception as exc:  # noqa: BLE001
            return CiCallbackOutcome(succeeded=False, history=history, error=f"installation token failed: {exc}")

        url = f"{self.api_base_url}/repos/{owner}/{repo}/check-runs"
        headers = {"Authorization": f"Bearer {token}", **_GITHUB_HEADERS}
        body = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "details_url": details_url,
            "output": {"title": title, "summary": summary},
        }
        for attempt in range(1, self.retry_times + 2):
            started = time.time()
            try:
                with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
                    response = client.post(url, headers=headers, json=body)
                history.append(
                    {
                        "attempt": attempt,
                        "status_code": response.status_code,
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "body": response.text[:1000],
                    }
                )
                if 200 <= response.status_code < 300:
                    return CiCallbackOutcome(succeeded=True, history=history)
            except Exception as exc:  # noqa: BLE001
                history.append(
                    {
                        "attempt": attempt,
                        "elapsed_ms": int((time.time() - started) * 1000),
                        "error": str(exc),
                    }
                )
            if attempt <= self.retry_times:
                self.sleep(min(attempt, 3))

        return CiCallbackOutcome(
            succeeded=False,
            history=history,
            error=f"GitHub check-run failed after {self.retry_times + 1} attempts",
        )


# --------------------------------------------------------------------------- #
# Context provider: the pull request is the issue
# --------------------------------------------------------------------------- #
class GithubContextProvider(CiContextProvider):
    name = "github"

    def build_context(
        self,
        record: CiTaskRecord,
        *,
        user_context: Optional[str] = None,
        commit_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        meta = _github_meta(record)
        parts = [part for part in (meta.get("prTitle"), meta.get("prBody")) if part]
        description = "\n\n".join(parts) or None
        issue = {
            "id": meta.get("prNumber"),
            "description": description,
            "source": "github_pr",
            "kind": "github_pr",
        }
        return assemble_base_context(
            record,
            issue=issue,
            user_context=user_context,
            commit_messages=commit_messages,
        )


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
class GithubWebhookProtocol(CiProtocol):
    name = "github"

    def __init__(
        self,
        *,
        webhook_secret: str = "",
        checks_client: Optional[GithubChecksClient] = None,
        check_name: str = "uta/unit-test-enforcement",
        context_provider: Optional[GithubContextProvider] = None,
    ) -> None:
        self.webhook_secret = webhook_secret
        self.checks_client = checks_client
        self.check_name = check_name
        self.context_provider = context_provider or GithubContextProvider()

    def verify(self, body: bytes, headers: Mapping[str, str]) -> None:
        if not self.webhook_secret:
            return  # dormant until configured
        signature = _header(headers, "x-hub-signature-256")
        if not signature:
            raise ValueError("missing X-Hub-Signature-256 header")
        expected = "sha256=" + hmac.new(self.webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("invalid webhook signature")

    def parse_trigger(self, body: bytes, headers: Mapping[str, str]) -> Optional[CiTriggerRequest]:
        if _header(headers, "x-github-event") != "pull_request":
            return None
        payload = json.loads(body or b"{}")
        if not isinstance(payload, dict) or payload.get("action") not in _TRIGGER_ACTIONS:
            return None

        pr = payload.get("pull_request") or {}
        head = pr.get("head") or {}
        repo = payload.get("repository") or {}
        full_name = repo.get("full_name") or ""
        owner = (repo.get("owner") or {}).get("login") or (full_name.split("/")[0] if "/" in full_name else "")
        repo_name = repo.get("name") or (full_name.split("/", 1)[1] if "/" in full_name else "")
        branch = head.get("ref")

        return CiTriggerRequest.model_validate(
            {
                "appName": full_name or repo_name,
                "gitUrl": repo.get("clone_url") or repo.get("html_url"),
                "branch": branch,
                "commitId": head.get("sha"),
                "jiraId": infer_jira_id(branch, pr.get("title")),
                "operator": (pr.get("user") or {}).get("login"),
                "language": "java",
                "metadata": {
                    "github": {
                        "owner": owner,
                        "repo": repo_name,
                        "headSha": head.get("sha"),
                        "installationId": (payload.get("installation") or {}).get("id"),
                        "prNumber": pr.get("number"),
                        "prTitle": pr.get("title"),
                        "prBody": pr.get("body"),
                    }
                },
            }
        )

    def trigger_response(
        self,
        record: CiTaskRecord,
        *,
        task_url: str,
        report_url: str,
    ) -> ProtocolResponse:
        return ProtocolResponse(status_code=200, body={"ok": True, "taskId": record.task_id})

    def ignored_response(self) -> ProtocolResponse:
        return ProtocolResponse(status_code=200, body={"ok": True, "ignored": True})

    def error_response(self, exc: Exception) -> ProtocolResponse:
        return ProtocolResponse(status_code=400, body={"ok": False, "error": str(exc)})

    def can_report(self, record: CiTaskRecord) -> bool:
        meta = _github_meta(record)
        return bool(
            self.checks_client
            and meta.get("owner")
            and meta.get("repo")
            and meta.get("headSha")
            and meta.get("installationId")
        )

    def reporting_configured(self) -> bool:
        return self.checks_client is not None

    def report_result(self, record: CiTaskRecord, result: CiResult) -> CiCallbackOutcome:
        if not self.checks_client:
            return CiCallbackOutcome(succeeded=False, error="no GitHub checks client configured")
        meta = _github_meta(record)
        return self.checks_client.create_check_run(
            owner=str(meta.get("owner")),
            repo=str(meta.get("repo")),
            head_sha=str(meta.get("headSha")),
            installation_id=meta.get("installationId"),
            name=self.check_name,
            conclusion="success" if result.passed else "failure",
            title=self.check_name,
            summary=result.summary or ("Passed" if result.passed else "Failed"),
            details_url=result.report_url,
        )


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup that works for dicts and Starlette Headers."""
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = headers.get(name)  # Starlette Headers is already case-insensitive
        if value is not None:
            return value
    lowered = name.lower()
    for key, value in dict(headers).items():
        if key.lower() == lowered:
            return value
    return None

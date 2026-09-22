"""RDC protocol adapter — the internal CI integration.

Owns everything specific to the internal RDC pipeline: the ``attribute``/``data``
trigger payload, the ``{state, attribute, data}`` ack callback, and Jira issue
enrichment through the internal Jira service. The generic service core never
imports anything from here directly.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

import httpx
from pydantic import BaseModel

from uta.app.context import assemble_base_context
from uta.shared.ci_models import CiTaskRecord, CiTriggerRequest, infer_jira_id
from uta.app.protocols.base import (
    CiCallbackOutcome,
    CiContextProvider,
    CiProtocol,
    CiResult,
    ProtocolResponse,
)


# --------------------------------------------------------------------------- #
# Inbound: RDC trigger payload -> normalized CiTriggerRequest
# --------------------------------------------------------------------------- #
def _normalize_attribute(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("attribute must be an object")


def parse_rdc_payload(payload: Dict[str, Any]) -> CiTriggerRequest:
    """Decode the RDC ``attribute``/``data`` payload into a normalized request."""
    attribute = _normalize_attribute(payload.get("attribute"))
    metadata = dict(payload.get("data")) if isinstance(payload.get("data"), dict) else {}
    if isinstance(payload.get("fields"), dict) and "fields" not in metadata:
        metadata["fields"] = payload.get("fields")

    def pick(*values: Any) -> Any:
        for value in values:
            if value is not None:
                return value
        return None

    def as_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value)

    branch = pick(attribute.get("branch"), payload.get("branch"), payload.get("gitBranchName"))
    app_type = pick(attribute.get("appType"), payload.get("appType"), payload.get("app_type"))
    explicit_language = pick(
        attribute.get("language"),
        attribute.get("projectLanguage"),
        attribute.get("backendLanguage"),
        payload.get("language"),
        payload.get("projectLanguage"),
        payload.get("backendLanguage"),
        metadata.get("language"),
    )
    app_type_language = _language_from_app_type(app_type)
    language = explicit_language or app_type_language or "java"
    if app_type_language == "python" and str(explicit_language or "").strip().lower() in {"", "java"}:
        language = "python"
    jira_id = pick(
        attribute.get("jiraId"),
        attribute.get("jiraKey"),
        attribute.get("jira_id"),
        payload.get("jiraId"),
        payload.get("jiraKey"),
        payload.get("jira_id"),
        infer_jira_id(branch, payload.get("sprintName"), attribute.get("sprintName")),
    )

    return CiTriggerRequest.model_validate(
        {
            "appName": pick(attribute.get("appName"), payload.get("appName"), payload.get("app_name")),
            "gitUrl": pick(
                attribute.get("gitUrl"),
                attribute.get("gitRepositoryPath"),
                payload.get("gitUrl"),
                payload.get("gitRepositoryPath"),
                payload.get("repoUrl"),
                payload.get("repo_url"),
            ),
            "branch": branch,
            "commitId": pick(attribute.get("commitId"), payload.get("commitId"), payload.get("commit_id")),
            "jiraId": jira_id,
            "operator": pick(payload.get("operator"), attribute.get("operator")),
            "taskId": as_str(pick(attribute.get("taskId"), payload.get("taskId"))),
            "recordId": as_str(pick(attribute.get("recordId"), payload.get("recordId"))),
            "parentId": as_str(pick(attribute.get("parentId"), payload.get("parentId"))),
            "taskTemplateId": as_str(pick(attribute.get("taskTemplateId"), payload.get("taskTemplateId"))),
            "pipelineId": as_str(pick(attribute.get("pipelineId"), payload.get("pipelineId"))),
            "sprintId": as_str(pick(attribute.get("sprintId"), payload.get("sprintId"))),
            "appType": app_type,
            "stage": pick(attribute.get("stage"), payload.get("stage")),
            "language": language,
            "metadata": metadata,
        }
    )


def _language_from_app_type(app_type: Any) -> Optional[str]:
    normalized = str(app_type or "").strip().lower()
    if normalized in {"py", "python", "python2", "python3"}:
        return "python"
    if normalized in {"java", "web"}:
        return "java"
    return None


# --------------------------------------------------------------------------- #
# Jira enrichment (internal Jira service)
# --------------------------------------------------------------------------- #
class JiraDescriptionClient:
    def __init__(
        self,
        endpoint: str,
        timeout_seconds: int = 10,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def fetch_description(self, jira_id: str) -> Optional[str]:
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            response = client.post(self.endpoint, json={"jira_key": jira_id})
        response.raise_for_status()
        data = response.json()
        item = data.get("data")
        if isinstance(item, list):
            item = item[0] if item else {}
        if not isinstance(item, dict):
            return None
        fields = item.get("fields")
        if not isinstance(fields, dict):
            return None
        description = fields.get("description")
        return description if isinstance(description, str) and description.strip() else None


def _jira_description_from_payload(record: CiTaskRecord) -> Optional[str]:
    metadata = record.request.metadata or {}
    candidates = [
        (metadata.get("fields") or {}).get("description") if isinstance(metadata.get("fields"), dict) else None,
        metadata.get("jiraDescription"),
        metadata.get("jira_description"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


class RdcContextProvider(CiContextProvider):
    name = "rdc"

    def __init__(self, jira_client: Optional[JiraDescriptionClient] = None) -> None:
        self._jira_client = jira_client

    def build_context(
        self,
        record: CiTaskRecord,
        *,
        user_context: Optional[str] = None,
        commit_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        jira_id = record.request.jira_id or infer_jira_id(record.request.branch)
        description = _jira_description_from_payload(record)
        source = "trigger_payload"
        if not description and self._jira_client and jira_id:
            try:
                description = self._jira_client.fetch_description(jira_id)
                source = "jira_raw_v2" if description else "unavailable"
            except Exception:  # noqa: BLE001
                source = "unavailable"
        issue = {"id": jira_id, "description": description, "source": source, "kind": "jira"}
        context = assemble_base_context(
            record,
            issue=issue,
            user_context=user_context,
            commit_messages=commit_messages,
        )
        if jira_id:
            context["pipeline"]["jiraId"] = jira_id
        return context

    def enrich_repair_context(
        self,
        record: CiTaskRecord,
        context: Dict[str, Any],
        repo_task: Dict[str, Any],
    ) -> Dict[str, Any]:
        issue = context.get("issue")
        if not isinstance(issue, dict):
            issue = {}
        context["issue"] = issue
        pipeline = context.setdefault("pipeline", {})

        jira_id = (
            issue.get("id")
            or pipeline.get("jiraId")
            or record.request.jira_id
            or infer_jira_id(pipeline.get("branch"), record.request.branch)
        )
        if jira_id:
            pipeline["jiraId"] = jira_id
            issue["id"] = jira_id
        if not issue.get("description") and jira_id and self._jira_client:
            try:
                description = self._jira_client.fetch_description(str(jira_id))
            except Exception:  # noqa: BLE001
                description = None
            if description:
                issue["description"] = description
                issue["source"] = "jira_raw_v2"
        return context


# --------------------------------------------------------------------------- #
# Outbound: RDC ack callback
# --------------------------------------------------------------------------- #
class RdcCallbackPayload(BaseModel):
    passed: bool
    score: int
    report_url: str
    summary: str


@dataclass
class RdcCallbackResult:
    succeeded: bool
    history: List[Dict[str, Any]]
    error: Optional[str] = None


class RdcCallbackClient:
    def __init__(
        self,
        ack_url: str,
        timeout_seconds: int = 10,
        retry_times: int = 3,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ack_url = ack_url
        self.timeout_seconds = timeout_seconds
        self.retry_times = retry_times
        self.transport = transport
        self.sleep = sleep

    def build_body(self, request: CiTriggerRequest, payload: RdcCallbackPayload) -> Dict[str, Any]:
        state = 0 if payload.passed else -1024
        return {
            "state": state,
            "attribute": {
                "taskId": request.task_id or "",
                "recordId": request.record_id or "",
                "taskTemplateId": request.task_template_id or "",
                "parentId": request.parent_id or "",
                "url": payload.report_url,
                "reportUrl": payload.report_url,
                "operator": request.operator or "",
            },
            "data": {
                "passed": str(payload.passed).lower(),
                "score": str(payload.score),
                "summary": payload.summary,
            },
        }

    def can_send(self, request: CiTriggerRequest) -> bool:
        return bool(
            self.ack_url
            and request.task_id
            and request.record_id
            and request.parent_id
            and request.task_template_id
        )

    def send(self, request: CiTriggerRequest, payload: RdcCallbackPayload) -> RdcCallbackResult:
        if not self.can_send(request):
            return RdcCallbackResult(succeeded=False, history=[], error="no RDC callback target configured")

        body = self.build_body(request, payload)
        headers = {"Content-Type": "application/json"}
        history: List[Dict[str, Any]] = []
        for attempt in range(1, self.retry_times + 2):
            started = time.time()
            try:
                with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
                    response = client.post(self.ack_url, headers=headers, json=body)
                entry = {
                    "attempt": attempt,
                    "status_code": response.status_code,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "body": response.text[:1000],
                    "request": {
                        "state": body.get("state"),
                        "passed": (body.get("data") or {}).get("passed"),
                        "summary": (body.get("data") or {}).get("summary"),
                    },
                }
                history.append(entry)
                if 200 <= response.status_code < 300:
                    mismatch = self._applied_result_mismatch(response.text, payload)
                    if mismatch:
                        return RdcCallbackResult(succeeded=False, history=history, error=mismatch)
                    return RdcCallbackResult(succeeded=True, history=history)
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

        return RdcCallbackResult(
            succeeded=False,
            history=history,
            error=f"RDC callback failed after {self.retry_times + 1} attempts",
        )

    @staticmethod
    def _applied_result_mismatch(response_text: str, payload: RdcCallbackPayload) -> Optional[str]:
        try:
            body = json.loads(response_text)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(body, dict):
            return None
        data = body.get("data")
        if not isinstance(data, dict):
            return None
        result = data.get("result")
        if isinstance(result, dict) and "passed" in result:
            applied = str(result.get("passed")).strip().lower()
            expected = str(payload.passed).lower()
            if applied and applied != expected:
                return f"RDC callback did not apply requested passed={expected}; response passed={applied}"
        state = data.get("state")
        if state is None:
            return None
        applied_state = str(state).strip().lower()
        success_states = {"0", "success", "succeeded", "passed"}
        failure_states = {"-1024", "failed", "failure", "error"}
        if payload.passed and applied_state in failure_states:
            return f"RDC callback did not apply requested success state; response state={state}"
        if not payload.passed and applied_state in success_states:
            return f"RDC callback did not apply requested failure state; response state={state}"
        return None


# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #
class RdcProtocol(CiProtocol):
    name = "rdc"

    def __init__(
        self,
        callback_client: Optional[RdcCallbackClient] = None,
        context_provider: Optional[RdcContextProvider] = None,
    ) -> None:
        self.callback_client = callback_client
        self.context_provider = context_provider or RdcContextProvider()

    def parse_trigger(self, body: bytes, headers: Mapping[str, str]) -> CiTriggerRequest:
        payload = json.loads(body or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("RDC payload must be an object")
        return parse_rdc_payload(payload)

    def trigger_response(
        self,
        record: CiTaskRecord,
        *,
        task_url: str,
        report_url: str,
    ) -> ProtocolResponse:
        return ProtocolResponse(
            status_code=200,
            body={
                "status": 0,
                "msg": "处理中",
                "data": {
                    "taskId": record.task_id,
                    "status": record.status.value,
                    "url": task_url,
                    "reportUrl": report_url,
                },
            },
        )

    def error_response(self, exc: Exception) -> ProtocolResponse:
        return ProtocolResponse(status_code=200, body={"status": -1, "msg": str(exc), "data": {}})

    def can_report(self, record: CiTaskRecord) -> bool:
        return bool(self.callback_client and self.callback_client.can_send(record.request))

    def reporting_configured(self) -> bool:
        return self.callback_client is not None

    def report_result(self, record: CiTaskRecord, result: CiResult) -> CiCallbackOutcome:
        if not self.callback_client:
            return CiCallbackOutcome(succeeded=False, error="no RDC callback target configured")
        callback_passed = result.passed
        callback_summary = result.summary
        if record.request.app_name == "fd_wmonitor_default_store" and not result.passed:
            callback_passed = True
            callback_summary = (
                "Temporary compatibility override: UTA reported success to RDC for "
                f"fd_wmonitor_default_store. Original UTA result: {result.summary}"
            )
        payload = RdcCallbackPayload(
            passed=callback_passed,
            score=100 if callback_passed else 0,
            report_url=result.report_url,
            summary=callback_summary,
        )
        outcome = self.callback_client.send(record.request, payload)
        return CiCallbackOutcome(succeeded=outcome.succeeded, history=outcome.history, error=outcome.error)

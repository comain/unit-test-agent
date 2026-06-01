from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from uta.ci_plugin.models import CiTaskRecord, utc_now

MISSING_PLUGIN_SUMMARY_PREFIX = "Missing test-enforcement plugin/profile"


class CreateFixSessionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    target_ids: List[str] = Field(default_factory=list, validation_alias=AliasChoices("target_ids", "targetIds"))
    user_context: Optional[str] = Field(default=None, validation_alias=AliasChoices("user_context", "userContext"))


class FixSessionMessageRequest(BaseModel):
    message: str


def create_fix_session(record: CiTaskRecord, request: CreateFixSessionRequest) -> Dict[str, Any]:
    now = utc_now().isoformat()
    selected_targets = _selected_targets(record, request.target_ids)
    messages = []
    if request.user_context and request.user_context.strip():
        messages.append(_message(request.user_context.strip(), now))
    session = {
        "sessionId": uuid.uuid4().hex,
        "status": "created",
        "selectedTargets": selected_targets,
        "messages": messages,
        "retryCount": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    record.fix_sessions.append(session)
    record.updated_at = utc_now()
    return session


def can_create_fix_session(record: CiTaskRecord) -> bool:
    if record.status.value != "failed":
        return False
    return not _is_missing_plugin_or_profile(record)


def _is_missing_plugin_or_profile(record: CiTaskRecord) -> bool:
    enforcement = record.enforcement_result or {}
    return (
        enforcement.get("status") == "missing_evidence"
        and str(enforcement.get("summary") or record.summary or "").startswith(MISSING_PLUGIN_SUMMARY_PREFIX)
    )


def append_message(record: CiTaskRecord, session_id: str, message: str) -> Optional[Dict[str, Any]]:
    session = find_session(record, session_id)
    if session is None:
        return None
    now = utc_now().isoformat()
    session["messages"].append(_message(message.strip(), now))
    session["updatedAt"] = now
    record.updated_at = utc_now()
    return session


def retry_session(record: CiTaskRecord, session_id: str, message: Optional[str] = None) -> Optional[Dict[str, Any]]:
    session = find_session(record, session_id)
    if session is None:
        return None
    now = utc_now().isoformat()
    session["retryCount"] += 1
    session["status"] = "retry_requested"
    if message and message.strip():
        session["messages"].append(_message(message.strip(), now))
    session["updatedAt"] = now
    record.updated_at = utc_now()
    return session


def find_session(record: CiTaskRecord, session_id: str) -> Optional[Dict[str, Any]]:
    return next((session for session in record.fix_sessions if session["sessionId"] == session_id), None)


def _selected_targets(record: CiTaskRecord, target_ids: List[str]) -> List[Dict[str, str]]:
    ids = target_ids or [_default_target_id(record)]
    return [{"id": target_id, "label": _target_label(target_id)} for target_id in ids]


def _default_target_id(record: CiTaskRecord) -> str:
    enforcement = record.enforcement_result or {}
    status = enforcement.get("status") or record.status.value
    return f"enforcement:{status}"


def _target_label(target_id: str) -> str:
    return target_id.split(":", 1)[-1] if ":" in target_id else target_id


def _message(content: str, created_at: str) -> Dict[str, str]:
    return {"role": "user", "content": content, "createdAt": created_at}

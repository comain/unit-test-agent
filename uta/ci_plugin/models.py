from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import AliasChoices, BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


JIRA_ID_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def infer_jira_id(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        match = JIRA_ID_PATTERN.search(str(value))
        if match:
            return match.group(1)
    return None


class CiTaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"


class CiTriggerRequest(BaseModel):
    """Protocol-neutral, normalized trigger for a CI check.

    Each CI protocol parses its own wire payload into this
    shape. Protocol-specific routing/callback coordinates that have no first-class
    field (for example, repository or callback coordinates) are carried in ``metadata`` and
    read back by the same protocol when reporting the result.
    """

    app_name: str = Field(validation_alias=AliasChoices("app_name", "appName"))
    git_url: str = Field(validation_alias=AliasChoices("git_url", "gitUrl", "repoUrl", "repo_url"))
    branch: str = Field(validation_alias=AliasChoices("branch", "gitBranchName"))
    commit_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("commit_id", "commitId"))
    jira_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("jira_id", "jiraId", "jiraKey"))
    operator: Optional[str] = None
    task_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("task_id", "taskId"))
    record_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("record_id", "recordId"))
    parent_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("parent_id", "parentId"))
    task_template_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("task_template_id", "taskTemplateId"),
    )
    pipeline_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("pipeline_id", "pipelineId"))
    sprint_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("sprint_id", "sprintId"))
    app_type: Optional[str] = Field(default=None, validation_alias=AliasChoices("app_type", "appType"))
    stage: Optional[str] = None
    language: str = Field(default="java", validation_alias=AliasChoices("language", "projectLanguage", "backendLanguage"))
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("app_name", "git_url", "branch")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("language")
    @classmethod
    def supported_language(cls, value: str) -> str:
        normalized = str(value or "java").strip().lower()
        if normalized in {"py", "python2", "python3"}:
            return "python"
        if normalized not in {"java", "python"}:
            raise ValueError("language must be java or python")
        return normalized


class CiTaskRecord(BaseModel):
    task_id: str
    status: CiTaskStatus
    request: CiTriggerRequest
    # Which CI protocol created this record, used to route the result callback
    # and repair-context provider.
    protocol: str = "manual"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    report_url: Optional[str] = None
    task_url: Optional[str] = None
    workspace_path: Optional[str] = None
    summary: Optional[str] = None
    enforcement_result: Optional[Dict[str, Any]] = None
    callback_history: list[Dict[str, Any]] = Field(default_factory=list)
    callback_succeeded: bool = False
    callback_error: Optional[str] = None
    fix_sessions: list[Dict[str, Any]] = Field(default_factory=list)

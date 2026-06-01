import datetime as _dt
import json
from dataclasses import dataclass
from typing import Any, Dict

DEFAULT_PRIORITY = 100

REPO_TASK_STATUSES = {
    "CREATED",
    "QUEUED",
    "RUNNING",
    "STOP_REQUESTED",
    "STOPPED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "POISONED",
    "BUDGET_EXCEEDED",
}

CLASS_TASK_STATUSES = {
    "PENDING",
    "CREATED",
    "QUEUED",
    "RUNNING",
    "STOPPED",
    "GENERATED",
    "PASS",
    "FAIL",
    "MUTATION_FAIL",
    "BUDGET_EXCEEDED",
    "PROVIDER_ERROR",
    "PROVIDER_RATE_LIMITED",
    "PUSH_FAILED",
    "UNSAFE_DIFF",
    "LLM_STALLED",
    "CANCELLED",
}

TERMINAL_REPO_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "POISONED", "BUDGET_EXCEEDED"}
TERMINAL_CLASS_STATUSES = {
    "PASS",
    "FAIL",
    "MUTATION_FAIL",
    "BUDGET_EXCEEDED",
    "PROVIDER_ERROR",
    "PROVIDER_RATE_LIMITED",
    "PUSH_FAILED",
    "UNSAFE_DIFF",
    "LLM_STALLED",
    "CANCELLED",
}


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, separators=(",", ":"))


def json_loads(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def normalize_status(status: str, allowed: set[str], *, fallback: str) -> str:
    candidate = (status or "").strip().upper()
    return candidate if candidate in allowed else fallback


@dataclass(frozen=True)
class RepoTaskRow:
    id: int
    repo_path: str
    repo_slug: str
    status: str
    priority: int
    branch_name: str


@dataclass(frozen=True)
class ClassTaskRow:
    id: int
    repo_task_id: int
    class_fqn: str
    status: str
    priority: int


@dataclass(frozen=True)
class TaskEventRow:
    id: int
    repo_task_id: int
    event_type: str
    severity: str
    message: str
    ts: str

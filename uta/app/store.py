from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest

LOGGER = logging.getLogger(__name__)

_FIX_SESSION_SUMMARY_KEYS = (
    "sessionId",
    "status",
    "repoTaskId",
    "repoTaskStatus",
    "repoTaskStage",
    "repoTaskDetail",
    "retryCount",
    "createdAt",
    "updatedAt",
    "canonicalTaskId",
    "canonicalTaskUrl",
)


class JsonCiTaskStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.tasks_root = self.root / "tasks"
        self.tasks_root.mkdir(parents=True, exist_ok=True)

    def save(self, record: CiTaskRecord) -> None:
        path = self._path(record.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_compact_readable_record(record).model_dump_json())
            tmp_path.replace(path)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    def load(self, task_id: str) -> Optional[CiTaskRecord]:
        path = self._path(task_id)
        if not path.exists():
            return None
        return CiTaskRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_records(
        self,
        *,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> list[CiTaskRecord]:
        records: list[CiTaskRecord] = []
        for path in self.tasks_root.glob("*.json"):
            try:
                record = CiTaskRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                LOGGER.warning("ci_record_load_failed path=%s", path, exc_info=True)
                continue
            if since and _aware_datetime(record.created_at) < _aware_datetime(since):
                continue
            records.append(record)
        records.sort(key=lambda item: _aware_datetime(item.created_at), reverse=True)
        return records[: max(1, int(limit))]

    def list_record_summaries(
        self,
        *,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> list[CiTaskRecord]:
        records: list[CiTaskRecord] = []
        candidates = sorted(
            self.tasks_root.glob("*.json"),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        since_timestamp = _aware_datetime(since).timestamp() if since else None
        for path in candidates:
            if since_timestamp is not None and path.stat().st_mtime < since_timestamp:
                break
            try:
                record = _load_record_summary(path, since=since)
            except Exception:  # noqa: BLE001
                LOGGER.warning("ci_record_summary_load_failed path=%s", path, exc_info=True)
                continue
            if record is None:
                continue
            if since and _aware_datetime(record.created_at) < _aware_datetime(since):
                continue
            records.append(record)
        records.sort(key=lambda item: _aware_datetime(item.created_at), reverse=True)
        return records[: max(1, int(limit))]

    def _path(self, task_id: str) -> Path:
        safe_task_id = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(task_id)).strip("-")
        return self.tasks_root / f"{safe_task_id or 'task'}.json"


def _compact_readable_record(record: CiTaskRecord) -> CiTaskRecord:
    """Keep repair identity fields ahead of evidence skipped by compact reads."""
    sessions = []
    for session in record.fix_sessions:
        if not isinstance(session, dict):
            sessions.append(session)
            continue
        summary = {key: session[key] for key in _FIX_SESSION_SUMMARY_KEYS if key in session}
        summary.update({key: value for key, value in session.items() if key not in summary})
        sessions.append(summary)
    return record.model_copy(update={"fix_sessions": sessions})


def _load_record_summary(path: Path, *, since: Optional[datetime] = None) -> Optional[CiTaskRecord]:
    prefix = _read_prefix(path)
    request_raw = _extract_json_value(prefix, "request")
    if not isinstance(request_raw, dict):
        raise ValueError("record request not found")

    task_id = _extract_string(prefix, "task_id")
    status = _extract_string(prefix, "status")
    created_at = _parse_datetime(_extract_string(prefix, "created_at"))
    updated_at = _parse_datetime(_extract_string(prefix, "updated_at"))
    if not task_id or not status or not created_at or not updated_at:
        raise ValueError("required record summary fields not found")
    if since and _aware_datetime(created_at) < _aware_datetime(since):
        return None

    tail = _read_tail(path)
    fix_sessions_raw = _extract_json_value(tail, "fix_sessions", reverse=True) or []
    fix_sessions = fix_sessions_raw if isinstance(fix_sessions_raw, list) else []
    if not fix_sessions:
        fix_sessions = _extract_fix_session_summaries(path)
    enforcement_result = _extract_enforcement_summary(prefix)
    callback_succeeded = _extract_bool(tail, "callback_succeeded") or False
    callback_override_raw = _extract_json_value(tail, "callback_override")
    callback_override = callback_override_raw if isinstance(callback_override_raw, dict) else None

    return CiTaskRecord(
        task_id=task_id,
        status=CiTaskStatus(status),
        request=CiTriggerRequest.model_validate(request_raw),
        protocol=_extract_string(prefix, "protocol") or "rdc",
        created_at=created_at,
        updated_at=updated_at,
        report_url=_extract_string(prefix, "report_url"),
        task_url=_extract_string(prefix, "task_url"),
        workspace_path=_extract_string(prefix, "workspace_path"),
        summary=_extract_string(prefix, "summary"),
        enforcement_result=enforcement_result,
        callback_succeeded=callback_succeeded,
        callback_error=_extract_string(tail, "callback_error"),
        callback_override=callback_override,
        fix_sessions=fix_sessions,
    )


def _read_prefix(path: Path, size: int = 64 * 1024) -> str:
    with path.open("r", encoding="utf-8") as handle:
        return handle.read(size)


def _read_tail(path: Path, size: int = 128 * 1024) -> str:
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, file_size - size))
        return handle.read().decode("utf-8", errors="ignore")


def _extract_fix_session_summaries(path: Path) -> list[dict[str, object]]:
    """Extract dashboard-safe repair-session metadata from large task records.

    Full repair sessions can embed multi-MB enforcement evidence. Recent-job
    pages need only shallow fields plus repoTaskId, then ApiTriggerService can
    refresh live status/budget from the task DB. Avoid parsing the full session
    array here; doing so reintroduces the large-record RSS spike this compact
    summary path was created to avoid.
    """
    marker = b'"fix_sessions"'
    offset = _find_last_marker_offset(path, marker)
    if offset is None:
        return []
    fragment = _read_from_offset(path, offset, size=256 * 1024)
    array_start = fragment.find("[")
    if array_start < 0:
        return []

    sessions: list[dict[str, object]] = []
    position = array_start + 1
    while position < len(fragment):
        session_marker = fragment.find('"sessionId"', position)
        if session_marker < 0:
            break
        object_start = fragment.rfind("{", 0, session_marker)
        if object_start < 0:
            break
        heavy_starts = [
            index
            for index in (
                fragment.find(',"rdcContext"', object_start),
                fragment.find(',"rerunEnforcement"', object_start),
            )
            if index >= 0
        ]
        next_session = fragment.find('{"sessionId"', session_marker + len('"sessionId"'))
        if next_session >= 0:
            heavy_starts.append(next_session)
        object_end = min(heavy_starts) if heavy_starts else min(len(fragment), object_start + 16 * 1024)
        segment = fragment[object_start:object_end]
        session_id = _extract_string(segment, "sessionId")
        if not session_id:
            position = object_end + 1
            continue
        session: dict[str, object] = {"sessionId": session_id}
        for key in (
            "status",
            "repoTaskStatus",
            "repoTaskStage",
            "repoTaskDetail",
            "createdAt",
            "updatedAt",
            "canonicalTaskId",
            "canonicalTaskUrl",
        ):
            value = _extract_string(segment, key)
            if value is not None:
                session[key] = value
        for key in ("repoTaskId", "retryCount"):
            value = _extract_int(segment, key)
            if value is not None:
                session[key] = value
        sessions.append(session)
        position = object_end + 1
    return sessions


def _find_last_marker_offset(path: Path, marker: bytes, *, chunk_size: int = 1024 * 1024) -> Optional[int]:
    file_size = path.stat().st_size
    overlap = b""
    position = file_size
    with path.open("rb") as handle:
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + overlap
            index = data.rfind(marker)
            if index >= 0:
                return position + index
            overlap = data[: max(0, len(marker) - 1)]
    return None


def _read_from_offset(path: Path, offset: int, *, size: int) -> str:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size).decode("utf-8", errors="ignore")


def _extract_string(text: str, key: str) -> Optional[str]:
    value = _extract_json_scalar(text, key)
    return value if isinstance(value, str) else None


def _extract_bool(text: str, key: str) -> Optional[bool]:
    value = _extract_json_scalar(text, key)
    return value if isinstance(value, bool) else None


def _extract_int(text: str, key: str) -> Optional[int]:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(-?\d+)', text)
    return int(match.group(1)) if match else None


def _extract_json_scalar(text: str, key: str) -> object:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(null|true|false|"(?:\\.|[^"\\])*")', text)
    if not match:
        return None
    return json.loads(match.group(1))


def _extract_json_value(text: str, key: str, *, reverse: bool = False) -> object:
    marker = f'"{key}"'
    start = text.rfind(marker) if reverse else text.find(marker)
    if start < 0:
        return None
    colon = text.find(":", start + len(marker))
    if colon < 0:
        return None
    value_start = colon + 1
    while value_start < len(text) and text[value_start].isspace():
        value_start += 1
    if value_start >= len(text):
        return None
    value_end = _json_value_end(text, value_start)
    if value_end is None:
        return None
    return json.loads(text[value_start:value_end])


def _json_value_end(text: str, start: int) -> Optional[int]:
    opening = text[start]
    if opening not in "[{":
        match = re.match(r'null|true|false|"(?:\\.|[^"\\])*"', text[start:])
        return start + match.end() if match else None
    closing = "]" if opening == "[" else "}"
    stack = [closing]
    in_string = False
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append("]" if char == "[" else "}")
        elif char in "]}":
            if not stack or char != stack.pop():
                return None
            if not stack:
                return index + 1
    return None


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_enforcement_summary(prefix: str) -> dict[str, object]:
    marker = '"enforcement_result"'
    start = prefix.find(marker)
    if start < 0:
        return {}
    snippet = prefix[start:]
    status = _extract_string(snippet, "status")
    passed = _extract_bool(snippet, "passed")
    result: dict[str, object] = {}
    if status is not None:
        result["status"] = status
    if passed is not None:
        result["passed"] = passed
    return result


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value

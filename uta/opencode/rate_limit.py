"""Shared rate-limit detection helpers for OpenCode server/process workflows."""

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def opencode_log_dir() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "log"


def uta_debug_log_dir() -> Path:
    return Path(tempfile.gettempdir()) / "uta-run-logs"


def recent_log_files(limit: int = 5) -> List[Path]:
    candidates: List[Path] = []
    op_log_dir = opencode_log_dir()
    if op_log_dir.exists():
        candidates.extend(p for p in op_log_dir.iterdir() if p.is_file())
    uta_log_dir = uta_debug_log_dir()
    if uta_log_dir.exists():
        candidates.extend(p for p in uta_log_dir.iterdir() if p.is_file() and "_opencode_" in p.name)
    if not candidates:
        return []
    unique = {p.resolve(): p for p in candidates}
    return sorted(unique.values(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def parse_rate_limit_payload(error_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(error_payload, dict):
        return None

    status_code = error_payload.get("statusCode")
    data = error_payload.get("data") or {}
    response_headers = error_payload.get("responseHeaders") or {}
    response_body = error_payload.get("responseBody")
    response_body_json: Dict[str, Any] = {}
    if isinstance(response_body, str):
        try:
            response_body_json = json.loads(response_body)
        except Exception:
            response_body_json = {}

    body_error = (response_body_json.get("error") or {}) if isinstance(response_body_json, dict) else {}
    data_error = data.get("error") or {}
    raw_type = (
        data_error.get("type")
        or body_error.get("type")
        or body_error.get("status")
        or error_payload.get("error", {}).get("type")
    )
    provider_metadata = data_error.get("metadata") or {}
    message = (
        data_error.get("message")
        or body_error.get("message")
        or ((error_payload.get("error") or {}).get("message"))
        or ""
    )
    if provider_metadata.get("raw") and (not message or message.lower() == "provider returned error"):
        message = provider_metadata["raw"]
    message_lower = message.lower()
    retry_after = (
        response_headers.get("retry-after")
        or response_headers.get("Retry-After")
        or response_headers.get("x-codex-primary-reset-after-seconds")
        or response_headers.get("x-ratelimit-reset-after")
        or body_error.get("resets_in_seconds")
    )
    reset_at = (
        response_headers.get("x-codex-primary-reset-at")
        or response_headers.get("x-ratelimit-reset")
        or body_error.get("resets_at")
    )

    is_rate_limited = (
        status_code == 429
        or raw_type in {"usage_limit_reached", "rate_limit", "rate_limit_exceeded"}
        or (isinstance(raw_type, str) and raw_type.upper() == "FREE_QUOTA_EXHAUSTED")
        or (isinstance(raw_type, str) and raw_type.upper() == "RESOURCE_EXHAUSTED")
        or "usage limit has been reached" in message_lower
        or "resource has been exhausted" in message_lower
        or "quota" in message_lower
        or "free_quota_exhausted" in message_lower
        or "endpoint is inactive" in message_lower
        or "too many requests" in message_lower
        or (status_code == 402 and ("requires more credits" in message_lower or "add more credits" in message_lower))
    )
    if not is_rate_limited:
        return None

    def _int_or_none(value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except Exception:
            return None

    return {
        "status_code": status_code,
        "raw_type": raw_type or ("insufficient_credits" if status_code == 402 else "rate_limit"),
        "message": message or ("Provider/model quota reached" if status_code == 402 else "Provider/model rate limit reached"),
        "retry_after_seconds": _int_or_none(retry_after),
        "reset_at": _int_or_none(reset_at),
        "response_headers": response_headers,
        "provider_metadata": provider_metadata,
    }


def _parse_log_timestamp(line: str) -> Optional[float]:
    parts = line.split()
    if len(parts) < 2:
        return None
    try:
        return datetime.fromisoformat(parts[1]).timestamp()
    except Exception:
        return None


def detect_rate_limit_in_logs(
    *,
    session_id: Optional[str] = None,
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
    since_time: Optional[float] = None,
    max_files: int = 5,
) -> Optional[Dict[str, Any]]:
    provider_re = re.compile(r"providerID=([^\s]+)")
    model_re = re.compile(r"modelID=([^\s]+)")
    session_marker = f"sessionID={session_id}" if session_id else None
    alt_session_marker = f"session.id={session_id}" if session_id else None

    for log_path in recent_log_files(limit=max_files):
        try:
            lines = log_path.read_text(errors="replace").splitlines()
        except Exception:
            continue

        for idx in range(len(lines) - 1, -1, -1):
            line = lines[idx]
            if "service=llm" not in line or "error=" not in line:
                continue
            if session_marker and session_marker not in line and alt_session_marker not in line:
                continue
            provider_match = provider_re.search(line)
            model_match = model_re.search(line)
            line_provider = provider_match.group(1) if provider_match else None
            line_model = model_match.group(1) if model_match else None
            if provider_id and line_provider and line_provider != provider_id:
                continue
            if model_id and line_model and line_model != model_id:
                continue
            if since_time is not None:
                ts = _parse_log_timestamp(line)
                if ts is not None and ts < since_time - 2:
                    continue

            _, _, payload = line.partition(" error=")
            error_payload = None
            combined = payload
            for next_idx in range(idx, min(idx + 8, len(lines))):
                if next_idx > idx:
                    combined += "\n" + lines[next_idx]
                try:
                    error_payload, _ = json.JSONDecoder().raw_decode(combined)
                    break
                except Exception:
                    continue
            if error_payload is None:
                continue
            parsed = parse_rate_limit_payload(error_payload)
            if not parsed:
                continue
            parsed.update(
                {
                    "session_id": session_id,
                    "provider_id": line_provider,
                    "model_id": line_model,
                    "log_path": str(log_path),
                }
            )
            return parsed
    return None

"""Fixed product audit schema for managed legacy prompt invocation scopes."""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Tuple


LEGACY_SCOPE_OPENED_EVENT = "legacy_scope_opened"
_LEGACY_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_LANGUAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PAYLOAD_KEYS = frozenset({"schema_version", "language", "legacy_run_id"})


def parse_legacy_scope_opened_payload(
    payload: Any,
) -> Optional[Tuple[str, str]]:
    """Return ``(run_id, language)`` only for the exact safe event shape."""
    if not isinstance(payload, Mapping) or frozenset(payload) != _PAYLOAD_KEYS:
        return None
    if payload.get("schema_version") != 1:
        return None
    run_id = payload.get("legacy_run_id")
    language = payload.get("language")
    if not isinstance(run_id, str) or not _LEGACY_RUN_ID.fullmatch(run_id):
        return None
    if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
        return None
    return run_id, language


def is_managed_legacy_run_id(value: Any) -> bool:
    """Return whether a value is the exact UUID-hex scope identity shape."""
    return isinstance(value, str) and _LEGACY_RUN_ID.fullmatch(value) is not None


__all__ = [
    "LEGACY_SCOPE_OPENED_EVENT",
    "is_managed_legacy_run_id",
    "parse_legacy_scope_opened_payload",
]

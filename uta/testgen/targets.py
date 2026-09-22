"""Normalized target access for the language-neutral generation lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List


def active_target_ids(state: Dict[str, Any]) -> List[str]:
    """Return active target IDs, with legacy Java fields as a fallback."""
    target_ids = _selection_ids(state.get("current_target_batch") or [])
    if not target_ids and state.get("current_target"):
        target_ids = _selection_ids([state["current_target"]])
    if not target_ids:
        target_ids = [str(value) for value in state.get("current_batch") or [] if value]
    if not target_ids and state.get("current_class"):
        target_ids = [str(state["current_class"])]
    return list(dict.fromkeys(target_ids))


def _selection_ids(selections: Any) -> List[str]:
    target_ids = []
    for selection in selections if isinstance(selections, list) else []:
        if isinstance(selection, Mapping):
            target_id = selection.get("target_id") or selection.get("targetId")
        else:
            target_id = getattr(selection, "target_id", None)
        if target_id:
            target_ids.append(str(target_id))
    return target_ids

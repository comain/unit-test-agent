"""Aggregate normalized agent-turn evidence without provider knowledge."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional


def session_refs(turn: Mapping[str, Any]) -> list[Dict[str, str]]:
    """Return JSON-safe neutral refs, adapting only persisted legacy evidence."""
    refs = [
        {
            "harness": str(ref.get("harness") or "unknown"),
            "locator": str(ref["locator"]),
            "scope": str(ref.get("scope") or "durable"),
        }
        for ref in turn.get("session_refs") or ()
        if isinstance(ref, Mapping) and ref.get("locator")
    ]
    legacy = turn.get("session_id")
    if legacy and not refs:
        refs.append(
            {"harness": "legacy", "locator": str(legacy), "scope": "durable"}
        )
    return refs


def last_session_locator(turn: Mapping[str, Any]) -> Optional[str]:
    """Compatibility projection; never use the opaque locator to select a harness."""
    refs = session_refs(turn)
    return refs[-1]["locator"] if refs else None


def aggregate_turn_accounting(turns: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Project full normalized turn evidence into legacy-compatible buckets."""
    sessions = []
    collected_refs = []
    usage: Dict[str, Any] = {}
    phase_usage: Dict[str, Any] = {}
    timings: Dict[str, float] = {}
    retrospective = {"turns": []}
    diagnostics = {"turns": []}
    patch_count = 0
    for raw in turns:
        turn = dict(raw)
        phase = str(turn.get("phase") or "unknown")
        refs = session_refs(turn)
        for ref in refs:
            if ref not in collected_refs:
                collected_refs.append(ref)
            locator = str(ref["locator"])
            if locator not in sessions:
                sessions.append(locator)
        session_id = sessions[-1] if refs else None
        turn_usage = dict(turn.get("usage") or {})
        usage = merge_numeric_mappings(usage, turn_usage)
        phase_usage[phase] = merge_numeric_mappings(
            dict(phase_usage.get(phase) or {}), _phase_token_bucket(turn_usage)
        )
        elapsed = float(turn.get("elapsed_seconds") or 0.0)
        timings[phase] = timings.get(phase, 0.0) + elapsed
        if turn.get("retrospective"):
            retrospective["turns"].append(
                {"phase": phase, "session_id": session_id, "value": dict(turn["retrospective"])}
            )
        if turn.get("diagnostics"):
            diagnostics["turns"].append(
                {"phase": phase, "session_id": session_id, "value": dict(turn["diagnostics"])}
            )
        patch_count += max(int(turn.get("patch_count") or 0), 0)
    return {
        "session_ids": sessions,
        "session_refs": collected_refs,
        "session_token_usage": usage,
        "phase_token_usage": phase_usage,
        "session_retrospect": retrospective,
        "session_diagnostics": diagnostics,
        "session_patch_count": patch_count,
        "agent_phase_timings": timings,
    }


def merge_accounting(existing: Mapping[str, Any], current: Mapping[str, Any]) -> Dict[str, Any]:
    """Accumulate completed batches without dropping earlier accounting."""
    return {
        "session_ids": _merge_unique(
            existing.get("session_ids"), current.get("session_ids")
        ),
        "session_refs": _merge_unique(
            existing.get("session_refs"), current.get("session_refs")
        ),
        "session_token_usage": merge_numeric_mappings(
            dict(existing.get("session_token_usage") or {}),
            dict(current.get("session_token_usage") or {}),
        ),
        "phase_token_usage": merge_numeric_mappings(
            dict(existing.get("phase_token_usage") or {}),
            dict(current.get("phase_token_usage") or {}),
        ),
        "phase_timings": merge_numeric_mappings(
            dict(existing.get("phase_timings") or {}),
            dict(current.get("phase_timings") or {}),
        ),
        "session_retrospect": _merge_turn_lists(
            existing.get("session_retrospect"), current.get("session_retrospect")
        ),
        "session_diagnostics": _merge_turn_lists(
            existing.get("session_diagnostics"), current.get("session_diagnostics")
        ),
        "session_patch_count": int(existing.get("session_patch_count") or 0)
        + int(current.get("session_patch_count") or 0),
    }


def merge_numeric_mappings(left: Mapping[str, Any], right: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively sum numeric leaves while retaining JSON-safe metadata."""
    merged = dict(left)
    for key, value in right.items():
        prior = merged.get(key)
        if isinstance(prior, Mapping) and isinstance(value, Mapping):
            merged[key] = merge_numeric_mappings(prior, value)
        elif isinstance(value, (int, float)) and isinstance(prior, (int, float)):
            merged[key] = prior + value
        elif key not in merged:
            merged[key] = value
        elif prior == value:
            merged[key] = prior
        else:
            merged[key] = value
    return merged


def _phase_token_bucket(usage: Mapping[str, Any]) -> Dict[str, Any]:
    total = usage.get("total_tokens")
    if isinstance(total, Mapping):
        return dict(total)
    aliases = {
        "input_tokens": "input",
        "output_tokens": "output",
        "reasoning_tokens": "reasoning",
        "cache_read_tokens": "cache_read",
        "cache_write_tokens": "cache_write",
        "total_tokens": "total",
    }
    return {
        aliases.get(key, key): value
        for key, value in usage.items()
        if isinstance(value, (int, float))
    }


def _merge_turn_lists(left: Any, right: Any) -> Dict[str, Any]:
    combined = []
    for source in (left, right):
        if not isinstance(source, Mapping):
            continue
        for item in source.get("turns") or []:
            if item not in combined:
                combined.append(item)
    return {"turns": combined}


def _merge_unique(left: Any, right: Any) -> list[Any]:
    combined = []
    for item in [*(left or []), *(right or [])]:
        if item not in combined:
            combined.append(item)
    return combined


__all__ = [
    "aggregate_turn_accounting",
    "last_session_locator",
    "merge_accounting",
    "merge_numeric_mappings",
    "session_refs",
]

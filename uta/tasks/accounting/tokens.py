from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from uta.tasks.domain_support import TOKEN_TOTAL_KEYS


class TaskTokenAccountingMixin:
    """Token aggregation, session merging, and phase usage accounting."""

    @staticmethod
    def _token_totals(
        session_token_usage: Dict[str, Any], phase_token_usage: Dict[str, Any]
    ) -> Dict[str, int]:
        total_tokens = (
            session_token_usage.get("total_tokens")
            if isinstance(session_token_usage, dict)
            else None
        )
        if isinstance(total_tokens, dict) and total_tokens:
            return {
                "input": int(total_tokens.get("input") or 0),
                "output": int(total_tokens.get("output") or 0),
                "cache_read": int(total_tokens.get("cache_read") or 0),
                "cache_write": int(total_tokens.get("cache_write") or 0),
                "reasoning": int(total_tokens.get("reasoning") or 0),
            }
        totals = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
        }
        for stats in (phase_token_usage or {}).values():
            if not isinstance(stats, dict):
                continue
            for key in totals:
                totals[key] += int(stats.get(key) or 0)
        return totals

    @staticmethod
    def _session_ids_from_json(raw: str) -> List[str]:
        try:
            parsed = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            parsed = []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed if item]

    @classmethod
    def _session_ids_from_row(cls, row: Any) -> List[str]:
        if not row or "session_ids_json" not in row.keys():
            return []
        return cls._session_ids_from_json(row["session_ids_json"])

    @staticmethod
    def _merge_session_ids(*groups: Iterable[str]) -> List[str]:
        merged: List[str] = []
        seen = set()
        for group in groups:
            for session_id in group or []:
                if not session_id or session_id in seen:
                    continue
                seen.add(session_id)
                merged.append(str(session_id))
        return merged

    @staticmethod
    def _token_totals_from_row(row: Any) -> Dict[str, int]:
        return {
            "input": int(row["input_tokens"] or row["actual_input_tokens"] or 0),
            "output": int(row["output_tokens"] or row["actual_output_tokens"] or 0),
            "cache_read": int(
                row["cache_read_tokens"] or row["actual_cache_read_tokens"] or 0
            ),
            "cache_write": int(
                row["cache_write_tokens"] or row["actual_cache_write_tokens"] or 0
            ),
            "reasoning": int(row["reasoning_tokens"] or 0),
        }

    @staticmethod
    def _add_token_totals(
        left: Dict[str, int], right: Dict[str, int]
    ) -> Dict[str, int]:
        return {
            key: int(left.get(key) or 0) + int(right.get(key) or 0)
            for key in TOKEN_TOTAL_KEYS
        }

    @staticmethod
    def _split_token_totals(totals: Dict[str, int], count: int) -> List[Dict[str, int]]:
        count = max(1, int(count or 1))
        splits = [{key: 0 for key in TOKEN_TOTAL_KEYS} for _ in range(count)]
        for key in TOKEN_TOTAL_KEYS:
            value = int(totals.get(key) or 0)
            base, remainder = divmod(value, count)
            for index in range(count):
                splits[index][key] = base + (1 if index < remainder else 0)
        return splits

    @staticmethod
    def _merge_phase_token_usage(
        existing: Dict[str, Any], incoming: Dict[str, Any]
    ) -> Dict[str, Dict[str, int]]:
        merged: Dict[str, Dict[str, int]] = {}
        for phase in list((existing or {}).keys()) + [
            phase for phase in (incoming or {}).keys() if phase not in (existing or {})
        ]:
            left = existing.get(phase) if isinstance(existing, dict) else {}
            right = incoming.get(phase) if isinstance(incoming, dict) else {}
            if not isinstance(left, dict):
                left = {}
            if not isinstance(right, dict):
                right = {}
            merged[phase] = {
                key: int(left.get(key) or 0) + int(right.get(key) or 0)
                for key in TOKEN_TOTAL_KEYS
            }
            if "total" in left or "total" in right:
                merged[phase]["total"] = int(left.get("total") or 0) + int(
                    right.get("total") or 0
                )
        return merged

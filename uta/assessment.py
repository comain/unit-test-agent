from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


@dataclass
class StepSummary:
    index: int
    reason: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    tool_calls: int
    tools: Counter[str]
    text_chars: int
    reasoning_chars: int
    started_at_ms: Optional[int]
    finished_at_ms: int


@dataclass
class SessionAssessment:
    session_id: str
    session_ids: List[str]
    first_part_ms: Optional[int]
    last_part_ms: Optional[int]
    part_count: int
    step_count: int
    stop_steps: int
    tool_steps: int
    tool_call_count: int
    tool_counts: Counter[str]
    text_part_count: int
    text_chars: int
    reasoning_part_count: int
    reasoning_chars: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    steps: List[StepSummary]

    @property
    def duration_seconds(self) -> float:
        if self.first_part_ms is None or self.last_part_ms is None:
            return 0.0
        return max(0.0, (self.last_part_ms - self.first_part_ms) / 1000.0)

    @property
    def output_tokens_per_step(self) -> float:
        return self.output_tokens / self.step_count if self.step_count else 0.0

    @property
    def output_tokens_per_tool_call(self) -> float:
        return self.output_tokens / self.tool_call_count if self.tool_call_count else 0.0

    @property
    def text_chars_per_step(self) -> float:
        return self.text_chars / self.step_count if self.step_count else 0.0

    @property
    def cache_share(self) -> float:
        return self.cache_read_tokens / self.total_tokens if self.total_tokens else 0.0

    def top_steps(self, limit: int = 5) -> List[StepSummary]:
        return sorted(self.steps, key=lambda item: item.total_tokens, reverse=True)[:limit]


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _session_parts(db_path: Path, session_id: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            """
            select time_created, data
            from part
            where session_id = ?
            order by time_created asc
            """,
            (session_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    parts: List[Dict[str, Any]] = []
    for time_created, raw_data in rows:
        try:
            payload = json.loads(raw_data)
        except Exception:
            payload = {}
        parts.append({
            "time_created": _as_int(time_created),
            "data": payload,
        })
    return parts


def assess_session(session_id: str, db_path: Path = DEFAULT_OPENCODE_DB) -> SessionAssessment:
    parts = _session_parts(db_path, session_id)
    if not parts:
        return SessionAssessment(
            session_id=session_id,
            session_ids=[session_id],
            first_part_ms=None,
            last_part_ms=None,
            part_count=0,
            step_count=0,
            stop_steps=0,
            tool_steps=0,
            tool_call_count=0,
            tool_counts=Counter(),
            text_part_count=0,
            text_chars=0,
            reasoning_part_count=0,
            reasoning_chars=0,
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            total_tokens=0,
            steps=[],
        )

    first_part_ms = parts[0]["time_created"]
    last_part_ms = parts[-1]["time_created"]
    tool_counts: Counter[str] = Counter()
    steps: List[StepSummary] = []
    text_part_count = 0
    text_chars = 0
    reasoning_part_count = 0
    reasoning_chars = 0
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    total_tokens = 0
    current_step: Dict[str, Any] = {}

    def _reset_step(start_ms: Optional[int]) -> Dict[str, Any]:
        return {
            "started_at_ms": start_ms,
            "tool_calls": 0,
            "tools": Counter(),
            "text_chars": 0,
            "reasoning_chars": 0,
        }

    current_step = _reset_step(None)

    for part in parts:
        payload = part["data"]
        part_type = payload.get("type") or ""

        if part_type == "step-start":
            current_step = _reset_step(part["time_created"])
            continue

        if part_type == "tool":
            tool_name = payload.get("tool") or payload.get("name") or "(unknown)"
            tool_counts[tool_name] += 1
            current_step["tool_calls"] += 1
            current_step["tools"][tool_name] += 1
            continue

        if part_type == "text":
            text = payload.get("text") or ""
            text_part_count += 1
            text_chars += len(text)
            current_step["text_chars"] += len(text)
            continue

        if part_type == "reasoning":
            blob = payload.get("text") or payload.get("reasoning") or ""
            reasoning_part_count += 1
            reasoning_chars += len(blob)
            current_step["reasoning_chars"] += len(blob)
            continue

        if part_type != "step-finish":
            continue

        tokens = payload.get("tokens") or {}
        cache = tokens.get("cache") or {}
        step = StepSummary(
            index=len(steps) + 1,
            reason=payload.get("reason") or "",
            input_tokens=_as_int(tokens.get("input")),
            output_tokens=_as_int(tokens.get("output")),
            reasoning_tokens=_as_int(tokens.get("reasoning")),
            cache_read_tokens=_as_int(cache.get("read")),
            cache_write_tokens=_as_int(cache.get("write")),
            total_tokens=_as_int(tokens.get("total")),
            tool_calls=_as_int(current_step["tool_calls"]),
            tools=current_step["tools"],
            text_chars=_as_int(current_step["text_chars"]),
            reasoning_chars=_as_int(current_step["reasoning_chars"]),
            started_at_ms=current_step["started_at_ms"],
            finished_at_ms=part["time_created"],
        )
        steps.append(step)
        input_tokens += step.input_tokens
        output_tokens += step.output_tokens
        reasoning_tokens += step.reasoning_tokens
        cache_read_tokens += step.cache_read_tokens
        cache_write_tokens += step.cache_write_tokens
        total_tokens += step.total_tokens
        current_step = _reset_step(None)

    return SessionAssessment(
        session_id=session_id,
        session_ids=[session_id],
        first_part_ms=first_part_ms,
        last_part_ms=last_part_ms,
        part_count=len(parts),
        step_count=len(steps),
        stop_steps=sum(1 for step in steps if step.reason == "stop"),
        tool_steps=sum(1 for step in steps if step.reason == "tool-calls"),
        tool_call_count=sum(step.tool_calls for step in steps),
        tool_counts=tool_counts,
        text_part_count=text_part_count,
        text_chars=text_chars,
        reasoning_part_count=reasoning_part_count,
        reasoning_chars=reasoning_chars,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=total_tokens,
        steps=steps,
    )


def assess_sessions(session_ids: Iterable[str], db_path: Path = DEFAULT_OPENCODE_DB) -> SessionAssessment:
    target_ids = [session_id for session_id in session_ids if session_id]
    if not target_ids:
        return assess_session("", db_path)

    assessments = [assess_session(session_id, db_path) for session_id in target_ids]
    if len(assessments) == 1:
        return assessments[0]

    parts_present = [item for item in assessments if item.part_count > 0]
    first_part_ms = min((item.first_part_ms for item in parts_present if item.first_part_ms is not None), default=None)
    last_part_ms = max((item.last_part_ms for item in parts_present if item.last_part_ms is not None), default=None)

    combined_steps: List[StepSummary] = []
    for item in assessments:
        combined_steps.extend(item.steps)
    combined_steps.sort(key=lambda step: (step.finished_at_ms, step.started_at_ms or 0, step.index))
    renumbered_steps = [
        StepSummary(
            index=idx,
            reason=step.reason,
            input_tokens=step.input_tokens,
            output_tokens=step.output_tokens,
            reasoning_tokens=step.reasoning_tokens,
            cache_read_tokens=step.cache_read_tokens,
            cache_write_tokens=step.cache_write_tokens,
            total_tokens=step.total_tokens,
            tool_calls=step.tool_calls,
            tools=step.tools,
            text_chars=step.text_chars,
            reasoning_chars=step.reasoning_chars,
            started_at_ms=step.started_at_ms,
            finished_at_ms=step.finished_at_ms,
        )
        for idx, step in enumerate(combined_steps, start=1)
    ]

    tool_counts: Counter[str] = Counter()
    for item in assessments:
        tool_counts.update(item.tool_counts)

    aggregate_session_id = target_ids[0] if len(target_ids) == 1 else f"{target_ids[0]}+{len(target_ids) - 1}"
    return SessionAssessment(
        session_id=aggregate_session_id,
        session_ids=target_ids,
        first_part_ms=first_part_ms,
        last_part_ms=last_part_ms,
        part_count=sum(item.part_count for item in assessments),
        step_count=sum(item.step_count for item in assessments),
        stop_steps=sum(item.stop_steps for item in assessments),
        tool_steps=sum(item.tool_steps for item in assessments),
        tool_call_count=sum(item.tool_call_count for item in assessments),
        tool_counts=tool_counts,
        text_part_count=sum(item.text_part_count for item in assessments),
        text_chars=sum(item.text_chars for item in assessments),
        reasoning_part_count=sum(item.reasoning_part_count for item in assessments),
        reasoning_chars=sum(item.reasoning_chars for item in assessments),
        input_tokens=sum(item.input_tokens for item in assessments),
        output_tokens=sum(item.output_tokens for item in assessments),
        reasoning_tokens=sum(item.reasoning_tokens for item in assessments),
        cache_read_tokens=sum(item.cache_read_tokens for item in assessments),
        cache_write_tokens=sum(item.cache_write_tokens for item in assessments),
        total_tokens=sum(item.total_tokens for item in assessments),
        steps=renumbered_steps,
    )


def compare_sessions(current: SessionAssessment, baseline: SessionAssessment) -> Dict[str, Dict[str, float]]:
    def metric_pair(name: str, current_value: float, baseline_value: float) -> Dict[str, float]:
        delta = current_value - baseline_value
        pct_delta = 0.0
        if baseline_value:
            pct_delta = (delta / baseline_value) * 100.0
        return {
            "current": current_value,
            "baseline": baseline_value,
            "delta": delta,
            "pct_delta": pct_delta,
        }

    return {
        "non_cache_total": metric_pair(
            "non_cache_total",
            current.input_tokens + current.output_tokens + current.reasoning_tokens,
            baseline.input_tokens + baseline.output_tokens + baseline.reasoning_tokens,
        ),
        "cache_read": metric_pair("cache_read", current.cache_read_tokens, baseline.cache_read_tokens),
        "total_tokens": metric_pair("total_tokens", current.total_tokens, baseline.total_tokens),
        "duration_seconds": metric_pair("duration_seconds", current.duration_seconds, baseline.duration_seconds),
        "step_count": metric_pair("step_count", current.step_count, baseline.step_count),
        "tool_call_count": metric_pair("tool_call_count", current.tool_call_count, baseline.tool_call_count),
        "output_tokens": metric_pair("output_tokens", current.output_tokens, baseline.output_tokens),
        "output_tokens_per_step": metric_pair(
            "output_tokens_per_step",
            current.output_tokens_per_step,
            baseline.output_tokens_per_step,
        ),
        "text_chars": metric_pair("text_chars", current.text_chars, baseline.text_chars),
        "text_chars_per_step": metric_pair(
            "text_chars_per_step",
            current.text_chars_per_step,
            baseline.text_chars_per_step,
        ),
    }


def top_tools_rows(assessment: SessionAssessment, limit: int = 8) -> List[tuple[str, int]]:
    return assessment.tool_counts.most_common(limit)


def iso_local(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")

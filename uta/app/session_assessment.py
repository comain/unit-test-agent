"""UTA-owned presentation over neutral, bounded session diagnostics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agent_core.harness import (
    AgentSessionRef,
    AvailableSessionDiagnostics,
    DiagnosticsLimits,
    SessionDiagnosticsProvider,
    SessionDiagnosticsReport,
    diagnose_sessions,
)

from uta.shared.config import settings
from uta.testgen.harness import create_agent_diagnostics_provider


@dataclass(frozen=True)
class SessionAssessment:
    session_id: str
    session_ids: List[str]
    session_refs: List[Dict[str, str]]
    status: str
    reason_codes: List[str] = field(default_factory=list)
    duration_seconds: Optional[float] = None
    step_count: Optional[int] = None
    tool_call_count: Optional[int] = None
    patch_count: Optional[int] = None
    tool_counts: Counter[str] = field(default_factory=Counter)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    usage_by_model: Dict[str, Dict[str, int]] = field(default_factory=dict)
    signals: List[Dict[str, Any]] = field(default_factory=list)
    truncated: bool = False

    @property
    def complete(self) -> bool:
        return self.status == "available"

    @property
    def output_tokens_per_step(self) -> Optional[float]:
        if self.output_tokens is None or self.step_count is None:
            return None
        return self.output_tokens / self.step_count if self.step_count else 0.0

    @property
    def output_tokens_per_tool_call(self) -> Optional[float]:
        if self.output_tokens is None or self.tool_call_count is None:
            return None
        return (
            self.output_tokens / self.tool_call_count
            if self.tool_call_count
            else 0.0
        )

    def top_tools(self, limit: int = 8) -> List[tuple[str, int]]:
        return self.tool_counts.most_common(limit)


def assess_sessions(
    session_ids: Iterable[str],
    database_path: Optional[Path] = None,
    *,
    harness_name: Optional[str] = None,
    provider: Optional[SessionDiagnosticsProvider] = None,
    limits: Optional[DiagnosticsLimits] = None,
) -> SessionAssessment:
    """Resolve durable locators through the configured harness capability."""
    name = str(harness_name or settings.agent_harness)
    locators = [str(item) for item in session_ids if item]
    refs = tuple(AgentSessionRef(name, locator) for locator in locators)
    if provider is None:
        provider = create_agent_diagnostics_provider(
            harness_name=name, database_path=database_path
        )
    providers = {name: provider} if provider is not None else {}
    report = diagnose_sessions(refs, providers=providers, limits=limits)
    return assessment_from_report(report)


def assessment_from_report(report: SessionDiagnosticsReport) -> SessionAssessment:
    refs = [item.session.as_dict() for item in report.items]
    locators = [ref["locator"] for ref in refs]
    statuses = [item.status.value for item in report.items]
    reasons = [
        item.reason_code.value
        for item in report.items
        if getattr(item, "reason_code", None) is not None
    ]
    available = [
        item for item in report.items if isinstance(item, AvailableSessionDiagnostics)
    ]
    complete = report.complete
    status = "available" if complete else _incomplete_status(statuses)
    usage = report.total_usage if complete else None

    tool_counts: Counter[str] = Counter()
    signals: List[Dict[str, Any]] = []
    for item in available:
        for step in item.steps:
            if step.tool_name:
                tool_counts[step.tool_name] += 1
        for signal in item.signals:
            signals.append(
                {
                    "category": signal.category.value,
                    "code": signal.code,
                    "count": signal.count,
                    "tool_name": signal.tool_name,
                }
            )

    return SessionAssessment(
        session_id=(locators[0] if len(locators) < 2 else f"{locators[0]}+{len(locators) - 1}") if locators else "",
        session_ids=locators,
        session_refs=refs,
        status=status,
        reason_codes=list(dict.fromkeys(reasons)),
        duration_seconds=_sum_optional(available, "duration_seconds") if complete else None,
        step_count=sum(len(item.steps) for item in available) if complete else None,
        tool_call_count=_sum_optional(available, "tool_calls") if complete else None,
        patch_count=_sum_optional(available, "patch_count") if complete else None,
        tool_counts=tool_counts,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        reasoning_tokens=usage.reasoning_tokens if usage else None,
        cache_read_tokens=usage.cache_read_tokens if usage else None,
        cache_write_tokens=usage.cache_write_tokens if usage else None,
        total_tokens=usage.total_tokens if usage else None,
        usage_by_model={entry.model: _usage_dict(entry.usage) for entry in report.usage_by_model},
        signals=signals,
        truncated=report.truncated,
    )


def compare_sessions(
    current: SessionAssessment, baseline: SessionAssessment
) -> Dict[str, Dict[str, Optional[float]]]:
    metrics = {
        "total_tokens": (current.total_tokens, baseline.total_tokens),
        "duration_seconds": (current.duration_seconds, baseline.duration_seconds),
        "step_count": (current.step_count, baseline.step_count),
        "tool_call_count": (current.tool_call_count, baseline.tool_call_count),
        "output_tokens": (current.output_tokens, baseline.output_tokens),
        "output_tokens_per_step": (
            current.output_tokens_per_step,
            baseline.output_tokens_per_step,
        ),
    }
    return {name: _metric_pair(*values) for name, values in metrics.items()}


def _metric_pair(
    current: Optional[float], baseline: Optional[float]
) -> Dict[str, Optional[float]]:
    if current is None or baseline is None:
        return {"current": current, "baseline": baseline, "delta": None, "pct_delta": None}
    delta = current - baseline
    return {
        "current": current,
        "baseline": baseline,
        "delta": delta,
        "pct_delta": (delta / baseline) * 100.0 if baseline else 0.0,
    }


def _incomplete_status(statuses: List[str]) -> str:
    distinct = list(dict.fromkeys(statuses))
    return distinct[0] if len(distinct) == 1 else "mixed"


def _sum_optional(items: List[AvailableSessionDiagnostics], field: str):
    values = [getattr(item, field) for item in items]
    return sum(values) if values and all(value is not None for value in values) else None


def _usage_dict(usage) -> Dict[str, int]:
    return {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "reasoning": usage.reasoning_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_write": usage.cache_write_tokens,
        "total": usage.total_tokens,
    }


__all__ = [
    "SessionAssessment",
    "assess_sessions",
    "assessment_from_report",
    "compare_sessions",
]

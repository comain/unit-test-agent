from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from uta.app.context import assemble_base_context
from uta.language.java.ci_evidence import (
    java_failure_diagnostics,
    test_enforcement_requirements,
)
from uta.language.java.maven_project import (
    declared_test_enforcement_tooling_status,
    has_declared_tooling_version_mismatch,
    test_enforcement_tooling_status,
)
from uta.language.python.ci_evidence import python_failure_diagnostics
from uta.shared.fix_sessions import can_create_fix_session
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus
from uta.enforcement.evidence import evidence_detail
from uta.enforcement.test_quality import summarize_test_quality_payloads

#: Report payloads are a view, not the record. Past this the HTML and
#: JSON responses stall the page rather than informing it.
MAX_REPORT_OUTPUT_BYTES = 128 * 1024

GMT8 = timezone(timedelta(hours=8))


class CiReportRenderer:
    def __init__(self) -> None:
        template_dir = Path(__file__).resolve().parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html"]),
        )
        self.env.filters["gmt8"] = format_gmt8

    def status_html(self, record: CiTaskRecord) -> str:
        return self.env.get_template("status.html").render(record=record, detail=self.detail(record))

    def report_html(self, record: CiTaskRecord) -> str:
        return self.env.get_template("report.html").render(record=record, detail=self.detail(record))

    def repair_progress_html(self, progress: Dict[str, Any]) -> str:
        session = progress.get("session") if isinstance(progress, dict) else None
        rerun_enforcement = session.get("rerunEnforcement") if isinstance(session, dict) else None
        refresh_rerun_enforcement = session.get("refreshRerunEnforcement") if isinstance(session, dict) else None
        progress = {
            **progress,
            "rerunEvidence": progress.get("rerunEvidence") or evidence_detail(rerun_enforcement),
            "refreshRerunEvidence": progress.get("refreshRerunEvidence") or evidence_detail(refresh_rerun_enforcement),
        }
        return self.env.get_template("repair_progress.html").render(progress=progress)

    def recent_jobs_html(
        self,
        records: list[CiTaskRecord],
        *,
        since: datetime,
        generated_at: datetime,
        hours: int,
        limit: int = 200,
    ) -> str:
        return self.env.get_template("recent_jobs.html").render(
            detail=self.recent_jobs_detail(records, since=since, generated_at=generated_at, hours=hours, limit=limit)
        )

    def recent_jobs_detail(
        self,
        records: list[CiTaskRecord],
        *,
        since: datetime,
        generated_at: datetime,
        hours: int,
        limit: int = 200,
    ) -> Dict[str, Any]:
        rows = [self._recent_job_row(record) for record in records]
        repair_sessions = self._recent_repair_session_rows(records)
        status_counts = Counter(row["status"] for row in rows)
        language_counts = Counter(row["language"] for row in rows)
        enforcement_counts = Counter(row["enforcementStatus"] for row in rows)
        return {
            "generatedAt": generated_at.isoformat(),
            "since": since.isoformat(),
            "hours": hours,
            "limit": limit,
            "total": len(rows),
            "statusCounts": dict(sorted(status_counts.items())),
            "languageCounts": dict(sorted(language_counts.items())),
            "enforcementCounts": dict(sorted(enforcement_counts.items())),
            "rows": rows,
            "repairSessions": repair_sessions,
        }

    def detail(self, record: CiTaskRecord) -> Dict[str, Any]:
        enforcement = self._enforcement_with_report_time_tooling(record)
        report_record = record.model_copy(update={"enforcement_result": enforcement})
        evidence = evidence_detail(enforcement)
        if not evidence.get("testQuality"):
            # Java repair evidence has no structured targetResults; fall back to
            # the session-level summary refreshed from the live repair task.
            evidence["testQuality"] = self._fix_session_test_quality(record.fix_sessions)
        return {
            "taskId": record.task_id,
            "status": record.status.value,
            "appName": record.request.app_name,
            "branch": record.request.branch,
            "gitUrl": record.request.git_url,
            "commitId": record.request.commit_id,
            "jiraId": record.request.jira_id,
            "operator": record.request.operator,
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "taskUrl": record.task_url,
            "reportUrl": record.report_url,
            "summary": record.summary,
            "displaySummary": self._display_summary(record),
            "enforcement": _report_enforcement(enforcement),
            "evidence": evidence,
            "failureDiagnostics": self._failure_diagnostics(enforcement),
            "testEnforcementRequirements": test_enforcement_requirements(enforcement),
            "canCreateFixSession": can_create_fix_session(report_record),
            "callback": {
                "succeeded": record.callback_succeeded,
                "error": record.callback_error,
                "history": record.callback_history,
            },
            "context": self._context_detail(record),
            "fixSessions": record.fix_sessions,
        }

    @staticmethod
    def _enforcement_with_report_time_tooling(record: CiTaskRecord) -> Dict[str, Any] | None:
        enforcement = record.enforcement_result
        if not isinstance(enforcement, dict):
            return enforcement
        evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
        if isinstance(evidence, dict) and isinstance(evidence.get("tooling"), dict):
            return enforcement
        language = str(enforcement.get("language") or record.request.language or "").lower()
        summary = str(enforcement.get("summary") or record.summary or "")
        if language != "java":
            return enforcement
        workspace_path = Path(record.workspace_path or "")
        if not workspace_path.is_dir():
            return enforcement
        tooling = declared_test_enforcement_tooling_status(workspace_path)
        if not has_declared_tooling_version_mismatch(tooling):
            if "Maven build did not compile or resolve" not in summary:
                return enforcement
            tooling = test_enforcement_tooling_status(workspace_path)
        if tooling.available:
            return enforcement
        updated_evidence = dict(evidence or {})
        updated_evidence["tooling"] = {
            "available": tooling.available,
            "artifactId": tooling.artifact_id,
            "version": tooling.version,
            "requiredVersion": tooling.required_version,
            "reason": tooling.reason,
        }
        return {**enforcement, "evidence": updated_evidence}

    @staticmethod
    def _recent_job_row(record: CiTaskRecord) -> Dict[str, Any]:
        enforcement = record.enforcement_result if isinstance(record.enforcement_result, dict) else {}
        enforcement_status = enforcement.get("status")
        if not enforcement_status:
            enforcement_status = "failed" if record.status == CiTaskStatus.failed else "pending"
        fix_status_counts = Counter(
            str(session.get("status") or "unknown")
            for session in record.fix_sessions
            if isinstance(session, dict)
        )
        return {
            "taskId": record.task_id,
            "status": record.status.value,
            "appName": record.request.app_name,
            "branch": record.request.branch,
            "gitUrl": record.request.git_url,
            "commitId": record.request.commit_id,
            "jiraId": record.request.jira_id,
            "operator": record.request.operator,
            "language": record.request.language,
            "rdcTaskId": record.request.task_id,
            "recordId": record.request.record_id,
            "taskTemplateId": record.request.task_template_id,
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "taskUrl": record.task_url,
            "reportUrl": record.report_url,
            "summary": record.summary,
            "enforcementStatus": str(enforcement_status),
            "enforcementPassed": enforcement.get("passed"),
            "callbackSucceeded": record.callback_succeeded,
            "callbackError": record.callback_error,
            "fixSessionCount": len(record.fix_sessions),
            "fixSessionStatusCounts": dict(sorted(fix_status_counts.items())),
            "gateOverride": record.gate_override,
            "canStop": record.status in {
                CiTaskStatus.queued,
                CiTaskStatus.pending,
                CiTaskStatus.running,
            },
            "canRetry": record.status in {
                CiTaskStatus.success,
                CiTaskStatus.failed,
                CiTaskStatus.stopped,
            },
            "canRdcCallback": (
                record.protocol == "rdc"
                and bool(record.request.task_id and record.request.record_id)
            ),
        }

    @staticmethod
    def _recent_repair_session_rows(records: Sequence[CiTaskRecord]) -> list[Dict[str, Any]]:
        rows: list[Dict[str, Any]] = []
        for record in records:
            for session in record.fix_sessions:
                if not isinstance(session, dict):
                    continue
                session_id = str(session.get("sessionId") or "")
                if not session_id:
                    continue
                updated_at = str(session.get("updatedAt") or session.get("createdAt") or record.updated_at.isoformat())
                rows.append(
                    {
                        "taskId": record.task_id,
                        "appName": record.request.app_name,
                        "branch": record.request.branch,
                        "jiraId": record.request.jira_id,
                        "language": record.request.language,
                        "sessionId": session_id,
                        "status": str(session.get("status") or "unknown"),
                        "repoTaskId": session.get("repoTaskId"),
                        "repoTaskStatus": session.get("repoTaskStatus"),
                        "repoTaskStage": session.get("repoTaskStage"),
                        "budgetUsed": session.get("budgetUsed") if isinstance(session.get("budgetUsed"), dict) else {},
                        "budgetUsedText": _format_budget_used(session.get("budgetUsed")),
                        "retryCount": session.get("retryCount", 0),
                        "createdAt": str(session.get("createdAt") or record.created_at.isoformat()),
                        "updatedAt": updated_at,
                        "progressUrl": f"../reports/{record.task_id}/fix-sessions/{session_id}/progress",
                    }
                )
        rows.sort(
            key=lambda row: (
                _numeric_sort_value(row.get("repoTaskId")),
                row["updatedAt"],
                row["sessionId"],
            ),
            reverse=True,
        )
        return rows


    @classmethod
    def _failure_diagnostics(cls, enforcement: Dict[str, Any] | None) -> list[Dict[str, Any]]:
        if not isinstance(enforcement, dict):
            return []
        evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
        diagnostics: list[Dict[str, Any]] = []
        diagnostics.extend(java_failure_diagnostics(enforcement, evidence))
        diagnostics.extend(python_failure_diagnostics(enforcement, evidence))
        return diagnostics

    @classmethod
    def _display_summary(cls, record: CiTaskRecord) -> str:
        summary = str(record.summary or "")
        enforcement = record.enforcement_result if isinstance(record.enforcement_result, dict) else {}
        evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
        if (
            str(enforcement.get("language") or evidence.get("language") or record.request.language or "") == "python"
            and str(evidence.get("reasonCode") or enforcement.get("reasonCode") or "") == "test_failed"
        ):
            diagnostics = python_failure_diagnostics(enforcement, evidence)
            if diagnostics:
                first = diagnostics[0]
                target = first.get("target") or "unknown target"
                error = first.get("errorExcerpt") or "selected unit test failed"
                return f"Python enforcement failed: test_failed for {target}: {error}"
            return "Python enforcement failed: selected unit test failed before coverage/mutation verification"
        if len(summary) > 1200:
            return summary[:1200].rstrip() + "\n... full command output is available below ..."
        return summary



    @staticmethod
    def _fix_session_test_quality(fix_sessions: Sequence[Any]) -> Dict[str, Any] | None:
        summary = summarize_test_quality_payloads(
            session.get("testQuality")
            for session in fix_sessions or []
            if isinstance(session, dict) and isinstance(session.get("testQuality"), dict)
        )
        if not summary:
            return None
        summary.pop("scannerFailureCount", None)
        summary["source"] = "fix_sessions"
        return summary











    @staticmethod
    def _context_detail(record: CiTaskRecord) -> Dict[str, Any]:
        context = CiReportRenderer._latest_repair_context(record) or assemble_base_context(record)
        return {
            "sources": context.get("sources") or [],
            "missingReasons": context.get("missingReasons") or [],
        }

    @staticmethod
    def _latest_repair_context(record: CiTaskRecord) -> Dict[str, Any] | None:
        for session in reversed(record.fix_sessions):
            context = session.get("rdcContext")
            if isinstance(context, dict) and context:
                return context
        return None


def _report_enforcement(enforcement: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Keep report payloads responsive without discarding authoritative evidence.

    The task record retains full stdout, stderr, and structured evidence for
    gate parsing and repair planning. Report callers receive the normalized
    evidence separately, so repeating the raw evidence here only creates
    multi-megabyte HTML/JSON responses and can delay the fix-session
    JavaScript from loading.
    """
    if not isinstance(enforcement, dict):
        return enforcement
    report_value = {key: value for key, value in enforcement.items() if key != "evidence"}
    for stream_name in ("stdout", "stderr"):
        if stream_name in report_value:
            report_value[stream_name] = _bounded_report_text(report_value[stream_name])
    return report_value


def _bounded_report_text(value: Any, max_bytes: int = MAX_REPORT_OUTPUT_BYTES) -> str:
    text = str(value or "")
    payload = text.encode("utf-8")
    if len(payload) <= max_bytes:
        return text
    head_size = max_bytes // 4
    tail_size = max_bytes - head_size
    omitted = len(payload) - max_bytes
    marker = (
        f"\n... output truncated in report: {omitted} bytes omitted; "
        "full output remains in task evidence ...\n"
    )
    return (
        payload[:head_size].decode("utf-8", errors="replace")
        + marker
        + payload[-tail_size:].decode("utf-8", errors="replace")
    )


def _numeric_sort_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _format_budget_used(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "N/A"
    actual_cost = _optional_float(value.get("actualCostUsd"))
    estimated_cost = _optional_float(value.get("estimatedCostUsd"))
    total_tokens = _optional_int(value.get("totalTokens"))
    parts: list[str] = []
    if actual_cost is not None:
        cost_text = f"${actual_cost:.4f}"
        if estimated_cost is not None:
            cost_text = f"{cost_text} / ${estimated_cost:.4f}"
        parts.append(cost_text)
    elif estimated_cost is not None:
        parts.append(f"$0.0000 / ${estimated_cost:.4f}")
    if total_tokens:
        parts.append(f"{_format_compact_number(total_tokens)} tokens")
    return " · ".join(parts) if parts else "N/A"


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_gmt8(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(GMT8).strftime("%Y-%m-%d %H:%M:%S GMT+8")

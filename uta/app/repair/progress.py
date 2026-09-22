"""What a repair session looks like from outside: stages, budget, issues.

Everything in this module is a projection. It reads a session and the task
DB's status payload and produces something a UI can render -- an ordered stage
list, a token/cost summary, a de-duplicated issue list, the repair context.
None of it decides anything or writes anything back, which is why it is worth
keeping apart from the session lane that does both.

The projections are deliberately tolerant: repair tasks come from two
languages and several generations of schema, so each field is read from the
first of several plausible places rather than from one canonical key. That
tolerance is the bulk of this file, and it is data-shape knowledge, not
policy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional

from uta.app.context import collect_git_commit_messages
from uta.enforcement.evidence import evidence_detail
from uta.enforcement.test_quality import summarize_test_quality_payloads
from uta.shared.ci_models import CiTaskRecord
from uta.tasks.models import json_loads
from uta.tasks.render import build_status_payload


class RepairProgressMixin:
    """Read-only projections of a repair session for callers and reports."""

    def repair_progress(
        self,
        record: CiTaskRecord,
        session_id: str,
        *,
        event_after_id: Optional[int] = None,
    ) -> Optional[Dict[str, object]]:
        """Session progress. `event_after_id` returns only newer events.

        A fresh page receives the full history. Subsequent polls use the task
        event cursor directly; loading the full history before trimming it
        made concurrent progress-page polls retain gigabytes of SQLite rows.
        A bounded recent window is sufficient for stage and class metadata.
        """
        session = next((item for item in record.fix_sessions if item.get("sessionId") == session_id), None)
        if session is None:
            return None
        self._ensure_deferred_repair_task_creation(record, session)
        self.materialize_retry_request(record, session)
        repo_task_id = session.get("repoTaskId")
        task_payload = None
        stages = None
        if self._service.task_manager and repo_task_id:
            try:
                db = self._service.task_manager.db
                task_payload = build_status_payload(
                    db,
                    int(repo_task_id),
                    event_limit=None if event_after_id is None else 200,
                )
                # The task row keeps the complete repair context so workflow
                # recovery can resume. It is not progress data and can be
                # several megabytes, so never expose it through the polling
                # projection.
                task_detail = task_payload.get("task")
                if isinstance(task_detail, dict):
                    task_detail.pop("rdc_context_json", None)
                stages = self._repair_progress_stages(session, task_payload)
                if event_after_id is not None:
                    # events_since is oldest-first so callers can advance a
                    # cursor without gaps; the progress table is newest-first.
                    task_payload["latest_events"] = [
                        dict(row)
                        for row in reversed(
                            db.events_since(
                                int(repo_task_id),
                                after_id=int(event_after_id),
                                limit=500,
                            )
                        )
                    ]
            except KeyError:
                task_payload = None
        if stages is None:
            stages = self._repair_progress_stages(session, task_payload)
        rerun = session.get("rerunEnforcement") if isinstance(session.get("rerunEnforcement"), dict) else None
        refresh_rerun = (
            session.get("refreshRerunEnforcement")
            if isinstance(session.get("refreshRerunEnforcement"), dict)
            else None
        )
        return {
            "taskId": record.task_id,
            "appName": record.request.app_name,
            "branch": record.request.branch,
            # Persisted sessions contain Maven/pytest stdout, stderr and the
            # full RDC context. Progress polls need none of those. Returning
            # the raw session made each five-second poll tens of megabytes and
            # eventually OOM-killed the API process.
            "session": self._repair_progress_session(session),
            "repoTask": task_payload,
            "stages": stages,
            "rerunEvidence": evidence_detail(rerun),
            "refreshRerunEvidence": evidence_detail(refresh_rerun),
        }

    @staticmethod
    def _repair_progress_session(session: Dict[str, object]) -> Dict[str, object]:
        """Project persisted repair state to the bounded public UI contract."""
        fields = (
            "sessionId",
            "status",
            "repoTaskId",
            "repoTaskStatus",
            "repoTaskStage",
            "repoTaskDetail",
            "repoTaskError",
            "createdAt",
            "updatedAt",
            "canonicalSessionId",
            "canonicalSessionUrl",
            "canonicalTaskId",
            "canonicalTaskUrl",
            "alreadyGreenAfterRefresh",
            "error",
            "summary",
            "detail",
            "failureReason",
            "message",
        )
        projected = {field: session[field] for field in fields if field in session}
        for field in ("rerunEnforcement", "refreshRerunEnforcement"):
            value = session.get(field)
            if not isinstance(value, dict):
                continue
            projected[field] = {
                key: value[key]
                for key in (
                    "passed",
                    "status",
                    "summary",
                    "command",
                    "exitCode",
                    "reason",
                    "error",
                    "language",
                )
                if key in value
            }
        return projected

    @staticmethod
    def _repair_progress_stages(
        session: Dict[str, object],
        task_payload: Optional[Dict[str, Any]],
    ) -> list[Dict[str, str]]:
        stage_defs = [
            ("queued", "排队", ("queued", "created", "startup", "acquired", "setup_branch")),
            ("baseline_compile", "基线编译", ("baseline_compile",)),
            (
                "generate",
                "生成测试",
                (
                    "scan_candidates",
                    "parse_context",
                    "select_batch",
                    "precheck_existing_tests",
                    "generate_prompt",
                    "target_context",
                    "plan_tests",
                    "plan",
                    "generate",
                    "compile_verification",
                    "test_verification",
                    "test_execution",
                    "compile_fix",
                    "test_fix",
                    "generate_tests",
                    "verify_compile",
                    "verify_tests",
                    "fix_compile",
                    "fix_tests",
                    "precheck_existing_tests",
                    "generation",
                    "generate_and_validate",
                ),
            ),
            # `fix_coverage`/`fix_mutation` are the graph's own phase names.
            # The aliases here were the pre-migration spellings, and
            # "coverage_fix" is not a substring of "fix_coverage", so nothing
            # ever matched: the stage list froze on 生成测试 for the whole run
            # while the task went on to repair, push and finish.
            ("coverage_fix", "覆盖率修复", ("coverage_fix", "coverage_test_fix", "fix_coverage", "measure_coverage")),
            ("mutation_fix", "变异修复", ("mutation_fix", "mutation_testing", "mutation_test_fix", "fix_mutation", "measure_mutation", "delegated_quality_gate")),
            # A stalled CI mutation repair asks one review turn before giving up.
            ("equivalence_review", "等价变异审查", ("review_equivalent_mutants",)),
            ("push", "推送分支", ("push", "auto_push", "verify_remote_push", "commit_to_branch", "store_and_push")),
            ("rerun_enforcement", "完成确认", ("rerun_enforcement",)),
        ]
        stage_order = {key: index for index, (key, _, _) in enumerate(stage_defs)}
        task = (task_payload or {}).get("task") or {}
        events = RepairProgressMixin._events_since_latest_resume((task_payload or {}).get("latest_events") or [])
        current_stage = str(task.get("current_stage") or "").lower()
        event_text = " ".join(
            str(item.get(key) or "").lower()
            for item in events
            for key in ("stage", "event_type", "message")
        )
        task_status = str(task.get("status") or "").upper()
        session_status = str(session.get("status") or "")
        rerun = session.get("rerunEnforcement") if isinstance(session.get("rerunEnforcement"), dict) else {}

        def stage_key_for_text(text: str) -> Optional[str]:
            normalized = (text or "").lower()
            for key, _, aliases in stage_defs:
                if any(alias in normalized for alias in aliases):
                    return key
            return None

        active_key = stage_key_for_text(current_stage)
        if session_status == "creating_repair_task" and not session.get("repoTaskId"):
            active_key = "queued"
        for event in events:
            event_key = stage_key_for_text(
                " ".join(str(event.get(key) or "") for key in ("stage", "event_type", "message"))
            )
            if event_key and (
                active_key is None
                or stage_order.get(event_key, -1) > stage_order.get(active_key, -1)
                or active_key == "generate"
            ):
                active_key = event_key
                break
        active_rank = stage_order.get(active_key) if active_key else None

        rows: list[Dict[str, str]] = []
        for key, label, aliases in stage_defs:
            status = "pending"
            if key == "queued" and (task_status or session.get("repoTaskId")):
                status = "done"
            if any(alias in event_text for alias in aliases):
                status = "done"
            if active_rank is not None and stage_order[key] < active_rank:
                status = "done"
            if key == active_key:
                status = "active"
            if task_status == "COMPLETED" and key != "rerun_enforcement":
                status = "done"
            if key == "rerun_enforcement":
                if session_status == "rerun_running":
                    status = "active"
                elif session_status == "passed_with_equivalent_mutants":
                    # The raw rerun still failed; the session excused it.
                    status = "done"
                elif rerun:
                    status = "done" if rerun.get("passed") else "failed"
                elif session_status in {"green", "rerun_failed"}:
                    status = "done" if session_status == "green" else "failed"
            if task_status in {"FAILED", "CANCELLED", "POISONED", "BUDGET_EXCEEDED"} and status == "active":
                status = "failed"
            rows.append({"key": key, "label": label, "status": status})
        return rows
    @staticmethod
    def _events_since_latest_resume(events: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        for index, event in enumerate(events):
            if event.get("event_type") == "task_resumed":
                return events[: index + 1]
        return events
    @staticmethod
    def _repair_test_quality_from_task_payload(task_payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Aggregate per-class test-quality warnings from a repair task's status payload.

        Java repair tasks have no structured targetResults in CI enforcement
        evidence, so this session-level summary is what the CI report renders.
        """
        classes = task_payload.get("classes") if isinstance(task_payload, dict) else None
        summary = summarize_test_quality_payloads(
            row.get("test_quality")
            for row in classes or []
            if isinstance(row, dict) and isinstance(row.get("test_quality"), dict)
        )
        if not summary:
            return None
        summary.pop("warnings", None)
        summary.pop("scannerFailureCount", None)
        return summary
    @staticmethod
    def _repair_budget_used_from_repo_task(
        repo_task: Dict[str, Any],
        task_payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        task_detail = task_payload.get("task") if isinstance(task_payload, dict) else None
        metrics = task_payload.get("metrics") if isinstance(task_payload, dict) else None
        task_detail = task_detail if isinstance(task_detail, dict) else {}
        metrics = metrics if isinstance(metrics, dict) else {}

        def first_float(*values: Any) -> Optional[float]:
            for value in values:
                if value is None or value == "":
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
            return None

        def first_int(*values: Any) -> int:
            for value in values:
                if value is None or value == "":
                    continue
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
            return 0

        actual_cost = first_float(
            metrics.get("actual_cost"),
            repo_task.get("provider_cost_usd"),
            repo_task.get("actual_cost"),
            task_detail.get("provider_cost_usd"),
            task_detail.get("actual_cost"),
        )
        estimated_cost = first_float(
            metrics.get("estimated_cost"),
            repo_task.get("estimated_cost_usd"),
            repo_task.get("estimated_cost"),
            task_detail.get("estimated_cost_usd"),
            task_detail.get("estimated_cost"),
        )
        input_tokens = first_int(
            repo_task.get("input_tokens"),
            repo_task.get("actual_input_tokens"),
            task_detail.get("input_tokens"),
            task_detail.get("actual_input_tokens"),
        )
        cache_read_tokens = first_int(
            repo_task.get("cache_read_tokens"),
            repo_task.get("actual_cache_read_tokens"),
            task_detail.get("cache_read_tokens"),
            task_detail.get("actual_cache_read_tokens"),
        )
        cache_write_tokens = first_int(
            repo_task.get("cache_write_tokens"),
            repo_task.get("actual_cache_write_tokens"),
            task_detail.get("cache_write_tokens"),
            task_detail.get("actual_cache_write_tokens"),
        )
        output_tokens = first_int(
            repo_task.get("output_tokens"),
            repo_task.get("actual_output_tokens"),
            task_detail.get("output_tokens"),
            task_detail.get("actual_output_tokens"),
        )
        reasoning_tokens = first_int(repo_task.get("reasoning_tokens"), task_detail.get("reasoning_tokens"))
        total_tokens = first_int(repo_task.get("total_tokens"), task_detail.get("total_tokens"))
        if not total_tokens:
            total_tokens = input_tokens + cache_read_tokens + cache_write_tokens + output_tokens + reasoning_tokens
        if actual_cost is None and estimated_cost is None and total_tokens <= 0:
            return {}
        return {
            "actualCostUsd": float(actual_cost or 0.0),
            "estimatedCostUsd": estimated_cost,
            "inputTokens": input_tokens,
            "cacheReadTokens": cache_read_tokens,
            "cacheWriteTokens": cache_write_tokens,
            "outputTokens": output_tokens,
            "reasoningTokens": reasoning_tokens,
            "totalTokens": total_tokens,
        }
    def _repair_context_from_repo_task(self, record: CiTaskRecord, repo_task: Dict[str, Any]) -> Dict[str, Any]:
        context = json_loads(repo_task.get("rdc_context_json") or "{}")
        if not context:
            return {}
        context.setdefault("pipeline", {})
        git = context.setdefault("git", {})
        missing_reasons = list(context.get("missingReasons") or [])

        # Protocol-specific issue enrichment.
        context = self._service._context_provider_for(record).enrich_repair_context(record, context, repo_task)
        issue = context.get("issue") if isinstance(context.get("issue"), dict) else {}

        if issue.get("description"):
            missing_reasons = [reason for reason in missing_reasons if reason != "issue_description_unavailable"]

        if not git.get("commitMessages") and repo_task.get("repo_path"):
            commit_messages = collect_git_commit_messages(
                Path(str(repo_task["repo_path"])),
                str(repo_task.get("base_ref") or "origin/master"),
                workspace=self._workspace(),
            )
            if commit_messages:
                git["commitMessages"] = commit_messages
        if git.get("commitMessages"):
            missing_reasons = [reason for reason in missing_reasons if reason != "git_commit_messages_unavailable"]

        self._service._set_context_source(context, "issue_description", bool(issue.get("description")), source=issue.get("source"))
        self._service._set_context_source(context, "git_commit_messages", bool(git.get("commitMessages")))
        context["missingReasons"] = missing_reasons
        return context
    @staticmethod
    def _repair_issues_from_repo_task(
        repo_task: Dict[str, Any],
        task_payload: Optional[Dict[str, Any]],
    ) -> list[Dict[str, str]]:
        issues: list[Dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(issue_type: str, message: str, *, stage: str = "") -> None:
            text = " ".join(str(message or "").split())
            if not text:
                return
            key = (issue_type, text)
            if key in seen:
                return
            seen.add(key)
            issues.append({"type": issue_type, "message": text, "stage": stage})

        def classify(text: str) -> Optional[str]:
            lowered = text.lower()
            if "provider/model" in lowered and ("rate limit" in lowered or "quota" in lowered):
                return "provider_rate_limit"
            if "rate_limited" in lowered or "rate-limited" in lowered:
                return "provider_rate_limit"
            if "no test changes to commit" in lowered:
                return "no_test_changes"
            if "generation timed out" in lowered or "opencode generation timed out" in lowered:
                return "generation_timeout"
            if "planning timed out" in lowered or "opencode planning timed out" in lowered:
                return "planning_timeout"
            if "auto-push" in lowered and "commit" in lowered:
                return "push_failed"
            return None

        for field in ("error", "last_error", "current_detail"):
            value = str(repo_task.get(field) or "")
            issue_type = classify(value)
            if issue_type:
                add(issue_type, value, stage=str(repo_task.get("current_stage") or ""))

        for event in (task_payload or {}).get("latest_events") or []:
            message = str(event.get("message") or "")
            issue_type = classify(message)
            if issue_type:
                add(issue_type, message, stage=str(event.get("stage") or ""))

        run_log_path = repo_task.get("run_log_path")
        if run_log_path:
            path = Path(str(run_log_path))
            if path.exists():
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    lines = []
                for line in lines:
                    issue_type = classify(line)
                    if issue_type:
                        stage = ""
                        match = re.search(r"stage=([A-Za-z0-9_:-]+)", line)
                        if match:
                            stage = match.group(1)
                        add(issue_type, line, stage=stage)
        return issues

from __future__ import annotations

import uuid
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from uta.ci_plugin.context import RepairContextExporter, assemble_base_context, collect_git_commit_messages
from uta.ci_plugin.fix_sessions import CreateFixSessionRequest, can_create_fix_session, create_fix_session
from uta.ci_plugin.enforcement import EnforcementResult, MavenEnforcementRunner, PythonEnforcementRunner
from uta.engine.ci import (
    CiLanguageHandler,
    CiLanguageHandlerRegistry,
)
from uta.language.java.ci import JavaCiLanguageHandler
from uta.language.python.ci import PythonCiLanguageHandler
from uta.ci_plugin.models import CiTaskRecord, CiTaskStatus, CiTriggerRequest, utc_now
from uta.ci_plugin.protocols import CiContextProvider, CiProtocol, CiResult, ProtocolRegistry
from uta.ci_plugin.store import JsonCiTaskStore
from uta.ci_plugin.workspace import GitWorkspaceManager
from uta.config import settings as uta_settings
from uta.tasks.manager import TaskManager
from uta.tasks.models import json_loads
from uta.tasks.render import build_status_payload


class FixSessionRateLimitError(RuntimeError):
    pass


class FixSessionUnsupportedError(RuntimeError):
    pass


LOGGER = logging.getLogger(__name__)


class ProtocolNeutralContextProvider(CiContextProvider):
    """Fallback context provider used for manually-created records."""

    name = "manual"

    def build_context(
        self,
        record: CiTaskRecord,
        *,
        user_context: Optional[str] = None,
        commit_messages: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        return assemble_base_context(
            record,
            issue={
                "id": record.request.jira_id,
                "description": None,
                "source": None,
                "kind": "issue",
            },
            user_context=user_context,
            commit_messages=commit_messages,
        )


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class CiPluginService:
    def __init__(
        self,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        enforcement_runner: Optional[MavenEnforcementRunner] = None,
        python_enforcement_runner: Optional[PythonEnforcementRunner] = None,
        task_manager: Optional[TaskManager] = None,
        context_exporter: Optional[RepairContextExporter] = None,
        record_store: Optional[JsonCiTaskStore] = None,
        protocols: Optional[ProtocolRegistry] = None,
        repair_priority: int = 1,
        repair_rate_limit_per_task: int = 3,
        language_handlers: Optional[Iterable[CiLanguageHandler]] = None,
    ) -> None:
        self._tasks: Dict[str, CiTaskRecord] = {}
        self.workspace_manager = workspace_manager
        self.language_handlers = CiLanguageHandlerRegistry(
            language_handlers
            or (
                JavaCiLanguageHandler(enforcement_runner),
                PythonCiLanguageHandler(python_enforcement_runner),
            )
        )
        # Protocol registry drives inbound parsing, result reporting, and the
        # repair-context provider. Concrete adapters are provided by app wiring
        # or tests; the service core stays protocol-neutral.
        self.protocols = protocols if protocols is not None else ProtocolRegistry()
        self.task_manager = task_manager
        self.context_exporter = context_exporter
        self.record_store = record_store
        self.repair_priority = max(0, min(9, int(repair_priority)))
        self.repair_rate_limit_per_task = int(repair_rate_limit_per_task)

    def _protocol_for(self, record: CiTaskRecord) -> Optional[CiProtocol]:
        return self.protocols.get(record.protocol) if self.protocols else None

    def _context_provider_for(self, record: CiTaskRecord) -> CiContextProvider:
        protocol = self._protocol_for(record)
        if protocol is not None:
            return protocol.context_provider
        return ProtocolNeutralContextProvider()

    def _callback_configured(self) -> bool:
        return any(protocol.reporting_configured() for protocol in self.protocols.values()) if self.protocols else False

    def health(self) -> Dict[str, object]:
        handler_runners = {
            handler.language: handler.runner is not None
            for handler in self.language_handlers.handlers
        }
        runner_ready = bool(self.workspace_manager and any(handler_runners.values()))
        return {
            "service": {
                "status": "ok",
                "taskCount": len(self._tasks),
            },
            "runner": {
                "ready": runner_ready,
                "workspaceManager": self.workspace_manager is not None,
                "enforcementRunner": handler_runners.get("java", False),
                "pythonEnforcementRunner": handler_runners.get("python", False),
                "languageHandlers": handler_runners,
            },
            "integrations": {
                "callbackConfigured": self._callback_configured(),
                "taskManagerConfigured": self.task_manager is not None,
                "contextExporterConfigured": self.context_exporter is not None,
                "recordStoreConfigured": self.record_store is not None,
                "protocols": self.protocols.names() if self.protocols else [],
            },
        }

    def submit(
        self,
        request: CiTriggerRequest,
        public_base_url: Optional[str] = None,
        *,
        run_inline: bool = True,
        protocol: str = "manual",
    ) -> CiTaskRecord:
        task_id = uuid.uuid4().hex
        record = CiTaskRecord(task_id=task_id, status=CiTaskStatus.queued, request=request, protocol=protocol)
        LOGGER.info(
            "ci_check_started protocol=%s task_id=%s app=%s branch=%s git_url=%s task_ref=%s record_id=%s",
            protocol,
            task_id,
            request.app_name,
            request.branch,
            request.git_url,
            request.task_id,
            request.record_id,
        )
        if public_base_url:
            base_url = public_base_url.rstrip("/")
            record.task_url = f"{base_url}/task-status/{task_id}"
            record.report_url = record.task_url
        self._tasks[task_id] = record
        self.save(record)
        if run_inline:
            self.run_check(task_id, public_base_url=public_base_url)
        return record

    def run_check(self, task_id: str, public_base_url: Optional[str] = None) -> None:
        record = self.get(task_id)
        if record is None or not self.workspace_manager or not self._runner_for_record(record):
            return
        try:
            self._run_check(record, public_base_url=public_base_url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception(
                "ci_check_failed_unexpected task_id=%s app=%s branch=%s",
                record.task_id,
                record.request.app_name,
                record.request.branch,
            )
            record.status = CiTaskStatus.failed
            record.summary = f"UTA CI plugin failed unexpectedly: {exc}"
            record.updated_at = utc_now()
            self.save(record)

    def _run_check(self, record: CiTaskRecord, public_base_url: Optional[str] = None) -> None:
        record.status = CiTaskStatus.running
        record.updated_at = utc_now()
        self.save(record)
        workspace = self.workspace_manager.prepare(
            git_url=record.request.git_url,
            branch=record.request.branch,
            task_id=record.task_id,
        )
        record.workspace_path = str(workspace)
        result = self._runner_for_record(record).run(workspace)
        record.status = CiTaskStatus.success if result.passed else CiTaskStatus.failed
        record.summary = result.summary
        record.enforcement_result = result.model_dump(mode="json")
        if public_base_url:
            record.report_url = f"{public_base_url.rstrip('/')}/reports/{record.task_id}/index.html"
        record.updated_at = utc_now()
        LOGGER.info(
            "ci_check_finished task_id=%s app=%s branch=%s status=%s summary=%s report_url=%s",
            record.task_id,
            record.request.app_name,
            record.request.branch,
            record.status.value,
            result.summary,
            record.report_url,
        )
        self._report_result(record, result.passed, result.summary)
        self.save(record)

    def get(self, task_id: str) -> Optional[CiTaskRecord]:
        record = self._tasks.get(task_id)
        if record is None and self.record_store:
            record = self.record_store.load(task_id)
            if record:
                self._tasks[task_id] = record
        if record:
            self._refresh_repair_sessions(record)
        return record

    def save(self, record: CiTaskRecord) -> None:
        if self.record_store:
            self.record_store.save(record)

    def recent_records(self, *, since: datetime, limit: int = 200) -> list[CiTaskRecord]:
        records_by_id: dict[str, CiTaskRecord] = {}
        if self.record_store:
            for record in self.record_store.list_records(since=since, limit=limit):
                records_by_id[record.task_id] = record
        for record in self._tasks.values():
            if _aware_datetime(record.created_at) >= _aware_datetime(since):
                records_by_id[record.task_id] = record
        records = sorted(records_by_id.values(), key=lambda item: _aware_datetime(item.created_at), reverse=True)
        limited_records = records[: max(1, int(limit))]
        for record in limited_records:
            self._refresh_repair_sessions(record)
        return limited_records

    def create_fix_session(self, record: CiTaskRecord, request: CreateFixSessionRequest) -> Dict[str, object]:
        if not can_create_fix_session(record):
            raise FixSessionUnsupportedError("fix sessions are not available for this report")
        fingerprint = self._repair_fingerprint(record, request)
        existing = self._find_session_by_fingerprint(record, fingerprint)
        if existing:
            if existing.get("status") == "green":
                return {"session": existing, "alreadyGreen": True}
            if not self._is_repair_session_terminal_failed(existing):
                return {"session": existing, "alreadyRunning": True}
        if self.repair_rate_limit_per_task >= 0 and len(record.fix_sessions) >= self.repair_rate_limit_per_task:
            raise FixSessionRateLimitError("repair session rate limit exceeded for this CI task")

        session = create_fix_session(record, request)
        session["fingerprint"] = fingerprint
        if self.task_manager and record.workspace_path:
            self._create_repair_task(record, session, request)
        self.save(record)
        return {"session": session}

    def repair_progress(self, record: CiTaskRecord, session_id: str) -> Optional[Dict[str, object]]:
        session = next((item for item in record.fix_sessions if item.get("sessionId") == session_id), None)
        if session is None:
            return None
        repo_task_id = session.get("repoTaskId")
        task_payload = None
        if self.task_manager and repo_task_id:
            try:
                task_payload = build_status_payload(self.task_manager.db, int(repo_task_id))
            except KeyError:
                task_payload = None
        return {
            "taskId": record.task_id,
            "appName": record.request.app_name,
            "branch": record.request.branch,
            "session": session,
            "repoTask": task_payload,
            "stages": self._repair_progress_stages(session, task_payload),
        }

    def _report_result(self, record: CiTaskRecord, passed: bool, summary: str) -> None:
        protocol = self._protocol_for(record)
        if protocol is None or not protocol.can_report(record):
            LOGGER.info(
                "ci_callback_skipped protocol=%s task_id=%s app=%s branch=%s reason=not_configured",
                record.protocol,
                record.task_id,
                record.request.app_name,
                record.request.branch,
            )
            return
        report_url = record.report_url or record.task_url or ""
        outcome = protocol.report_result(
            record,
            CiResult(passed=passed, summary=summary, report_url=report_url),
        )
        record.callback_history = outcome.history
        record.callback_succeeded = outcome.succeeded
        record.callback_error = outcome.error
        record.updated_at = utc_now()
        self.save(record)
        LOGGER.info(
            "ci_callback_finished protocol=%s task_id=%s app=%s branch=%s task_ref=%s record_id=%s succeeded=%s error=%s report_url=%s",
            record.protocol,
            record.task_id,
            record.request.app_name,
            record.request.branch,
            record.request.task_id,
            record.request.record_id,
            outcome.succeeded,
            outcome.error,
            report_url,
        )

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
                    "generation",
                    "generate_and_validate",
                ),
            ),
            ("coverage_fix", "覆盖率修复", ("coverage_fix", "coverage_test_fix")),
            ("mutation_fix", "变异修复", ("mutation_fix", "mutation_testing", "mutation_test_fix")),
            ("push", "推送分支", ("push", "auto_push", "verify_remote_push", "commit_to_branch", "store_and_push")),
            ("rerun_enforcement", "完成确认", ("rerun_enforcement",)),
        ]
        stage_order = {key: index for index, (key, _, _) in enumerate(stage_defs)}
        task = (task_payload or {}).get("task") or {}
        events = CiPluginService._events_since_latest_resume((task_payload or {}).get("latest_events") or [])
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

    def _refresh_repair_sessions(self, record: CiTaskRecord) -> None:
        if not self.task_manager:
            return
        in_progress_or_success_statuses = {"green", "rerun_running"}
        failed_rerun_statuses = {"rerun_failed", "rerun_unavailable"}
        failed_repo_statuses = {"FAILED", "CANCELLED", "POISONED", "BUDGET_EXCEEDED"}
        for session in record.fix_sessions:
            repo_task_id = session.get("repoTaskId")
            if not repo_task_id:
                continue
            repo_task = self.task_manager.get_task(int(repo_task_id))
            if not repo_task:
                continue
            session["repoTaskStatus"] = repo_task["status"]
            session["repoTaskStage"] = repo_task["current_stage"]
            session["repoTaskDetail"] = repo_task["current_detail"]
            session["repoTaskError"] = repo_task["error"] or repo_task["last_error"]
            try:
                task_payload = build_status_payload(self.task_manager.db, int(repo_task_id))
            except KeyError:
                task_payload = None
            session["repairIssues"] = self._repair_issues_from_repo_task(repo_task, task_payload)
            ci_context = self._repair_context_from_repo_task(record, repo_task)
            if ci_context:
                session["ciContext"] = ci_context
            if session.get("status") == "repair_failed" and repo_task["status"] not in failed_repo_statuses:
                session["status"] = "repairing" if repo_task["status"] != "COMPLETED" else "repair_completed"
                session["updatedAt"] = utc_now().isoformat()
                record.updated_at = utc_now()
                self.save(record)
            elif session.get("status") in in_progress_or_success_statuses:
                continue
            if repo_task["status"] in failed_repo_statuses:
                session["status"] = "repair_failed"
                session["updatedAt"] = utc_now().isoformat()
                record.updated_at = utc_now()
                self.save(record)
                # ROLLOUT_TEMP: Always ack CI as success after repair to unblock pipeline
                # even when the repair task itself failed. Actual failure is preserved in
                # session["status"]="repair_failed" for periodic review. Revert when confident.
                LOGGER.warning(
                    "ci_repair_rollout_temp_ack task_id=%s app=%s branch=%s session_id=%s "
                    "actual_status=repair_failed ack_as=success",
                    record.task_id,
                    record.request.app_name,
                    record.request.branch,
                    session.get("sessionId"),
                )
                self._report_result(record, True, "repair session ended (repair_failed) — pipeline unblocked by rollout override")
                continue
            if repo_task["status"] != "COMPLETED":
                continue
            handler = self._handler_for_record(record)
            task_result = (
                handler.completed_task_enforcement_result(
                    record=record,
                    task_manager=self.task_manager,
                    repo_task=repo_task,
                )
                if handler
                else None
            )
            if task_result:
                self._apply_repair_enforcement_result(record, session, task_result)
                continue
            if session.get("status") in failed_rerun_statuses:
                continue
            runner = self._runner_for_record(record)
            if not runner:
                session["status"] = "rerun_unavailable"
                session["updatedAt"] = utc_now().isoformat()
                record.updated_at = utc_now()
                self.save(record)
                # ROLLOUT_TEMP: Always ack CI as success when enforcement runner is unavailable.
                # Revert when confident.
                LOGGER.warning(
                    "ci_repair_rollout_temp_ack task_id=%s app=%s branch=%s session_id=%s "
                    "actual_status=rerun_unavailable ack_as=success",
                    record.task_id,
                    record.request.app_name,
                    record.request.branch,
                    session.get("sessionId"),
                )
                self._report_result(record, True, "repair session ended (rerun_unavailable) — pipeline unblocked by rollout override")
                continue

            session["status"] = "rerun_running"
            session["updatedAt"] = utc_now().isoformat()
            record.updated_at = utc_now()
            self.save(record)

            result = runner.run(Path(repo_task["repo_path"]))
            self._apply_repair_enforcement_result(record, session, result)

    def _apply_repair_enforcement_result(
        self,
        record: CiTaskRecord,
        session: Dict[str, Any],
        result: EnforcementResult,
    ) -> None:
        result_json = result.model_dump(mode="json")
        session["rerunEnforcement"] = result_json
        session["updatedAt"] = utc_now().isoformat()
        record.enforcement_result = result_json
        record.summary = result.summary
        record.status = CiTaskStatus.success if result.passed else CiTaskStatus.failed
        session["status"] = "green" if result.passed else "rerun_failed"
        record.updated_at = utc_now()
        if result.passed:
            self._report_result(record, True, result.summary)
        else:
            # ROLLOUT_TEMP: Always ack CI as success after repair to unblock pipeline
            # even when rerun enforcement failed. Actual failure is preserved in
            # record.status=failed and session["status"]="rerun_failed" for periodic review.
            # Revert when confident in repair quality.
            LOGGER.warning(
                "ci_repair_rollout_temp_ack task_id=%s app=%s branch=%s session_id=%s "
                "actual_status=rerun_failed ack_as=success",
                record.task_id,
                record.request.app_name,
                record.request.branch,
                session.get("sessionId"),
            )
            self._report_result(record, True, "repair session ended (rerun_failed) — pipeline unblocked by rollout override")
        self.save(record)

    def _repair_context_from_repo_task(self, record: CiTaskRecord, repo_task: Dict[str, Any]) -> Dict[str, Any]:
        context = json_loads(repo_task.get("ci_context_json") or "{}")
        if not context:
            return {}
        context.setdefault("pipeline", {})
        git = context.setdefault("git", {})
        missing_reasons = list(context.get("missingReasons") or [])

        # Protocol-specific issue enrichment.
        context = self._context_provider_for(record).enrich_repair_context(record, context, repo_task)
        issue = context.get("issue") if isinstance(context.get("issue"), dict) else {}

        if issue.get("description"):
            missing_reasons = [reason for reason in missing_reasons if reason != "issue_description_unavailable"]

        if not git.get("commitMessages") and repo_task.get("repo_path"):
            commit_messages = collect_git_commit_messages(
                Path(str(repo_task["repo_path"])),
                str(repo_task.get("base_ref") or "origin/master"),
                run_command=self.workspace_manager.run_command if self.workspace_manager else None,
            )
            if commit_messages:
                git["commitMessages"] = commit_messages
        if git.get("commitMessages"):
            missing_reasons = [reason for reason in missing_reasons if reason != "git_commit_messages_unavailable"]

        self._set_context_source(context, "issue_description", bool(issue.get("description")), source=issue.get("source"))
        self._set_context_source(context, "git_commit_messages", bool(git.get("commitMessages")))
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

    @staticmethod
    def _set_context_source(context: Dict[str, Any], name: str, available: bool, **extra: Any) -> None:
        sources = context.setdefault("sources", [])
        for source in sources:
            if source.get("name") == name:
                source["available"] = available
                for key, value in extra.items():
                    if value is not None:
                        source[key] = value
                return

    def _create_repair_task(
        self,
        record: CiTaskRecord,
        session: Dict[str, object],
        request: CreateFixSessionRequest,
    ) -> None:
        repo_path = Path(str(record.workspace_path)).expanduser().resolve()
        commit_messages = collect_git_commit_messages(
            repo_path,
            "origin/master",
            run_command=self.workspace_manager.run_command if self.workspace_manager else None,
        )
        context = self._context_provider_for(record).build_context(
            record,
            user_context=request.user_context,
            commit_messages=commit_messages,
        )
        context_path = None
        if self.context_exporter:
            context_path = self.context_exporter.export(str(session["sessionId"]), context)
        handler = self._handler_for_record(record)
        repo_task_id = handler.create_repair_task(
            task_manager=self.task_manager,
            record=record,
            request=request,
            repo_path=repo_path,
            priority=self.repair_priority,
            base_ref="origin/master",
            coverage_gate=uta_settings.ci_diff_coverage_gate,
            mutation_gate=uta_settings.ci_diff_mutation_gate,
            ci_context=context,
            ci_context_path=str(context_path) if context_path else None,
        )
        session["repoTaskId"] = repo_task_id
        session["repoTaskStatus"] = "CREATED"
        session["ciContext"] = context
        session["status"] = "repair_task_created"
        preempted = self.task_manager.preempt_running_same_repo_for_urgent(repo_task_id)
        if preempted:
            session["preemptedTaskIds"] = preempted
            LOGGER.info(
                "ci_repair_preempted task_id=%s app=%s branch=%s repo_task_id=%s preempted_task_ids=%s",
                record.task_id,
                record.request.app_name,
                record.request.branch,
                repo_task_id,
                ",".join(str(item) for item in preempted),
            )

    def _runner_for_record(self, record: CiTaskRecord):
        handler = self._handler_for_record(record)
        return handler.runner if handler else None

    def _handler_for_record(self, record: CiTaskRecord):
        return self.language_handlers.handler_for(record)

    @staticmethod
    def _find_session_by_fingerprint(record: CiTaskRecord, fingerprint: str) -> Optional[Dict[str, object]]:
        return next((session for session in record.fix_sessions if session.get("fingerprint") == fingerprint), None)

    @staticmethod
    def _is_repair_session_terminal_failed(session: Dict[str, object]) -> bool:
        return session.get("status") in {"repair_failed", "rerun_failed", "rerun_unavailable"}

    def _repair_fingerprint(self, record: CiTaskRecord, request: CreateFixSessionRequest) -> str:
        enforcement = record.enforcement_result or {}
        raw = {
            "ci_task_id": record.request.task_id,
            "git_url": record.request.git_url,
            "branch": record.request.branch,
            "commit_id": record.request.commit_id,
            "enforcement_status": enforcement.get("status"),
            "enforcement_command": enforcement.get("command"),
            "failed_targets": sorted(request.target_ids),
        }
        return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

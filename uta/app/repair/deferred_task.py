"""Creating the repo task that does the repairing, now or on a thread.

Creating a repair task is slow: it refreshes a checkout, may rerun enforcement,
resolves targets, builds context and talks to the task manager. An HTTP caller
asking for a fix session should not wait for all of that, so the same work runs
either inline or on a daemon thread depending on `async_repair_task_creation`.

Both paths are here on purpose. The deferred path is not a wrapper around the
inline one -- it owns the extra obligations that deferral creates: a thread
registry keyed by task and session so a retry cannot start a second creation,
a `creating_repair_task` state the progress endpoint can render, a failure
state written back onto the session when the thread raises, and a join point
for tests and shutdown. Splitting inline creation away from those obligations
would put the two halves of one decision in two files.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from uta.app.context import collect_git_commit_messages
from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.shared.ci_models import CiTaskRecord, utc_now
from uta.shared.config import settings as uta_settings
from uta.testgen.spec_context import resolve_spec_context
from uta.shared.workspace_rules import WorkspaceRulesUnavailable, validate_workspace_rules

# The service's logger, not this module's -- see `repair.service`.
LOGGER = logging.getLogger("uta.app.service")


class DeferredRepairTaskMixin:
    """Repair-task creation: the inline path, the thread, and its bookkeeping."""

    def _ensure_deferred_repair_task_creation(self, record: CiTaskRecord, session: Dict[str, Any]) -> None:
        if not self.async_repair_task_creation:
            return
        if session.get("repoTaskId") or session.get("alreadyGreenAfterRefresh"):
            return
        if session.get("status") not in {"creating_repair_task"}:
            return
        self._start_deferred_repair_task_creation(record, session, self._retry_request_from_session(session))
    def _mark_repair_task_creation_started(self, record: CiTaskRecord, session: Dict[str, Any]) -> None:
        session["status"] = "creating_repair_task"
        session["repoTaskStage"] = "refresh_workspace"
        session["repoTaskDetail"] = "刷新修复工作区并创建修复任务"
        session["updatedAt"] = utc_now().isoformat()
        record.updated_at = utc_now()
    def _start_deferred_repair_task_creation(
        self,
        record: CiTaskRecord,
        session: Dict[str, Any],
        request: CreateFixSessionRequest,
    ) -> None:
        session_id = str(session.get("sessionId") or "")
        if not session_id:
            return
        key = f"{record.task_id}:{session_id}"
        with self._repair_creation_lock:
            existing = self._repair_creation_threads.get(key)
            if existing and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._run_deferred_repair_task_creation,
                args=(record, session_id, request, key),
                name=f"ci-repair-create-{record.task_id}-{session_id[:8]}",
                daemon=True,
            )
            self._repair_creation_threads[key] = thread
            thread.start()
    def _run_deferred_repair_task_creation(
        self,
        record: CiTaskRecord,
        session_id: str,
        request: CreateFixSessionRequest,
        key: str,
    ) -> None:
        try:
            session = next((item for item in record.fix_sessions if item.get("sessionId") == session_id), None)
            if session is None:
                return
            if session.get("repoTaskId") or session.get("alreadyGreenAfterRefresh"):
                return
            self._mark_repair_task_creation_started(record, session)
            self._service.save(record)
            self._create_repair_task(record, session, request)
            session["updatedAt"] = utc_now().isoformat()
            record.updated_at = utc_now()
            self._service.save(record)
        except Exception as exc:  # noqa: BLE001
            session = next((item for item in record.fix_sessions if item.get("sessionId") == session_id), None)
            if session is not None:
                session["status"] = "repair_task_create_failed"
                session["repoTaskError"] = str(exc)
                session["repoTaskDetail"] = f"创建修复任务失败: {exc}"
                session["updatedAt"] = utc_now().isoformat()
                record.updated_at = utc_now()
                self._service.save(record)
            LOGGER.exception(
                "ci_repair_task_creation_failed task_id=%s session_id=%s",
                record.task_id,
                session_id,
            )
        finally:
            with self._repair_creation_lock:
                self._repair_creation_threads.pop(key, None)
    def wait_for_deferred_repair_tasks(self, timeout: Optional[float] = None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._repair_creation_lock:
                threads = list(self._repair_creation_threads.values())
            if not threads:
                return
            for thread in threads:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                thread.join(remaining)
                if deadline is not None and time.monotonic() >= deadline:
                    return
    def _create_repair_task(
        self,
        record: CiTaskRecord,
        session: Dict[str, object],
        request: CreateFixSessionRequest,
    ) -> None:
        repo_path = Path(str(record.workspace_path)).expanduser().resolve()
        original_enforcement_result = record.enforcement_result if isinstance(record.enforcement_result, dict) else None
        handler = self._service._handler_for_record(record)
        self._refresh_repair_workspace(record, session, repo_path)
        try:
            validate_workspace_rules(repo_path)
        except WorkspaceRulesUnavailable as exc:
            session.update({
                "status": "repair_task_create_failed",
                "repoTaskStage": "workspace_precheck",
                "repoTaskFailureKind": exc.reason_code,
                "repoTaskError": str(exc),
                "repoTaskDetail": str(exc),
                "updatedAt": utc_now().isoformat(),
            })
            record.updated_at = utc_now()
            self._service.save(record)
            return
        if handler.should_rerun_after_repair_workspace_refresh(record=record, request=request):
            rerun_result = self._rerun_after_repair_workspace_refresh(record, session, request, repo_path)
            if rerun_result and rerun_result.passed:
                self._apply_repair_enforcement_result(record, session, rerun_result)
                session["alreadyGreenAfterRefresh"] = True
                return
        base_ref = "origin/master"
        repair_record = record
        repair_target_ids = handler.repair_target_ids(
            record=repair_record,
            request=request,
            repo_path=repo_path,
            base_ref=base_ref,
        )
        if not repair_target_ids and original_enforcement_result:
            original_record = record.model_copy(
                deep=True,
                update={"enforcement_result": original_enforcement_result},
            )
            original_target_ids = handler.repair_target_ids(
                record=original_record,
                request=request,
                repo_path=repo_path,
                base_ref=base_ref,
            )
            if original_target_ids:
                repair_record = original_record
                repair_target_ids = original_target_ids
        commit_messages = collect_git_commit_messages(
            repo_path,
            base_ref,
            workspace=self._workspace(),
        )
        context = self._service._context_provider_for(repair_record).build_context(
            repair_record,
            user_context=request.user_context,
            commit_messages=commit_messages,
        )
        handler.validate_repair_context(
            record=repair_record,
            request=request,
            repo_path=repo_path,
            base_ref=base_ref,
            rdc_context=context,
            target_ids=repair_target_ids,
        )
        spec_context = resolve_spec_context(record.request.spec_context)
        if spec_context:
            context["specContext"] = spec_context
        context_path = None
        if self._service.context_exporter:
            context_path = self._service.context_exporter.export(str(session["sessionId"]), context)
        pipeline = context.get("pipeline") if isinstance(context.get("pipeline"), dict) else {}
        repo_identity = (
            str(pipeline.get("gitUrl") or "").strip()
            or str(pipeline.get("git_url") or "").strip()
            or str(record.request.git_url or "").strip()
            or str(pipeline.get("appName") or "").strip()
            or str(record.request.app_name or "").strip()
        )
        duplicate_repo_task_id = self._service.task_manager.find_active_duplicate_repair_task_for_targets(
            repo_identity=repo_identity,
            branch_name=record.request.branch,
            base_ref=base_ref,
            language=handler.language,
            quality_gate_backend=handler.quality_gate_backend,
            target_ids=repair_target_ids,
        )
        if duplicate_repo_task_id:
            repo_task_id = duplicate_repo_task_id
            session["reusedRepoTaskId"] = repo_task_id
        else:
            repo_task_id = handler.create_repair_task(
                task_manager=self._service.task_manager,
                record=repair_record,
                request=request,
                repo_path=repo_path,
                priority=self.repair_priority,
                base_ref=base_ref,
                coverage_gate=uta_settings.ci_diff_coverage_gate,
                mutation_gate=self._service._ci_diff_mutation_gate_for(handler.language),
                rdc_context=context,
                rdc_context_path=str(context_path) if context_path else None,
            )
            duplicate_repo_task_id = self._service.task_manager.find_active_duplicate_repair_task(repo_task_id)
            if duplicate_repo_task_id:
                self._service.task_manager.cancel_task(
                    repo_task_id,
                    reason=f"duplicate of active repair task {duplicate_repo_task_id}",
                )
                session["deduplicatedRepoTaskId"] = repo_task_id
                repo_task_id = duplicate_repo_task_id
        session["repoTaskId"] = repo_task_id
        live_task = self._service._live_repo_task(repo_task_id)
        session["repoTaskStatus"] = live_task["status"] if live_task else "CREATED"
        if live_task:
            session["repoTaskStage"] = live_task["current_stage"]
            session["repoTaskDetail"] = live_task["current_detail"]
            session["repoTaskError"] = live_task["error"] or live_task["last_error"]
        session["rdcContext"] = context
        session["status"] = "repair_task_created"
        preempted = [] if duplicate_repo_task_id else self._service.task_manager.preempt_running_same_repo_for_urgent(repo_task_id)
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
    def _validate_repair_context_before_session_creation(
        self,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
    ) -> None:
        handler = self._service._handler_for_record(record)
        if not handler:
            return
        repo_path = Path(str(record.workspace_path)).expanduser().resolve()
        base_ref = "origin/master"
        repair_target_ids = handler.repair_target_ids(
            record=record,
            request=request,
            repo_path=repo_path,
            base_ref=base_ref,
        )
        handler.validate_repair_context(
            record=record,
            request=request,
            repo_path=repo_path,
            base_ref=base_ref,
            rdc_context={
                "enforcement": record.enforcement_result
                if isinstance(record.enforcement_result, dict)
                else {}
            },
            target_ids=repair_target_ids,
        )

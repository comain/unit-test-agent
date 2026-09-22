"""The repair session itself: create it, advance it, finish it once.

A fix session is a small state machine stored on the CI record. This module
owns every transition of it -- admission (is a session allowed, is one already
running, has the rate limit been reached), reconciliation against the repo
task that is doing the work, and the terminal outcome that is reported back to
the protocol exactly once.

The de-duplication is the reason "exactly once" is spelled out. A terminal
callback is keyed by outcome, so re-reporting the same result is a no-op while
a genuinely different outcome -- a stale repair, an unavailable rerun -- still
gets through. Progress projection and workspace refresh were split out from
here because they answer questions; this file makes decisions.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from uta.enforcement.enforcement import QualityGateResult
from uta.enforcement.equivalent_mutants import ReviewDecision, decide_grant
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, utc_now
from uta.shared.fix_sessions import (
    CreateFixSessionRequest,
    can_create_fix_session,
    create_fix_session,
)
from uta.tasks.models import json_loads
from uta.tasks.render import build_status_payload
from uta.shared.config import settings


LOGGER = logging.getLogger(__name__)


#: A granted session's status, and the key its one success callback is sent under.
EQUIVALENT_MUTANTS_STATUS = "passed_with_equivalent_mutants"
#: A session in either of these has reported its terminal success.
SESSION_PASSED_STATUSES = frozenset({"green", EQUIVALENT_MUTANTS_STATUS})


class FixSessionRateLimitError(RuntimeError):
    pass


class FixSessionUnsupportedError(RuntimeError):
    pass


class RepairSessionMixin:
    """Admission, reconciliation and terminal reporting for fix sessions."""

    def create_fix_session(self, record: CiTaskRecord, request: CreateFixSessionRequest) -> Dict[str, object]:
        if not can_create_fix_session(record):
            raise FixSessionUnsupportedError("fix sessions are not available for this report")
        if self._service.task_manager:
            self._refresh_repair_sessions(record)
        fingerprint = self._repair_fingerprint(record, request)
        existing = self._find_session_by_fingerprint(record, fingerprint)
        if existing:
            if existing.get("status") in SESSION_PASSED_STATUSES:
                return {"session": existing, "alreadyGreen": True}
            if not self._is_repair_session_terminal_failed(existing):
                return {"session": existing, "alreadyRunning": True}
            self._cancel_terminal_failed_live_task(existing)
        if self.repair_rate_limit_per_task >= 0 and len(record.fix_sessions) >= self.repair_rate_limit_per_task:
            raise FixSessionRateLimitError("repair session rate limit exceeded for this CI task")
        if self._service.task_manager and record.workspace_path:
            try:
                self._validate_repair_context_before_session_creation(record, request)
            except ValueError as exc:
                raise FixSessionUnsupportedError(str(exc)) from exc

        session = create_fix_session(record, request)
        session["fingerprint"] = fingerprint
        if self._service.task_manager and record.workspace_path and self.async_repair_task_creation:
            self._mark_repair_task_creation_started(record, session)
            self._service.save(record)
            self._start_deferred_repair_task_creation(record, session, request)
            return {"session": session}
        try:
            if self._service.task_manager and record.workspace_path:
                self._create_repair_task(record, session, request)
        except ValueError as exc:
            record.fix_sessions = [item for item in record.fix_sessions if item is not session]
            record.updated_at = utc_now()
            self._service.save(record)
            raise FixSessionUnsupportedError(str(exc)) from exc
        self._service.save(record)
        return {"session": session}
    def materialize_retry_request(self, record: CiTaskRecord, session: Dict[str, Any]) -> None:
        if session.get("status") != "retry_requested":
            return
        if not self._service.task_manager or not record.workspace_path:
            return
        previous_task_id = session.get("repoTaskId")
        live_task = self._service._live_repo_task(previous_task_id)
        if live_task:
            session["repoTaskStatus"] = live_task["status"]
            session["repoTaskStage"] = live_task["current_stage"]
            session["repoTaskDetail"] = live_task["current_detail"]
            session["repoTaskError"] = live_task["error"] or live_task["last_error"]
            session["status"] = "repair_task_created"
            session["updatedAt"] = utc_now().isoformat()
            record.updated_at = utc_now()
            self._service.save(record)
            return
        if previous_task_id:
            history = session.setdefault("repoTaskHistory", [])
            if not any(item.get("repoTaskId") == previous_task_id for item in history):
                history.append(
                    {
                        "repoTaskId": previous_task_id,
                        "repoTaskStatus": session.get("repoTaskStatus"),
                        "repoTaskStage": session.get("repoTaskStage"),
                        "repoTaskDetail": session.get("repoTaskDetail"),
                        "repoTaskError": session.get("repoTaskError"),
                        "archivedAt": utc_now().isoformat(),
                    }
                )
        for key in (
            "repoTaskId",
            "repoTaskStatus",
            "repoTaskStage",
            "repoTaskDetail",
            "repoTaskError",
            "repairIssues",
            "rerunEnforcement",
        ):
            session.pop(key, None)
        request = self._retry_request_from_session(session)
        if self.async_repair_task_creation:
            self._mark_repair_task_creation_started(record, session)
            self._service.save(record)
            self._start_deferred_repair_task_creation(record, session, request)
            return
        self._create_repair_task(record, session, request)
        session["updatedAt"] = utc_now().isoformat()
        record.updated_at = utc_now()
        self._service.save(record)
    @staticmethod
    def _retry_request_from_session(session: Dict[str, Any]) -> CreateFixSessionRequest:
        selected_targets = session.get("selectedTargets") if isinstance(session.get("selectedTargets"), list) else []
        target_ids = [str(item.get("id")) for item in selected_targets if isinstance(item, dict) and item.get("id")]
        messages = session.get("messages") if isinstance(session.get("messages"), list) else []
        user_context = "\n\n".join(
            str(item.get("content") or "").strip()
            for item in messages
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        )
        return CreateFixSessionRequest(target_ids=target_ids, user_context=user_context or None)
    def _report_repair_result_once(
        self,
        record: CiTaskRecord,
        session: Dict[str, Any],
        passed: bool,
        summary: str,
        callback_key: Optional[str] = None,
    ) -> None:
        callback_key = callback_key or self._repair_terminal_callback_key(passed, summary)
        reported_key = str(session.get("terminalCallbackReportedKey") or "")
        if reported_key == callback_key:
            return
        if session.get("terminalCallbackReportedAt") and not reported_key and callback_key != "repair_stale":
            return
        self._service._report_result(record, passed, summary)
        session["terminalCallbackReportedKey"] = callback_key
        session["terminalCallbackReportedAt"] = utc_now().isoformat()
        record.updated_at = utc_now()
        self._service.save(record)
    @staticmethod
    def _repair_terminal_callback_key(passed: bool, summary: str) -> str:
        if passed:
            return "success"
        if "repair_stale" in str(summary or ""):
            return "repair_stale"
        if "rerun_unavailable" in str(summary or ""):
            return "rerun_unavailable"
        if "repair_failed" in str(summary or ""):
            return "repair_failed"
        return "failure"
    def _refresh_repair_sessions(self, record: CiTaskRecord) -> None:
        if not self._service.task_manager:
            return
        success_statuses = SESSION_PASSED_STATUSES
        failed_rerun_statuses = {"rerun_failed", "rerun_unavailable", "repair_stale"}
        failed_repo_statuses = {"FAILED", "CANCELLED", "STOPPED", "POISONED", "BUDGET_EXCEEDED"}
        for session in record.fix_sessions:
            self._ensure_deferred_repair_task_creation(record, session)
            self.materialize_retry_request(record, session)
            repo_task_id = session.get("repoTaskId")
            if not repo_task_id:
                continue
            repo_task = self._service.task_manager.get_task(int(repo_task_id))
            if not repo_task:
                continue
            session["repoTaskStatus"] = repo_task["status"]
            session["repoTaskStage"] = repo_task["current_stage"]
            session["repoTaskDetail"] = repo_task["current_detail"]
            session["repoTaskError"] = repo_task["error"] or repo_task["last_error"]
            try:
                task_payload = build_status_payload(self._service.task_manager.db, int(repo_task_id))
            except KeyError:
                task_payload = None
            session["budgetUsed"] = self._repair_budget_used_from_repo_task(repo_task, task_payload)
            session["repairIssues"] = self._repair_issues_from_repo_task(repo_task, task_payload)
            quality = self._repair_test_quality_from_task_payload(task_payload)
            if quality:
                session["testQuality"] = quality
            rdc_context = self._repair_context_from_repo_task(record, repo_task)
            if rdc_context:
                session["rdcContext"] = rdc_context
            if session.get("status") == "repair_failed" and repo_task["status"] not in failed_repo_statuses:
                session["status"] = "repairing" if repo_task["status"] != "COMPLETED" else "repair_completed"
                session["updatedAt"] = utc_now().isoformat()
                record.updated_at = utc_now()
                self._service.save(record)
            if session.get("status") in success_statuses:
                continue
            if repo_task["status"] in failed_repo_statuses:
                session["status"] = "repair_failed"
                session["updatedAt"] = utc_now().isoformat()
                record.updated_at = utc_now()
                self._service.save(record)
                self._report_repair_result_once(record, session, False, "repair session ended (repair_failed)")
                continue
            if repo_task["status"] != "COMPLETED":
                continue
            handler = self._service._handler_for_record(record)
            task_result = (
                handler.completed_task_enforcement_result(
                    record=record,
                    task_manager=self._service.task_manager,
                    repo_task=repo_task,
                )
                if handler
                else None
            )
            if task_result:
                self._apply_repair_enforcement_result(record, session, task_result, repo_task)
                continue
            if session.get("status") in failed_rerun_statuses:
                continue
            if session.get("status") == "rerun_running":
                # `rerun_running` is written before a rerun that can take as
                # long as a full Maven build, and only cleared when that call
                # returns. An interrupted rerun -- API restart, an exception
                # inside the runner -- therefore left the session in a state
                # this very check skipped on every later poll, so it never
                # retried and never reported. One session sat here 13 hours.
                # Past the staleness bound the rerun is assumed dead and the
                # session is allowed to take its turn again.
                if not self._rerun_is_stale(session):
                    continue
                LOGGER.warning(
                    "ci_repair_rerun_stale task_id=%s session=%s started_at=%s",
                    record.task_id,
                    session.get("sessionId"),
                    session.get("rerunStartedAt"),
                )
            runner = self._service._runner_for_record(record)
            if not runner:
                session["status"] = "rerun_unavailable"
                session["updatedAt"] = utc_now().isoformat()
                record.updated_at = utc_now()
                self._service.save(record)
                self._report_repair_result_once(record, session, False, "repair session ended (rerun_unavailable)")
                continue

            repo_path = Path(repo_task["repo_path"])
            with self._repair_rerun_lock(record, session, repo_path) as acquired:
                if not acquired:
                    continue
                session["status"] = "rerun_running"
                # Stamped so an interrupted rerun can be recognised as dead
                # rather than merely in progress.
                session["rerunStartedAt"] = utc_now().isoformat()
                session["updatedAt"] = utc_now().isoformat()
                record.updated_at = utc_now()
                self._service.save(record)
                result = runner.run(repo_path)
                self._apply_repair_enforcement_result(record, session, result, repo_task)
    @staticmethod
    def _rerun_is_stale(session: Dict[str, Any]) -> bool:
        """Has a `rerun_running` session outlived any rerun that could finish?

        Only a stamped session can be judged. An unstamped one is left alone:
        every site that starts a rerun stamps it, so the absence of a stamp
        means some other path set the state, and guessing "dead" there would
        launch the duplicate rerun this guard exists to prevent.
        """
        bound = int(getattr(settings, "ci_repair_rerun_stale_seconds", 0) or 0)
        if bound <= 0:
            return False
        started = str(session.get("rerunStartedAt") or "").strip()
        if not started:
            return False
        try:
            stamp = datetime.fromisoformat(started)
        except ValueError:
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds() > bound

    def _refresh_repair_session_summaries(self, record: CiTaskRecord) -> None:
        """Refresh repair-session display fields without mutating persisted CI records.

        Recent-job pages intentionally use compact records so large enforcement
        stdout/stderr blobs are not loaded into the API process. Those compact
        records must not be saved back to disk, but their repair task status
        should still reflect the live task DB instead of stale persisted values
        such as CREATED after a task has finished.
        """
        if not self._service.task_manager:
            return
        failed_repo_statuses = {"FAILED", "CANCELLED", "STOPPED", "POISONED", "BUDGET_EXCEEDED"}
        for session in record.fix_sessions:
            repo_task_id = session.get("repoTaskId")
            if not repo_task_id:
                continue
            try:
                repo_task = self._service.task_manager.get_task(int(repo_task_id))
            except (KeyError, TypeError, ValueError):
                continue
            session["repoTaskStatus"] = repo_task["status"]
            session["repoTaskStage"] = repo_task["current_stage"]
            session["repoTaskDetail"] = repo_task["current_detail"]
            session["repoTaskError"] = repo_task["error"] or repo_task["last_error"]
            try:
                task_payload = build_status_payload(self._service.task_manager.db, int(repo_task_id))
            except KeyError:
                task_payload = None
            session["budgetUsed"] = self._repair_budget_used_from_repo_task(repo_task, task_payload)
            session["repairIssues"] = self._repair_issues_from_repo_task(repo_task, task_payload)
            quality = self._repair_test_quality_from_task_payload(task_payload)
            if quality:
                session["testQuality"] = quality
            if repo_task["status"] in failed_repo_statuses:
                session["status"] = "repair_failed"
                continue
            if repo_task["status"] != "COMPLETED" or session.get("status") in SESSION_PASSED_STATUSES:
                continue
            handler = self._service._handler_for_record(record)
            task_result = (
                handler.completed_task_enforcement_result(
                    record=record,
                    task_manager=self._service.task_manager,
                    repo_task=repo_task,
                )
                if handler
                else None
            )
            if not task_result:
                session["status"] = "repair_completed"
                continue
            result_json = task_result.model_dump(mode="json")
            session["status"] = "green" if task_result.passed else "rerun_failed"
            session["rerunEnforcement"] = result_json
            record.enforcement_result = result_json
            record.summary = task_result.summary
            record.status = CiTaskStatus.success if task_result.passed else CiTaskStatus.failed
    def _apply_repair_enforcement_result(
        self,
        record: CiTaskRecord,
        session: Dict[str, Any],
        result: QualityGateResult,
        repo_task: Optional[Dict[str, Any]] = None,
    ) -> None:
        result_json = result.model_dump(mode="json")
        session["rerunEnforcement"] = result_json
        session["updatedAt"] = utc_now().isoformat()
        record.enforcement_result = result_json
        if not result.passed and repo_task and self._grant_equivalent_mutants(record, session, result, repo_task):
            return
        # A result that was not excused ends any earlier session's override.
        record.gate_override = None
        record.summary = result.summary
        record.status = CiTaskStatus.success if result.passed else CiTaskStatus.failed
        session["status"] = "green" if result.passed else "rerun_failed"
        record.updated_at = utc_now()
        if result.passed:
            self._report_repair_result_once(record, session, True, result.summary)
        else:
            self._report_repair_result_once(record, session, False, result.summary)
        self._service.save(record)
    def _grant_equivalent_mutants(
        self,
        record: CiTaskRecord,
        session: Dict[str, Any],
        result: QualityGateResult,
        repo_task: Dict[str, Any],
    ) -> bool:
        """Grant if the saved reviews allow it; any error keeps the normal failure path."""
        try:
            return self._try_grant_equivalent_mutants(record, session, result, repo_task)
        except Exception:  # noqa: BLE001 - a grant must never strand the session
            LOGGER.exception(
                "ci_equivalence_not_granted task_id=%s session=%s reason=grant_error",
                record.task_id, session.get("sessionId"),
            )
            return False

    def _try_grant_equivalent_mutants(
        self,
        record: CiTaskRecord,
        session: Dict[str, Any],
        result: QualityGateResult,
        repo_task: Dict[str, Any],
    ) -> bool:
        """Excuse a mutation-only rerun failure the repair task's reviews explain.

        The repair task reviewed its stalled survivors before giving up; this
        fresh rerun is the recheck. It is excused only if every failing unit
        was reviewed all-equivalent and the gate reproduces exactly those
        mutants on unchanged source. The raw result stays the record's
        evidence -- the override is visible, never a claim the mutants died.
        """
        rows = self._service.task_manager.list_class_tasks(int(repo_task["id"]))
        failing = [row for row in rows if str(_row_value(row, "status") or "").upper() != "PASS"]
        reviews = [_saved_review(row) for row in failing]
        if not failing or any(review is None for review in reviews):
            return False
        handler = self._service._handler_for_record(record)
        flags = handler.gate_failure_flags(record=record, result=result) if handler else None
        if flags is None:
            return _not_granted(record, session, "gate_flags_unknown")
        fresh = handler.scoring_survivors(record=record, result=result, repo_path=Path(repo_task["repo_path"]))
        decision = decide_grant(
            reviews,
            fresh=fresh,
            tests_passed=bool(flags.get("tests_passed")),
            coverage_passed=bool(flags.get("coverage_passed")),
            mutation_only_failure=bool(flags.get("mutation_only_failure")),
        )
        if not decision.granted:
            return _not_granted(record, session, decision.reason)

        # A grant means the fresh survivors are exactly the reviewed ones.
        mutants = len(fresh.mutants)
        raw_rate, gate = fresh.mutation_rate, fresh.mutation_gate
        now = utc_now().isoformat()
        session["status"] = EQUIVALENT_MUTANTS_STATUS
        session["equivalenceOverride"] = {
            "reviews": [
                {
                    "unitId": str(_row_value(row, "target_id") or _row_value(row, "class_fqn") or ""),
                    "verdictsSha256": review.verdicts_sha256,
                    "agentSessionRef": dict(review.agent_session_ref),
                    "modelId": review.model_id,
                    "verdicts": review.to_dict()["verdicts"],
                }
                for row, review in zip(failing, reviews)
            ],
            "rawMutationRate": raw_rate,
            "mutationGate": gate,
        }
        record.gate_override = {
            "kind": "equivalent_mutants",
            "sessionId": session.get("sessionId"),
            "rawMutationRate": raw_rate,
            "mutationGate": gate,
            "mutants": mutants,
        }
        record.status = CiTaskStatus.success
        record.summary = (
            f"Passed with equivalent-mutant override (fix session {session.get('sessionId')}): "
            f"raw mutation {raw_rate}% < gate {gate}%; "
            f"{mutants} mutants reviewed equivalent, not killed; the next CI run uses the normal gate."
        )
        record.updated_at = utc_now()
        session["updatedAt"] = now
        LOGGER.info(
            "ci_equivalence_granted task_id=%s session=%s raw_rate=%s gate=%s mutants=%d",
            record.task_id, session.get("sessionId"), raw_rate, gate, mutants,
        )
        self._report_repair_result_once(record, session, True, record.summary, callback_key=EQUIVALENT_MUTANTS_STATUS)
        self._service.save(record)
        return True
    @staticmethod
    def _find_session_by_fingerprint(record: CiTaskRecord, fingerprint: str) -> Optional[Dict[str, object]]:
        return next((session for session in record.fix_sessions if session.get("fingerprint") == fingerprint), None)
    def _cancel_terminal_failed_live_task(self, session: Dict[str, object]) -> None:
        if not self._service.task_manager:
            return
        repo_task_id = session.get("repoTaskId")
        live_task = self._service._live_repo_task(repo_task_id)
        if not live_task:
            return
        self._service.task_manager.cancel_task(
            int(live_task["id"]),
            reason="superseded by replacement repair session after terminal failure",
        )
    @staticmethod
    def _is_repair_session_terminal_failed(session: Dict[str, object]) -> bool:
        return session.get("status") in {
            "repair_failed",
            "rerun_failed",
            "rerun_unavailable",
            "repair_task_create_failed",
        }
    def _repair_fingerprint(self, record: CiTaskRecord, request: CreateFixSessionRequest) -> str:
        enforcement = record.enforcement_result or {}
        raw = {
            "rdc_task_id": record.request.task_id,
            "git_url": record.request.git_url,
            "branch": record.request.branch,
            "commit_id": record.request.commit_id,
            "enforcement_status": enforcement.get("status"),
            "enforcement_command": enforcement.get("command"),
            "failed_targets": sorted(request.target_ids),
        }
        return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _saved_review(row: Any) -> Optional[ReviewDecision]:
    payload = json_loads(_row_value(row, "equivalence_review_json"))
    return ReviewDecision.from_dict(payload) if isinstance(payload.get("survivors"), dict) else None


def _not_granted(record: CiTaskRecord, session: Dict[str, Any], reason: str) -> bool:
    LOGGER.info(
        "ci_equivalence_not_granted task_id=%s session=%s reason=%s",
        record.task_id, session.get("sessionId"), reason,
    )
    return False

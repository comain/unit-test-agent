from __future__ import annotations

import uuid
import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, TYPE_CHECKING

from uta.app.context import RepairContextExporter
from uta.app.ci import CiLanguageHandler
from uta.app.ci import EnforcementRunner
from uta.app.service_composition import (
    ProtocolNeutralContextProvider,
    build_language_handler_registry,
    build_protocol_registry,
)
from uta.shared.ci_models import CiTaskRecord, CiTaskStatus, CiTriggerRequest, utc_now
from uta.app.protocols import CiContextProvider, CiProtocol, CiResult, ProtocolRegistry
from uta.app.store import JsonCiTaskStore
from uta.app.workspace import GitWorkspaceManager
from uta.app.ci_operations import stop_workspace_processes
from uta.shared.config import settings as uta_settings
from uta.tasks.manager import TaskManager


LOGGER = logging.getLogger(__name__)
RERUN_LOCK_STALE_SECONDS = 6 * 60 * 60
PENDING_CI_CHECK_SUMMARY = "CI test-enforcement report task is pending; another report task is running."
RUNNING_CI_CHECK_SUMMARY = "CI test-enforcement report task is running."


# Defined with the fix sessions that raise them. Re-exported here because
# routes and tests import them from this module, and two classes of the same
# name would mean an `except` that silently stops catching.
from uta.app.repair import (  # noqa: E402
    FixSessionRateLimitError,
    FixSessionUnsupportedError,
)


if TYPE_CHECKING:  # pragma: no cover - resolves the string annotation below
    from uta.app.repair import RepairSessions


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class ApiTriggerService:
    def __init__(
        self,
        workspace_manager: Optional[GitWorkspaceManager] = None,
        enforcement_runner: Optional[EnforcementRunner] = None,
        python_enforcement_runner: Optional[EnforcementRunner] = None,
        task_manager: Optional[TaskManager] = None,
        context_exporter: Optional[RepairContextExporter] = None,
        record_store: Optional[JsonCiTaskStore] = None,
        inflight_dir: Optional[Path] = None,
        protocols: Optional[ProtocolRegistry] = None,
        repair_priority: int = 1,
        repair_rate_limit_per_task: int = 3,
        ci_report_parallel_limit: int = 1,
        language_handlers: Optional[Iterable[CiLanguageHandler]] = None,
        async_repair_task_creation: bool = False,
        config: Optional[Any] = None,
        ci_process_stopper: Optional[Callable[[Path], int]] = None,
    ) -> None:
        self._tasks: Dict[str, CiTaskRecord] = {}
        self.workspace_manager = workspace_manager
        self.language_handlers = build_language_handler_registry(
            config=config,
            language_handlers=language_handlers,
            enforcement_runner=enforcement_runner,
            python_enforcement_runner=python_enforcement_runner,
        )
        self.protocols = build_protocol_registry(protocols)
        self.task_manager = task_manager
        self.context_exporter = context_exporter
        self.record_store = record_store
        self.inflight_dir = Path(inflight_dir) if inflight_dir else None
        self.repair_priority = max(0, min(9, int(repair_priority)))
        self.repair_rate_limit_per_task = int(repair_rate_limit_per_task)
        self.ci_report_parallel_limit = int(ci_report_parallel_limit)
        self.async_repair_task_creation = bool(async_repair_task_creation)
        self._ci_queue_lock = threading.Lock()
        self._ci_active_task_ids: set[str] = set()
        self._ci_stop_requested: set[str] = set()
        self._ci_process_stopper = ci_process_stopper or stop_workspace_processes
        self._repair_creation_lock = threading.Lock()
        self._repair_creation_threads: Dict[str, threading.Thread] = {}
        self._repair = None


    # -- fix sessions ------------------------------------------------------
    #
    # The agent lane lives in `repair.py`. These four are the surface the
    # routes and the tests call, kept here so nothing outside had to learn
    # about the split.

    @property
    def repair(self) -> "RepairSessions":
        if self._repair is None:
            from uta.app.repair import RepairSessions

            self._repair = RepairSessions(self)
        return self._repair

    def create_fix_session(self, record, request):
        return self.repair.create_fix_session(record, request)

    def repair_progress(self, record, session_id, *, event_after_id=None):
        return self.repair.repair_progress(record, session_id, event_after_id=event_after_id)

    def materialize_retry_request(self, record, session) -> None:
        self.repair.materialize_retry_request(record, session)

    def wait_for_deferred_repair_tasks(self, timeout=None) -> None:
        self.repair.wait_for_deferred_repair_tasks(timeout)

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
                "ciReportParallelLimit": self.ci_report_parallel_limit,
            },
        }

    def submit(
        self,
        request: CiTriggerRequest,
        public_base_url: Optional[str] = None,
        *,
        run_inline: bool = True,
        protocol: Optional[str] = None,
    ) -> CiTaskRecord:
        task_id = uuid.uuid4().hex
        protocol_name = protocol or (self.protocols.names()[0] if self.protocols and self.protocols.names() else "manual")
        record = CiTaskRecord(task_id=task_id, status=CiTaskStatus.queued, request=request, protocol=protocol_name)
        LOGGER.info(
            "ci_check_started protocol=%s task_id=%s app=%s branch=%s git_url=%s task_ref=%s record_id=%s",
            protocol_name,
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
        record = self._load_record(task_id)
        if record is None or not self.workspace_manager or not self._runner_for_record(record):
            return
        if record.status not in {CiTaskStatus.queued, CiTaskStatus.pending, CiTaskStatus.running}:
            return
        if not self._claim_ci_check(record):
            return
        self._run_check_and_drain(record, public_base_url=public_base_url)

    @contextmanager
    def _inflight_marker(self, record: CiTaskRecord) -> Iterator[None]:
        if self.inflight_dir is None:
            yield
            return

        self.inflight_dir.mkdir(parents=True, exist_ok=True)
        marker_path = self.inflight_dir / f"{os.getpid()}-{record.task_id}.json"
        tmp_path = marker_path.with_suffix(".tmp")
        payload = {
            "pid": os.getpid(),
            "task_id": record.task_id,
            "app_name": record.request.app_name,
            "branch": record.request.branch,
            "started_at": utc_now().isoformat(),
        }
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(marker_path)
        try:
            yield
        finally:
            try:
                marker_path.unlink()
            except FileNotFoundError:
                pass

    def _run_check(self, record: CiTaskRecord, public_base_url: Optional[str] = None) -> None:
        if self._ci_check_stop_requested(record.task_id):
            return
        record.status = CiTaskStatus.running
        record.summary = RUNNING_CI_CHECK_SUMMARY
        record.updated_at = utc_now()
        self.save(record)
        workspace = self.workspace_manager.prepare(
            git_url=record.request.git_url,
            branch=record.request.branch,
            task_id=record.task_id,
        )
        record.workspace_path = str(workspace)
        self.save(record)
        if self._ci_check_stop_requested(record.task_id):
            return
        result = self._runner_for_record(record).run(workspace)
        with self._ci_queue_lock:
            # Stop and worker completion can race. The status transition and
            # stop-intent check must be atomic or a late Maven return can turn
            # a user-stopped task back into failed/success.
            if record.task_id in self._ci_stop_requested:
                return
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

    def _run_check_and_drain(self, record: CiTaskRecord, public_base_url: Optional[str] = None) -> None:
        self._run_claimed_ci_check(record, public_base_url=public_base_url)
        while True:
            pending = self._claim_next_pending_ci_check()
            if pending is None:
                return
            self._run_claimed_ci_check(
                pending,
                public_base_url=public_base_url or self._public_base_url_from_record(pending),
            )

    def _run_claimed_ci_check(self, record: CiTaskRecord, public_base_url: Optional[str] = None) -> None:
        try:
            with self._inflight_marker(record):
                try:
                    self._run_check(record, public_base_url=public_base_url)
                except Exception as exc:  # noqa: BLE001
                    with self._ci_queue_lock:
                        if record.task_id in self._ci_stop_requested:
                            return
                        record.status = CiTaskStatus.failed
                        record.summary = f"UTA API trigger failed unexpectedly: {exc}"
                        record.updated_at = utc_now()
                    LOGGER.exception(
                        "ci_check_failed_unexpected task_id=%s app=%s branch=%s",
                        record.task_id,
                        record.request.app_name,
                        record.request.branch,
                    )
                    self.save(record)
        finally:
            with self._ci_queue_lock:
                self._ci_active_task_ids.discard(record.task_id)
                self._ci_stop_requested.discard(record.task_id)

    def _claim_ci_check(self, record: CiTaskRecord) -> bool:
        with self._ci_queue_lock:
            if record.status not in {CiTaskStatus.queued, CiTaskStatus.pending, CiTaskStatus.running}:
                return False
            if self._ci_report_limit_reached_locked(exclude_task_id=record.task_id):
                self._mark_ci_check_pending(record)
                self.save(record)
                LOGGER.info(
                    "ci_check_pending task_id=%s app=%s branch=%s limit=%s",
                    record.task_id,
                    record.request.app_name,
                    record.request.branch,
                    self.ci_report_parallel_limit,
                )
                return False
            self._mark_ci_check_claimed(record)
            self.save(record)
            return True

    def _claim_next_pending_ci_check(self) -> Optional[CiTaskRecord]:
        with self._ci_queue_lock:
            if self._ci_report_limit_reached_locked():
                return None
            pending_records = [
                record
                for record in self._iter_ci_records_for_queue_locked()
                # `queued` is persisted before FastAPI schedules the
                # background callback. If the API exits in that window, the
                # next process must claim it just like a capacity-pending job.
                if record.status in {CiTaskStatus.pending, CiTaskStatus.queued}
            ]
            if not pending_records:
                return None
            pending_records.sort(key=lambda item: _aware_datetime(item.created_at))
            record = pending_records[0]
            self._tasks[record.task_id] = record
            self._mark_ci_check_claimed(record)
            self.save(record)
            LOGGER.info(
                "ci_check_pending_released task_id=%s app=%s branch=%s",
                record.task_id,
                record.request.app_name,
                record.request.branch,
            )
            return record

    def stop_ci_task(self, task_id: str) -> CiTaskRecord:
        record = self._load_record(task_id)
        if record is None:
            raise KeyError(task_id)
        active_statuses = {CiTaskStatus.queued, CiTaskStatus.pending, CiTaskStatus.running}
        with self._ci_queue_lock:
            if record.status not in active_statuses:
                raise ValueError(f"CI task is already terminal: {record.status.value}")
            self._ci_stop_requested.add(task_id)
            record.status = CiTaskStatus.stopped
            record.summary = "CI test-enforcement report task was stopped by an operator."
            record.updated_at = utc_now()
            self._ci_active_task_ids.discard(task_id)
            self.save(record)
        workspace_path = record.workspace_path
        if not workspace_path and self.workspace_manager is not None:
            # Clone/setup also runs before ``workspace_path`` is persisted. Its
            # task-scoped parent still contains the task id, so Stop can kill a
            # slow clone instead of waiting for setup to finish.
            workspace_path = str(self.workspace_manager.workspace_root / task_id)
        if workspace_path:
            self._ci_process_stopper(Path(workspace_path))
        self._start_pending_ci_checks_if_possible()
        return record

    def retry_ci_task(self, task_id: str, *, public_base_url: Optional[str] = None) -> CiTaskRecord:
        record = self._load_record(task_id)
        if record is None:
            raise KeyError(task_id)
        if record.status in {CiTaskStatus.queued, CiTaskStatus.pending, CiTaskStatus.running}:
            raise ValueError("A live CI task cannot be retried")
        return self.submit(
            record.request.model_copy(deep=True),
            public_base_url=public_base_url,
            run_inline=False,
            protocol=record.protocol,
        )

    def replay_ci_callback(self, task_id: str) -> CiTaskRecord:
        record = self._load_record(task_id)
        if record is None:
            raise KeyError(task_id)
        protocol = self._protocol_for(record)
        if protocol is None or not protocol.can_report(record):
            raise ValueError("RDC callback coordinates or callback configuration are unavailable")
        previous_history = list(record.callback_history)
        summary = "Released successfully to RDC by operator from the recent jobs page."
        outcome = protocol.report_result(
            record,
            CiResult(
                passed=True,
                summary=summary,
                report_url=record.report_url or record.task_url or "",
            ),
        )
        record.callback_history = previous_history + outcome.history
        record.callback_succeeded = outcome.succeeded
        record.callback_error = outcome.error
        if outcome.succeeded:
            record.callback_override = {
                "passed": True,
                "source": "recent_jobs_operator",
                "createdAt": utc_now().isoformat(),
                "summary": summary,
            }
        record.updated_at = utc_now()
        self.save(record)
        return record

    def _ci_check_stop_requested(self, task_id: str) -> bool:
        with self._ci_queue_lock:
            return task_id in self._ci_stop_requested

    def _start_pending_ci_checks_if_possible(self) -> None:
        if not self.workspace_manager:
            return
        record = self._claim_next_pending_ci_check()
        if record is None:
            return
        if not self._runner_for_record(record):
            with self._ci_queue_lock:
                self._ci_active_task_ids.discard(record.task_id)
                self._mark_ci_check_pending(record)
                self.save(record)
            return
        thread = threading.Thread(
            target=self._run_check_and_drain,
            args=(record,),
            kwargs={"public_base_url": self._public_base_url_from_record(record)},
            name=f"ci-check-pending-{record.task_id[:8]}",
            daemon=True,
        )
        thread.start()

    def recover_ci_queue(self) -> None:
        """Resume unclaimed persisted CI work without delaying API startup."""
        thread = threading.Thread(
            target=self._start_pending_ci_checks_if_possible,
            name="ci-queue-startup-recovery",
            daemon=True,
        )
        thread.start()

    def _mark_ci_check_pending(self, record: CiTaskRecord) -> None:
        record.status = CiTaskStatus.pending
        record.summary = PENDING_CI_CHECK_SUMMARY
        record.updated_at = utc_now()

    def _mark_ci_check_claimed(self, record: CiTaskRecord) -> None:
        record.status = CiTaskStatus.running
        record.summary = RUNNING_CI_CHECK_SUMMARY
        record.updated_at = utc_now()
        self._ci_active_task_ids.add(record.task_id)

    def _ci_report_limit_reached_locked(self, *, exclude_task_id: Optional[str] = None) -> bool:
        if self.ci_report_parallel_limit <= 0:
            return False
        return self._active_ci_check_count_locked(exclude_task_id=exclude_task_id) >= self.ci_report_parallel_limit

    def _active_ci_check_count_locked(self, *, exclude_task_id: Optional[str] = None) -> int:
        active_ids: set[str] = set()
        live_inflight_ids = self._live_inflight_task_ids()
        for record in self._iter_ci_records_for_queue_locked():
            if exclude_task_id and record.task_id == exclude_task_id:
                continue
            if record.task_id in self._ci_active_task_ids:
                active_ids.add(record.task_id)
                continue
            if record.status != CiTaskStatus.running:
                continue
            if self.inflight_dir is None or record.task_id in live_inflight_ids:
                active_ids.add(record.task_id)
        return len(active_ids)

    def _iter_ci_records_for_queue_locked(self) -> list[CiTaskRecord]:
        records_by_id: dict[str, CiTaskRecord] = {}
        if self.record_store:
            since = utc_now() - timedelta(days=7)
            # Queue selection needs identity, request and status only. Full
            # records can contain tens of megabytes of enforcement output;
            # loading 1,000 of them during startup can OOM the API before it
            # binds its health port.
            for record in self.record_store.list_record_summaries(since=since, limit=1000):
                records_by_id[record.task_id] = record
        records_by_id.update(self._tasks)
        return list(records_by_id.values())

    def _live_inflight_task_ids(self) -> set[str]:
        if self.inflight_dir is None or not self.inflight_dir.exists():
            return set()
        live_ids: set[str] = set()
        for path in self.inflight_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            task_id = str(payload.get("task_id") or "")
            pid = payload.get("pid")
            if task_id and self._pid_is_live(pid):
                live_ids.add(task_id)
        return live_ids

    def _mark_orphaned_running_record_interrupted(self, record: CiTaskRecord) -> None:
        if record.status != CiTaskStatus.running:
            return
        if record.task_id in self._ci_active_task_ids:
            return
        if record.task_id in self._live_inflight_task_ids():
            return
        record.status = CiTaskStatus.failed
        record.summary = "CI test-enforcement report task was interrupted before completion."
        record.updated_at = utc_now()
        self.save(record)

    @staticmethod
    def _pid_is_live(pid: Any) -> bool:
        try:
            os.kill(int(pid), 0)
        except (TypeError, ValueError, ProcessLookupError):
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _public_base_url_from_record(record: CiTaskRecord) -> Optional[str]:
        for value in (record.task_url, record.report_url):
            url = str(value or "")
            for marker in ("/task-status/", "/reports/"):
                if marker in url:
                    return url.split(marker, 1)[0]
        return None

    def _load_record(self, task_id: str) -> Optional[CiTaskRecord]:
        record = self._tasks.get(task_id)
        if record is None and self.record_store:
            record = self.record_store.load(task_id)
            # `_tasks` owns live process state. Completed persisted reports can
            # include tens of megabytes of enforcement output; retaining every
            # report viewed since process start made memory grow without bound.
            if record and record.status in {
                CiTaskStatus.pending,
                CiTaskStatus.queued,
                CiTaskStatus.running,
            }:
                self._tasks[task_id] = record
        return record

    def get(self, task_id: str) -> Optional[CiTaskRecord]:
        record = self._load_record(task_id)
        if record:
            self._mark_orphaned_running_record_interrupted(record)
            self.repair._refresh_repair_sessions(record)
            if record.status in {CiTaskStatus.pending, CiTaskStatus.queued}:
                self._start_pending_ci_checks_if_possible()
        return record

    def save(self, record: CiTaskRecord) -> None:
        if self.record_store:
            self.record_store.save(record)

    def recent_records(
        self,
        *,
        since: datetime,
        limit: int = 200,
        compact: bool = False,
        refresh_repair_sessions: bool = True,
        refresh_repair_session_summaries: bool = False,
    ) -> list[CiTaskRecord]:
        records_by_id: dict[str, CiTaskRecord] = {}
        if self.record_store:
            if compact and hasattr(self.record_store, "list_record_summaries"):
                stored_records = self.record_store.list_record_summaries(since=since, limit=limit)
            else:
                stored_records = self.record_store.list_records(since=since, limit=limit)
            for record in stored_records:
                records_by_id[record.task_id] = record
        for record in self._tasks.values():
            if _aware_datetime(record.created_at) >= _aware_datetime(since):
                records_by_id[record.task_id] = record
        records = sorted(records_by_id.values(), key=lambda item: _aware_datetime(item.created_at), reverse=True)
        limited_records = records[: max(1, int(limit))]
        for record in limited_records:
            self._mark_orphaned_running_record_interrupted(record)
        if refresh_repair_sessions:
            for record in limited_records:
                self.repair._refresh_repair_sessions(record)
        elif refresh_repair_session_summaries:
            for record in limited_records:
                self.repair._refresh_repair_session_summaries(record)
        if any(record.status in {CiTaskStatus.pending, CiTaskStatus.queued} for record in limited_records):
            self._start_pending_ci_checks_if_possible()
        return limited_records









    def _live_repo_task(self, repo_task_id: Any) -> Optional[Dict[str, Any]]:
        if not self.task_manager or not repo_task_id:
            return None
        try:
            repo_task = self.task_manager.get_task(int(repo_task_id))
        except (KeyError, TypeError, ValueError):
            return None
        if repo_task.get("status") in {"CREATED", "QUEUED", "RUNNING", "STOP_REQUESTED"}:
            return repo_task
        return None


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
        override = record.callback_override if isinstance(record.callback_override, dict) else None
        if override and override.get("passed") is True:
            passed = True
            summary = str(override.get("summary") or "Released successfully to RDC by operator.")
        report_url = record.report_url or record.task_url or ""
        previous_history = list(record.callback_history)
        outcome = protocol.report_result(
            record,
            CiResult(passed=passed, summary=summary, report_url=report_url),
        )
        record.callback_history = previous_history + outcome.history
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
    def _set_context_source(context: Dict[str, Any], name: str, available: bool, **extra: Any) -> None:
        sources = context.setdefault("sources", [])
        for source in sources:
            if source.get("name") == name:
                source["available"] = available
                for key, value in extra.items():
                    if value is not None:
                        source[key] = value
                return







    def _runner_for_record(self, record: CiTaskRecord):
        return self.language_handlers.runner_for(record)

    def _handler_for_record(self, record: CiTaskRecord):
        return self.language_handlers.handler_for(record)

    @staticmethod
    def _ci_diff_mutation_gate_for(language: str) -> float:
        if str(language or "").lower() == "python":
            return float(uta_settings.ci_python_diff_mutation_gate)
        return float(uta_settings.ci_diff_mutation_gate)

# Re-exported on purpose: these names are imported from this module
# elsewhere in the tree. Declaring them makes that a contract rather than
# an accident, and lets the linter tell a re-export from a dead import.
__all__ = [
    "FixSessionRateLimitError",
    "FixSessionUnsupportedError",
    "ProtocolNeutralContextProvider",
]

"""Refreshing the repair checkout, and the rerun that may make repair moot.

Between the CI check that failed and the moment a repair session starts, the
branch may have moved -- someone pushed the fix by hand, or an earlier repair
landed. So the workspace is refreshed first and, when the language handler
says the evidence is worth re-establishing, enforcement is run once against
the refreshed tree. If that run is green there is nothing to repair and no
agent is paid to find that out.

This is a distinct responsibility from creating the repair task: it decides
whether a repair task should exist at all, and it is the only part of repair
that touches the Git working copy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from uta.enforcement.enforcement import QualityGateResult
from uta.shared.ci_models import CiTaskRecord, utc_now
from uta.shared.fix_sessions import CreateFixSessionRequest

# The service's logger, not this module's -- see `repair.service`.
LOGGER = logging.getLogger("uta.app.service")


class RepairWorkspaceMixin:
    """Workspace refresh and the pre-repair rerun it enables."""

    def _workspace(self):
        """The git workspace for the checkout, when the service has one.

        Commit-message context reads the repository the workspace prepared, so
        it should be read the same way -- with this deployment's credentials,
        timeout and cancellation -- rather than by a runner assembled at the
        point of use.
        """
        manager = self._service.workspace_manager
        return manager.workspace if manager else None
    def _refresh_repair_workspace(
        self,
        record: CiTaskRecord,
        session: Dict[str, object],
        repo_path: Path,
    ) -> Optional[str]:
        if not self._service.workspace_manager:
            return None
        head = self._service.workspace_manager.refresh_branch(repo_path, branch=record.request.branch)
        session["refreshedHead"] = head
        session["refreshedAt"] = utc_now().isoformat()
        LOGGER.info(
            "ci_repair_workspace_refreshed task_id=%s app=%s branch=%s head=%s",
            record.task_id,
            record.request.app_name,
            record.request.branch,
            head,
        )
        return head
    def _rerun_after_repair_workspace_refresh(
        self,
        record: CiTaskRecord,
        session: Dict[str, object],
        request: CreateFixSessionRequest,
        repo_path: Path,
    ) -> Optional[QualityGateResult]:
        handler = self._service._handler_for_record(record)
        if not handler:
            return None
        session["status"] = "rerun_running"
        # Stamped at every site that starts a rerun, so an interrupted one can
        # be told from a live one. See `_rerun_is_stale`.
        session["rerunStartedAt"] = utc_now().isoformat()
        session["updatedAt"] = utc_now().isoformat()
        record.updated_at = utc_now()
        self._service.save(record)
        # The handler decides what this rerun is. Calling `runner.run` directly
        # re-ran the same sampled profile that produced the failure, so the
        # evidence repair targets are derived from stayed diagnostic.
        result = handler.run_repair_preflight(
            record=record,
            request=request,
            repo_path=repo_path,
        )
        if result is None:
            raise ValueError(
                "Repair preflight was required but did not produce enforcement evidence"
            )
        session["refreshRerunEnforcement"] = result.model_dump(mode="json")
        if result.passed:
            LOGGER.info(
                "ci_repair_workspace_already_green task_id=%s app=%s branch=%s",
                record.task_id,
                record.request.app_name,
                record.request.branch,
            )
        else:
            result_json = result.model_dump(mode="json")
            record.enforcement_result = result_json
            record.summary = result.summary
            session["status"] = "repairing"
            session["updatedAt"] = utc_now().isoformat()
            record.updated_at = utc_now()
            self._service.save(record)
        return result

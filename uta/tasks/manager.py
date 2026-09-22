from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

from uta.tasks.accounting import TaskAccountingMixin
from uta.tasks.creation import TaskCreationMixin
from uta.tasks.db import TaskDB
from uta.tasks.domain_support import config_hash as config_hash  # noqa: F401
from uta.tasks.estimates import TaskEstimateService
from uta.tasks.lifecycle import TaskLifecycleMixin
from uta.tasks.reporting import TaskReportingService


class TaskManager(
    TaskCreationMixin,
    TaskLifecycleMixin,
    TaskAccountingMixin,
):
    def __init__(self, db_path: Optional[Union[os.PathLike[str], str]] = None):
        self.db = TaskDB(db_path)
        self.db.init()
        self.estimates = TaskEstimateService(self.db)
        self.reporting = TaskReportingService(self.db)

    @property
    def db_path(self) -> Path:
        return self.db.path

    def status_payload(self, repo_task_id: int) -> Dict[str, Any]:
        return self.reporting.status_payload(repo_task_id)

    def build_task_summary(
        self,
        repo_task_id: int,
        *,
        recalc_project_coverage: bool = False,
    ) -> Dict[str, Any]:
        return self.reporting.build_summary(
            repo_task_id,
            recalc_project_coverage=recalc_project_coverage,
        )

    def write_live_status(self, repo_task_id: int) -> Dict[str, str]:
        return self.reporting.write_live_status(repo_task_id)

# Re-exported on purpose: these names are imported from this module
# elsewhere in the tree. Declaring them makes that a contract rather than
# an accident, and lets the linter tell a re-export from a dead import.
__all__ = [
    "config_hash",
]

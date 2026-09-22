"""`RepairSessions`: the agent lane of the trigger service, composed.

Split out of `ApiTriggerService`, which was two services in one class. Ten of
its methods ran the deterministic CI check -- claim, queue, concurrency caps --
and thirty ran repair. Those thirty then grew into a 1,090-line module of their
own, so they are now five modules, each with one reason to change:

- `session`      -- admission, reconciliation, terminal reporting
- `progress`     -- read-only projections for callers and reports
- `deferred_task`-- creating the repo task that does the repairing
- `workspace`    -- refreshing the checkout and the rerun that may make
                    repair unnecessary
- `locking`      -- cross-process exclusion for that rerun

They are mixins rather than collaborator objects because they are one object's
behaviour, not five objects: every one of them reads and writes the same fix
session on the same CI record, through the same `self._service`. Turning them
into separate instances would mean passing that shared state across five
boundaries and would change how every caller and test reaches them --
`RepairSessions._repair_progress_stages` is called directly by tests, and the
routes call four methods on the composed object. The split is by file, and
the object stays one object.

The dependency still runs one way, and this class holds the reference: repair
needs the service's store, task manager, workspace manager and protocol
resolution; the service needs nothing from repair beyond the four entry points
it delegates.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from uta.app.repair.deferred_task import DeferredRepairTaskMixin
from uta.app.repair.locking import RERUN_LOCK_STALE_SECONDS, RepairRerunLockMixin
from uta.app.repair.progress import RepairProgressMixin
from uta.app.repair.session import (
    FixSessionRateLimitError,
    FixSessionUnsupportedError,
    RepairSessionMixin,
)
from uta.app.repair.workspace import RepairWorkspaceMixin


# Deliberately the service's logger, not this module's. These are one
# service's operational events -- `ci_repair_preempted` sits in the same stream
# as `ci_check_started` -- and a deployment filtering or alerting on
# "uta.app.service" would have silently stopped seeing half of them the moment
# this file was split out. Splitting a module should not split a log stream,
# and splitting it five ways should not split it five ways.
LOGGER = logging.getLogger("uta.app.service")

PENDING_CI_CHECK_SUMMARY = "CI test-enforcement report task is pending; another report task is running."
RUNNING_CI_CHECK_SUMMARY = "CI test-enforcement report task is running."


if TYPE_CHECKING:  # pragma: no cover - resolves the string annotation below
    from uta.app.service import ApiTriggerService


class RepairSessions(
    RepairSessionMixin,
    RepairProgressMixin,
    DeferredRepairTaskMixin,
    RepairWorkspaceMixin,
    RepairRerunLockMixin,
):
    """Fix-session handling for one `ApiTriggerService`."""

    def __init__(self, service: "ApiTriggerService") -> None:
        self._service = service
        # Repair's own state, which used to sit on the service alongside the
        # CI check's -- two lifecycles in one namespace.
        self.async_repair_task_creation = service.async_repair_task_creation
        self.repair_priority = service.repair_priority
        self.repair_rate_limit_per_task = service.repair_rate_limit_per_task
        self._repair_creation_lock = service._repair_creation_lock
        self._repair_creation_threads = service._repair_creation_threads


__all__ = [
    "FixSessionRateLimitError",
    "FixSessionUnsupportedError",
    "LOGGER",
    "PENDING_CI_CHECK_SUMMARY",
    "RERUN_LOCK_STALE_SECONDS",
    "RUNNING_CI_CHECK_SUMMARY",
    "RepairSessions",
]

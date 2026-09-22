"""Fix sessions: the agent lane of the trigger service.

`uta.app.repair` was a single 1,090-line module. It is now a package whose
composed object, `RepairSessions`, is unchanged: same methods, same names,
same behaviour, same import path. See `repair.service` for why the seams fall
where they do.
"""

from __future__ import annotations

from uta.app.repair.locking import RERUN_LOCK_STALE_SECONDS
from uta.app.repair.service import (
    LOGGER,
    PENDING_CI_CHECK_SUMMARY,
    RUNNING_CI_CHECK_SUMMARY,
    FixSessionRateLimitError,
    FixSessionUnsupportedError,
    RepairSessions,
)

__all__ = [
    "FixSessionRateLimitError",
    "FixSessionUnsupportedError",
    "LOGGER",
    "PENDING_CI_CHECK_SUMMARY",
    "RERUN_LOCK_STALE_SECONDS",
    "RUNNING_CI_CHECK_SUMMARY",
    "RepairSessions",
]

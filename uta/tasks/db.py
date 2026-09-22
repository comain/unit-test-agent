"""The task database: one connection, one transaction owner, five repositories.

This was 1,631 lines. The repositories are mixins rather than collaborators,
and that is the whole design constraint rather than a convenience: `TaskDB`
owns `connect()` and `transaction()`, and every repository writes through
them. Giving each its own connection is precisely how one atomic transition
becomes several, which is the failure the crash-ordering work exists to
prevent.

The class, its name and its import path are unchanged. Callers and tests
reach dozens of these methods directly, and moving them was never the point.
"""

from uta.tasks.storage.base import (
    WorkflowOperationConflict,
    default_db_path,
)
from uta.tasks.storage.connection import SQLiteConnectionOwner
from uta.tasks.storage.event_repository import EventRepositoryMixin
from uta.tasks.storage.operation_repository import OperationRepositoryMixin
from uta.tasks.storage.schema import SchemaMixin
from uta.tasks.storage.scheduler_repository import SchedulerRepositoryMixin
from uta.tasks.storage.task_repository import TaskRepositoryMixin


class TaskDB(
    SQLiteConnectionOwner,
    SchemaMixin,
    TaskRepositoryMixin,
    EventRepositoryMixin,
    OperationRepositoryMixin,
    SchedulerRepositoryMixin,
):
    """SQLite-backed task storage.

    The connection and the transaction live here, alone, so that every
    repository above shares exactly one of each.
    """


__all__ = ["TaskDB", "WorkflowOperationConflict", "default_db_path"]

from __future__ import annotations

from uta.tasks.accounting.git import TaskGitAccountingMixin
from uta.tasks.accounting.results import TaskResultSyncMixin
from uta.tasks.accounting.tokens import TaskTokenAccountingMixin


class TaskAccountingMixin(
    TaskResultSyncMixin,
    TaskTokenAccountingMixin,
    TaskGitAccountingMixin,
):
    """Task accounting mixin combining result synchronization, token usage, and git recording."""
    pass


__all__ = [
    "TaskAccountingMixin",
    "TaskGitAccountingMixin",
    "TaskResultSyncMixin",
    "TaskTokenAccountingMixin",
]

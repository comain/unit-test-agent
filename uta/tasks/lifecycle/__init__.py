from __future__ import annotations

from uta.tasks.lifecycle.recovery import TaskLifecycleRecoveryMixin
from uta.tasks.lifecycle.stages import TaskLifecycleStageMixin
from uta.tasks.lifecycle.state import TaskLifecycleStateMixin


class TaskLifecycleMixin(
    TaskLifecycleStateMixin,
    TaskLifecycleRecoveryMixin,
    TaskLifecycleStageMixin,
):
    """Lifecycle mixin for task manager combining state, recovery, and stage tracking."""
    pass


__all__ = [
    "TaskLifecycleMixin",
    "TaskLifecycleRecoveryMixin",
    "TaskLifecycleStageMixin",
    "TaskLifecycleStateMixin",
]

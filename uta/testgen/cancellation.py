"""Turning UTA's stop and cancel rows into the predicate agent-core wants.

agent-core owns the mechanism: its git operations, process runner and session
poll all take an ``is_cancelled`` callable and check it while they work. What
it cannot know is what "cancelled" means here -- that a repo task row reads
``STOP_REQUESTED``, or that the latest control row says stop or cancel. That
is this project's vocabulary, over this project's tables.

Before this, the only place a stop was noticed was `llm_guard_before`, which
runs *between* phases. A stop requested during a fifteen-minute generation
turn therefore did nothing until the turn ended on its own -- the operator saw
a task that would not stop, and the run kept billing.

Not yet threaded into the poll. `poll_completion_with_task_guard` accepts it,
and agent-core's client passes it down to the process, which terminates the
child -- but UTA's own test doubles model a `poll_completion` without the
parameter, so wiring it means updating about thirteen of them. That is worth
doing and is not worth doing in the middle of another change to the same call
paths.

The predicate is deliberately cheap and silent. It is called on every pass of
a poll loop, so it must not raise -- a database hiccup should not turn a
running turn into a crash -- and on failure it answers "not cancelled", which
leaves the run exactly where it would have been without any of this.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - the annotation above is a string
    from uta.testgen.ports.persistence import TaskControlPort

logger = logging.getLogger("uta")


def cancellation_predicate(
    control: "TaskControlPort", task_id: Optional[int]
) -> Optional[Callable[[], bool]]:
    """An ``is_cancelled`` for one task, or None when there is nothing to watch.

    Returning None rather than a predicate that is always False matters:
    agent-core omits the argument entirely when it is None, which keeps the
    call it makes identical for callers that have no task at all.
    """
    if control is None or task_id is None:
        return None

    task = int(task_id)

    def is_cancelled() -> bool:
        try:
            return control.is_stop_requested(str(task))
        except Exception:  # noqa: BLE001 - a stop check must never fail a turn
            logger.debug("stop check failed for task %s", task, exc_info=True)
            return False

    return is_cancelled


def cancellation_from_state(
    state, control: "Optional[TaskControlPort]" = None
) -> Optional[Callable[[], bool]]:
    """The same, from the workflow state a node already carries plus its port.

    The state says *which* task to watch; the injected port says how to ask.
    Without a port there is nothing to ask, so this returns None -- the same
    answer it gives for a state with no task, and for the same reason: agent-core
    omits `is_cancelled` entirely when it is None.
    """
    if not state:
        return None
    task_id = state.get("task_id")
    db_path = state.get("task_db_path")
    if not task_id or not db_path:
        return None

    # The port arrives from the application, which is the only layer allowed to
    # know what implements it. An earlier version imported the adapter here and
    # the dependency scanner caught it immediately: `uta.app` joined the package
    # cycle, because a workflow importing its own composition root is the same
    # inversion in a nicer coat.
    if control is None:
        return None
    return cancellation_predicate(control, task_id)

"""UTA's stop rows, as the predicate agent-core checks while it works.

agent-core owns the mechanism -- git operations, the process runner and the
session poll all take `is_cancelled` and check it mid-flight. What "cancelled"
means is this project's: a repo task row reading STOP_REQUESTED, or a control
row saying stop or cancel.

Before this the only place a stop was noticed was `llm_guard_before`, which
runs *between* phases, so a stop requested during a fifteen-minute generation
turn did nothing until the turn ended on its own.
"""

from __future__ import annotations

import pytest

from uta.testgen.cancellation import cancellation_from_state, cancellation_predicate


class Manager:
    """A stand-in for the control port, which is all the predicate needs.

    It used to stand in for `TaskManager`, and the difference is the point: a
    fake with one method is a fake of something narrow, and it is now the whole
    surface the predicate can reach.
    """

    def __init__(self, reason=None, raises=False):
        self.reason = reason
        self.raises = raises
        self.calls = 0

    def is_stop_requested(self, task_id):
        self.calls += 1
        if self.raises:
            raise RuntimeError("database is locked")
        return self.reason is not None


def test_no_stop_means_not_cancelled():
    assert cancellation_predicate(Manager(), 1)() is False


def test_a_requested_stop_cancels():
    assert cancellation_predicate(Manager(reason="stop requested"), 1)() is True


def test_it_is_asked_every_time_rather_than_cached():
    """It is polled during a turn; a stop arriving mid-turn must be seen."""
    manager = Manager()
    predicate = cancellation_predicate(manager, 1)

    predicate()
    assert predicate() is False

    manager.reason = "cancel requested"
    assert predicate() is True
    assert manager.calls == 3


def test_a_failing_check_does_not_fail_the_turn():
    """It runs on every pass of a poll loop; a hiccup must not crash a run."""
    assert cancellation_predicate(Manager(raises=True), 1)() is False


def test_no_task_yields_no_predicate():
    """None, not an always-False callable: agent-core then omits the argument
    entirely, keeping its call identical for callers with no task."""
    assert cancellation_predicate(Manager(), None) is None
    assert cancellation_predicate(None, 1) is None


@pytest.mark.parametrize(
    "state",
    [None, {}, {"task_id": 1}, {"task_db_path": "/tmp/x.db"}, {"task_id": None, "task_db_path": "x"}],
)
def test_an_incomplete_state_yields_no_predicate(state):
    assert cancellation_from_state(state) is None


def test_a_complete_state_yields_one(tmp_path):
    predicate = cancellation_from_state(
        {"task_id": 1, "task_db_path": str(tmp_path / "tasks.db")},
        Manager(),
    )

    assert callable(predicate)
    assert predicate() is False, "nothing has asked this task to stop"


def test_a_complete_state_without_a_port_yields_nothing():
    """Testgen cannot build the port itself -- that would mean importing the
    application that composes it, which is the inversion being removed."""
    assert cancellation_from_state({"task_id": 1, "task_db_path": "tasks.db"}) is None

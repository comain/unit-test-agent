"""Which branch delivery pushes.

Found replaying a production task on beta: every run died at

    error: src refspec unit-code-gen does not match any

`commit_to_branch` fell back to the literal `"unit-code-gen"` whenever
`state["branch_name"]` was missing, and for Python it is always missing --
`BatchGenerationRequest` has no branch field, and `prepare_workspace` for a
caller-supplied workspace deliberately skips the branch setup that would
otherwise populate it. The commit itself succeeded; only the push failed, so
the work was done and then stranded, and the task reported FAILED.

The task row is the authority: `uta tasks create --branch-name` writes it, and
the daemon checked out that branch before any of this ran. Delivery already
holds the task id and DB path, so it can simply ask.
"""

from __future__ import annotations

from uta.testgen.delivery import _resolve_push_branch


class FakeManager:
    def __init__(self, branch):
        self._branch = branch
        self.asked = 0

    def get_task(self, task_id):
        self.asked += 1
        return {"branch_name": self._branch} if self._branch is not None else None


def test_state_wins_when_it_has_a_branch():
    """The Java path sets it explicitly; that must keep working untouched."""
    manager = FakeManager("from-db")

    assert _resolve_push_branch({"branch_name": "from-state"}, manager, 1) == "from-state"
    assert manager.asked == 0, "the DB was queried when state already knew"


def test_the_task_row_supplies_it_when_state_does_not():
    """The Python path. This is the bug: it used to push `unit-code-gen`, a
    branch nobody had created."""
    assert _resolve_push_branch({}, FakeManager("TASK-40990-20260810"), 1) == "TASK-40990-20260810"


def test_a_blank_state_value_is_not_treated_as_a_branch():
    assert _resolve_push_branch({"branch_name": ""}, FakeManager("real-branch"), 1) == "real-branch"


def test_the_legacy_default_remains_the_last_resort():
    """Unchanged for any caller with neither source, so nothing that works
    today starts failing."""
    assert _resolve_push_branch({}, FakeManager(None), 1) == "unit-code-gen"


def test_a_broken_task_lookup_does_not_break_delivery():
    """Delivery runs after the expensive work. A failure reading the task row
    must not turn a completed generation into a crash."""

    class Exploding:
        def get_task(self, task_id):
            raise RuntimeError("db is gone")

    assert _resolve_push_branch({}, Exploding(), 1) == "unit-code-gen"


def test_no_manager_falls_back_cleanly():
    assert _resolve_push_branch({}, None, None) == "unit-code-gen"

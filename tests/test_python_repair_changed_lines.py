"""A python repair must scope verification to the lines CI flagged.

Beta: enforcement correctly failed `dify_app_sync.py` at 9.09% coverage over
eighteen new lines, a fix session opened, and the repair task ran
`precheck_existing_tests` -> `complete_generation` with zero LLM turns, zero
tokens and no commit. The session went green having written nothing.

Python verification scopes coverage and mutation to `state["changed_lines"]`.
The repair task never received them, so the precheck measured coverage over an
empty set of lines, scored the vacuous 100% that an empty denominator gives,
concluded the target already passed, and skipped generation.

`_changed_lines_from_task_context` did this on main, in the
`uta/language/python/batch.py` module this branch replaced with a facade -- the
helper went with it and nothing took its place. That is a migration
regression, not an upstream defect.
"""

from __future__ import annotations

import json

import pytest

from uta.language.python.cycle_inputs import _changed_lines
from uta.language.python.selection import changed_lines_from_task_context


CHANGED = {"chat_robot/service/dify_app_sync.py": [568, 569, 570]}


class _Manager:
    def __init__(self, context):
        self._context = context

    def get_task(self, task_id):
        return {"rdc_context_json": json.dumps(self._context)}


def _context(changed):
    return {"enforcement": {"evidence": {"changedLines": changed}}}


def test_the_enforcement_lines_are_recovered():
    lines = changed_lines_from_task_context(_Manager(_context(CHANGED)), 1)

    assert lines == CHANGED


def test_the_snake_case_spelling_is_accepted():
    manager = _Manager({"enforcement": {"evidence": {"changed_lines": CHANGED}}})

    assert changed_lines_from_task_context(manager, 1) == CHANGED


def test_lines_are_sorted_and_deduplicated():
    manager = _Manager(_context({"a.py": [5, 3, 3, 5, 1]}))

    assert changed_lines_from_task_context(manager, 1) == {"a.py": [1, 3, 5]}


def test_non_positive_lines_are_dropped():
    """A zero or negative line number is not a line, and passing one into
    coverage scoping silently widens or breaks the measurement."""
    manager = _Manager(_context({"a.py": [0, -2, 7]}))

    assert changed_lines_from_task_context(manager, 1) == {"a.py": [7]}


@pytest.mark.parametrize(
    "context",
    [{}, {"enforcement": {}}, {"enforcement": {"evidence": {}}},
     {"enforcement": {"evidence": {"changedLines": {}}}}],
    ids=["empty", "no-evidence", "no-lines", "empty-map"],
)
def test_nothing_recorded_yields_nothing(context):
    assert changed_lines_from_task_context(_Manager(context), 1) is None


@pytest.mark.parametrize(
    "manager,task_id", [(None, 1), (_Manager({}), None)], ids=["no-manager", "no-task"]
)
def test_without_a_task_there_is_nothing_to_read(manager, task_id):
    assert changed_lines_from_task_context(manager, task_id) is None


# -- the wiring, which is what actually broke -------------------------------


def test_an_explicit_value_is_not_overridden():
    """A caller that scoped the run itself means it."""
    assert _changed_lines({"changed_lines": {"x.py": [1]}, "task_id": 1}) == {"x.py": [1]}


def test_a_cycle_without_a_task_keeps_the_empty_scope():
    assert _changed_lines({}) == {}


def test_the_cycle_recovers_the_lines_from_the_task(tmp_path):
    """End to end through `_changed_lines`, which is what
    `prepare_python_cycle_state` actually calls. Every test above passes even
    if nothing ever calls the helper -- that is exactly how this shipped."""
    import uta.language.python.batch  # noqa: F401  -- registers the backend
    from uta.shared.languages import RawTargetSelection, default_registry
    from uta.tasks.manager import TaskManager

    repo = tmp_path / "repo"
    (repo / "jobs").mkdir(parents=True)
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    db = tmp_path / "tasks.db"
    manager = TaskManager(db)
    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py")
    )
    task_id = manager.create_task_targets(
        repo_path=str(repo),
        targets=[target],
        language="python",
        rdc_context=_context({"jobs/forecast.py": [12, 13]}),
    )

    recovered = _changed_lines({"task_id": task_id, "task_db_path": str(db)})

    assert recovered == {"jobs/forecast.py": [12, 13]}

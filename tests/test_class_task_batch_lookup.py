"""Looking up a batch of class rows in one query instead of one per class.

`llm_guard_before` runs before every LLM phase and looked each class up
individually — and `find_class_task` opens its own connection, so a batch of
twenty classes cost twenty connections and twenty queries, three times over in
the same guard call. The guard runs on the hot path of every phase of every
target, so this multiplies out.

The behaviour must not change. These tests pin both halves: the lookup returns
exactly what the per-class calls returned, and it does so in one query.
"""

from __future__ import annotations


import pytest

from uta.tasks.manager import TaskManager


@pytest.fixture
def manager(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return TaskManager(tmp_path / "tasks.db")


@pytest.fixture
def task(manager):
    return manager.create_task(
        repo_path=str(manager.db.path.parent / "repo"),
        class_fqns=["pkg.A", "pkg.B", "pkg.C"],
    )


def counting(db):
    """Count queries by wrapping the connection factory."""
    calls = {"queries": 0, "connections": 0}
    original = db.connect

    class CountingConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args, **kwargs):
            calls["queries"] += 1
            return self._conn.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    class Ctx:
        def __enter__(self):
            calls["connections"] += 1
            self._ctx = original()
            return CountingConn(self._ctx.__enter__())

        def __exit__(self, *exc):
            return self._ctx.__exit__(*exc)

    db.connect = lambda: Ctx()
    return calls


# -- the same answers --------------------------------------------------------

def test_it_returns_the_same_rows_as_the_per_class_lookup(manager, task):
    one_at_a_time = {
        fqn: manager.db.find_class_task(task, fqn) for fqn in ("pkg.A", "pkg.B", "pkg.C")
    }

    batched = manager.db.find_class_tasks(task, ["pkg.A", "pkg.B", "pkg.C"])

    assert set(batched) == set(one_at_a_time)
    for fqn, row in one_at_a_time.items():
        assert dict(batched[fqn]) == dict(row)


def test_an_unknown_class_is_absent_rather_than_none(manager, task):
    """The caller's loop does `if not row: continue`. An explicit None entry
    would work too, but absence keeps "we have no row" and "the row is empty"
    from ever being confused."""
    batched = manager.db.find_class_tasks(task, ["pkg.A", "pkg.MISSING"])

    assert "pkg.A" in batched
    assert "pkg.MISSING" not in batched


def test_it_does_not_leak_rows_from_another_task(manager, task):
    """The scariest possible bug here: charging one task's budget to another."""
    other = manager.create_task(
        repo_path=str(manager.db.path.parent / "repo"), class_fqns=["pkg.A"]
    )

    batched = manager.db.find_class_tasks(other, ["pkg.A"])

    assert batched["pkg.A"]["repo_task_id"] == other
    assert batched["pkg.A"]["id"] != manager.db.find_class_task(task, "pkg.A")["id"]


def test_an_empty_batch_asks_nothing(manager, task):
    calls = counting(manager.db)

    assert manager.db.find_class_tasks(task, []) == {}
    assert calls["queries"] == 0


def test_duplicate_names_are_tolerated(manager, task):
    batched = manager.db.find_class_tasks(task, ["pkg.A", "pkg.A", "pkg.B"])

    assert set(batched) == {"pkg.A", "pkg.B"}


# -- in one query ------------------------------------------------------------

def test_a_batch_costs_one_query(manager, task):
    calls = counting(manager.db)

    manager.db.find_class_tasks(task, ["pkg.A", "pkg.B", "pkg.C"])

    assert calls["queries"] == 1
    assert calls["connections"] == 1


def test_the_old_path_would_have_cost_one_each(manager, task):
    """States the thing being fixed, so the improvement cannot silently
    regress into a loop that happens to still return the right answers."""
    calls = counting(manager.db)

    for fqn in ("pkg.A", "pkg.B", "pkg.C"):
        manager.db.find_class_task(task, fqn)

    assert calls["queries"] == 3
    assert calls["connections"] == 3


def test_a_batch_larger_than_sqlites_parameter_limit_still_works(manager):
    """SQLite caps host parameters per statement. A batch big enough to exceed
    it must be chunked rather than raising -- and a raise here would take down
    a guard on the hot path of every phase."""
    task_id = manager.create_task(
        repo_path=str(manager.db.path.parent / "repo"),
        class_fqns=[f"pkg.C{i}" for i in range(1200)],
    )

    batched = manager.db.find_class_tasks(task_id, [f"pkg.C{i}" for i in range(1200)])

    assert len(batched) == 1200


def test_a_huge_batch_is_still_far_fewer_queries_than_classes(manager):
    task_id = manager.create_task(
        repo_path=str(manager.db.path.parent / "repo"),
        class_fqns=[f"pkg.C{i}" for i in range(1200)],
    )
    calls = counting(manager.db)

    manager.db.find_class_tasks(task_id, [f"pkg.C{i}" for i in range(1200)])

    assert calls["queries"] < 10

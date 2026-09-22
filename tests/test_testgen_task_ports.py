"""Testgen talks to task storage through narrow ports it owns.

Success criterion 7. Eighteen sites across eight testgen modules took a
`task_db_path` out of workflow state and constructed a `TaskManager` or `TaskDB`
on the spot. That is the broad-manager-in-domain-code the criterion names: the
workflow could reach any persistence method that exists, so the dependency was
"all of task storage" and no test could say otherwise.

The ports are split by responsibility rather than gathered into one interface.
A single port with eighteen methods is the manager again, wearing a Protocol.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest



REPO_ROOT = Path(__file__).resolve().parents[1]
TESTGEN = REPO_ROOT / "uta" / "testgen"


def _constructions(root: Path) -> list[str]:
    """Every `TaskManager(...)` / `TaskDB(...)` call under `root`."""
    found = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"TaskManager", "TaskDB"}:
                found.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    return found


#: Every remaining site that takes a `task_db_path` out of workflow state and
#: builds the whole of task storage from it. A ratchet, not a waiver: a new one
#: fails this test, and so does removing one, which forces the list to shrink
#: deliberately instead of rotting.
#:
#: They are not all the same job, which is why they are not all closed at once.
#: `task_guard`'s remaining site and `progress` read per-class turn counts and
#: write them back -- that is a repository, not a narrow port, and pretending
#: otherwise is how a port ends up eighteen methods wide. `delivery` writes
#: accounting. `standalone_execution` creates the task in the first place, which
#: is arguably composition rather than workflow.
REMAINING_MANAGER_CONSTRUCTIONS: set[str] = set()


def test_no_new_broad_manager_construction_appears_in_testgen():
    found = set(_constructions(TESTGEN))
    new = sorted(found - REMAINING_MANAGER_CONSTRUCTIONS)
    closed = sorted(REMAINING_MANAGER_CONSTRUCTIONS - found)
    assert new == [], f"new broad-manager construction in testgen: {new}"
    assert closed == [], f"these are gone -- remove them from the list: {closed}"


def test_testgen_modules_import_no_task_storage():
    """All testgen modules must use narrow injected ports, not direct task storage."""
    for path in sorted(TESTGEN.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not any(m.startswith(("uta.app", "uta.tasks")) for m in imported), f"{path.relative_to(TESTGEN)} imported {imported}"


def test_the_cancellation_path_takes_an_injected_port():
    """The first one migrated, and the shape the rest must take."""
    path = TESTGEN / "cancellation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    assert not any(m.startswith("uta.app") for m in imported), sorted(imported)
    assert not any(m.startswith("uta.tasks") for m in imported), sorted(imported)
    assert "uta.testgen.ports.persistence" in imported or "uta.testgen.ports.registry" in imported


def test_the_ports_are_split_by_responsibility():
    from uta.testgen.ports import persistence

    for name in ("TaskControlPort", "TaskEventPort", "DeliveryOutcomePort", "TaskReaderPort"):
        assert hasattr(persistence, name), name


def test_no_port_is_a_manager_in_disguise():
    """The guardrail that keeps this from collapsing back into one interface."""
    from uta.testgen.ports import persistence

    for name in dir(persistence):
        if not name.endswith("Port"):
            continue
        port = getattr(persistence, name)
        methods = [m for m in dir(port) if not m.startswith("_")]
        assert len(methods) <= 6, f"{name} has {len(methods)} methods: {methods}"


def test_the_app_adapter_satisfies_every_port():
    from uta.app.persistence import TaskPersistenceAdapter
    from uta.testgen.ports import persistence

    adapter = TaskPersistenceAdapter(":memory:")
    for name in ("TaskControlPort", "TaskEventPort", "DeliveryOutcomePort", "TaskReaderPort"):
        port = getattr(persistence, name)
        for method in (m for m in dir(port) if not m.startswith("_")):
            assert hasattr(adapter, method), f"{name}.{method} is unimplemented"


def test_the_ports_module_imports_no_task_implementation():
    """The direction that makes them ports rather than aliases."""
    source = (TESTGEN / "ports" / "persistence.py").read_text(encoding="utf-8")
    assert "uta.tasks" not in source


# -- how the port reaches a graph node ----------------------------------------
#
# Not through the state: workflow state is serialised into LangGraph
# checkpoints, so a live database handle in it would not survive a resume and
# would not serialise in the first place. Not by import either -- that is the
# inversion being removed. The application registers a provider once at
# startup, and testgen asks for one by database path.


def test_without_registration_there_is_no_provider():
    from uta.testgen.ports import registry

    registry.reset_task_persistence_provider()
    assert registry.task_ports_for("tasks.db") is None


def test_the_app_registers_the_provider():
    from uta.app.persistence import register_task_persistence
    from uta.testgen.ports import registry

    registry.reset_task_persistence_provider()
    register_task_persistence()
    ports = registry.task_ports_for(":memory:")
    assert ports is not None
    assert hasattr(ports, "is_stop_requested")


def test_the_registry_module_imports_no_application_code():
    source = (TESTGEN / "ports" / "registry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any(m.startswith(("uta.app", "uta.tasks")) for m in imported), sorted(imported)


@pytest.mark.parametrize("entrypoint", ["uta/app/cli.py", "uta/app/app.py"])
def test_every_entrypoint_registers_the_provider(entrypoint):
    """Registering late does not raise -- it silently skips a safety guard,
    because `task_ports_for` answers None until someone registers. So both
    entrypoints have to do it, and this is what says so."""
    source = (REPO_ROOT / entrypoint).read_text(encoding="utf-8")
    assert "register_task_persistence()" in source, entrypoint


def test_the_guard_records_through_the_port(tmp_path):
    """The safety guard writes its verdict through the port, not a manager."""
    from uta.testgen.ports import registry

    recorded = {}

    class Ports:
        def record_unsafe_diff(self, task_id, class_fqns, *, stage, message, paths=(), fail_task=False):
            recorded.update(
                task_id=task_id, stage=stage, message=message,
                paths=list(paths), fail_task=fail_task,
            )

        def mark_failed(self, task_id, reason="", *, stage=""):
            recorded.update(failed=task_id, reason=reason, stage=stage)

    registry.set_task_persistence_provider(lambda _path: Ports())
    try:
        from uta.testgen.task_guard import TaskUnsafeDiffError, verify_task_branch_and_preexisting_diff

        repo = tmp_path / "repo"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        (repo / "stray.txt").write_text("unexpected\n", encoding="utf-8")

        state = {"task_id": 7, "task_db_path": str(tmp_path / "t.db"), "repo_path": str(repo)}
        with pytest.raises(TaskUnsafeDiffError):
            verify_task_branch_and_preexisting_diff(state, ["com.example.Foo"])
        assert recorded["task_id"] == "7"
        assert recorded["fail_task"] is True
        assert recorded["stage"] == "branch_safety"
    finally:
        registry.reset_task_persistence_provider()


def test_the_event_port_translates_to_the_row_storage_expects():
    """`TaskDB.add_event` takes a class-task id, a message and three keyword
    fields. The workflow knows none of that shape, so the adapter owns the
    translation -- doing it in a graph node is what a port is meant to stop."""
    from uta.app.persistence import TaskPersistenceAdapter

    seen = {}

    class FakeDB:
        def add_event(self, repo_task_id, class_task_id, event_type, message, *, stage=None, severity="INFO", payload=None):
            seen.update(
                repo_task_id=repo_task_id, class_task_id=class_task_id,
                event_type=event_type, message=message,
                stage=stage, severity=severity, payload=payload,
            )

    class FakeManager:
        db = FakeDB()

    adapter = TaskPersistenceAdapter(":memory:")
    adapter._manager = FakeManager()
    adapter.add_event(
        "7",
        "generation_engine_selected",
        {"message": "picked durable_v2", "stage": "run_generation_cycle", "payload": {"engine": "durable_v2"}},
    )

    assert seen["repo_task_id"] == 7
    assert seen["class_task_id"] is None
    assert seen["event_type"] == "generation_engine_selected"
    assert seen["message"] == "picked durable_v2"
    assert seen["stage"] == "run_generation_cycle"
    assert seen["payload"] == {"engine": "durable_v2"}


def test_task_snapshot_satisfies_durable_generation_requirement(tmp_path):
    """The snapshot returned by get_task_snapshot must pass require_durable_generation_task."""
    from uta.app.persistence import TaskPersistenceAdapter
    from uta.tasks.manager import TaskManager
    from uta.testgen.cutover import require_durable_generation_task

    db_path = tmp_path / "tasks.sqlite"
    manager = TaskManager(db_path)
    task_id = manager.create_task(
        repo_path=str(tmp_path),
        class_fqns=["com.example.Foo"],
    )
    adapter = TaskPersistenceAdapter(db_path)
    snapshot = adapter.get_task_snapshot(str(task_id))
    # Must not raise LegacyGenerationTaskError
    require_durable_generation_task(snapshot, action="run durable generation")


def test_require_durable_generation_task_rejects_non_integer_versions():
    import json
    from uta.testgen.cutover import LegacyGenerationTaskError, require_durable_generation_task

    with pytest.raises(LegacyGenerationTaskError, match="unsupported_generation_engine_version"):
        require_durable_generation_task(
            {"id": 1, "config_snapshot_json": json.dumps({"generation_engine_version": 2.0})},
            action="test",
        )

    with pytest.raises(LegacyGenerationTaskError, match="unsupported_generation_engine_version"):
        require_durable_generation_task(
            {"id": 1, "config_snapshot_json": json.dumps({"generation_engine_version": True})},
            action="test",
        )


def test_testgen_workflow_modules_do_not_leak_or_reach_db_handles():
    """No testgen workflow module should reach into ports.db, ports.manager, or getattr(..., 'db')."""
    workflow_files = [
        "delivery.py",
        "cutover.py",
        "task_guard.py",
        "cancellation.py",
        "standalone_execution.py",
        "graph/application.py",
        "graph/durable_cycle.py",
        "graph/generation_cycle.py",
        "graph/cycle.py",
    ]
    leaks = []
    for rel in workflow_files:
        path = TESTGEN / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"db", "_manager", "manager"}:
                leaks.append(f"{rel}:{node.lineno} (attr: {node.attr})")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value in {"db", "_manager", "manager"}:
                    leaks.append(f"{rel}:{node.lineno} (getattr: {node.args[1].value})")
    assert leaks == [], f"testgen workflow modules leak or access DB handles: {leaks}"

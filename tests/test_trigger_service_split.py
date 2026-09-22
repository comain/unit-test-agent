"""The trigger service is two services, and now says so.

`ApiTriggerService` held both lanes in one class: ten methods running the
deterministic CI check -- claim, queue, concurrency caps -- and thirty running
repair. They shared a name, a file and a `self`, so the boundary between the
deterministic lane and the agent lane existed in the description of this
system and nowhere in its code.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


REPAIR_PKG = ROOT / "uta" / "app" / "repair"


def _methods(path, class_name):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name)
    return {m.name for m in cls.body if isinstance(m, ast.FunctionDef)}


def _repair_methods():
    """Every method `RepairSessions` composes, wherever its mixin lives."""
    names = set()
    for module in sorted(REPAIR_PKG.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                names |= {m.name for m in node.body if isinstance(m, ast.FunctionDef)}
    return names


def test_repair_lives_in_its_own_module():
    """Still one import path, now backed by one module per reason to change."""
    from uta.app.repair import RepairSessions

    assert REPAIR_PKG.is_dir()
    assert {path.stem for path in REPAIR_PKG.glob("*.py")} == {
        "__init__", "service", "session", "progress",
        "deferred_task", "workspace", "locking",
    }
    assert RepairSessions is not None


def test_the_service_no_longer_implements_repair():
    service = _methods("uta/app/service.py", "ApiTriggerService")
    repair = _repair_methods()

    # The four the routes call are kept as delegating entry points; nothing else
    # about repair should remain implemented on the service.
    delegated = {
        "create_fix_session", "repair_progress",
        "materialize_retry_request", "wait_for_deferred_repair_tasks",
    }
    # `__init__` is on both because both are classes, not because repair is
    # still implemented twice.
    assert (service & repair) - {"__init__"} == delegated, sorted(service & repair)


def test_the_public_surface_is_unchanged():
    """Routes and tests call these; the split must be invisible to them."""
    from uta.app.service import ApiTriggerService

    for name in (
        "create_fix_session", "repair_progress",
        "materialize_retry_request", "wait_for_deferred_repair_tasks",
    ):
        assert callable(getattr(ApiTriggerService, name)), name


def test_the_exceptions_have_exactly_one_identity():
    """Two classes of the same name would mean an `except` that silently stops
    catching -- which is what the first version of this split produced."""
    from uta.app.repair import FixSessionUnsupportedError as owned
    from uta.app.service import FixSessionUnsupportedError as reexported

    assert owned is reexported


def test_repair_logs_into_the_services_stream():
    """`ci_repair_preempted` belongs beside `ci_check_started`. A deployment
    filtering on "uta.app.service" would otherwise have silently lost half its
    operational events when this file was split out."""
    from uta.app import repair

    assert repair.LOGGER.name == "uta.app.service"


def test_the_dependency_runs_one_way():
    """Repair holds the service; the service knows only the four entry points.

    Same direction the packages take -- testgen above enforcement -- and now
    true of the trigger service too.
    """
    sources = "".join(
        path.read_text(encoding="utf-8") for path in sorted(REPAIR_PKG.glob("*.py"))
    )
    assert "self._service" in sources

    service_source = (ROOT / "uta" / "app" / "service.py").read_text(encoding="utf-8")
    assert "self.repair" in service_source

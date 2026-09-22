"""How a graph node gets its task ports.

Three routes were possible and two are closed.

*Through the workflow state* is closed because state is serialised into
LangGraph checkpoints. A live database handle would not serialise, and if it
did it would be meaningless after a resume in a different process.

*By import* is closed because it inverts the dependency: a workflow that imports
its composition root is the thing this whole boundary exists to remove, and the
dependency scanner reports it immediately.

What is left is registration. The application calls
`set_task_persistence_provider` once at startup with a factory; testgen asks for
ports by database path and gets None when nothing has registered. None is a real
answer -- a standalone run with no task database has no ports and no stop to
check -- so callers handle it rather than treating it as an error.
"""

from __future__ import annotations

from typing import Any, Callable, Optional


#: Set by the application. Deliberately module-level: there is one task database
#: per process, and threading a factory through every graph node would put the
#: composition root back in every signature.
_provider: Optional[Callable[[str], Any]] = None


def set_task_persistence_provider(provider: Callable[[str], Any]) -> None:
    """Register how to build task ports for a database path."""
    global _provider
    _provider = provider


def reset_task_persistence_provider() -> None:
    """Forget the provider. For tests, and for a process that changes tenancy."""
    global _provider
    _provider = None


def task_ports_for(task_db_path: Any) -> Optional[Any]:
    """Ports for this database, or None when nothing has registered a provider."""
    if _provider is None or not task_db_path:
        return None
    return _provider(str(task_db_path))


def task_ports_from_state(state: Any) -> Optional[Any]:
    """The same, for a graph node that already carries workflow state."""
    if not state:
        return None
    return task_ports_for(state.get("task_db_path"))


__all__ = [
    "reset_task_persistence_provider",
    "set_task_persistence_provider",
    "task_ports_for",
    "task_ports_from_state",
]

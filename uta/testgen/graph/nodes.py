"""Language-neutral node surface for the test-generation graph."""

from __future__ import annotations

from typing import Any, Dict

from uta.shared.languages import default_registry
from uta.testgen.backend import TestGenerationBackend
from uta.testgen.graph.durable_cycle import run_generation_cycle as run_durable_cycle
from uta.testgen.targets import active_target_ids
from uta.testgen.workspace_guard import guard_language


def _backend(state: Dict[str, Any]) -> TestGenerationBackend:
    language = guard_language(state, active_target_ids(state) or state.get("candidates") or [])
    from uta.shared.backends import UnknownBackendError, backend_class

    try:
        candidate = backend_class(language, "generation_backend")
        return candidate() if callable(candidate) else candidate
    except UnknownBackendError:
        adapter = default_registry().adapter_for(language)
        factory = getattr(adapter, "test_generation_backend", None)
        if not callable(factory):
            raise RuntimeError(f"{language} does not provide the test-generation graph backend")
        return factory()


def prepare_workspace(state):
    from uta.shared.workspace_rules import validate_workspace_rules

    result = _backend(state).prepare_workspace(state)
    # Branch preparation may switch to a revision with different rule links.
    validate_workspace_rules((result or {}).get("repo_path", state["repo_path"]))
    return result


def baseline_validate(state):
    return _backend(state).baseline_validate(state)


def select_targets(state):
    return _backend(state).select_targets(state)


def prepare_context(state):
    return _backend(state).prepare_context(state)


def select_next_target(state):
    return _backend(state).select_next_target(state)


def run_generation_cycle(state):
    return run_durable_cycle(state)


def deliver_target(state):
    return _backend(state).deliver_target(state)


def finalize(state):
    return _backend(state).finalize(state)

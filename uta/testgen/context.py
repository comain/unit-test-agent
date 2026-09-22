from __future__ import annotations

from pathlib import Path
from typing import Any, List, Mapping, Optional

from uta.shared.backends import BackendConstructionRequest, UnknownBackendError, construct_backend
from uta.shared.context import ContextProvider, ContextQuery

__all__ = ["ContextProvider", "ContextQuery", "make_context_provider"]


def make_context_provider(
    language: str,
    repo_path: str | Path,
    *,
    graph: Optional[Any] = None,
    flows: Optional[List[Any]] = None,
    backend_inputs: Optional[Mapping[str, Any]] = None,
) -> ContextProvider:
    inputs = {"graph": graph, "flows": flows or []}
    inputs.update(dict(backend_inputs or {}))
    try:
        return construct_backend(
            language,
            "context_factory",
            BackendConstructionRequest(Path(repo_path).resolve(), inputs),
        )
    except UnknownBackendError as exc:
        raise ValueError(f"Unsupported context provider language: {language}") from exc

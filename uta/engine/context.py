from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, runtime_checkable

from uta.engine.targets import TargetRef


@dataclass(frozen=True)
class ContextQuery:
    """Language-neutral options for querying target context.

    Language providers may ignore fields they do not support, but callers can
    pass the same query shape for class, file, or function targets.
    """

    module: Optional[str] = None
    test_file_rel: Optional[str] = None
    sections: Sequence[str] = field(default_factory=tuple)
    limit: int = 20
    method_name: Optional[str] = None
    symbol: Optional[str] = None


@runtime_checkable
class ContextProvider(Protocol):
    """Builds project and target context for one backend language.

    Implementations convert parser-specific facts into normalized context
    payloads used by prompts, validation, and reporting.
    """

    language: str
    repo_path: Path

    def export_project_context(self, **kwargs: Any) -> Any:
        ...

    def export_target_context(self, target: TargetRef, **kwargs: Any) -> Mapping[str, str]:
        ...

    def query_target(self, target: TargetRef, query: Optional[ContextQuery] = None) -> Dict[str, Any]:
        ...


def make_context_provider(
    language: str,
    repo_path: str | Path,
    *,
    graph: Optional[Any] = None,
    flows: Optional[List[Any]] = None,
) -> ContextProvider:
    normalized = str(language or "").strip().lower()
    if normalized == "java":
        if graph is None:
            raise ValueError("Java context provider requires a parsed CodeGraph")
        from uta.language.java.context import JavaContextProvider

        return JavaContextProvider(repo_path, graph, flows or [])
    if normalized == "python":
        from uta.language.python.context import PythonContextProvider

        return PythonContextProvider(repo_path)
    raise ValueError(f"Unsupported context provider language: {language}")

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from uta.shared.context import ContextQuery
from uta.shared.backends import BackendConstructionRequest
from uta.language.python.context_builder import PythonContextBuilder
from uta.shared.targets import TargetRef


class PythonContextProvider:
    language = "python"

    def __init__(self, repo_path: str | Path):
        self.repo_path = Path(repo_path).resolve()
        self.builder = PythonContextBuilder(self.repo_path)

    def export_project_context(self, **kwargs: Any) -> Dict[str, Any]:
        return self.builder.export_project_index(**kwargs)

    def export_target_context(
        self,
        target: TargetRef,
        *,
        output_dir: Optional[Path] = None,
        **_: Any,
    ) -> Mapping[str, str]:
        return self.builder.export_target_context(target, output_dir=output_dir)

    def query_target(self, target: TargetRef, query: Optional[ContextQuery] = None) -> Dict[str, Any]:
        return self.builder.query_target(target)


class PythonContextProviderFactory:
    """Construct Python context without requiring a pre-built code graph."""

    required_inputs = frozenset()

    def create(self, request: BackendConstructionRequest) -> PythonContextProvider:
        return PythonContextProvider(request.repo_path)

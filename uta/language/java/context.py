from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from uta.engine.context import ContextQuery
from uta.language.java.context_builder import ContextBuilder
from uta.language.java.parse.models import CodeGraph, ProcessFlow
from uta.engine.targets import TargetRef


class JavaContextProvider:
    language = "java"

    def __init__(self, repo_path: str | Path, graph: CodeGraph, flows: List[ProcessFlow]):
        self.repo_path = Path(repo_path).resolve()
        self.builder = ContextBuilder(str(self.repo_path), graph, flows)

    def export_project_context(self, **kwargs: Any) -> Path:
        return self.builder.export_context_files()

    def export_target_context(
        self,
        target: TargetRef,
        *,
        module: Optional[str] = None,
        test_file_rel: Optional[str] = None,
        **_: Any,
    ) -> Mapping[str, str]:
        return self.builder.export_target_context_files(
            target.target_id,
            module=module,
            test_file_rel=test_file_rel,
        )

    def query_target(self, target: TargetRef, query: Optional[ContextQuery] = None) -> Dict[str, Any]:
        query = query or ContextQuery()
        payload = self.builder.build_index_payload(
            target.target_id,
            module=query.module,
            test_file_rel=query.test_file_rel,
            sections=[section.lower() for section in query.sections],
            limit=query.limit,
            method_name=query.method_name,
            symbol=query.symbol,
        )
        payload["language"] = self.language
        payload["target"] = target.as_selection()
        return payload

    def build_for_class(self, class_fqn: str) -> Dict[str, Any]:
        return self.builder.build_for_class(class_fqn)

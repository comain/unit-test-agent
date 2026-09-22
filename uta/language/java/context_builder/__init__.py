"""Java generation context: the artifacts a turn is pointed at.

``ContextBuilder`` is the stable facade and the only name callers use. It owns
the workspace paths and the public export calls; the work is split by
responsibility across four modules it composes -- graph ``analysis``, Markdown
``rendering``, the ``index_payload`` schema, and the ``roi_store`` cache.

They are composed as base classes rather than collaborators because they all
read the same three attributes (``repo_path``, ``queries``, ``context_dir``)
and were one class until now; composing them keeps the split a pure move, with
no call-site rewriting and no behaviour change.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.language.java.context_builder.analysis import JavaGraphAnalysis
from uta.language.java.context_builder.index_payload import JavaIndexPayload
from uta.language.java.context_builder.rendering import JavaContextRendering
from uta.language.java.context_builder.roi_store import JavaRoiStore
from uta.language.java.parse.models import CodeGraph, ProcessFlow
from uta.language.java.parse.queries import GraphQueries


class ContextBuilder(
    JavaGraphAnalysis,
    JavaContextRendering,
    JavaIndexPayload,
    JavaRoiStore,
):
    def __init__(self, repo_path: str, graph: CodeGraph, flows: List[ProcessFlow]):
        self.repo_path = repo_path
        self.queries = GraphQueries(graph, flows)
        self.context_dir = Path(repo_path) / ".uta_cache" / "context"
        self.context_dir.mkdir(parents=True, exist_ok=True)

    def export_context_files(self) -> Path:
        """Export the full code graph and process flows to human-readable files.

        Creates:
          .uta_cache/context/class_map.md      — all classes with annotations, fields, methods
          .uta_cache/context/call_graph.md      — who calls whom
          .uta_cache/context/process_flows.md   — detected execution flows
          .uta_cache/context/dependency_map.md  — class → dependency signatures
        """
        self._export_class_map()
        self._export_call_graph()
        self._export_process_flows()
        self._export_dependency_map()
        return self.context_dir

    def export_target_context_files(
        self,
        class_fqn: str,
        *,
        module: Optional[str] = None,
        test_file_rel: Optional[str] = None,
    ) -> Dict[str, str]:
        context_path = self.context_dir / f"{class_fqn.split('.')[-1]}.context.md"
        symbols_path = self.context_dir / f"{class_fqn.split('.')[-1]}.symbols.md"
        context_path.write_text(
            self._build_target_context_markdown(
                class_fqn,
                module=module,
                test_file_rel=test_file_rel,
            ),
            encoding="utf-8",
        )
        symbols_path.write_text(
            self._build_target_symbols_markdown(class_fqn),
            encoding="utf-8",
        )
        return {
            "context_abs": str(context_path.resolve()),
            "symbols_abs": str(symbols_path.resolve()),
        }

    def export_generation_pack(
        self,
        class_fqn: str,
        *,
        method_names: Optional[List[str]] = None,
        plan_path: Optional[str] = None,
        max_methods: int = 8,
    ) -> str:
        pack_path = self.context_dir / f"{class_fqn.split('.')[-1]}.generation_pack.md"
        pack_path.write_text(
            self._build_generation_pack_markdown(
                class_fqn,
                method_names=method_names or [],
                plan_path=plan_path,
                max_methods=max_methods,
            ),
            encoding="utf-8",
        )
        return str(pack_path.resolve())

    def get_class_source_path(self, class_fqn: str) -> str:
        """Return the file path for a class FQN."""
        node = self.queries.graph.nodes.get(class_fqn)
        return node.file_path if node else ""

    def build_for_class(self, class_fqn: str) -> Dict[str, Any]:
        node = self.queries.graph.nodes.get(class_fqn)
        if not node:
            return {}

        with open(node.file_path, "r", encoding="utf-8") as f:
            source_code = f.read()

        deps = self.queries.get_class_deps(class_fqn)
        dep_signatures = {}
        for dep in deps:
            dep_signatures[dep] = self.queries.get_method_signatures(dep)

        flows = self.queries.get_flows_for(class_fqn)

        return {
            "class_fqn": class_fqn,
            "source_code": source_code,
            "dependencies": deps,
            "dependency_signatures": dep_signatures,
            "process_flows": flows,
            "callers": {m: self.queries.get_callers(m) for m in self.queries.get_method_signatures(class_fqn)}
        }


__all__ = ["ContextBuilder"]

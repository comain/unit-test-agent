"""The machine-readable index payload behind ``uta-query-index``.

One JSON-shaped mapping describing a class: its shape, its collaborators, the
plan and fix sections, and the artifact paths. It is a published payload
schema rather than prose, so it gets its own module.
"""

from typing import Any, Dict, List, Optional

class JavaIndexPayload:
    """The ``build_index_payload`` capability of ``ContextBuilder``."""

    def build_index_payload(
        self,
        class_fqn: str,
        *,
        module: Optional[str] = None,
        test_file_rel: Optional[str] = None,
        sections: Optional[List[str]] = None,
        limit: int = 20,
        method_name: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a structured, query-friendly class payload for CLI/tool use."""
        class_node = self._class_node(class_fqn)
        selected = set(sections or [])
        include_all = not selected or "summary" in selected

        def wants(name: str) -> bool:
            return include_all or name in selected

        payload: Dict[str, Any] = {
            "found": bool(class_node),
            "class_fqn": class_fqn,
        }
        if not class_node:
            return payload

        if wants("class"):
            payload["class"] = {
                "fqn": class_fqn,
                "source_path": class_node.file_path,
                "line": class_node.line,
                "module": module,
                "test_file_rel": test_file_rel,
                "annotations": list(class_node.metadata.get("annotations", [])),
            }

        if wants("imports"):
            payload["imports"] = self._class_imports(class_fqn)[:limit]

        if wants("fields"):
            payload["fields"] = self._class_fields(class_fqn)[:limit]

        methods = self._public_methods(class_fqn)
        if method_name:
            methods = [m for m in methods if m["name"] == method_name]
        if wants("methods"):
            payload["methods"] = methods[:limit]

        deps = self.queries.get_class_deps(class_fqn)
        if wants("dependencies"):
            payload["dependencies"] = [
                {
                    "fqn": dep,
                    "source_path": self._dep_source_path(dep),
                    "method_count": len(self.queries.get_method_signatures(dep)),
                }
                for dep in deps[:limit]
            ]

        if wants("flows"):
            payload["flows"] = [
                {
                    "name": flow.name,
                    "entry_point": flow.entry_point,
                    "steps": [
                        {
                            "fqn": step.fqn,
                            "kind": step.kind,
                            "detail": step.detail,
                        }
                        for step in flow.steps[:limit]
                    ],
                }
                for flow in self.queries.get_flows_for(class_fqn)[:limit]
            ]

        if wants("nearby_tests"):
            payload["nearby_tests"] = self._nearby_test_files(class_fqn, limit=limit)

        symbol_map = self._resolve_import_candidates(class_fqn)
        if symbol:
            symbol_map = {key: value for key, value in symbol_map.items() if key == symbol}
        if wants("symbols"):
            payload["symbols"] = dict(sorted(list(symbol_map.items())[:limit]))

        if wants("callers"):
            payload["callers"] = {
                method["fqn"]: self.queries.get_callers(method["fqn"])[:limit]
                for method in methods[:limit]
            }

        if wants("plan_summary"):
            payload.setdefault("class", {
                "fqn": class_fqn,
                "source_path": class_node.file_path,
                "line": class_node.line,
                "module": module,
                "test_file_rel": test_file_rel,
                "annotations": list(class_node.metadata.get("annotations", [])),
            })
            payload["plan_summary"] = self._plan_summary(class_fqn, limit=limit)

        if wants("generation_summary"):
            payload.setdefault("class", {
                "fqn": class_fqn,
                "source_path": class_node.file_path,
                "line": class_node.line,
                "module": module,
                "test_file_rel": test_file_rel,
                "annotations": list(class_node.metadata.get("annotations", [])),
            })
            payload["generation_summary"] = self._generation_summary(class_fqn, limit=limit)

        if wants("generation_lookup"):
            payload.setdefault("class", {
                "fqn": class_fqn,
                "source_path": class_node.file_path,
                "line": class_node.line,
                "module": module,
                "test_file_rel": test_file_rel,
                "annotations": list(class_node.metadata.get("annotations", [])),
            })
            payload["generation_lookup"] = self._generation_lookup(
                class_fqn,
                symbol=symbol,
                limit=limit,
            )

        if wants("fix_summary"):
            payload.setdefault("class", {
                "fqn": class_fqn,
                "source_path": class_node.file_path,
                "line": class_node.line,
                "module": module,
                "test_file_rel": test_file_rel,
                "annotations": list(class_node.metadata.get("annotations", [])),
            })
            payload["fix_summary"] = self._fix_summary(
                class_fqn,
                method_name=method_name,
                symbol=symbol,
                limit=limit,
            )

        payload["counts"] = {
            "imports": len(self._class_imports(class_fqn)),
            "fields": len(self._class_fields(class_fqn)),
            "methods": len(self._public_methods(class_fqn)),
            "dependencies": len(deps),
            "flows": len(self.queries.get_flows_for(class_fqn)),
            "nearby_tests": len(self._nearby_test_files(class_fqn, limit=limit)),
            "symbols": len(self._resolve_import_candidates(class_fqn)),
        }
        return payload

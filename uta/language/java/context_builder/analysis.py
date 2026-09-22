"""Reading the parsed Java code graph.

Every method here answers a question about the graph or a source file --
which fields, which public methods, which collaborators, which nearby tests,
what a method's branch cues are. Nothing here writes a file or renders
Markdown, so the derivation rules can be read and tested on their own.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

class JavaGraphAnalysis:
    """Graph-derived facts about a Java class.

    Mixed into ``ContextBuilder``; it uses ``self.queries`` and
    ``self.repo_path`` and owns no state of its own.
    """

    def _class_node(self, class_fqn: str):
        return self.queries.graph.nodes.get(class_fqn)

    def _class_fields(self, class_fqn: str) -> List[Dict[str, Any]]:
        fields: List[Dict[str, Any]] = []
        for fqn, node in self.queries.graph.nodes.items():
            if node.kind != "field" or node.metadata.get("parent_fqn") != class_fqn:
                continue
            fields.append(
                {
                    "name": fqn.split(".")[-1],
                    "type": node.metadata.get("field_type") or "",
                    "annotations": node.metadata.get("annotations", []),
                    "line": node.line,
                }
            )
        return sorted(fields, key=lambda item: item["name"])

    def _class_methods(self, class_fqn: str) -> List[Dict[str, Any]]:
        methods: List[Dict[str, Any]] = []
        for fqn, node in self.queries.graph.nodes.items():
            if node.kind != "method" or node.metadata.get("parent_fqn") != class_fqn:
                continue
            params = node.metadata.get("params", [])
            param_str = ", ".join(f"{p[0]} {p[1]}" for p in params)
            ret = node.metadata.get("return_type") or "void"
            callers = self.queries.get_callers(fqn)
            methods.append(
                {
                    "fqn": fqn,
                    "name": fqn.split(".")[-1],
                    "signature": f"{ret} {fqn.split('.')[-1]}({param_str})",
                    "line": node.line,
                    "caller_count": len(callers),
                    "modifiers": list(node.metadata.get("modifiers", []) or []),
                    "return_type": ret,
                    "params": list(params),
                    "visibility": self._method_visibility(class_fqn, node.line),
                }
            )
        return sorted(methods, key=lambda item: (item["name"], item["line"]))

    def _method_visibility(self, class_fqn: str, line_no: int) -> str:
        source_text = self._source_text(class_fqn)
        if not source_text:
            return "unknown"
        lines = source_text.splitlines()
        if line_no <= 0 or line_no > len(lines):
            return "unknown"
        start = max(0, line_no - 2)
        end = min(len(lines), line_no + 1)
        window = "\n".join(lines[start:end])
        if re.search(r"\bprivate\b", window):
            return "private"
        if re.search(r"\bprotected\b", window):
            return "protected"
        if re.search(r"\bpublic\b", window):
            return "public"
        return "package"

    def _public_methods(self, class_fqn: str) -> List[Dict[str, Any]]:
        class_node = self._class_node(class_fqn)
        class_kind = class_node.kind if class_node else ""
        methods = []
        for method in self._class_methods(class_fqn):
            if method["name"].startswith("<"):
                continue
            visibility = method.get("visibility")
            if visibility in {"private", "protected"}:
                continue
            if visibility == "public" or class_kind == "interface":
                methods.append(method)
        return methods

    def _class_imports(self, class_fqn: str) -> List[str]:
        node = self._class_node(class_fqn)
        if not node:
            return []
        return list(node.metadata.get("imports", []))

    def _nearby_test_files(self, class_fqn: str, limit: int = 8) -> List[str]:
        source_path = self.get_class_source_path(class_fqn)
        if not source_path:
            return []
        source = Path(source_path)
        repo_root = Path(self.repo_path)
        parts = list(source.parts)
        try:
            idx = parts.index("src")
        except ValueError:
            return []
        module_root = Path(*parts[:idx])
        if not module_root.is_absolute():
            module_root = repo_root / module_root
        test_root = module_root / "src" / "test" / "java"
        if not test_root.exists():
            return []
        package_parts = class_fqn.split(".")[:-1]
        simple_name = class_fqn.split(".")[-1]
        same_package_dir = test_root.joinpath(*package_parts)
        candidates: List[Path] = []
        if same_package_dir.exists():
            candidates.extend(sorted(same_package_dir.glob("*Test.java")))
        if len(candidates) < limit:
            for path in sorted(test_root.rglob("*Test.java")):
                if path in candidates:
                    continue
                try:
                    body = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if simple_name.replace("Impl", "") in body or simple_name in body:
                    candidates.append(path)
                if len(candidates) >= limit:
                    break
        return [str(path.resolve()) for path in candidates[:limit]]

    def _dep_source_path(self, dep_fqn: str) -> str:
        node = self.queries.graph.nodes.get(dep_fqn)
        return node.file_path if node else ""

    def _resolve_import_candidates(self, class_fqn: str) -> Dict[str, str]:
        candidates: Dict[str, str] = {}
        for imp in self._class_imports(class_fqn):
            simple = imp.split(".")[-1]
            candidates[simple] = imp
        for field in self._class_fields(class_fqn):
            field_type = field["type"]
            if not field_type:
                continue
            simple = self._simple_type_name(field_type)
            resolved = self._resolve_simple_name(simple)
            if resolved:
                candidates.setdefault(simple, resolved)
        for dep in self.queries.get_class_deps(class_fqn):
            simple = dep.split(".")[-1]
            candidates.setdefault(simple, dep)
        return dict(sorted(candidates.items()))

    def _resolve_simple_name(self, simple_name: str) -> str:
        if not simple_name:
            return ""
        for fqn, node in self.queries.graph.nodes.items():
            if node.kind in ("class", "interface", "enum", "annotation_type") and fqn.endswith(f".{simple_name}"):
                return fqn
        return ""

    def _simple_type_name(self, type_name: str) -> str:
        cleaned = re.sub(r"<.*?>", "", type_name or "").strip()
        cleaned = cleaned.replace("...", "[]")
        if "." in cleaned:
            cleaned = cleaned.split(".")[-1]
        if "[" in cleaned:
            cleaned = cleaned.split("[", 1)[0]
        return cleaned.strip()

    def _source_text(self, class_fqn: str) -> str:
        source_path = self.get_class_source_path(class_fqn)
        if not source_path:
            return ""
        try:
            return Path(source_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _style_ref_summary(self, path: str) -> Dict[str, str]:
        summary = {
            "path": path,
            "runner": "",
            "notes": "",
        }
        try:
            body = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return summary
        if "MockitoJUnitRunner" in body:
            summary["runner"] = "MockitoJUnitRunner"
        elif "PowerMockRunner" in body:
            summary["runner"] = "PowerMockRunner"
        elif "MockitoAnnotations.initMocks" in body or "MockitoAnnotations.openMocks" in body:
            summary["runner"] = "MockitoAnnotations"
        notes: List[str] = []
        if "setAccessible(true)" in body or "ReflectionTestUtils" in body:
            notes.append("reflection")
        if "@InjectMocks" in body:
            notes.append("inject-mocks")
        if "@Mock" in body:
            notes.append("mock-heavy")
        summary["notes"] = ", ".join(notes)
        return summary

    def _method_source_span(self, class_fqn: str, method_name: str, line_no: int, *, max_lines: int = 36) -> str:
        source_text = self._source_text(class_fqn)
        if not source_text:
            return ""
        lines = source_text.splitlines()
        if line_no <= 0 or line_no > len(lines):
            return ""
        methods = sorted(self._class_methods(class_fqn), key=lambda item: item["line"])
        next_line = len(lines) + 1
        for method in methods:
            if method["line"] > line_no:
                next_line = method["line"]
                break
        end_line = min(next_line - 1, line_no + max_lines - 1, len(lines))
        snippet = lines[line_no - 1 : end_line]
        return "\n".join(snippet).rstrip()

    def _method_branch_cues(self, method_text: str) -> List[str]:
        cues: List[str] = []
        checks = [
            (r"==\s*null|!=\s*null|Objects\.isNull|Objects\.nonNull", "null guards"),
            (r"isEmpty\(|isNotEmpty\(|CollectionUtils\.|StringUtils\.", "empty/non-empty guards"),
            (r"status|Status|state|State|finished", "status/state transitions"),
            (r"queryTableSuffix|tableSuffix|路由配置", "route suffix handling"),
            (r"forEach|for\s*\(|while\s*\(|stream\(", "loop or batch iteration"),
            (r"throw\s+new|catch\s*\(", "error/exception path"),
        ]
        for pattern, label in checks:
            if re.search(pattern, method_text):
                cues.append(label)
        return cues[:5]

    def _method_collaborators(self, class_fqn: str, method_text: str, *, limit: int = 8) -> List[str]:
        hits: List[str] = []
        for field in self._class_fields(class_fqn):
            name = field["name"]
            if not name:
                continue
            if re.search(rf"\b{re.escape(name)}\b", method_text):
                field_type = self._simple_type_name(field.get("type") or "")
                label = f"`{name}`" + (f" : `{field_type}`" if field_type else "")
                hits.append(label)
        return hits[:limit]

    def _normalize_planned_method_names(
        self,
        class_fqn: str,
        method_names: List[str],
        *,
        max_methods: int = 8,
    ) -> List[str]:
        available = {method["name"] for method in self._public_methods(class_fqn)}
        chosen: List[str] = []
        seen: Set[str] = set()
        for name in method_names:
            if name in available and name not in seen:
                chosen.append(name)
                seen.add(name)
            if len(chosen) >= max_methods:
                break
        if chosen:
            return chosen
        ranked = sorted(
            self._public_methods(class_fqn),
            key=lambda item: (-int(item.get("caller_count") or 0), item["line"], item["name"]),
        )
        return [method["name"] for method in ranked[:max_methods]]

    def _plan_branch_axes(self, source_text: str) -> List[str]:
        axes: List[str] = []
        checks = [
            (r"==\s*null|!=\s*null|notNull\(", "null vs non-null inputs"),
            (r"isEmpty\(|isNotEmpty\(|StringUtils\.isBlank|CollectionUtils\.", "empty/non-empty collections or blank/non-blank strings"),
            (r"queryTableSuffix|tableSuffix|路由配置", "route suffix present vs missing"),
            (r"status|Status|state|State|pickingStatus|finished", "status/state/finished transitions"),
            (r"page|Page|index|next|pre", "paging/index navigation"),
            (r"forEach|stream\(|try\s*\{|catch\s*\(", "batch iteration and partial-success/error handling"),
        ]
        for pattern, label in checks:
            if re.search(pattern, source_text):
                axes.append(label)
        return axes[:6]

    def _plan_blockers(self, source_text: str) -> List[str]:
        blockers: List[str] = []
        checks = [
            (r"ExecutorService|submit\(", "async executor usage"),
            (r"PagerCollector", "pager collector / paging helper loops"),
            (r"queryTableSuffix|tableSuffix|路由配置", "route suffix lookup required for some paths"),
            (r"static\s+final\s+Logger|Metrics\.", "static metrics/logger side effects"),
            (r"JsonUtil|Convertor|Converter", "static converter / serialization helpers"),
        ]
        for pattern, label in checks:
            if re.search(pattern, source_text):
                blockers.append(label)
        return blockers[:6]

    def _plan_mock_boundaries(self, class_fqn: str, limit: int = 12) -> Dict[str, List[str]]:
        safe_to_mock: List[str] = []
        manual_types: List[str] = []
        for field in self._class_fields(class_fqn):
            field_type = self._simple_type_name(field["type"])
            if not field_type:
                continue
            if re.search(r"(Adapter|Remote|Wrapper|Service|Storage|Biz|Application|Handler)$", field_type):
                if field_type not in safe_to_mock:
                    safe_to_mock.append(field_type)
        for method in self._public_methods(class_fqn):
            return_type = self._simple_type_name(method.get("return_type") or "")
            if return_type and re.search(r"(Query|Request|Resp|Response|Entity|Item|Data|Main|Ao)$", return_type):
                if return_type not in manual_types:
                    manual_types.append(return_type)
            for param_type, _ in method.get("params") or []:
                simple = self._simple_type_name(param_type)
                if simple and re.search(r"(Query|Request|Resp|Response|Entity|Item|Data|Main|Ao)$", simple):
                    if simple not in manual_types:
                        manual_types.append(simple)
        return {
            "safe_to_mock": safe_to_mock[:limit],
            "manual_construction": manual_types[:limit],
        }

    def _plan_summary(self, class_fqn: str, limit: int = 12) -> Dict[str, Any]:
        methods = self._public_methods(class_fqn)
        nearby_tests = self._nearby_test_files(class_fqn, limit=limit)
        source_text = self._source_text(class_fqn)
        return {
            "public_entry_methods": [
                {
                    "name": method["name"],
                    "signature": method["signature"],
                    "line": method["line"],
                    "caller_count": method["caller_count"],
                }
                for method in methods[:limit]
            ],
            "branch_axes": self._plan_branch_axes(source_text),
            "mock_boundaries": self._plan_mock_boundaries(class_fqn, limit=limit),
            "style_refs": [self._style_ref_summary(path) for path in nearby_tests[:3]],
            "blockers": self._plan_blockers(source_text),
        }

    def _generation_summary(self, class_fqn: str, limit: int = 12) -> Dict[str, Any]:
        methods = self._public_methods(class_fqn)
        source_text = self._source_text(class_fqn)
        mock_boundaries = self._plan_mock_boundaries(class_fqn, limit=limit)
        symbol_map = self._resolve_import_candidates(class_fqn)
        high_yield = sorted(
            methods,
            key=lambda item: (-int(item.get("caller_count") or 0), item["line"], item["name"]),
        )[:limit]
        return {
            "high_yield_methods": [
                {
                    "name": method["name"],
                    "signature": method["signature"],
                    "line": method["line"],
                    "caller_count": method["caller_count"],
                }
                for method in high_yield
            ],
            "branch_axes": self._plan_branch_axes(source_text),
            "mock_boundaries": mock_boundaries,
            "style_refs": [self._style_ref_summary(path) for path in self._nearby_test_files(class_fqn, limit=3)],
            "construction_hints": {
                "manual_types": mock_boundaries.get("manual_construction", [])[:limit],
                "safe_to_mock": mock_boundaries.get("safe_to_mock", [])[:limit],
                "defer_external_types": [
                    symbol
                    for symbol in sorted(symbol_map.keys())
                    if re.search(r"(Query|Request|Resp|Response|Entity|Item|Data|Main|Ao)$", symbol)
                ][:limit],
            },
            "source_paths": {
                "class": self.get_class_source_path(class_fqn),
                "nearby_tests": self._nearby_test_files(class_fqn, limit=3),
            },
        }

    def _generation_lookup(
        self,
        class_fqn: str,
        *,
        symbol: Optional[str] = None,
        limit: int = 12,
    ) -> Dict[str, Any]:
        symbol_map = self._resolve_import_candidates(class_fqn)
        hits: List[Dict[str, Any]] = []
        wanted = (symbol or "").strip()

        def _add_hit(*, fqn: str, path: str, kind: str, notes: List[str], source: str) -> None:
            if len(hits) >= limit:
                return
            simple = fqn.split(".")[-1]
            if wanted and simple != wanted:
                return
            entry = {
                "symbol": simple,
                "fqn": fqn,
                "source_path": path,
                "kind": kind,
                "source": source,
                "notes": notes[:6],
            }
            if entry not in hits:
                hits.append(entry)

        for simple, fqn in sorted(symbol_map.items()):
            if wanted and simple != wanted:
                continue
            node = self.queries.graph.nodes.get(fqn)
            if node and node.kind in {"class", "interface", "enum", "record", "annotation_type"}:
                notes: List[str] = []
                if re.search(r"Builder$", simple):
                    notes.append("builder type")
                if re.search(r"(Query|Request|Resp|Response|Entity|Item|Data|Main|Ao)$", simple):
                    notes.append("manual DTO/value type")
                _add_hit(
                    fqn=fqn,
                    path=node.file_path,
                    kind=node.kind,
                    notes=notes,
                    source="symbol_map",
                )

        for dep in self.queries.get_class_deps(class_fqn):
            simple = dep.split(".")[-1]
            if wanted and simple != wanted:
                continue
            node = self.queries.graph.nodes.get(dep)
            if not node:
                continue
            notes: List[str] = []
            if re.search(r"(Adapter|Remote|Wrapper|Service|Storage|Biz|Handler)$", simple):
                notes.append("collaborator seam")
            if re.search(r"(Query|Request|Resp|Response|Entity|Item|Data|Main|Ao)$", simple):
                notes.append("manual DTO/value type")
            _add_hit(
                fqn=dep,
                path=node.file_path,
                kind=node.kind,
                notes=notes,
                source="dependency",
            )

        nearby_usage: List[str] = []
        if wanted:
            for path in self._nearby_test_files(class_fqn, limit=6):
                try:
                    body = Path(path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if re.search(rf"\b{re.escape(wanted)}\b", body):
                    nearby_usage.append(path)
                if len(nearby_usage) >= 3:
                    break

        return {
            "symbol": wanted,
            "hits": hits[:limit],
            "nearby_usage": nearby_usage[:3],
            "class_source_path": self.get_class_source_path(class_fqn),
        }

    def _fix_summary(
        self,
        class_fqn: str,
        *,
        method_name: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 12,
    ) -> Dict[str, Any]:
        methods = self._class_methods(class_fqn)
        if method_name:
            methods = [method for method in methods if method["name"] == method_name]
        symbol_map = self._resolve_import_candidates(class_fqn)
        if symbol:
            symbol_map = {name: fqn for name, fqn in symbol_map.items() if name == symbol}
        deps = self.queries.get_class_deps(class_fqn)
        dep_hits = [
            {
                "fqn": dep,
                "source_path": self._dep_source_path(dep),
            }
            for dep in deps
            if not symbol or dep.endswith(f".{symbol}")
        ]
        return {
            "matching_methods": [
                {
                    "name": method["name"],
                    "signature": method["signature"],
                    "line": method["line"],
                    "visibility": method.get("visibility"),
                }
                for method in methods[:limit]
            ],
            "symbol_hits": dict(sorted(list(symbol_map.items())[:limit])),
            "dependency_hits": dep_hits[:limit],
            "nearby_tests": self._nearby_test_files(class_fqn, limit=3),
            "class_source_path": self.get_class_source_path(class_fqn),
        }

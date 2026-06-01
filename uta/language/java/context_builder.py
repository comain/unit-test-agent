import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Set
from uta.language.java.parse.models import CodeGraph, ProcessFlow
from uta.language.java.parse.queries import GraphQueries


class ContextBuilder:
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

    def _export_class_map(self):
        """Export all classes with their methods, fields, and annotations."""
        lines = ["# Class Map\n"]
        lines.append("Each class lists its annotations, fields (with types), and method signatures.\n")

        # Group methods/fields by parent class
        classes = {}
        for fqn, node in self.queries.graph.nodes.items():
            if node.kind in ("class", "interface", "enum"):
                classes[fqn] = {
                    "file": node.file_path,
                    "line": node.line,
                    "annotations": node.metadata.get("annotations", []),
                    "imports": node.metadata.get("imports", []),
                    "fields": [],
                    "methods": [],
                }

        for fqn, node in self.queries.graph.nodes.items():
            parent = node.metadata.get("parent_fqn")
            if not parent or parent not in classes:
                continue
            if node.kind == "field":
                classes[parent]["fields"].append({
                    "name": fqn.split(".")[-1],
                    "type": node.metadata.get("field_type") or "",
                    "annotations": node.metadata.get("annotations", []),
                })
            elif node.kind == "method":
                params = node.metadata.get("params", [])
                param_str = ", ".join(f"{p[0]} {p[1]}" for p in params)
                ret = node.metadata.get("return_type") or "void"
                classes[parent]["methods"].append({
                    "fqn": fqn,
                    "name": fqn.split(".")[-1],
                    "signature": f"{ret} {fqn.split('.')[-1]}({param_str})",
                    "annotations": node.metadata.get("annotations", []),
                })

        for class_fqn in sorted(classes.keys()):
            info = classes[class_fqn]
            lines.append(f"## {class_fqn}")
            lines.append(f"File: `{info['file']}` (line {info['line']})")
            if info["annotations"]:
                lines.append(f"Annotations: {', '.join('@' + a for a in info['annotations'])}")
            if info["imports"]:
                lines.append(f"Imports: {', '.join(f'`{imp}`' for imp in info['imports'][:25])}")

            if info["fields"]:
                lines.append("\n**Fields:**")
                for f in info["fields"]:
                    ann = f" ({', '.join('@' + a for a in f['annotations'])})" if f["annotations"] else ""
                    field_type = f" : `{f['type']}`" if f["type"] else ""
                    lines.append(f"- `{f['name']}`{field_type}{ann}")

            if info["methods"]:
                lines.append("\n**Methods:**")
                for m in info["methods"]:
                    ann = f" ({', '.join('@' + a for a in m['annotations'])})" if m["annotations"] else ""
                    lines.append(f"- `{m['signature']}`{ann}")

            lines.append("")

        (self.context_dir / "class_map.md").write_text("\n".join(lines), encoding="utf-8")

    def _export_call_graph(self):
        """Export call edges as a readable file."""
        lines = ["# Call Graph\n"]
        lines.append("Format: `caller` → `callee`\n")

        calls = [e for e in self.queries.graph.edges if e.relation == "CALLS"]
        calls.sort(key=lambda e: e.source)
        for edge in calls:
            lines.append(f"- `{edge.source}` → `{edge.target}`")

        (self.context_dir / "call_graph.md").write_text("\n".join(lines), encoding="utf-8")

    def _export_process_flows(self):
        """Export process flows as a readable file."""
        lines = ["# Process Flows\n"]
        lines.append("Execution flows traced from entry points through the call graph.\n")

        for flow in self.queries.flows:
            lines.append(f"## {flow.name}")
            lines.append(f"Entry: `{flow.entry_point}`")
            for i, step in enumerate(flow.steps, 1):
                lines.append(f"  {i}. [{step.kind}] `{step.fqn}`")
            lines.append("")

        (self.context_dir / "process_flows.md").write_text("\n".join(lines), encoding="utf-8")

    def _export_dependency_map(self):
        """Export per-class dependency info: what each class depends on and their method signatures."""
        lines = ["# Dependency Map\n"]
        lines.append("For each class, lists its dependencies and their available method signatures.\n")

        classes = [fqn for fqn, n in self.queries.graph.nodes.items() if n.kind == "class"]
        for class_fqn in sorted(classes):
            deps = self.queries.get_class_deps(class_fqn)
            if not deps:
                continue

            lines.append(f"## {class_fqn}")
            for dep in deps:
                sigs = self.queries.get_method_signatures(dep)
                lines.append(f"\n### → {dep}")
                if sigs:
                    for method_fqn, sig in sigs.items():
                        lines.append(f"- `{sig}`")
                else:
                    lines.append("- (no methods resolved)")
            lines.append("")

        (self.context_dir / "dependency_map.md").write_text("\n".join(lines), encoding="utf-8")

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

    def _build_generation_pack_markdown(
        self,
        class_fqn: str,
        *,
        method_names: List[str],
        plan_path: Optional[str] = None,
        max_methods: int = 8,
    ) -> str:
        methods_by_name = {method["name"]: method for method in self._public_methods(class_fqn)}
        selected_names = self._normalize_planned_method_names(
            class_fqn,
            method_names,
            max_methods=max_methods,
        )
        generation_summary = self._generation_summary(class_fqn, limit=max_methods)
        lines = [
            "# Generation Method Pack",
            "",
            f"- Class: `{class_fqn}`",
            f"- Source: `{self.get_class_source_path(class_fqn)}`",
        ]
        if plan_path:
            lines.append(f"- Plan file: `{plan_path}`")
        lines.extend(
            [
                "",
                "## Selected First-Pass Methods",
                *(f"- `{name}`" for name in selected_names),
                "",
                "## Construction Hints",
            ]
        )
        construction_hints = generation_summary.get("construction_hints", {})
        safe_to_mock = construction_hints.get("safe_to_mock") or []
        manual_types = construction_hints.get("manual_types") or []
        defer_external = construction_hints.get("defer_external_types") or []
        lines.append(
            "- Safe to mock: "
            + (", ".join(f"`{name}`" for name in safe_to_mock[:8]) if safe_to_mock else "(none)")
        )
        lines.append(
            "- Manual data types: "
            + (", ".join(f"`{name}`" for name in manual_types[:8]) if manual_types else "(none)")
        )
        lines.append(
            "- Defer exact external APIs until compile-fix: "
            + (", ".join(f"`{name}`" for name in defer_external[:8]) if defer_external else "(none)")
        )
        style_refs = generation_summary.get("style_refs") or []
        lines.extend(["", "## Nearby Style References"])
        if style_refs:
            for ref in style_refs[:3]:
                notes = f" ({ref.get('notes')})" if ref.get("notes") else ""
                runner = f" [{ref.get('runner')}]" if ref.get("runner") else ""
                lines.append(f"- `{ref.get('path', '')}`{runner}{notes}")
        else:
            lines.append("- (none)")
        lines.extend(["", "## Method Windows"])
        for name in selected_names:
            method = methods_by_name.get(name)
            if not method:
                continue
            snippet = self._method_source_span(class_fqn, name, int(method["line"]))
            branch_cues = self._method_branch_cues(snippet)
            collaborators = self._method_collaborators(class_fqn, snippet)
            lines.extend(
                [
                    f"### `{method['signature']}`",
                    f"- Line: `{method['line']}`",
                    f"- Caller count: `{method['caller_count']}`",
                    "- Branch cues: "
                    + (", ".join(branch_cues) if branch_cues else "(none obvious)"),
                    "- Collaborators referenced: "
                    + (", ".join(collaborators) if collaborators else "(none obvious)"),
                    "```java",
                    snippet or "// source snippet unavailable",
                    "```",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

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

    def _build_target_context_markdown(
        self,
        class_fqn: str,
        *,
        module: Optional[str] = None,
        test_file_rel: Optional[str] = None,
    ) -> str:
        class_node = self._class_node(class_fqn)
        if not class_node:
            return f"# Target Context\n\nMissing class: `{class_fqn}`\n"

        imports = self._class_imports(class_fqn)
        fields = self._class_fields(class_fqn)
        methods = self._public_methods(class_fqn)
        deps = self.queries.get_class_deps(class_fqn)
        flows = self.queries.get_flows_for(class_fqn)
        nearby_tests = self._nearby_test_files(class_fqn)
        lines = [
            "# Target Test Context",
            "",
            f"## Class",
            f"- FQN: `{class_fqn}`",
            f"- Source: `{class_node.file_path}`",
        ]
        if module:
            lines.append(f"- Module: `{module}`")
        if test_file_rel:
            lines.append(f"- Expected test path: `{test_file_rel}`")
        lines.extend(["", "## Imports"])
        if imports:
            lines.extend(f"- `{imp}`" for imp in imports[:60])
        else:
            lines.append("- (no imports captured)")
        lines.extend(["", "## Fields"])
        if fields:
            for field in fields:
                ann = f" ({', '.join('@' + a for a in field['annotations'])})" if field["annotations"] else ""
                field_type = field["type"] or "(unknown)"
                lines.append(f"- `{field['name']}` : `{field_type}`{ann}")
        else:
            lines.append("- (no fields)")
        lines.extend(["", "## Public Methods"])
        if methods:
            for method in methods:
                lines.append(f"- `{method['signature']}`")
        else:
            lines.append("- (no public methods)")
        lines.extend(["", "## Dependency Types"])
        if deps:
            for dep in deps[:40]:
                dep_path = self._dep_source_path(dep)
                suffix = f" — `{dep_path}`" if dep_path else ""
                lines.append(f"- `{dep}`{suffix}")
        else:
            lines.append("- (no resolved dependencies)")
        lines.extend(["", "## Relevant Process Flows"])
        if flows:
            for flow in flows[:12]:
                steps = ", ".join(step.fqn.split(".")[-1] for step in flow.steps[:6])
                lines.append(f"- `{flow.name}` → {steps}")
        else:
            lines.append("- (no flow extracted)")
        lines.extend(["", "## Nearby Test References"])
        if nearby_tests:
            lines.extend(f"- `{path}`" for path in nearby_tests)
        else:
            lines.append("- (no nearby test references found)")
        return "\n".join(lines) + "\n"

    def _build_target_symbols_markdown(self, class_fqn: str) -> str:
        imports = self._class_imports(class_fqn)
        symbol_map = self._resolve_import_candidates(class_fqn)
        fields = self._class_fields(class_fqn)
        deps = self.queries.get_class_deps(class_fqn)
        lines = [
            "# Target Symbol / Import Map",
            "",
            f"## Class",
            f"- `{class_fqn}`",
            "",
            "## Imported Symbols",
        ]
        if imports:
            for imp in imports[:80]:
                lines.append(f"- `{imp.split('.')[-1]}` -> `{imp}`")
        else:
            lines.append("- (no imports captured)")
        lines.extend(["", "## Field Types"])
        if fields:
            for field in fields:
                field_type = field["type"] or "(unknown)"
                resolved = symbol_map.get(self._simple_type_name(field_type), "")
                suffix = f" -> `{resolved}`" if resolved else ""
                lines.append(f"- `{field['name']}` : `{field_type}`{suffix}")
        else:
            lines.append("- (no fields)")
        lines.extend(["", "## Dependency Source Paths"])
        if deps:
            for dep in deps[:50]:
                dep_path = self._dep_source_path(dep)
                suffix = f"`{dep_path}`" if dep_path else "(path unavailable)"
                lines.append(f"- `{dep}` -> {suffix}")
        else:
            lines.append("- (no resolved dependencies)")
        return "\n".join(lines) + "\n"

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

    def export_roi_scores(
        self,
        class_fqn: str,
        roi_data: Dict[str, Any],
        *,
        source_path: str = "",
        jacoco_xml_path: Optional[str] = None,
        debug: bool = False,
    ) -> str:
        """Write ROI scores to a cached markdown file.

        Returns the absolute path to the file. Reuses cache if source/jacoco
        mtimes haven't changed.
        """
        from uta.language.java.scoring.coverage_roi import (
            compute_roi_cache_key,
            format_roi_markdown,
            is_degenerate_roi_markdown,
        )

        simple_name = class_fqn.split(".")[-1]
        roi_path = self.context_dir / f"{simple_name}.roi.md"

        # Check cache
        src = source_path or self.get_class_source_path(class_fqn)
        cache_key = compute_roi_cache_key(src, jacoco_xml_path, debug=debug)
        cache_marker = f"<!-- cache:{cache_key} -->"

        if roi_path.exists():
            existing = roi_path.read_text(encoding="utf-8")
            first_lines = existing[:200]
            if cache_marker in first_lines and not is_degenerate_roi_markdown(existing):
                return str(roi_path.resolve())

        content = cache_marker + "\n" + format_roi_markdown(roi_data, debug=debug)
        roi_path.write_text(content, encoding="utf-8")
        return str(roi_path.resolve())

    def clear_roi_scores(self, class_fqn: str) -> None:
        """Remove the cached ROI artifact for a class if it exists."""
        simple_name = class_fqn.split(".")[-1]
        roi_path = self.context_dir / f"{simple_name}.roi.md"
        if roi_path.exists():
            roi_path.unlink()

    def get_class_source_path(self, class_fqn: str) -> str:
        """Return the file path for a class FQN."""
        node = self.queries.graph.nodes.get(class_fqn)
        return node.file_path if node else ""

    # Legacy: still used by prompt rendering tests
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

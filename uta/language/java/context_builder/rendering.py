"""Rendering graph facts as the Markdown artifacts a turn reads.

These are the exact documents the model is pointed at -- the class map, the
call graph, the per-target context and symbol files, the generation pack.
Their wording and section order are the product contract, which is why they
are kept apart from the analysis that supplies their content.
"""

from typing import List, Optional

class JavaContextRendering:
    """Markdown projections of the Java code graph.

    Mixed into ``ContextBuilder``; reads graph facts through the analysis
    methods and writes into ``self.context_dir``.
    """

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
            "## Class",
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
            "## Class",
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

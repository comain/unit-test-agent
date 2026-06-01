from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from uta.engine.parse import ParsedCallable, ParsedImport, ParseDiagnostic, ParseProjectRequest
from uta.engine.targets import TargetIdentity
from uta.language.java.parse.cache import CacheManager
from uta.language.java.parse.graph_builder import GraphBuilder
from uta.language.java.parse.java_parser import JavaParser
from uta.language.java.parse.models import CodeGraph, ParseResult, ProcessFlow
from uta.language.java.parse.process_extractor import ProcessExtractor


_ENTRY_POINT_ANNOTATIONS = {"Controller", "DubboService", "Service", "Component", "RestController"}


@dataclass
class JavaParseProjectResult:
    """Parsed Java project with normalized facts and typed Java semantic graph."""

    repo_path: Path
    module: Optional[str]
    parsed_files: List[ParseResult]
    graph: CodeGraph
    flows: List[ProcessFlow]
    source_files: Sequence[str]
    callables: Sequence[ParsedCallable]
    imports: Sequence[ParsedImport] = field(default_factory=tuple)
    diagnostics: Sequence[ParseDiagnostic] = field(default_factory=tuple)
    language: str = "java"

    @property
    def path_to_target_id(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for fqn, node in self.graph.nodes.items():
            if node.kind != "class":
                continue
            mapping[node.file_path] = fqn
            try:
                mapping[Path(node.file_path).relative_to(self.repo_path).as_posix()] = fqn
            except ValueError:
                pass
        return mapping

    def contains_target(self, target_id: str) -> bool:
        return target_id in self.graph.nodes

    def target_id_for_source_path(self, source_path: str) -> Optional[str]:
        return self.path_to_target_id.get(source_path) or self.path_to_target_id.get(str(self.repo_path / source_path))

    def is_testable_target(self, target_id: str) -> bool:
        return _is_testable_java_class(target_id, self.graph)

    def target_selections(self, target_ids: Sequence[str]) -> List[Mapping[str, Any]]:
        return [TargetIdentity.java_class(target_id).as_selection() for target_id in target_ids]


class JavaParseProvider:
    language = "java"

    def parse_project(self, request: ParseProjectRequest) -> JavaParseProjectResult:
        repo_path = Path(request.repo_path).resolve()
        source_files = list(request.source_files or _java_source_files(repo_path, request.module))
        parsed_files = _parse_java_files(repo_path, source_files)
        graph = GraphBuilder().build(parsed_files)
        flows = _extract_process_flows(graph)
        return JavaParseProjectResult(
            repo_path=repo_path,
            module=request.module,
            parsed_files=parsed_files,
            graph=graph,
            flows=flows,
            source_files=tuple(_relative_or_absolute(repo_path, path) for path in source_files),
            callables=tuple(_java_callables(graph)),
            imports=tuple(_java_imports(repo_path, parsed_files)),
        )


def _java_source_files(repo_path: Path, module: Optional[str]) -> List[Path]:
    module_dir = repo_path / module if module else repo_path
    return sorted(module_dir.glob("**/src/main/java/**/*.java"))


def _parse_java_files(repo_path: Path, source_files: Sequence[Path]) -> List[ParseResult]:
    cache_manager = CacheManager(str(repo_path / ".uta_cache"))
    parser = JavaParser()
    parsed_files: List[ParseResult] = []
    for source_file in source_files:
        source_abs = Path(source_file).resolve()
        parsed = cache_manager.get_parsed(str(source_abs))
        if not parsed:
            parsed = parser.parse_file(str(source_abs))
            cache_manager.save_parsed(str(source_abs), parsed)
        parsed_files.append(parsed)
    return parsed_files


def _extract_process_flows(graph: CodeGraph) -> List[ProcessFlow]:
    entry_class_fqns = {
        fqn
        for fqn, node in graph.nodes.items()
        if node.kind == "class" and any(annotation in _ENTRY_POINT_ANNOTATIONS for annotation in node.metadata.get("annotations", []))
    }
    entry_points = [
        fqn
        for fqn, node in graph.nodes.items()
        if node.kind == "method" and node.metadata.get("parent_fqn") in entry_class_fqns
    ]
    return ProcessExtractor(graph).extract_flows(entry_points[:100])


def _java_callables(graph: CodeGraph) -> List[ParsedCallable]:
    callables: List[ParsedCallable] = []
    for fqn, node in graph.nodes.items():
        if node.kind not in {"class", "interface", "enum", "method"}:
            continue
        metadata = node.metadata or {}
        callables.append(
            ParsedCallable(
                name=fqn.rsplit(".", 1)[-1],
                qualified_name=fqn,
                kind=node.kind,
                source_path=node.file_path,
                line=int(node.line or 0),
                parent=metadata.get("parent_fqn"),
                visibility=_visibility_from_modifiers(metadata.get("modifiers") or []),
            )
        )
    return callables


def _java_imports(repo_path: Path, parsed_files: Sequence[ParseResult]) -> List[ParsedImport]:
    imports: List[ParsedImport] = []
    for parsed in parsed_files:
        source_path = _relative_or_absolute(repo_path, Path(parsed.path))
        imports.extend(
            ParsedImport(source_path=source_path, module=item)
            for item in parsed.imports
        )
    return imports


def _visibility_from_modifiers(modifiers: Sequence[str]) -> Optional[str]:
    for modifier in ("public", "protected", "private"):
        if modifier in modifiers:
            return modifier
    return None


def _relative_or_absolute(repo_path: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_path).as_posix()
    except ValueError:
        return str(path)


_ACCESSOR_METHOD_NAMES = {"equals", "hashCode", "toString", "canEqual"}


def _method_simple_name(method_node) -> str:
    return method_node.fqn.rsplit(".", 1)[-1]


def _is_accessor_like_method_name(name: str) -> bool:
    return (
        name in _ACCESSOR_METHOD_NAMES
        or (name.startswith("get") and len(name) > 3 and name[3].isupper())
        or (name.startswith("set") and len(name) > 3 and name[3].isupper())
        or (name.startswith("is") and len(name) > 2 and name[2].isupper())
    )


def _is_accessor_like_method(method_node) -> bool:
    name = _method_simple_name(method_node)
    if not _is_accessor_like_method_name(name):
        return False
    complexity = method_node.metadata.get("complexity") or {}
    cyclomatic = int(complexity.get("cyclomatic_approx", 1) or 1)
    non_comment_lines = int(complexity.get("non_comment_lines", 0) or 0)
    return cyclomatic <= 1 and non_comment_lines <= 8


def _is_testable_java_class(fqn: str, graph: CodeGraph) -> bool:
    node = graph.nodes.get(fqn)
    if not node or node.kind not in {"class", "enum"}:
        return False
    simple_name = fqn.rsplit(".", 1)[-1]
    if simple_name.endswith(("Test", "Tests", "IT", "DTO", "VO", "PO", "Entity", "Enum")):
        return False
    methods = [
        item
        for item_fqn, item in graph.nodes.items()
        if item.kind == "method" and item.metadata.get("parent_fqn") == fqn
    ]
    public_methods = [item for item in methods if "public" in (item.metadata.get("modifiers") or [])]
    if not public_methods:
        return False
    non_accessor = [item for item in public_methods if not _is_accessor_like_method(item)]
    return bool(non_accessor)

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple

from uta.language.python.parse.models import PythonImport, PythonParseResult, PythonSymbol, SideEffectHint


_PY2_PATTERNS = (
    re.compile(r"^\s*except\s+[\w.]+\s*,\s*\w+\s*:", re.MULTILINE),
    re.compile(r"^\s*print\s+(['\"].*|[A-Za-z_][\w.]*)", re.MULTILINE),
    re.compile(r"\bxrange\s*\("),
)

_DEF_RE = re.compile(r"^(?P<indent>\s*)def\s+(?P<name>[A-Za-z_]\w*)\((?P<params>[^)]*)\)\s*:", re.MULTILINE)
_CLASS_RE = re.compile(r"^(?P<indent>\s*)class\s+(?P<name>[A-Za-z_]\w*)\b", re.MULTILINE)
_IMPORT_RE = re.compile(r"^\s*(?:import\s+(?P<import>[\w.,\s]+)|from\s+(?P<module>[\w.]+)\s+import\s+(?P<names>[\w.,\s*]+))", re.MULTILINE)


def _tree_sitter_parser() -> Optional[Tuple[Any, Any]]:
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_python  # noqa: F401

        language = Language(tree_sitter_python.language())
        return Parser(language), language
    except Exception:
        return None


class PythonParser:
    """Source-only Python parser. It never imports target modules."""

    def __init__(self) -> None:
        self._tree_sitter = _tree_sitter_parser()

    def parse_file(self, file_path: Path, *, repo_path: Optional[Path] = None) -> PythonParseResult:
        path = Path(file_path)
        repo = Path(repo_path) if repo_path else path.parent
        source = path.read_text(encoding="utf-8")
        relative_path = _relative_path(path, repo)
        if self._tree_sitter:
            parser, _ = self._tree_sitter
            tree = parser.parse(source.encode("utf-8"))
            syntax_version, syntax_error = _syntax_info(source)
            return PythonParseResult(
                path=str(path),
                relative_path=relative_path,
                syntax_version=syntax_version,
                parser_backend="tree_sitter",
                imports=_tree_sitter_imports(tree.root_node, source),
                symbols=_tree_sitter_symbols(tree.root_node, source),
                side_effect_hints=_side_effect_hints(source),
                syntax_error=syntax_error,
            )
        try:
            module = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            return PythonParseResult(
                path=str(path),
                relative_path=relative_path,
                syntax_version="python2" if _looks_like_python2(source) else "unknown",
                parser_backend="regex",
                imports=_regex_imports(source),
                symbols=_regex_symbols(source),
                side_effect_hints=_side_effect_hints(source),
                syntax_error={"message": exc.msg, "line": exc.lineno, "offset": exc.offset},
            )

        return PythonParseResult(
            path=str(path),
            relative_path=relative_path,
            syntax_version="python3",
            parser_backend="ast",
            imports=_ast_imports(module),
            symbols=_ast_symbols(module),
            side_effect_hints=_side_effect_hints(source),
        )


def _syntax_info(source: str) -> Tuple[str, Optional[dict]]:
    try:
        ast.parse(source)
        return "python3", None
    except SyntaxError as exc:
        return (
            "python2" if _looks_like_python2(source) else "unknown",
            {"message": exc.msg, "line": exc.lineno, "offset": exc.offset},
        )


def _relative_path(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.name


def _looks_like_python2(source: str) -> bool:
    return any(pattern.search(source) for pattern in _PY2_PATTERNS)


def _node_text(node, source: str) -> str:
    content = source.encode("utf-8")
    return content[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _walk(node):
    yield node
    for child in getattr(node, "children", []) or []:
        yield from _walk(child)


def _tree_sitter_symbols(root, source: str) -> List[PythonSymbol]:
    symbols: List[PythonSymbol] = []
    for node in getattr(root, "children", []) or []:
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            class_name = _node_text(name_node, source)
            symbols.append(
                PythonSymbol(
                    kind="class",
                    name=class_name,
                    qualified_name=class_name,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                )
            )
            body = node.child_by_field_name("body")
            for child in getattr(body, "children", []) if body else []:
                if child.type == "function_definition":
                    fn = _tree_sitter_function_symbol(child, source, parent=class_name)
                    if fn:
                        symbols.append(fn)
        elif node.type == "function_definition":
            fn = _tree_sitter_function_symbol(node, source, parent=None)
            if fn:
                symbols.append(fn)
    return symbols


def _tree_sitter_function_symbol(node, source: str, *, parent: Optional[str]) -> Optional[PythonSymbol]:
    name_node = node.child_by_field_name("name")
    if not name_node:
        return None
    name = _node_text(name_node, source)
    params_node = node.child_by_field_name("parameters")
    params = _parameter_names(_node_text(params_node, source)) if params_node else []
    return PythonSymbol(
        kind="method" if parent else "function",
        name=name,
        qualified_name=f"{parent}.{name}" if parent else name,
        line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        parent=parent,
        params=params,
    )


def _parameter_names(raw: str) -> List[str]:
    raw = raw.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    names = []
    for item in raw.split(","):
        name = item.strip().split("=", 1)[0].split(":", 1)[0].strip()
        if name and name not in {"*", "/"}:
            names.append(name)
    return names


def _tree_sitter_imports(root, source: str) -> List[PythonImport]:
    imports: List[PythonImport] = []
    for node in _walk(root):
        if node.type in {"import_statement", "import_from_statement"}:
            line = node.start_point[0] + 1
            text = _node_text(node, source).strip()
            match = _IMPORT_RE.match(text)
            if not match:
                continue
            if match.group("import"):
                imports.append(PythonImport(module="", names=_split_names(match.group("import")), line=line))
            else:
                imports.append(PythonImport(module=match.group("module") or "", names=_split_names(match.group("names") or ""), line=line))
    return imports


def _name_from_ast(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _decorators(node) -> List[str]:
    return [_name_from_ast(item) for item in getattr(node, "decorator_list", []) if _name_from_ast(item)]


def _params(args: ast.arguments) -> List[str]:
    names = []
    for arg in getattr(args, "posonlyargs", []):
        names.append(arg.arg)
    for arg in args.args:
        names.append(arg.arg)
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    for arg in args.kwonlyargs:
        names.append(arg.arg)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return names


def _ast_symbols(module: ast.Module) -> List[PythonSymbol]:
    symbols: List[PythonSymbol] = []
    for node in module.body:
        if isinstance(node, ast.ClassDef):
            class_name = node.name
            symbols.append(
                PythonSymbol(
                    kind="class",
                    name=node.name,
                    qualified_name=class_name,
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    decorators=_decorators(node),
                    docstring=ast.get_docstring(node),
                )
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_function_symbol(child, parent=class_name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_function_symbol(node, parent=None))
    return symbols


def _function_symbol(node, *, parent: Optional[str]) -> PythonSymbol:
    qualified_name = f"{parent}.{node.name}" if parent else node.name
    return PythonSymbol(
        kind="function" if parent is None else "method",
        name=node.name,
        qualified_name=qualified_name,
        line=node.lineno,
        end_line=getattr(node, "end_lineno", node.lineno),
        parent=parent,
        params=_params(node.args),
        decorators=_decorators(node),
        returns=_name_from_ast(node.returns) if getattr(node, "returns", None) else None,
        docstring=ast.get_docstring(node),
    )


def _ast_imports(module: ast.Module) -> List[PythonImport]:
    imports: List[PythonImport] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imports.append(PythonImport(module="", names=[alias.name for alias in node.names], line=node.lineno))
        elif isinstance(node, ast.ImportFrom):
            imports.append(
                PythonImport(
                    module="." * int(node.level or 0) + (node.module or ""),
                    names=[alias.name for alias in node.names],
                    line=node.lineno,
                )
            )
    return imports


def _regex_symbols(source: str) -> List[PythonSymbol]:
    class_lines = {}
    for match in _CLASS_RE.finditer(source):
        class_lines[match.start()] = (match.group("name"), len(match.group("indent")), source[: match.start()].count("\n") + 1)
    classes_by_line = sorted(class_lines.items(), key=lambda item: item[0])
    symbols = [
        PythonSymbol(kind="class", name=name, qualified_name=name, line=line, end_line=line)
        for _, (name, _, line) in classes_by_line
    ]
    for match in _DEF_RE.finditer(source):
        line = source[: match.start()].count("\n") + 1
        indent = len(match.group("indent"))
        parent = _nearest_parent_class(classes_by_line, match.start(), indent)
        name = match.group("name")
        params = [item.strip().split("=")[0].strip() for item in match.group("params").split(",") if item.strip()]
        symbols.append(
            PythonSymbol(
                kind="method" if parent else "function",
                name=name,
                qualified_name=f"{parent}.{name}" if parent else name,
                line=line,
                end_line=line,
                parent=parent,
                params=params,
            )
        )
    return sorted(symbols, key=lambda symbol: (symbol.line, symbol.qualified_name))


def _nearest_parent_class(classes_by_offset, offset: int, indent: int) -> Optional[str]:
    parent = None
    for class_offset, (name, class_indent, _) in classes_by_offset:
        if class_offset < offset and indent > class_indent:
            parent = name
    return parent


def _regex_imports(source: str) -> List[PythonImport]:
    imports: List[PythonImport] = []
    for match in _IMPORT_RE.finditer(source):
        line = source[: match.start()].count("\n") + 1
        if match.group("import"):
            imports.append(PythonImport(module="", names=_split_names(match.group("import")), line=line))
        else:
            imports.append(PythonImport(module=match.group("module") or "", names=_split_names(match.group("names") or ""), line=line))
    return imports


def _split_names(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _side_effect_hints(source: str) -> List[SideEffectHint]:
    patterns = [
        ("filesystem", re.compile(r"\bopen\s*\(|\bPath\s*\(|\bshutil\.|\bos\.remove\b|\bos\.rename\b")),
        ("subprocess", re.compile(r"\bsubprocess\.|\bos\.system\s*\(")),
        ("network", re.compile(r"\brequests\.|\burllib\.|\bsocket\.|\bhttpx\.")),
        ("data_platform", re.compile(r"\bhive\b|\bhdfs\b|\bspark\b|\bpyspark\b", re.IGNORECASE)),
        ("model_runtime", re.compile(r"\btorch\b|\btensorflow\b|\bsklearn\b")),
    ]
    hints: List[SideEffectHint] = []
    lines = source.splitlines()
    for line_no, line in enumerate(lines, start=1):
        for kind, pattern in patterns:
            if pattern.search(line):
                hints.append(SideEffectHint(kind=kind, detail=line.strip()[:160], line=line_no))
    return hints

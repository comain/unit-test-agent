import tree_sitter_java
from tree_sitter import Language, Parser, Node
from typing import Any, Dict, List, Optional
from uta.language.java.parse.models import (
    ParsedSymbol, ExtractedCall, ExtractedHeritage,
    Annotation, Param, ParseResult
)

JAVA_LANGUAGE = Language(tree_sitter_java.language())

# Simplified queries compatible with local tree-sitter-java
JAVA_QUERIES = """
; Classes, interfaces, enums, annotations
(class_declaration name: (identifier) @name) @definition.class
(interface_declaration name: (identifier) @name) @definition.interface
(enum_declaration name: (identifier) @name) @definition.enum
(annotation_type_declaration name: (identifier) @name) @definition.annotation

; Methods with full signature
(method_declaration
  type: (_) @return_type
  name: (identifier) @name
  parameters: (formal_parameters) @params) @definition.method

(constructor_declaration
  name: (identifier) @name
  parameters: (formal_parameters) @params) @definition.constructor

; Fields
(field_declaration
  type: (_) @type
  declarator: (variable_declarator name: (identifier) @name)) @definition.field

; Imports
(import_declaration (scoped_identifier) @import.source) @import

; Calls
(method_invocation
  object: (_)? @call.receiver
  name: (identifier) @call.name) @call

(method_reference) @call.reference

; Constructor calls
(object_creation_expression
  type: (_) @call.name) @call.constructor

; Heritage
(class_declaration
  name: (identifier) @heritage.class
  (superclass (type_identifier) @heritage.extends)) @heritage

(class_declaration
  name: (identifier) @heritage.class
  (super_interfaces (type_list (type_identifier) @heritage.implements))) @heritage.impl
"""


def _text(content: bytes, node: Node) -> str:
    """Extract text from a tree-sitter node. content must be bytes."""
    return content[node.start_byte:node.end_byte].decode('utf-8', errors='replace')


def _captures_by_name(query, root_node: Node) -> Dict[str, List[Node]]:
    """Return query captures across tree-sitter Python API variants."""
    if hasattr(query, "captures"):
        captures = query.captures(root_node)
    else:
        from tree_sitter import QueryCursor

        try:
            captures = QueryCursor(query).captures(root_node)
        except TypeError:
            cursor = QueryCursor()
            captures = cursor.captures(query, root_node)

    if isinstance(captures, dict):
        return captures

    grouped: Dict[str, List[Node]] = {}
    for item in captures:
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        first, second = item
        if isinstance(first, str):
            name, node = first, second
        else:
            node, name = first, second
        grouped.setdefault(str(name), []).append(node)
    return grouped


class JavaParser:
    def __init__(self):
        self.parser = Parser(JAVA_LANGUAGE)
        self.query = JAVA_LANGUAGE.query(JAVA_QUERIES)

    def parse_file(self, file_path: str) -> ParseResult:
        with open(file_path, "rb") as f:
            content = f.read()

        tree = self.parser.parse(content)
        root_node = tree.root_node

        package = self._extract_package(root_node, content)

        symbols = []
        calls = []
        heritages = []
        field_bindings = {}
        imports = []

        self._extract_all_imports(root_node, content, imports)
        self._traverse(root_node, content, package, symbols, calls, heritages, field_bindings)

        return ParseResult(
            path=file_path,
            package=package,
            imports=imports,
            symbols=symbols,
            calls=calls,
            heritage=heritages,
            field_bindings=field_bindings
        )

    def _extract_package(self, root_node: Node, content: bytes) -> str:
        query = JAVA_LANGUAGE.query("(package_declaration (scoped_identifier) @package)")
        captures = _captures_by_name(query, root_node)
        if "package" in captures:
            node = captures["package"][0]
            return _text(content, node)
        return ""

    def _extract_all_imports(self, root_node: Node, content: bytes, imports: List[str]):
        query = JAVA_LANGUAGE.query("(import_declaration) @import")
        captures = _captures_by_name(query, root_node)
        if "import" in captures:
            for node in captures["import"]:
                source = ""
                is_wildcard = False
                for child in node.children:
                    if child.type == "scoped_identifier":
                        source = _text(content, child)
                    elif child.type == "asterisk":
                        is_wildcard = True
                if source:
                    if is_wildcard:
                        source += ".*"
                    imports.append(source)

    def _traverse(self, node: Node, content: bytes, current_package: str,
                  symbols: List[ParsedSymbol], calls: List[ExtractedCall],
                  heritages: List[ExtractedHeritage], field_bindings: Dict[str, str],
                  parent_fqn: Optional[str] = None):

        kind = node.type

        if kind in ["class_declaration", "interface_declaration", "enum_declaration", "annotation_type_declaration"]:
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(content, name_node)
                fqn = f"{current_package}.{name}" if current_package else name
                symbol_kind = kind.replace("_declaration", "")
                symbol = ParsedSymbol(
                    kind=symbol_kind,
                    name=name,
                    fqn=fqn,
                    line=node.start_point[0] + 1,
                    modifiers=self._extract_modifiers(node, content),
                    annotations=self._extract_annotations(node, content)
                )
                symbols.append(symbol)

                new_parent_fqn = fqn
                self._extract_heritage(node, content, heritages)

                for child in node.children:
                    self._traverse(child, content, current_package, symbols, calls, heritages, field_bindings, new_parent_fqn)
                return

        elif kind in ("method_declaration", "constructor_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = _text(content, name_node)
                fqn = f"{parent_fqn}.{name}" if parent_fqn else name
                ret_type_node = node.child_by_field_name("type")
                ret_type = _text(content, ret_type_node) if ret_type_node else None

                body = node.child_by_field_name("body")
                complexity = self._extract_method_complexity(body, content) if body else None
                symbol = ParsedSymbol(
                    kind="method" if kind == "method_declaration" else "constructor",
                    name=name,
                    fqn=fqn,
                    line=node.start_point[0] + 1,
                    parent_fqn=parent_fqn,
                    modifiers=self._extract_modifiers(node, content),
                    return_type=ret_type,
                    annotations=self._extract_annotations(node, content),
                    params=self._extract_params(node, content),
                    complexity=complexity,
                )
                symbols.append(symbol)

                if body:
                    self._extract_calls(body, content, fqn, calls)
                return

        elif kind == "field_declaration":
            type_node = node.child_by_field_name("type")
            type_name = _text(content, type_node) if type_node else ""

            for child in node.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    if name_node:
                        name = _text(content, name_node)
                        fqn = f"{parent_fqn}.{name}" if parent_fqn else name
                        symbol = ParsedSymbol(
                            kind="field",
                            name=name,
                            fqn=fqn,
                            line=node.start_point[0] + 1,
                            parent_fqn=parent_fqn,
                            modifiers=self._extract_modifiers(node, content),
                            annotations=self._extract_annotations(node, content)
                        )
                        symbols.append(symbol)
                        field_bindings[name] = type_name
            return

        for child in node.children:
            self._traverse(child, content, current_package, symbols, calls, heritages, field_bindings, parent_fqn)

    def _extract_method_complexity(self, body_node: Node, content: bytes) -> Dict[str, Any]:
        """Count control-flow nodes and call patterns in a method body."""
        branch_types = {"if_statement"}
        loop_types = {"for_statement", "while_statement", "do_statement", "enhanced_for_statement"}
        counts = {
            "branches": 0, "loops": 0, "catches": 0, "tries": 0,
            "throws": 0, "ternaries": 0, "switch_cases": 0,
            "total_calls": 0, "external_calls": 0,
        }
        receiver_names: List[str] = []

        def _walk(node: Node) -> None:
            ntype = node.type
            if ntype in branch_types:
                counts["branches"] += 1
            elif ntype in loop_types:
                counts["loops"] += 1
            elif ntype == "catch_clause":
                counts["catches"] += 1
            elif ntype == "try_statement":
                counts["tries"] += 1
            elif ntype == "throw_statement":
                counts["throws"] += 1
            elif ntype == "ternary_expression":
                counts["ternaries"] += 1
            elif ntype in ("switch_block_statement_group", "switch_expression"):
                counts["switch_cases"] += 1
            elif ntype == "method_invocation":
                counts["total_calls"] += 1
                obj_node = node.child_by_field_name("object")
                if obj_node:
                    receiver = _text(content, obj_node)
                    # External call = receiver is not 'this' or 'super'
                    if receiver not in ("this", "super"):
                        counts["external_calls"] += 1
                        # Track unique receiver names for domain boundary detection
                        base = receiver.split(".")[0].split("(")[0]
                        if base and base[0].islower():
                            receiver_names.append(base)
            for child in node.children:
                _walk(child)

        _walk(body_node)
        body_lines = body_node.end_point[0] - body_node.start_point[0] + 1
        cyclomatic = 1 + counts["branches"] + counts["loops"] + counts["catches"] + counts["ternaries"] + counts["switch_cases"]
        return {
            "cyclomatic_approx": cyclomatic,
            "body_lines": body_lines,
            "receiver_names": list(set(receiver_names)),
            **counts,
        }

    def _extract_annotations(self, node: Node, content: bytes) -> List[Annotation]:
        annotations = []
        for child in node.children:
            if child.type == "modifiers":
                for mod_child in child.children:
                    if "annotation" in mod_child.type:
                        annotations.append(self._parse_annotation_node(mod_child, content))
            elif "annotation" in child.type:
                annotations.append(self._parse_annotation_node(child, content))
        return annotations

    def _extract_modifiers(self, node: Node, content: bytes) -> List[str]:
        modifiers: List[str] = []
        keyword_modifiers = {
            "public", "protected", "private", "static", "final", "abstract",
            "synchronized", "native", "transient", "volatile", "strictfp",
            "default",
        }
        for child in node.children:
            if child.type == "modifiers":
                for mod_child in child.children:
                    if "annotation" in mod_child.type:
                        continue
                    mod_text = _text(content, mod_child).strip()
                    if mod_text in keyword_modifiers:
                        modifiers.append(mod_text)
        return modifiers

    def _parse_annotation_node(self, node: Node, content: bytes) -> Annotation:
        name_node = node.child_by_field_name("name")
        if not name_node:
            for child in node.children:
                if child.type in ["identifier", "scoped_identifier"]:
                    name_node = child
                    break

        name = _text(content, name_node) if name_node else ""
        return Annotation(name=name)

    def _extract_params(self, node: Node, content: bytes) -> List[Param]:
        params = []
        params_node = node.child_by_field_name("parameters")
        if params_node:
            for child in params_node.children:
                if child.type == "formal_parameter":
                    type_node = child.child_by_field_name("type")
                    name_node = child.child_by_field_name("name")
                    if type_node and name_node:
                        params.append(Param(
                            type=_text(content, type_node),
                            name=_text(content, name_node)
                        ))
        return params

    def _extract_heritage(self, node: Node, content: bytes, heritages: List[ExtractedHeritage]):
        super_node = node.child_by_field_name("superclass")
        if super_node:
            for child in super_node.children:
                if child.type in ["type_identifier", "generic_type", "scoped_type_identifier"]:
                    heritages.append(ExtractedHeritage(
                        type_name=_text(content, child),
                        relation="extends"
                    ))

        interfaces_node = node.child_by_field_name("interfaces")
        if interfaces_node:
            for child in interfaces_node.children:
                if child.type == "type_list":
                    for type_child in child.children:
                        if type_child.type in ["type_identifier", "generic_type", "scoped_type_identifier"]:
                            heritages.append(ExtractedHeritage(
                                type_name=_text(content, type_child),
                                relation="implements"
                            ))

    def _extract_calls(self, node: Node, content: bytes, caller_fqn: str, calls: List[ExtractedCall]):
        if node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            obj_node = node.child_by_field_name("object")

            name = _text(content, name_node) if name_node else ""
            obj = _text(content, obj_node) if obj_node else None

            calls.append(ExtractedCall(
                caller_fqn=caller_fqn,
                callee_name=name,
                receiver_name=obj,
                line=node.start_point[0] + 1
            ))
        elif node.type == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            if type_node:
                name = _text(content, type_node)
                calls.append(ExtractedCall(
                    caller_fqn=caller_fqn,
                    callee_name=name,
                    receiver_name="new",
                    line=node.start_point[0] + 1
                ))

        for child in node.children:
            self._extract_calls(child, content, caller_fqn, calls)

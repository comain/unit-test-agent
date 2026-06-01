from typing import List, Dict, Optional, Set
from uta.language.java.parse.models import (
    ParseResult, CodeGraph, GraphNode, GraphEdge, 
    ParsedSymbol, ExtractedCall, ExtractedHeritage
)

class GraphBuilder:
    def __init__(self):
        self.graph = CodeGraph()
        self.fqn_to_file: Dict[str, str] = {}
        self.simple_name_to_fqn: Dict[str, str] = {}
        self.package_to_files: Dict[str, List[str]] = {} # package -> list of class FQNs
        self.module_results: List[ParseResult] = []

    def build(self, results: List[ParseResult]) -> CodeGraph:
        self.module_results = results
        self._build_indexes()
        self._create_nodes()
        self._resolve_heritage()
        self._resolve_calls()
        return self.graph

    def _build_indexes(self):
        for res in self.module_results:
            pkg = res.package
            if pkg not in self.package_to_files:
                self.package_to_files[pkg] = []
                
            for sym in res.symbols:
                if sym.kind in ["class", "interface", "enum", "annotation_type"]:
                    self.fqn_to_file[sym.fqn] = res.path
                    self.simple_name_to_fqn[sym.name] = sym.fqn
                    self.package_to_files[pkg].append(sym.fqn)

    def _create_nodes(self):
        for res in self.module_results:
            file_imports = list(res.imports)
            for sym in res.symbols:
                metadata = {
                    "annotations": [a.name for a in sym.annotations],
                    "modifiers": list(sym.modifiers or []),
                    "return_type": sym.return_type,
                    "params": [(p.type, p.name) for p in sym.params],
                    "parent_fqn": sym.parent_fqn,
                }
                if sym.kind in ["class", "interface", "enum", "annotation_type"]:
                    metadata["imports"] = file_imports
                elif sym.kind == "field":
                    metadata["field_type"] = res.field_bindings.get(sym.name)
                if sym.complexity is not None:
                    metadata["complexity"] = sym.complexity
                node = GraphNode(
                    fqn=sym.fqn,
                    kind=sym.kind,
                    file_path=res.path,
                    line=sym.line,
                    metadata=metadata
                )
                self.graph.nodes[sym.fqn] = node
                
                if sym.parent_fqn:
                    self.graph.edges.append(GraphEdge(
                        source=sym.parent_fqn,
                        target=sym.fqn,
                        relation="CONTAINS"
                    ))

    def _resolve_heritage(self):
        for res in self.module_results:
            # Main class in file
            main_sym = next((s for s in res.symbols if s.kind in ["class", "interface"] and s.parent_fqn is None), None)
            if not main_sym: continue
            
            for h in res.heritage:
                targets = self._resolve_type(h.type_name, res)
                for target_fqn in targets:
                    relation = "EXTENDS" if h.relation == "extends" else "IMPLEMENTS"
                    self.graph.edges.append(GraphEdge(
                        source=main_sym.fqn,
                        target=target_fqn,
                        relation=relation
                    ))

    def _resolve_calls(self):
        for res in self.module_results:
            for call in res.calls:
                receiver_types = []
                if call.receiver_name:
                    if call.receiver_name == "this":
                        receiver_types = [".".join(call.caller_fqn.split(".")[:-1])]
                    elif call.receiver_name == "super":
                        # TODO: resolve super class
                        pass
                    elif call.receiver_name == "new":
                        # Constructor call
                        receiver_types = self._resolve_type(call.callee_name, res)
                    else:
                        type_name = res.field_bindings.get(call.receiver_name)
                        if type_name:
                            receiver_types = self._resolve_type(type_name, res)
                else:
                    # No receiver, try current class
                    receiver_types = [".".join(call.caller_fqn.split(".")[:-1])]
                
                for r_type in receiver_types:
                    target_fqn = f"{r_type}.{call.callee_name}"
                    if target_fqn in self.graph.nodes:
                        self.graph.edges.append(GraphEdge(
                            source=call.caller_fqn,
                            target=target_fqn,
                            relation="CALLS"
                        ))

    def _resolve_type(self, type_name: str, res: ParseResult) -> List[str]:
        """Resolve a simple type name to its FQN(s). Returns a list for wildcards."""
        # 1. Check explicit imports
        for imp in res.imports:
            if imp.endswith(f".{type_name}"):
                return [imp]
            if imp == type_name:
                return [imp]
        
        # 2. Check same package
        fqn = f"{res.package}.{type_name}"
        if fqn in self.fqn_to_file:
            return [fqn]
            
        # 3. Check wildcard imports
        for imp in res.imports:
            if imp.endswith(".*"):
                pkg = imp[:-2]
                if pkg in self.package_to_files:
                    for class_fqn in self.package_to_files[pkg]:
                        if class_fqn.endswith(f".{type_name}"):
                            return [class_fqn]
                            
        # 4. Check java.lang (implicit) - just a guess if not found
        # (We don't have java.lang in our module_results usually)
        
        # 5. Fallback: simple name index
        if type_name in self.simple_name_to_fqn:
            return [self.simple_name_to_fqn[type_name]]
            
        return []

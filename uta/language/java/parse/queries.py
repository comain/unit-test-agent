from typing import List, Dict, Optional
from uta.language.java.parse.models import CodeGraph, ProcessFlow

class GraphQueries:
    def __init__(self, graph: CodeGraph, flows: List[ProcessFlow]):
        self.graph = graph
        self.flows = flows

    def get_class_deps(self, class_fqn: str) -> List[str]:
        """Get FQNs of classes this class depends on (calls, heritage)."""
        deps = set()
        # Find all methods in this class
        methods = [node_fqn for node_fqn, node in self.graph.nodes.items() 
                  if node.kind == "method" and node_fqn.startswith(f"{class_fqn}.")]
        
        for method_fqn in methods:
            for edge in self.graph.edges:
                if edge.source == method_fqn and edge.relation == "CALLS":
                    # target is method FQN, get class FQN
                    target_class = ".".join(edge.target.split(".")[:-1])
                    if target_class != class_fqn:
                        deps.add(target_class)
        
        # Heritage
        for edge in self.graph.edges:
            if edge.source == class_fqn and edge.relation in ["EXTENDS", "IMPLEMENTS"]:
                deps.add(edge.target)
                
        return sorted(list(deps))

    def get_flows_for(self, class_fqn: str) -> List[ProcessFlow]:
        """Get flows that start in this class."""
        return [f for f in self.flows if f.entry_point.startswith(f"{class_fqn}.")]

    def get_callers(self, method_fqn: str) -> List[str]:
        """Get FQNs of methods that call this method."""
        callers = []
        for edge in self.graph.edges:
            if edge.target == method_fqn and edge.relation == "CALLS":
                callers.append(edge.source)
        return callers

    def get_method_signatures(self, class_fqn: str) -> Dict[str, str]:
        """Get signatures of all methods in a class."""
        signatures = {}
        for fqn, node in self.graph.nodes.items():
            if fqn.startswith(f"{class_fqn}.") and node.kind == "method":
                params = ", ".join([f"{p[0]} {p[1]}" for p in node.metadata.get("params", [])])
                ret = node.metadata.get("return_type") or "void"
                signatures[fqn] = f"{ret} {fqn.split('.')[-1]}({params})"
        return signatures

from typing import List, Dict, Set, Optional
from uta.language.java.parse.models import CodeGraph, ProcessFlow, FlowStep, ExternalCall

class ProcessExtractor:
    def __init__(self, graph: CodeGraph):
        self.graph = graph

    def extract_flows(self, entry_points: List[str], max_depth: int = 5) -> List[ProcessFlow]:
        flows = []
        for entry in entry_points:
            if entry not in self.graph.nodes:
                continue
            
            # Simple BFS to find paths
            path = self._trace_path(entry, max_depth)
            if path:
                flow = ProcessFlow(
                    name=entry.split(".")[-2] + "." + entry.split(".")[-1], # Class.method
                    entry_point=entry,
                    steps=path
                )
                flows.append(flow)
        return flows

    def _trace_path(self, start_fqn: str, max_depth: int) -> List[FlowStep]:
        steps = []
        visited = {start_fqn}
        queue = [(start_fqn, 0)]
        
        while queue:
            current_fqn, depth = queue.pop(0)
            if depth > max_depth:
                continue
                
            node = self.graph.nodes.get(current_fqn)
            if not node:
                continue
            
            # Detect kind of step
            kind = "internal_call"
            annos = node.metadata.get("annotations", [])
            if any(a in ["Select", "Update", "Insert", "Delete"] for a in annos):
                kind = "db_query"
            elif any(a in ["DubboReference", "Reference"] for a in annos):
                kind = "rpc_call"
                
            steps.append(FlowStep(fqn=current_fqn, kind=kind))
            
            # Find outgoing CALLS
            for edge in self.graph.edges:
                if edge.source == current_fqn and edge.relation == "CALLS":
                    if edge.target not in visited:
                        visited.add(edge.target)
                        queue.append((edge.target, depth + 1))
        
        return steps

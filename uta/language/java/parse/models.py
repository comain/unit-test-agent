from dataclasses import dataclass, field
from typing import Any, List, Dict, Optional

@dataclass
class Annotation:
    name: str
    arguments: Dict[str, str] = field(default_factory=dict)

@dataclass
class Param:
    type: str
    name: str

@dataclass
class ParsedSymbol:
    kind: str           # "class" | "interface" | "enum" | "method" | "field"
    name: str
    fqn: str            # fully qualified name (package.Class.method)
    line: int
    modifiers: List[str] = field(default_factory=list)
    annotations: List[Annotation] = field(default_factory=list)
    params: List[Param] = field(default_factory=list)   # for methods
    return_type: Optional[str] = None                   # for methods
    parent_fqn: Optional[str] = None                    # enclosing class FQN
    complexity: Optional[Dict[str, Any]] = None         # method body complexity metrics

@dataclass
class ExtractedCall:
    caller_fqn: str
    callee_name: str
    receiver_name: Optional[str] = None
    line: int = 0
    callee_fqn: Optional[str] = None  # resolved later
    confidence: float = 0.0

@dataclass
class ExtractedHeritage:
    type_name: str
    relation: str  # "extends" | "implements"

@dataclass
class ExternalCall:
    kind: str  # "database" | "rpc" | "messaging"
    detail: str
    fqn: str

@dataclass
class ParseResult:
    path: str
    package: str
    imports: List[str] = field(default_factory=list)
    symbols: List[ParsedSymbol] = field(default_factory=list)
    calls: List[ExtractedCall] = field(default_factory=list)
    heritage: List[ExtractedHeritage] = field(default_factory=list)
    annotations: List[Annotation] = field(default_factory=list)
    field_bindings: Dict[str, str] = field(default_factory=dict) # field_name -> type_name

@dataclass
class GraphNode:
    fqn: str
    kind: str
    file_path: str
    line: int
    metadata: Dict = field(default_factory=dict)

@dataclass
class GraphEdge:
    source: str
    target: str
    relation: str  # CONTAINS | CALLS | IMPORTS | EXTENDS | IMPLEMENTS | INJECTS

@dataclass
class CodeGraph:
    nodes: Dict[str, GraphNode] = field(default_factory=dict)
    edges: List[GraphEdge] = field(default_factory=list)

@dataclass
class FlowStep:
    fqn: str
    kind: str              # "internal_call" | "db_query" | "rpc_call" | "mq_publish"
    detail: Optional[str] = None

@dataclass
class ProcessFlow:
    name: str
    entry_point: str
    steps: List[FlowStep] = field(default_factory=list)
    external_deps: List[ExternalCall] = field(default_factory=list)

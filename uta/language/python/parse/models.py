from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PythonImport:
    module: str
    names: List[str] = field(default_factory=list)
    line: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PythonSymbol:
    kind: str
    name: str
    qualified_name: str
    line: int
    end_line: int
    parent: Optional[str] = None
    params: List[str] = field(default_factory=list)
    decorators: List[str] = field(default_factory=list)
    returns: Optional[str] = None
    docstring: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SideEffectHint:
    kind: str
    detail: str
    line: int

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PythonParseResult:
    path: str
    relative_path: str
    syntax_version: str
    parser_backend: str
    imports: List[PythonImport] = field(default_factory=list)
    symbols: List[PythonSymbol] = field(default_factory=list)
    side_effect_hints: List[SideEffectHint] = field(default_factory=list)
    syntax_error: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "relative_path": self.relative_path,
            "syntax_version": self.syntax_version,
            "parser_backend": self.parser_backend,
            "imports": [item.as_dict() for item in self.imports],
            "symbols": [item.as_dict() for item in self.symbols],
            "side_effect_hints": [item.as_dict() for item in self.side_effect_hints],
            "syntax_error": self.syntax_error,
        }

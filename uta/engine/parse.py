from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Mapping, Optional, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class ParsedCallable:
    """Language-neutral callable discovered during source parsing."""

    name: str
    qualified_name: str
    kind: str
    source_path: str
    line: int
    end_line: Optional[int] = None
    parent: Optional[str] = None
    visibility: Optional[str] = None


@dataclass(frozen=True)
class ParsedImport:
    """Language-neutral import/dependency edge discovered during parsing."""

    source_path: str
    module: str
    line: int = 0
    names: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class ParseDiagnostic:
    """Parser diagnostic that can be shown to users or reports."""

    source_path: str
    severity: str
    message: str
    line: int = 0
    code: Optional[str] = None


@dataclass(frozen=True)
class ParseProjectRequest:
    """Language-neutral request to parse a repository or module."""

    repo_path: Path
    module: Optional[str] = None
    source_files: Optional[Sequence[Path]] = None
    max_files: Optional[int] = None


@runtime_checkable
class ParseProjectResult(Protocol):
    """Parsed project facts exposed by a language backend.

    Concrete language results may add typed semantic-model accessors, but core
    parse consumers should rely on this normalized surface where possible.
    """

    language: str
    repo_path: Path
    source_files: Sequence[str]
    callables: Sequence[ParsedCallable]
    imports: Sequence[ParsedImport]
    diagnostics: Sequence[ParseDiagnostic]

    def contains_target(self, target_id: str) -> bool:
        ...

    def target_id_for_source_path(self, source_path: str) -> Optional[str]:
        ...

    def is_testable_target(self, target_id: str) -> bool:
        ...

    def target_selections(self, target_ids: Sequence[str]) -> List[Mapping[str, Any]]:
        ...


@runtime_checkable
class ParseProvider(Protocol):
    """Parses repository source for one backend language."""

    language: str

    def parse_project(self, request: ParseProjectRequest) -> ParseProjectResult:
        ...


def make_parse_provider(language: str) -> ParseProvider:
    normalized = str(language or "").strip().lower()
    if normalized == "java":
        from uta.language.java.parse import JavaParseProvider

        return JavaParseProvider()
    if normalized == "python":
        from uta.language.python.parse import PythonParseProvider

        return PythonParseProvider()
    raise ValueError(f"Unsupported parse provider language: {language}")

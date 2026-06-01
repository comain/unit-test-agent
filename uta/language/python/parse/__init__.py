from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from uta.engine.parse import ParsedCallable, ParsedImport, ParseDiagnostic, ParseProjectRequest
from uta.engine.targets import TargetRef
from uta.engine.languages import RawTargetSelection
from uta.language.python.adapter import PythonLanguageAdapter
from uta.language.python.parse.models import PythonParseResult
from uta.language.python.parse.parser import PythonParser


@dataclass(frozen=True)
class PythonParseProjectResult:
    """Parsed Python project with normalized symbols and diagnostics."""

    repo_path: Path
    parsed_files: Sequence[PythonParseResult]
    source_files: Sequence[str]
    callables: Sequence[ParsedCallable]
    selection: Mapping[str, Any]
    imports: Sequence[ParsedImport] = field(default_factory=tuple)
    diagnostics: Sequence[ParseDiagnostic] = field(default_factory=tuple)
    language: str = "python"

    def contains_target(self, target_id: str) -> bool:
        return self.target_id_for_source_path(_source_path_from_target(target_id)) is not None

    def target_id_for_source_path(self, source_path: str) -> Optional[str]:
        for item in self.source_files:
            if item == source_path:
                return f"pyfile:{item}"
        return None

    def is_testable_target(self, target_id: str) -> bool:
        return self.contains_target(target_id)

    def target_selections(self, target_ids: Sequence[str]) -> List[Mapping[str, Any]]:
        adapter = PythonLanguageAdapter()
        selections: List[Mapping[str, Any]] = []
        for target_id in target_ids:
            try:
                selections.append(adapter.normalize_target(RawTargetSelection(target=target_id)).as_selection())
            except Exception:
                continue
        return selections

    def as_project_index(self) -> Dict[str, Any]:
        files = [
            {
                "path": item.relative_path,
                "syntax_version": item.syntax_version,
                "parser_backend": item.parser_backend,
                "symbol_count": len(item.symbols),
                "side_effect_hint_count": len(item.side_effect_hints),
                "syntax_error": item.syntax_error,
            }
            for item in self.parsed_files
        ]
        symbols = []
        for item in self.parsed_files:
            for symbol in item.as_dict()["symbols"]:
                symbols.append({"path": item.relative_path, **symbol})
        return {
            "language": self.language,
            "repo_path": str(self.repo_path),
            "files": files,
            "symbols": symbols,
            "selection": dict(self.selection),
        }

    def write_project_index(self, output_dir: Optional[Path] = None) -> Path:
        out_dir = Path(output_dir) if output_dir else self.repo_path / ".uta_cache" / "python_context"
        out_dir.mkdir(parents=True, exist_ok=True)
        index_path = out_dir / "index.json"
        index_path.write_text(json.dumps(self.as_project_index(), indent=2, sort_keys=True), encoding="utf-8")
        return index_path


class PythonParseProvider:
    language = "python"

    def parse_project(self, request: ParseProjectRequest) -> PythonParseProjectResult:
        repo_path = Path(request.repo_path).resolve()
        adapter = PythonLanguageAdapter()
        selection = adapter.select_candidates(repo_path, max_files=request.max_files or 500)
        source_paths = [target.source_path for target in selection.targets if target.source_path]
        if request.source_files:
            source_paths = [_relative_or_absolute(repo_path, Path(path)) for path in request.source_files]
        parser = PythonParser()
        parsed_files = [
            parser.parse_file(repo_path / source_path, repo_path=repo_path)
            for source_path in dict.fromkeys(source_paths)
        ]
        return PythonParseProjectResult(
            repo_path=repo_path,
            parsed_files=tuple(parsed_files),
            source_files=tuple(item.relative_path for item in parsed_files),
            callables=tuple(_python_callables(parsed_files)),
            selection=selection.as_dict(),
            imports=tuple(_python_imports(parsed_files)),
            diagnostics=tuple(_python_diagnostics(parsed_files)),
        )


def _python_callables(parsed_files: Sequence[PythonParseResult]) -> List[ParsedCallable]:
    callables: List[ParsedCallable] = []
    for parsed in parsed_files:
        for symbol in parsed.symbols:
            if symbol.kind not in {"class", "function", "method"}:
                continue
            callables.append(
                ParsedCallable(
                    name=symbol.name,
                    qualified_name=symbol.qualified_name,
                    kind=symbol.kind,
                    source_path=parsed.relative_path,
                    line=symbol.line,
                    end_line=symbol.end_line,
                    parent=symbol.parent,
                )
            )
    return callables


def _python_imports(parsed_files: Sequence[PythonParseResult]) -> List[ParsedImport]:
    imports: List[ParsedImport] = []
    for parsed in parsed_files:
        imports.extend(
            ParsedImport(
                source_path=parsed.relative_path,
                module=item.module,
                line=item.line,
                names=tuple(item.names),
            )
            for item in parsed.imports
        )
    return imports


def _python_diagnostics(parsed_files: Sequence[PythonParseResult]) -> List[ParseDiagnostic]:
    diagnostics: List[ParseDiagnostic] = []
    for parsed in parsed_files:
        if not parsed.syntax_error:
            continue
        diagnostics.append(
            ParseDiagnostic(
                source_path=parsed.relative_path,
                severity="error",
                message=str(parsed.syntax_error.get("message") or "Python syntax error"),
                line=int(parsed.syntax_error.get("line") or 0),
                code="syntax_error",
            )
        )
    return diagnostics


def _source_path_from_target(target_id: str) -> str:
    raw = str(target_id or "")
    if raw.startswith("pyfile:"):
        return raw[len("pyfile:") :]
    if raw.startswith("pysymbol:"):
        raw = raw[len("pysymbol:") :]
    return raw.split("::", 1)[0]


def _relative_or_absolute(repo_path: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_path).as_posix()
    except ValueError:
        return str(path)

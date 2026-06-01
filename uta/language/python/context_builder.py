from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.engine.targets import TargetRef
from uta.language.python.parse.parser import PythonParser


class PythonContextBuilder:
    language = "python"

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.parser = PythonParser()

    def parse_source(self, source_path: str):
        return self.parser.parse_file(self.repo_path / source_path, repo_path=self.repo_path)

    def build_target_context(self, target: TargetRef) -> Dict[str, Any]:
        source_path = target.source_path or _source_path_from_target(target.target_id)
        source_abs = self.repo_path / source_path
        if not source_abs.is_file():
            return {
                "language": "python",
                "found": False,
                "target": target.as_selection(),
                "source_path": source_path,
                "error": f"Python target source not found: {source_path}",
            }
        parse_result = self.parser.parse_file(source_abs, repo_path=self.repo_path)
        target_symbol = _find_symbol(parse_result, target.symbol)
        if target.symbol and not target_symbol:
            return {
                "language": "python",
                "found": False,
                "target": target.as_selection(),
                "source_path": source_path,
                "syntax": {
                    "version": parse_result.syntax_version,
                    "parser_backend": parse_result.parser_backend,
                    "syntax_error": parse_result.syntax_error,
                },
                "symbols": [item.as_dict() for item in parse_result.symbols],
                "error": f"Python target symbol not found: {target.symbol} in {source_path}",
            }
        source = source_abs.read_text(encoding="utf-8")
        context = {
            "language": "python",
            "found": True,
            "target": target.as_selection(),
            "source_path": source_path,
            "syntax": {
                "version": parse_result.syntax_version,
                "parser_backend": parse_result.parser_backend,
                "syntax_error": parse_result.syntax_error,
            },
            "imports": [item.as_dict() for item in parse_result.imports],
            "symbols": [item.as_dict() for item in parse_result.symbols],
            "target_symbol": target_symbol.as_dict() if target_symbol else None,
            "side_effect_hints": [item.as_dict() for item in parse_result.side_effect_hints],
            "companion_files": self._companion_files(source_abs),
            "source_excerpt": _source_excerpt(source, target_symbol),
        }
        return context

    def export_target_context(self, target: TargetRef, *, output_dir: Optional[Path] = None) -> Dict[str, str]:
        context = self.build_target_context(target)
        out_dir = Path(output_dir) if output_dir else self.repo_path / ".uta_cache" / "python_context"
        out_dir.mkdir(parents=True, exist_ok=True)
        slug = _target_slug(target.target_id)
        json_path = out_dir / f"{slug}.json"
        markdown_path = out_dir / f"{slug}.md"
        json_path.write_text(json.dumps(context, indent=2, sort_keys=True), encoding="utf-8")
        markdown_path.write_text(_context_markdown(context), encoding="utf-8")
        return {"json_abs": str(json_path.resolve()), "context_abs": str(markdown_path.resolve())}

    def query_target(self, target: TargetRef) -> Dict[str, Any]:
        context = self.build_target_context(target)
        if context.get("found"):
            context["context"] = self.export_target_context(target)
        return context

    def export_project_context(self, **kwargs: Any) -> Dict[str, Any]:
        return self.export_project_index(**kwargs)

    def export_project_index(self, *, output_dir: Optional[Path] = None, max_files: Optional[int] = None) -> Dict[str, Any]:
        from uta.engine.parse import ParseProjectRequest, make_parse_provider

        parsed = make_parse_provider("python").parse_project(
            ParseProjectRequest(repo_path=self.repo_path, max_files=max_files)
        )
        parsed.write_project_index(output_dir=output_dir)
        return parsed.as_project_index()

    def _companion_files(self, source_abs: Path) -> List[Dict[str, Any]]:
        companions: List[Path] = []
        package_init = source_abs.parent / "__init__.py"
        if package_init.exists() and package_init != source_abs:
            companions.append(package_init)
        stem = source_abs.stem
        for candidate in (
            self.repo_path / "tests" / f"test_{stem}.py",
            self.repo_path / "test" / f"test_{stem}.py",
            source_abs.parent / f"test_{stem}.py",
        ):
            if candidate.exists() and candidate != source_abs:
                companions.append(candidate)
        out = []
        for path in dict.fromkeys(companions):
            out.append({"path": path.relative_to(self.repo_path).as_posix(), "size_bytes": path.stat().st_size})
        return out


def _source_path_from_target(target_id: str) -> str:
    raw = target_id
    if raw.startswith("pyfile:"):
        return raw[len("pyfile:") :]
    if raw.startswith("pysymbol:"):
        raw = raw[len("pysymbol:") :]
    return raw.split("::", 1)[0]


def _find_symbol(parse_result, symbol_name: Optional[str]):
    if not symbol_name:
        return None
    for symbol in parse_result.symbols:
        if symbol.qualified_name == symbol_name or symbol.name == symbol_name:
            return symbol
    return None


def _source_excerpt(source: str, symbol) -> str:
    lines = source.splitlines()
    if not symbol:
        return "\n".join(lines[:200])
    start = max(1, int(symbol.line or 1) - 8)
    end = min(len(lines), int(symbol.end_line or symbol.line or 1) + 8)
    numbered = []
    for line_no in range(start, end + 1):
        numbered.append(f"{line_no}: {lines[line_no - 1]}")
    return "\n".join(numbered)


def _target_slug(target_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", target_id).strip("_") or "python_target"


def _context_markdown(context: Dict[str, Any]) -> str:
    target = context.get("target") or {}
    lines = [
        "# Python Target Context",
        "",
        f"- Target: `{target.get('display_name') or target.get('target_id') or ''}`",
        f"- Source: `{context.get('source_path') or ''}`",
        f"- Syntax: `{(context.get('syntax') or {}).get('version')}` via `{(context.get('syntax') or {}).get('parser_backend')}`",
        "",
        "## Symbols",
    ]
    for symbol in context.get("symbols") or []:
        lines.append(f"- `{symbol.get('qualified_name')}` ({symbol.get('kind')}) line {symbol.get('line')}")
    lines.extend(["", "## Side Effects"])
    for hint in context.get("side_effect_hints") or []:
        lines.append(f"- {hint.get('kind')} line {hint.get('line')}: `{hint.get('detail')}`")
    lines.extend(["", "## Companion Files"])
    for item in context.get("companion_files") or []:
        lines.append(f"- `{item.get('path')}`")
    lines.extend(["", "## Source Excerpt", "", "```python", context.get("source_excerpt") or "", "```", ""])
    return "\n".join(lines)

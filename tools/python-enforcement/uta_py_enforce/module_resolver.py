"""Module resolution helpers for Python enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class PythonModuleResolution:
    """Resolved import identity for a Python source file."""

    module_name: str
    pythonpath_roots: Sequence[Path]
    reason: str


def resolve_python_module(repo_path: Path, source_path: str) -> PythonModuleResolution:
    """Resolve the dotted module name used by tests and mutmut for ``source_path``."""
    repo = Path(repo_path).expanduser().resolve()
    normalized = _normalize_source_path(source_path)
    module_rel = _source_module_relative_path(normalized)

    if module_rel.parts and module_rel.parts[0] == "src":
        src_dir = repo / "src"
        if (src_dir / "__init__.py").exists():
            return PythonModuleResolution(
                module_name=_parts_to_module(module_rel.parts),
                pythonpath_roots=(repo,),
                reason="src package",
            )
        module_rel = Path(*module_rel.parts[1:]) if len(module_rel.parts) > 1 else Path("")
        return PythonModuleResolution(
            module_name=_parts_to_module(module_rel.parts),
            pythonpath_roots=(repo / "src", repo),
            reason="src source root without package marker",
        )

    return PythonModuleResolution(
        module_name=_parts_to_module(module_rel.parts),
        pythonpath_roots=(repo,),
        reason="path-derived module",
    )


def module_name_from_source_path(source_path: str, *, repo_path: Path | None = None) -> str:
    """Backward-compatible module-name helper; prefer ``resolve_python_module``."""
    if repo_path is not None:
        return resolve_python_module(repo_path, source_path).module_name
    module_rel = _source_module_relative_path(_normalize_source_path(source_path))
    if module_rel.parts and module_rel.parts[0] == "src":
        module_rel = Path(*module_rel.parts[1:]) if len(module_rel.parts) > 1 else Path("")
    return _parts_to_module(module_rel.parts)


def _normalize_source_path(source_path: str) -> str:
    return str(source_path or "").replace("\\", "/").strip("/")


def _source_module_relative_path(source_path: str) -> Path:
    path = Path(source_path)
    if path.suffix == ".py":
        path = path.with_suffix("")
    if path.name == "__init__":
        path = path.parent
    return path


def _parts_to_module(parts: Sequence[str]) -> str:
    normalized = []
    for part in parts:
        if not part or part == ".":
            continue
        normalized.append(part.split(".", 1)[0])
    return ".".join(normalized)

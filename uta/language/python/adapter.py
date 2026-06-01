from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from uta.engine.languages import DetectionSignal, GeneratedTestPolicy, LanguageCapabilities, PromptBundle, RawTargetSelection
from uta.engine.targets import TargetRef


_PYTHON_MARKERS = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg", "tox.ini", "noxfile.py")
_EXCLUDED_PARTS = {".git", ".hg", ".svn", ".tox", ".nox", ".venv", "venv", "__pycache__", "site-packages", "dist", "build"}
DEFAULT_MAX_FILES = 500


@dataclass(frozen=True)
class PythonTargetSelection:
    targets: Sequence[TargetRef]
    skipped_targets: Sequence[Dict[str, Any]]
    max_files: int

    @property
    def selected_count(self) -> int:
        return len(self.targets)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_targets)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "selected_count": self.selected_count,
            "skipped_count": self.skipped_count,
            "max_files": self.max_files,
            "skipped_targets": list(self.skipped_targets),
        }


class PythonLanguageAdapter:
    language = "python"

    def capabilities(self) -> LanguageCapabilities:
        return LanguageCapabilities(
            supports_function_targets=True,
            supports_branch_coverage=True,
            supports_mutation=True,
            supports_incremental_diff_enforcement=True,
            supports_import_safety_hints=True,
            generated_tests_are_autopushable=True,
        )

    def detect(self, repo_path: Path, changed_paths: Optional[Sequence[str]] = None) -> DetectionSignal:
        reasons = []
        for marker in _PYTHON_MARKERS:
            if (repo_path / marker).exists():
                reasons.append(marker)
        if changed_paths:
            reasons.extend(path for path in changed_paths if str(path).endswith(".py"))
        elif not reasons:
            for py_file in _iter_production_python_files(repo_path):
                reasons.append(str(py_file.relative_to(repo_path)))
                break
        return DetectionSignal(self.language, len(reasons), reasons)

    def normalize_target(self, raw: RawTargetSelection) -> TargetRef:
        source_path, symbol, granularity = _parse_python_selection(raw)
        display = raw.display_name or (f"{source_path}::{symbol}" if symbol else source_path)
        target_id = f"pysymbol:{source_path}::{symbol}" if symbol else f"pyfile:{source_path}"
        return raw.to_target_ref(
            language=self.language,
            target_id=target_id,
            display_name=display,
            granularity=granularity,
            source_path=source_path,
            symbol=symbol,
        )

    def scan_candidates(self, repo_path: Path) -> Sequence[TargetRef]:
        return self.select_candidates(repo_path).targets

    def select_candidates(self, repo_path: Path, *, max_files: int = DEFAULT_MAX_FILES) -> PythonTargetSelection:
        targets = []
        skipped: List[Dict[str, Any]] = []
        max_files = max(1, int(max_files or DEFAULT_MAX_FILES))
        for py_file in _iter_production_python_files(repo_path):
            relative = py_file.relative_to(repo_path).as_posix()
            target = self.normalize_target(RawTargetSelection(target=relative))
            if len(targets) < max_files:
                targets.append(target)
            else:
                skipped.append(
                    {
                        "target_id": target.target_id,
                        "source_path": target.source_path,
                        "reason": "max_files_exceeded",
                    }
                )
        return PythonTargetSelection(targets=targets, skipped_targets=skipped, max_files=max_files)

    def generated_test_policy(self, repo_path: Path, target: Optional[TargetRef]) -> GeneratedTestPolicy:
        return GeneratedTestPolicy(
            language=self.language,
            allowed_test_roots=("tests", "tests/uta_generated"),
            autopushable=True,
        )

    def prompt_bundle(self) -> PromptBundle:
        return PromptBundle(
            language=self.language,
            plan="python_plan_tests",
            generate="python_generate_test",
            fix_compile="python_fix_compile",
            fix_coverage="python_fix_coverage",
            fix_mutations="python_fix_mutations",
        )


def _is_production_python_file(path: Path, repo_path: Path) -> bool:
    try:
        relative = path.relative_to(repo_path)
    except ValueError:
        return False
    parts = set(relative.parts)
    if parts & _EXCLUDED_PARTS:
        return False
    if relative.parts and relative.parts[0] in {"tests", "test"}:
        return False
    if path.name == "__init__.py":
        return False
    return not path.name.startswith("test_")


def _iter_production_python_files(repo_path: Path):
    for root, dirnames, filenames in os.walk(repo_path):
        root_path = Path(root)
        try:
            relative_root = root_path.relative_to(repo_path)
        except ValueError:
            continue
        root_parts = relative_root.parts
        dirnames[:] = sorted(
            dirname
            for dirname in dirnames
            if dirname not in _EXCLUDED_PARTS and not (not root_parts and dirname in {"tests", "test"})
        )
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            py_file = root_path / filename
            if _is_production_python_file(py_file, repo_path):
                yield py_file


def _normalize_source_path(source_path: str) -> str:
    path = str(source_path or "").strip()
    if not path:
        raise ValueError("Python target requires a source path")
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    parts = [part for part in path.split("/") if part]
    if path.startswith("/") or any(part == ".." for part in parts):
        raise ValueError("Python target source path must be repo-relative and stay inside the repository")
    if not path.endswith(".py"):
        raise ValueError("Python target source path must point to a .py file")
    return "/".join(parts)


def _strip_prefixed_target(value: str) -> Tuple[str, Optional[str]]:
    if value.startswith("pyfile:"):
        return value[len("pyfile:") :], None
    if value.startswith("pysymbol:"):
        rest = value[len("pysymbol:") :]
        if "::" not in rest:
            raise ValueError("Python symbol target must use pysymbol:path.py::symbol")
        source_path, symbol = rest.split("::", 1)
        return source_path, symbol
    if "::" in value:
        source_path, symbol = value.split("::", 1)
        return source_path, symbol
    return value, None


def _parse_python_selection(raw: RawTargetSelection) -> Tuple[str, Optional[str], str]:
    value = raw.target or raw.target_id
    if value:
        source_path, symbol = _strip_prefixed_target(str(value))
    else:
        source_path, symbol = raw.source_path, raw.symbol
    source_path = _normalize_source_path(str(source_path or ""))
    symbol = str(symbol or raw.symbol or "").strip() or None
    granularity = raw.granularity or ("function" if symbol else "file")
    return source_path, symbol, str(granularity)

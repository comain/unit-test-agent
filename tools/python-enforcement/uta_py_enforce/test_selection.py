"""Strict Python unit-test selection and target context discovery for enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Optional, Sequence
from .module_resolver import module_name_from_source_path


GENERATED_ROOT = Path("tests") / "uta_generated"
_AMBIGUOUS_MODULE_STEMS = {"admin", "api", "apps", "config", "constants", "models", "settings", "urls", "utils", "views"}


@dataclass(frozen=True)
class ExistingPythonTestMatch:
    path: str
    score: int
    reasons: Sequence[str] = ()


@dataclass(frozen=True)
class PythonTestDestination:
    path: str
    existing_match: Optional[ExistingPythonTestMatch] = None

    @property
    def uses_existing_test(self) -> bool:
        return self.existing_match is not None


def generated_test_path(source_path_or_target: Any) -> str:
    """Derive canonical generated test path from a source path or target object."""
    if hasattr(source_path_or_target, "source_path") and source_path_or_target.source_path:
        source_path = source_path_or_target.source_path
    elif hasattr(source_path_or_target, "target_id") and source_path_or_target.target_id:
        source_path = source_path_from_target(source_path_or_target.target_id)
    else:
        source_path = str(source_path_or_target or "")
    source_stem = source_path[:-3] if source_path.endswith(".py") else source_path
    slug = re.sub(r"[^A-Za-z0-9]+", "_", source_stem).strip("_").lower()
    return (GENERATED_ROOT / f"test_{slug or 'python_target'}.py").as_posix()


def source_path_from_target(target_id: str) -> str:
    raw = str(target_id or "")
    if raw.startswith("pyfile:"):
        return raw[len("pyfile:") :]
    if raw.startswith("pysymbol:"):
        return raw[len("pysymbol:") :]
    return raw.split("::", 1)[0]


def strict_test_candidates(
    repo: Path,
    source_path: str,
    configured: Sequence[str] = (),
    *,
    symbol: str | None = None,
    context_payload: Mapping[str, Any] | None = None,
    max_results: int = 5,
) -> list[str]:
    """Find narrow unit-test files targeting source_path, prioritizing configured paths."""
    repo = Path(repo).resolve()
    matches = discover_strict_python_test_candidates(
        repo,
        source_path,
        symbol=symbol,
        context_payload=context_payload,
        max_results=max(100, max_results),
    )
    configured_paths = {str(path).replace("\\", "/").strip() for path in configured}
    preferred = [match.path for match in matches if match.path in configured_paths]
    return (preferred or [match.path for match in matches])[:max_results]


def select_python_test_destination(
    repo_path: Path,
    target: Any,
    *,
    context_payload: Optional[Mapping[str, Any]] = None,
) -> PythonTestDestination:
    source_path = getattr(target, "source_path", None) or source_path_from_target(getattr(target, "target_id", str(target)))
    symbol = getattr(target, "symbol", None)
    generated = generated_test_path(source_path)
    if (Path(repo_path) / generated).exists():
        return PythonTestDestination(path=generated)
    matches = discover_strict_python_test_candidates(
        repo_path,
        source_path,
        symbol=symbol,
        context_payload=context_payload,
    )
    if matches:
        return PythonTestDestination(path=matches[0].path, existing_match=matches[0])
    return PythonTestDestination(path=generated)


def discover_strict_python_test_candidates(
    repo_path: Path,
    source_path_or_target: Any,
    *,
    symbol: str | None = None,
    context_payload: Optional[Mapping[str, Any]] = None,
    max_results: int = 5,
) -> list[ExistingPythonTestMatch]:
    """Find narrow unit-test files whose path/name strictly targets source_path."""
    repo = Path(repo_path).resolve()
    if hasattr(source_path_or_target, "source_path") and source_path_or_target.source_path:
        source_path = source_path_or_target.source_path
        if symbol is None:
            symbol = getattr(source_path_or_target, "symbol", None)
    elif hasattr(source_path_or_target, "target_id") and source_path_or_target.target_id:
        source_path = source_path_from_target(source_path_or_target.target_id)
        if symbol is None:
            symbol = getattr(source_path_or_target, "symbol", None)
    else:
        source_path = str(source_path_or_target or "")

    generated = generated_test_path(source_path)
    matches: list[ExistingPythonTestMatch] = []
    if (repo / generated).exists():
        matches.append(
            ExistingPythonTestMatch(
                path=generated,
                score=10_000,
                reasons=("canonical UTA target test",),
            )
        )

    matches.extend(
        discover_related_existing_tests(
            repo,
            source_path,
            symbol=symbol,
            context_payload=context_payload,
            max_results=max_results,
            strict=True,
        )
    )

    seen = set()
    deduped: list[ExistingPythonTestMatch] = []
    for match in sorted(matches, key=lambda item: (-item.score, len(item.path), item.path)):
        if match.path in seen:
            continue
        seen.add(match.path)
        deduped.append(match)
    return deduped[:max_results]


def discover_related_existing_tests(
    repo_path: Path,
    source_path: str,
    *,
    symbol: str | None = None,
    context_payload: Optional[Mapping[str, Any]] = None,
    max_results: int = 5,
    strict: bool = True,
) -> list[ExistingPythonTestMatch]:
    """Find existing hand-written unit tests targeting source_path across layouts."""
    repo = Path(repo_path).resolve()
    source_module = module_name_from_source_path(source_path, repo_path=repo)
    source_tokens = _path_tokens(source_path)
    source_stem_tokens = _path_tokens(Path(source_path).stem)
    strict_symbol_names = {symbol} if symbol else set()
    content_symbol_names = set(strict_symbol_names)
    if context_payload:
        for item in context_payload.get("symbols") or []:
            if isinstance(item, dict) and item.get("name"):
                content_symbol_names.add(str(item["name"]))

    matches: list[ExistingPythonTestMatch] = []
    for test_file in _iter_existing_python_test_files(repo):
        try:
            relative = test_file.relative_to(repo).as_posix()
        except ValueError:
            continue
        if relative.startswith(f"{GENERATED_ROOT.as_posix()}/"):
            continue
        name_score, name_reasons = _strict_name_score(relative, source_path, source_stem_tokens, strict_symbol_names)
        if strict and name_score <= 0:
            continue
        try:
            content = test_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        _, needs_exact_module = _ambiguous_module_package_match(relative, source_path)
        if needs_exact_module and source_module not in content and source_path not in content:
            continue
        score = name_score
        reasons: list[str] = list(name_reasons)
        file_tokens = _path_tokens(relative)
        path_overlap = source_tokens & file_tokens
        if path_overlap:
            score += len(path_overlap)
        if source_path and source_path in content:
            score += 12
            reasons.append(f"references source path {source_path}")
        if source_module and source_module in content:
            score += 10
            reasons.append(f"references module {source_module}")
        source_stem = Path(source_path).stem
        if source_stem and source_stem in content:
            score += 4
            reasons.append(f"references source stem {source_stem}")
        symbol_hits = sorted(name for name in content_symbol_names if name and name in content)
        if symbol_hits:
            score += min(8, 2 * len(symbol_hits))
            reasons.append(f"references symbols: {', '.join(symbol_hits[:4])}")
        if score >= 8 and (not strict or name_score > 0):
            matches.append(ExistingPythonTestMatch(path=relative, score=score, reasons=tuple(reasons)))

    return sorted(matches, key=lambda item: (-item.score, len(item.path), item.path))[:max_results]


def is_strict_test_path(path: str, source_stem: str, canonical: str, *, source_path: str = "") -> bool:
    normalized = path.replace("\\", "/").lower()
    name = Path(normalized).name
    if normalized == canonical.lower():
        return True
    if source_path and not _ambiguous_module_package_match(normalized, source_path)[0]:
        return False
    if not (name.startswith("test_") or name.endswith("_test.py")):
        return False
    broad = ("e2e", "integration", "smoke", "workflow", "check_test", "pure")
    if any(token in name for token in broad) and source_stem not in name:
        return False
    return source_stem in name


def strict_test_score(path: str, source_stem: str, canonical: str) -> int:
    normalized = path.replace("\\", "/").lower()
    if normalized == canonical.lower():
        return 100
    if Path(normalized).name == f"test_{source_stem}.py":
        return 90
    if source_stem in Path(normalized).name:
        return 70
    return 0


def _iter_existing_python_test_files(repo: Path) -> Iterable[Path]:
    excluded = {".git", ".hg", ".svn", ".tox", ".nox", ".venv", "venv", "__pycache__", ".uta_cache", ".uta_reports", "mutants"}
    for root, dirnames, filenames in os.walk(repo):
        root_path = Path(root)
        dirnames[:] = sorted(name for name in dirnames if name not in excluded and not name.startswith("."))
        try:
            relative_root = root_path.relative_to(repo)
        except ValueError:
            continue
        if not _looks_like_test_root(relative_root.parts):
            continue
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            if filename.startswith("test_") or filename.endswith("_test.py"):
                yield root_path / filename


def _looks_like_test_root(parts: Sequence[str]) -> bool:
    return bool(parts) and any(part in {"tests", "test"} or part.endswith("_test") for part in parts)


def _path_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^A-Za-z0-9]+", str(value or "").lower())
        if len(token) >= 3 and token not in {"test", "tests", "src", "main", "python", "service"}
    }


def _symbol_names(symbol: str | None, context_payload: Optional[Mapping[str, Any]] = None) -> set[str]:
    names: set[str] = set()
    if symbol:
        names.add(symbol)
    if context_payload:
        for item in context_payload.get("symbols") or []:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
    return names


def _strict_name_score(
    test_path: str,
    source_path: str,
    source_stem_tokens: set[str],
    symbol_names: set[str],
) -> tuple[int, tuple[str, ...]]:
    if not _ambiguous_module_package_match(test_path, source_path)[0]:
        return 0, ()
    filename = Path(test_path).name.lower()
    stem = Path(source_path).stem.lower()
    test_stem = filename[:-3] if filename.endswith(".py") else filename
    test_stem = re.sub(r"^(test_|check_)", "", test_stem)
    test_stem = re.sub(r"(_test|_tests)$", "", test_stem)
    test_tokens = _path_tokens(test_stem)
    path_tokens = _path_tokens(test_path)
    reasons: list[str] = []
    score = 0
    if test_stem == stem:
        score += 120
        reasons.append(f"filename exactly targets {stem}")
    if stem and test_stem.endswith(f"_{stem}"):
        score += 90
        reasons.append(f"filename ends with target stem {stem}")
    if stem and stem in test_tokens:
        score += 70
        reasons.append(f"filename contains target stem {stem}")
    symbol_hits = sorted(
        name
        for name in symbol_names
        if name and (name.lower() in test_stem or name.lower() in path_tokens)
    )
    if symbol_hits:
        score += min(60, 20 * len(symbol_hits))
        reasons.append(f"filename contains target symbols: {', '.join(symbol_hits[:3])}")
    return score, tuple(reasons)


def _ambiguous_module_package_match(test_path: str, source_path: str) -> tuple[bool, bool]:
    """Return path eligibility and whether an exact module import is required."""
    source = Path(source_path)
    if source.stem.lower() not in _AMBIGUOUS_MODULE_STEMS:
        return True, False
    package = [part.lower() for part in source.parent.parts if part.lower() not in {".", "src", "python"}]
    if not package:
        return True, False
    test = Path(test_path.replace("\\", "/"))
    test_dirs = [part.lower() for part in test.parent.parts if part.lower() not in {"tests", "test", "unit"}]
    if any(test_dirs[index:index + len(package)] == package for index in range(len(test_dirs) - len(package) + 1)):
        return True, False
    if "_".join(package) in test.stem.lower():
        return True, False
    if package[0] in test_dirs:
        return True, True
    return False, False

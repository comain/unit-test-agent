"""Pytest invocation context: import roots, PYTHONPATH, and repo isolation.

Selected tests are run against a target repository whose layout UTA does not
control, so before pytest is invoked this module decides the import roots, the
deterministic PYTHONPATH order that the startup plugin reapplies, and -- for a
repository that is itself a package -- the isolated temporary copy of the test
files. It only computes the invocation; ``process`` runs it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Dict, List, Sequence, Tuple

from uta_py_enforce.optional_plugins import optional_plugin_env
from uta.language.python.verification.mutmut_runtime import _normalize_relpath


@dataclass(frozen=True)
class _PytestExecutionContext:
    test_paths: Tuple[str, ...]
    env_overrides: Dict[str, str]


def _append_csv_env(current: str, value: str) -> str:
    entries = [item.strip() for item in current.split(",") if item.strip()]
    if value not in entries:
        entries.append(value)
    return ",".join(entries)


def _prepare_pytest_execution_context(
    repo: Path,
    test_paths: Sequence[str],
    *,
    source_path: str | None = None,
) -> _PytestExecutionContext:
    original_paths = tuple(str(path) for path in test_paths)
    if not original_paths:
        return _PytestExecutionContext(original_paths, {})

    test_import_roots = _pytest_import_roots_for_tests(repo, original_paths)
    source_import_roots: List[str] = []
    if source_path:
        source_import_roots = _python_import_roots(
            repo, repo / _normalize_relpath(source_path)
        )
    import_roots = list(dict.fromkeys([*test_import_roots, *source_import_roots]))
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    repo_root = repo.resolve().as_posix()
    # Source directories are deliberately not put on PYTHONPATH: Python reads
    # it before initializing the standard library, so a legacy file such as
    # common/enum.py can shadow stdlib enum and prevent the interpreter from
    # starting. The pytest plugin applies those roots after startup instead.
    pythonpath_entries = [*test_import_roots, repo_root]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    pythonpath = os.pathsep.join(dict.fromkeys(path for path in pythonpath_entries if path))
    env_overrides = {
        "PYTHONPATH": pythonpath,
        "UTA_REPO_ROOT": repo_root,
        # UTA runs project tests, not plugins installed in UTA's own runtime.
        # Autoloading langsmith and similar plugins lets unrelated dependencies
        # leak into the isolated project overlay.
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    if import_roots:
        # pytest may prepend its root after Python has consumed PYTHONPATH. The
        # startup plugin reapplies this deterministic order before collection.
        env_overrides["UTA_PYTEST_IMPORT_ROOTS"] = os.pathsep.join(import_roots)
        env_overrides["PYTEST_PLUGINS"] = _append_csv_env(
            os.environ.get("PYTEST_PLUGINS", ""),
            "uta_py_enforce.pytest_import_roots_plugin",
        )
    # Unconditional: the shim carries pytest-timeout for every run, not only
    # the async ones. A test that deadlocks otherwise spends the whole
    # enforcement budget and is reaped with no traceback and no coverage.
    env_overrides["PYTEST_PLUGINS"] = _append_csv_env(
        env_overrides.get("PYTEST_PLUGINS", os.environ.get("PYTEST_PLUGINS", "")),
        "uta_py_enforce.optional_plugins",
    )
    env_overrides.update(
        optional_plugin_env(
            asyncio=_uses_pytest_marker(repo, original_paths, "asyncio"),
            environ=os.environ,
        )
    )
    if not (repo / "__init__.py").is_file():
        return _PytestExecutionContext(original_paths, env_overrides)

    digest = hashlib.sha1(str(repo.resolve()).encode("utf-8")).hexdigest()[:16]
    isolated_root = Path(tempfile.gettempdir()) / f"uta-pytest-isolated-{digest}"
    shutil.rmtree(isolated_root, ignore_errors=True)
    copied_paths: List[str] = []
    copied_support: set[Path] = set()
    for raw_path in original_paths:
        source = repo / _normalize_relpath(raw_path)
        if not source.is_file():
            copied_paths.append(raw_path)
            continue
        relative = Path(_normalize_relpath(raw_path))
        destination = isolated_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            _rewrite_isolated_pytest_source(source.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        copied_paths.append(destination.as_posix())

        current = source.parent
        while current != repo and repo in current.parents:
            conftest = current / "conftest.py"
            if conftest.is_file() and conftest not in copied_support:
                target = isolated_root / conftest.relative_to(repo)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(conftest, target)
                copied_support.add(conftest)
            current = current.parent

    return _PytestExecutionContext(tuple(copied_paths), env_overrides)


def _pytest_import_roots_for_tests(repo: Path, test_paths: Sequence[str]) -> List[str]:
    roots: List[str] = []
    for raw_path in test_paths:
        source = repo / _normalize_relpath(raw_path)
        roots.extend(_python_import_roots(repo, source))
    return list(dict.fromkeys(roots))


def _python_import_roots(repo: Path, source: Path) -> List[str]:
    """Return every package/search root between one Python file and the repo.

    Target source roots matter even when the selected test is a canonical file
    under top-level ``tests/``. Legacy repositories commonly import sibling
    packages by bare name (for example ``pipecat/api`` importing
    ``wechatlet_ai``), so deriving roots only from the test path makes a valid
    project fail collection before coverage starts.
    """
    if not source.is_file():
        return []
    roots: List[str] = []
    current = source.parent.resolve()
    repo_resolved = repo.resolve()
    while current != repo_resolved and repo_resolved in current.parents:
        roots.append(current.as_posix())
        current = current.parent
    return roots


def _uses_pytest_marker(repo: Path, test_paths: Sequence[str], marker: str) -> bool:
    pattern = re.compile(rf"\bpytest\.mark\.{re.escape(marker)}\b")
    for raw_path in test_paths:
        path = repo / _normalize_relpath(raw_path)
        try:
            if pattern.search(path.read_text(encoding="utf-8")):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def _rewrite_isolated_pytest_source(text: str) -> str:
    return re.sub(
        r"(?m)^(\s*(?:(?:[A-Z][A-Z0-9_]*_)?ROOT)\s*=\s*)"
        r"Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]\s*$",
        r'\1Path(__import__("os").environ.get("UTA_REPO_ROOT", Path(__file__).resolve().parents[\2]))',
        text,
    )

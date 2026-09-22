"""Import roots for the repository's own pytest runs.

Repository tests commonly `import conftest`, or import a sibling helper by bare
name. That resolves only when the test's own directory is on `sys.path`.
pytest's default `prepend` import mode inserts that directory only when it is
not a package: a `test/__init__.py` makes the inserted root the repository
instead, and every such import fails at collection with
`ModuleNotFoundError: No module named 'conftest'`.

Legacy built these roots and passed them down as `PYTHONPATH`. The binding
shipped `pytest_import_roots_plugin` -- the consumer of `UTA_PYTEST_IMPORT_ROOTS`
-- without anything that sets it, so the plugin was inert and seven targets in
one production report never reached coverage.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Mapping, Sequence

from .optional_plugins import OPTIONAL_PLUGINS_ENV, optional_plugin_env

_PLUGIN = "uta_py_enforce.pytest_import_roots_plugin"
_OPTIONAL_PLUGINS = "uta_py_enforce.optional_plugins"


def pytest_process_command(python_bin, args, *, coverage_include=None, coverage_omit=None):
    """Use the same finalized-result shutdown boundary in every Python lane."""
    command = [python_bin, str(Path(__file__).with_name("pytest_process.py"))]
    if coverage_include:
        command.extend(["--coverage-include", coverage_include])
    if coverage_omit:
        command.extend(["--coverage-omit", coverage_omit])
    return [*command, "--", *args]


def pytest_import_roots(repo: Path, test_paths: Sequence[str]) -> list[str]:
    """Each test file's directory, and every directory up to the repo root."""
    roots: list[str] = []
    repo_resolved = Path(repo).resolve()
    for raw_path in test_paths:
        relative = str(raw_path or "").replace("\\", "/").strip()
        if not relative:
            continue
        source = repo_resolved / relative
        if not source.is_file():
            continue
        current = source.parent.resolve()
        while current != repo_resolved and repo_resolved in current.parents:
            roots.append(current.as_posix())
            current = current.parent
    return list(dict.fromkeys(roots))


def pytest_env(
    repo: Path,
    test_paths: Sequence[str],
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """A complete child environment whose `PYTHONPATH` carries the import roots.

    `run_command` passes `env` straight to `subprocess.run`, which replaces the
    environment rather than extending it, so this returns the whole thing.
    """
    env = dict(base_env if base_env is not None else os.environ)
    repo_resolved = Path(repo).resolve()
    repo_root = repo_resolved.as_posix()
    roots = pytest_import_roots(repo_resolved, test_paths)

    entries = [*roots, repo_root]
    if roots:
        # The distribution root, so the child can import the plugin below
        # whatever interpreter the repository is verified with.
        entries.append(Path(__file__).resolve().parent.parent.as_posix())
    existing = str(env.get("PYTHONPATH", "") or "")
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(item for item in entries if item))
    env["UTA_REPO_ROOT"] = repo_root
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    plugins = [item for item in str(env.get("PYTEST_PLUGINS", "") or "").split(",") if item]
    if roots:
        env["UTA_PYTEST_IMPORT_ROOTS"] = os.pathsep.join(roots)
        # pytest may prepend its own rootdir after Python has consumed
        # PYTHONPATH; the plugin reapplies this order before collection.
        if _PLUGIN not in plugins:
            plugins.append(_PLUGIN)
    # Unconditional: the shim carries pytest-timeout for every run, not only
    # the async ones, and resolves each name rather than asserting it.
    if _OPTIONAL_PLUGINS not in plugins:
        plugins.append(_OPTIONAL_PLUGINS)
    requested_optional_plugins = {
        item.strip()
        for item in str(env.get(OPTIONAL_PLUGINS_ENV, "") or "").split(",")
        if item.strip()
    }
    env.update(
        optional_plugin_env(
            # mutation_pytest_env is built before mutmut copies tests into its
            # generated workspace. Preserve a request already established from
            # the real checkout instead of treating absent generated files as
            # evidence that the async plugin is unnecessary.
            asyncio=(
                "pytest_asyncio.plugin" in requested_optional_plugins
                or _uses_pytest_marker(repo_resolved, test_paths, "asyncio")
            ),
            environ=env,
        )
    )
    # Same mutant wall-clock as pytest-timeout so python-enforce and repair
    # cannot disagree on whether a hanging test is a kill or a timeout.
    if not str(env.get("UTA_PYTHON_MUTATION_PER_MUTANT_TIMEOUT_SECONDS", "") or "").strip():
        shared = str(env.get("PYTEST_TIMEOUT", "") or "").strip()
        if shared:
            env["UTA_PYTHON_MUTATION_PER_MUTANT_TIMEOUT_SECONDS"] = shared
    if plugins:
        env["PYTEST_PLUGINS"] = ",".join(plugins)
    return env


def _uses_pytest_marker(repo: Path, test_paths: Sequence[str], marker: str) -> bool:
    pattern = re.compile(rf"\bpytest\.mark\.{re.escape(marker)}\b")
    for raw_path in test_paths:
        path = repo / str(raw_path or "").replace("\\", "/").strip()
        try:
            if pattern.search(path.read_text(encoding="utf-8")):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False


def mutation_pytest_env(
    mutants_dir: Path,
    test_paths: Sequence[str],
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """The same import roots, rooted inside mutmut's generated tree.

    mutmut runs the selected tests from a regenerated copy of the repository
    under `mutants/`, and spawns pytest itself, so the roots have to point at
    that copy and travel in the environment the child inherits.
    """
    mutation_base_env = dict(base_env)
    # Coverage may have populated roots from the original checkout. Reusing
    # those roots lets the startup plugin put unmutated source ahead of the
    # generated workspace, so mutmut sees passing tests but records no test
    # association for every mutant.
    mutation_base_env.pop("UTA_PYTEST_IMPORT_ROOTS", None)
    env = pytest_env(mutants_dir, test_paths, base_env=mutation_base_env)
    compat = str(env.get("UTA_MUTMUT_IMPORT_COMPAT_DIR") or "").strip()
    if not compat:
        return env
    # sitecustomize must win over a repository's own module of that name.
    existing = [item for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item and item != compat]
    env["PYTHONPATH"] = os.pathsep.join([compat, *existing])
    return env


__all__ = ["mutation_pytest_env", "pytest_env", "pytest_import_roots"]

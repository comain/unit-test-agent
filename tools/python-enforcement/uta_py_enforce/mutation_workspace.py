"""Build a focused mutmut workspace without copying the target over its mutant."""

from __future__ import annotations

import ast
import errno
from pathlib import Path
import shlex
from typing import Iterable, Mapping, Sequence


_EXCLUDED_ROOTS = {
    ".git", ".mypy_cache", ".pytest_cache", ".tox", ".uta_cache", ".venv",
    "__pycache__", "build", "dist", "mutants", "test", "tests", "venv",
}


def mutation_support_copy_paths(repo: Path, source_path: str, test_paths: Sequence[str] = ()) -> list[str]:
    """Return deterministic project files mutmut must copy beside the target."""
    source = Path(normalize_relpath(source_path))
    tests = {Path(normalize_relpath(path)) for path in test_paths}
    conftests = _active_pytest_conftest_paths(repo, tests)
    result: list[str] = []

    def add(path: Path, *, allow_selected_test: bool = False) -> None:
        normalized = Path(normalize_relpath(path.as_posix()))
        if not normalized.parts or normalized.parts[0] in _EXCLUDED_ROOTS:
            return
        # Never copy the repository root's own package marker. mutmut generates
        # its tree at `<repo>/mutants`, so an `__init__.py` there makes that
        # directory a package too -- and pytest's `prepend` import mode then
        # walks the basedir up out of `mutants/` and puts the *real* repository
        # on sys.path. Every test then imports the unmutated module: mutants are
        # generated and scored but nothing can ever kill them, which mutmut
        # reports as "could not find any test case for any mutant".
        if normalized == Path("__init__.py"):
            return
        if normalized == source or (normalized in tests and not allow_selected_test):
            return
        if path_contains(normalized, source):
            return
        value = normalized.as_posix()
        if value not in result:
            result.append(value)

    # Pytest loads conftest modules by test-path ancestry rather than ordinary
    # imports. Mutmut runs from ``mutants/``, so preserve those modules and
    # include their imports in the focused workspace dependency closure.
    for conftest in conftests:
        add(conftest)
    # Mutmut copies conventional root test directories itself. Tests nested
    # inside a source package are not copied by that path, so carry only the
    # selected file; copying their common package root would overwrite the
    # generated mutant and restore the unmutated source.
    for test_path in sorted(tests):
        if test_path.parts and test_path.parts[0] not in {"test", "tests"}:
            add(test_path, allow_selected_test=True)
    dependencies = _project_import_closure(repo, [source, *sorted(tests), *conftests])
    for dependency in dependencies:
        # The mutmut adapter creates destination parent directories before
        # copying support files. Copy the resolved module itself, not its whole
        # package: one imported module in a model repository can otherwise pull
        # hundreds of megabytes of unrelated weights and utilities into every
        # target workspace.
        add(dependency)
        _add_package_initializers(repo, dependency, add)
    for dependency in (
        *_project_literal_path_dependencies(
            repo,
            [source, *sorted(tests), *dependencies],
        ),
        *_file_relative_path_dependencies(
            repo,
            [source, *sorted(tests), *dependencies],
        ),
    ):
        dependency_path = repo / dependency
        # A string that happens to name a Python package is not evidence that
        # every module in that package is runtime data. Imported code is
        # already supplied by the exact import closure above; copying the whole
        # directory here made common constants such as ``"main/tools"`` pull
        # model repositories into every mutation workspace.
        if dependency_path.is_dir() and _contains_python_sources(dependency_path):
            continue
        add(dependency)
        _add_package_initializers(repo, dependency, add)
    _add_package_initializers(repo, source, add)
    for test_path in tests:
        _add_package_initializers(repo, test_path, add)

    for resource_root in _referenced_project_resource_roots(
        repo,
        [source, *sorted(tests), *dependencies],
    ):
        add(resource_root)
    return result


def _pytest_conftest_paths(repo: Path, test_paths: set[Path]) -> list[Path]:
    """Return existing conftest files on each selected test's ancestry."""
    paths: set[Path] = set()
    for test_path in test_paths:
        current = test_path.parent
        while True:
            candidate = current / "conftest.py"
            if (repo / candidate).is_file():
                paths.add(candidate)
            if current == Path("."):
                break
            current = current.parent
    return sorted(paths)


def _active_pytest_conftest_paths(repo: Path, test_paths: set[Path]) -> list[Path]:
    """Keep package-local conftests only when selected tests consume them."""
    conftests = _pytest_conftest_paths(repo, test_paths)
    if not conftests:
        return []
    if any(path.parts and path.parts[0] in {"test", "tests"} for path in test_paths):
        return conftests
    return conftests if _selected_tests_need_conftest(repo, test_paths, conftests) else []


def _selected_tests_need_conftest(
    repo: Path,
    test_paths: set[Path],
    conftest_paths: Sequence[Path],
) -> bool:
    requested: set[str] = set()
    for test_path in test_paths:
        tree = _read_python_tree(repo / test_path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                requested.update(arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
                requested.update(_usefixtures_names(node))

    fixtures: dict[str, set[str]] = {}
    for conftest_path in conftest_paths:
        tree = _read_python_tree(repo / conftest_path)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            fixture_name, autouse = _pytest_fixture_metadata(node)
            if fixture_name is None:
                continue
            fixtures[fixture_name] = {
                arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            }
            if autouse:
                requested.add(fixture_name)

    pending = list(requested)
    while pending:
        fixture_name = pending.pop()
        dependencies = fixtures.get(fixture_name)
        if dependencies is None:
            continue
        for dependency in dependencies:
            if dependency not in requested:
                requested.add(dependency)
                pending.append(dependency)
    return bool(requested.intersection(fixtures))


def _read_python_tree(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _usefixtures_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or not _dotted_name(decorator.func).endswith("usefixtures"):
            continue
        names.update(
            arg.value for arg in decorator.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        )
    return names


def _pytest_fixture_metadata(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str | None, bool]:
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        if not _dotted_name(target).endswith("fixture"):
            continue
        name = node.name
        autouse = False
        for keyword in call.keywords if call is not None else ():
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                name = keyword.value.value
            if keyword.arg == "autouse" and isinstance(keyword.value, ast.Constant):
                autouse = keyword.value.value is True
        return name, autouse
    return None, False


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def focused_pytest_runner(python_bin: str, test_paths: Sequence[str]) -> str:
    """Return the exact pytest command mutmut should run for each mutant."""
    from uta_py_enforce.pytest_env import pytest_process_command

    command = pytest_process_command(python_bin, ["-x", "--assert=plain", *map(str, test_paths)])
    return " ".join(shlex.quote(part) for part in command)


def mutmut_pytest_add_cli_args(repo: Path, test_paths: Sequence[str]) -> tuple[str, ...]:
    """Return pytest flags that apply independently of test selection.

    Test paths belong only in mutmut's ``tests_dir`` setting. Mutmut 3 appends
    that setting to its test-selection arguments itself; repeating a path in
    either pytest argument setting executes the selected tests twice.
    """
    tests = {Path(normalize_relpath(path)) for path in test_paths}
    return ("--noconftest",) if _mutmut_disables_conftest(repo, tests) else ()


def normalize_relpath(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _mutmut_disables_conftest(repo: Path, test_paths: set[Path]) -> bool:
    """Match the package-local conftest isolation used by mutmut pytest runs."""
    all_conftests = _pytest_conftest_paths(repo, test_paths)
    return bool(all_conftests) and not _active_pytest_conftest_paths(repo, test_paths)


def _project_import_closure(repo: Path, roots: Iterable[Path]) -> list[Path]:
    root_paths = list(roots)
    pending = [path for root in root_paths for path in (root, *_package_initializers(repo, root))]
    visited: set[Path] = set()
    dependencies: list[Path] = []
    while pending:
        current = pending.pop(0)
        if current in visited:
            continue
        visited.add(current)
        try:
            tree = ast.parse((repo / current).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for module_name, level in _imported_modules(tree):
            resolved = _resolve_project_module(repo, current, module_name, level)
            if resolved is None or resolved in visited:
                continue
            if resolved not in dependencies:
                dependencies.append(resolved)
            pending.append(resolved)
            pending.extend(_package_initializers(repo, resolved))
        for module_name in _literal_module_names(tree):
            resolved = _resolve_literal_project_module(repo, current, module_name)
            if resolved is None or resolved in visited:
                continue
            if resolved not in dependencies:
                dependencies.append(resolved)
            pending.append(resolved)
            pending.extend(_package_initializers(repo, resolved))
    return dependencies


def _literal_module_names(tree: ast.AST) -> Iterable[str]:
    """Yield dotted local-module candidates used by dynamic configuration."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value.strip()
        parts = value.split(".")
        if len(parts) > 1 and all(part.isidentifier() for part in parts):
            yield value


def _project_literal_path_dependencies(repo: Path, code_paths: Iterable[Path]) -> list[Path]:
    """Find repository-relative files that selected code opens directly.

    Mutmut runs pytest from ``mutants/``. Imports are handled separately, but
    targets and tests may inspect source, fixtures, templates, or package data
    through relative paths. Copy only literal paths that resolve inside the
    repository; dynamic paths remain fail-closed and are reported as
    workspace/tool failures.
    """

    dependencies: list[Path] = []
    for code_path in code_paths:
        try:
            tree = ast.parse((repo / code_path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            values = []
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                values.append(node.value)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                parts = _path_expression_parts(node)
                if parts:
                    values.append("/".join(parts))
            for value in values:
                candidate = _literal_repo_path(repo, value)
                if candidate is None:
                    candidate = _literal_repo_path(repo, (code_path.parent / value).as_posix())
                if candidate is not None and candidate not in dependencies:
                    dependencies.append(candidate)
    return dependencies


def _path_expression_parts(node: ast.AST) -> list[str]:
    """Collect static path segments from a ``Path / 'dir' / 'file'`` chain."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [*_path_expression_parts(node.left), *_path_expression_parts(node.right)]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = normalize_relpath(node.value)
        if value and "\n" not in value and "://" not in value:
            return [part for part in Path(value).parts if part not in {"", "."}]
    return []


def _file_relative_path_dependencies(repo: Path, code_paths: Iterable[Path]) -> list[Path]:
    """Find resources opened through ``__file__``-relative paths.

    Literal scanners reject ``..`` and f-strings. Import-time loads such as
    ``cfg.read(f'{FILE_DIR}/../config/config.cfg')`` after
    ``FILE_DIR = os.path.dirname(os.path.abspath(__file__))`` then vanish from
    the mutmut workspace, and the clean test dies before any mutant is scored.
    """
    dependencies: list[Path] = []
    for code_path in code_paths:
        try:
            tree = ast.parse((repo / code_path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        env = _file_relative_names(tree, code_path)
        for node in ast.walk(tree):
            origin = _file_relative_origin(node, env, code_path)
            resolved = _existing_repo_path(repo, origin) if origin is not None else None
            if resolved is not None and resolved not in dependencies:
                dependencies.append(resolved)
    return dependencies


def _file_relative_names(tree: ast.AST, code_path: Path) -> dict[str, Path]:
    env: dict[str, Path] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            origin = _file_relative_origin(node.value, env, code_path)
            if origin is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    env[target.id] = origin
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            origin = _file_relative_origin(node.value, env, code_path)
            if origin is not None:
                env[node.target.id] = origin
    return env


def _file_relative_origin(node: ast.AST, env: Mapping[str, Path], code_path: Path) -> Path | None:
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return code_path
        return env.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        origin = _file_relative_origin(node.value, env, code_path)
        return None if origin is None else origin.parent
    if isinstance(node, ast.Call):
        leaf = _call_leaf(node.func)
        if leaf in {"abspath", "realpath", "normpath", "resolve"} and node.args:
            return _file_relative_origin(node.args[0], env, code_path)
        if leaf == "resolve" and isinstance(node.func, ast.Attribute) and not node.args:
            return _file_relative_origin(node.func.value, env, code_path)
        if leaf == "dirname" and node.args:
            origin = _file_relative_origin(node.args[0], env, code_path)
            return None if origin is None else origin.parent
        if leaf in {"Path", "PurePath", "PurePosixPath"} and node.args:
            return _file_relative_origin(node.args[0], env, code_path)
        if leaf == "join" and node.args:
            origin = _file_relative_origin(node.args[0], env, code_path)
            if origin is None:
                return None
            parts: list[str] = []
            for argument in node.args[1:]:
                value = _static_string(argument)
                if value is None:
                    return None
                parts.append(value)
            return _norm_join(origin, *parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        origin = _file_relative_origin(node.left, env, code_path)
        suffix = _static_string(node.right)
        if origin is None or suffix is None:
            return None
        return _norm_join(origin, suffix)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        origin = _file_relative_origin(node.left, env, code_path)
        suffix = _static_string(node.right)
        if origin is None or suffix is None:
            return None
        return _norm_join(origin, suffix)
    if isinstance(node, ast.JoinedStr):
        return _file_relative_joined_origin(node, env, code_path)
    return None


def _file_relative_joined_origin(
    node: ast.JoinedStr,
    env: Mapping[str, Path],
    code_path: Path,
) -> Path | None:
    if not node.values:
        return None
    first = node.values[0]
    origin = None
    rest: list[ast.AST] = list(node.values)
    if isinstance(first, ast.FormattedValue):
        origin = _file_relative_origin(first.value, env, code_path)
        rest = node.values[1:]
    elif (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and len(node.values) > 1
        and isinstance(node.values[1], ast.FormattedValue)
    ):
        # Rare ``f'prefix{FILE_DIR}/../config/config.cfg'`` — only the
        # formatted path is authoritative.
        return None
    if origin is None:
        return None
    parts: list[str] = []
    for value in rest:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
            continue
        return None
    return _norm_join(origin, *parts) if parts else origin


def _call_leaf(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _norm_join(base: Path, *parts: str) -> Path:
    current = list(base.parts)
    for part in parts:
        for segment in part.replace("\\", "/").split("/"):
            if not segment or segment == ".":
                continue
            if segment == "..":
                if current:
                    current.pop()
                continue
            current.append(segment)
    return Path(*current) if current else Path()


def _existing_repo_path(repo: Path, relative: Path) -> Path | None:
    parts: list[str] = []
    for part in relative.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        return None
    normalized = Path(*parts)
    try:
        return normalized if (repo / normalized).exists() else None
    except OSError as error:
        if error.errno != errno.ENAMETOOLONG:
            raise
        return None


def _literal_repo_path(repo: Path, value: str) -> Path | None:
    raw = normalize_relpath(value)
    if not raw or "\n" in raw or "://" in raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    # Plain words in assertions are not file dependencies. Requiring a path
    # separator or suffix keeps this scan conservative.
    if "/" not in raw and not candidate.suffix:
        return None
    try:
        return candidate if (repo / candidate).exists() else None
    except OSError as error:
        # Parent-join of a long string literal (policy answers, prompts)
        # can exceed NAME_MAX. That is not a file dependency.
        if error.errno != errno.ENAMETOOLONG:
            raise
        return None


def _imported_modules(tree: ast.AST) -> Iterable[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, 0
        elif isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            level = int(node.level or 0)
            yield module, level
            # `from package import submodule` names the submodule in `names`,
            # not in `module`. Yielding only the package resolved to its
            # `__init__.py` and left the submodule out of the copy set, so
            # mutmut's generated tree lacked a module the selected tests import
            # and every mutant died at collection with an ImportError.
            #
            # Imported *attributes* (functions, classes, constants) simply do
            # not resolve to a file, so they cost one lookup and are dropped.
            for alias in node.names:
                if alias.name == "*":
                    continue
                yield (f"{module}.{alias.name}" if module else alias.name), level


def _resolve_project_module(repo: Path, importer: Path, module_name: str, level: int) -> Path | None:
    module_parts = tuple(part for part in module_name.split(".") if part)
    candidates: list[Path] = []
    if level:
        package = importer.parent.parts
        keep = len(package) - max(level - 1, 0)
        if keep >= 0:
            candidates.append(Path(*package[:keep], *module_parts))
    else:
        # Some repositories put a package ancestor on sys.path and use imports
        # such as ``from config import settings`` from ``app/nested/target.py``.
        # Resolve that exact module at each ancestor instead of copying every
        # sibling package (or every Python tree in the repository) into each
        # mutmut workspace. The broad fallback made a 100-mutant CI run copy a
        # 644 MB repository once per target and hit the report timeout.
        ancestor_roots = [
            importer.parent.parts[:depth]
            for depth in range(len(importer.parent.parts), 0, -1)
        ]
        candidates.extend(Path(*root, *module_parts) for root in ancestor_roots)
        candidates.extend(Path(*(root + module_parts)) for root in ((), ("src",)))
    for candidate in candidates:
        for path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
            if (repo / path).is_file():
                return path
    return None


def _resolve_literal_project_module(repo: Path, importer: Path, module_name: str) -> Path | None:
    """Resolve the longest importable prefix of a dotted configuration value.

    Framework settings commonly name an object rather than only its module,
    for example ``common.middleware.RequestMetricsMiddleware``. The mutation
    workspace needs the module file, while the final class or function segment
    is not itself importable.
    """
    parts = tuple(part for part in module_name.split(".") if part)
    for length in range(len(parts), 0, -1):
        resolved = _resolve_project_module(repo, importer, ".".join(parts[:length]), 0)
        if resolved is not None:
            return resolved
    return None


def _add_package_initializers(repo: Path, path: Path, add) -> None:
    for initializer in _package_initializers(repo, path):
        add(initializer)


def _package_initializers(repo: Path, path: Path) -> list[Path]:
    result: list[Path] = []
    current = path.parent
    while current != Path("."):
        initializer = current / "__init__.py"
        if (repo / initializer).is_file():
            result.append(initializer)
        current = current.parent
    return result


def path_contains(parent: Path, child: Path) -> bool:
    return len(parent.parts) <= len(child.parts) and child.parts[: len(parent.parts)] == parent.parts


def _contains_python_sources(path: Path) -> bool:
    return any(candidate.is_file() and candidate.suffix == ".py" for candidate in path.rglob("*.py"))


def _referenced_project_resource_roots(repo: Path, code_paths: Sequence[Path]) -> list[Path]:
    """Find top-level non-code directories explicitly named by selected code.

    Mutmut executes tests with ``mutants/`` as the project root. A selected test
    may legitimately read templates or other repository data through a path
    literal; those resources must follow the mutant workspace just like imported
    Python dependencies. Only roots named by the target/import closure/selected
    tests qualify, so unrelated data directories are not copied.
    """
    referenced_roots: set[str] = set()
    for code_path in code_paths:
        try:
            tree = ast.parse((repo / code_path).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            value = getattr(node, "value", None)
            if not isinstance(value, str):
                continue
            normalized = value.strip().replace("\\", "/")
            if not normalized or "://" in normalized or normalized.startswith("/"):
                continue
            while normalized.startswith("./"):
                normalized = normalized[2:]
            referenced_roots.add(normalized.split("/", 1)[0])

    return [
        child.relative_to(repo)
        for child in sorted(repo.iterdir(), key=lambda item: item.name)
        if child.is_dir()
        and child.name not in _EXCLUDED_ROOTS
        and child.name in referenced_roots
        and not _contains_python_sources(child)
    ]

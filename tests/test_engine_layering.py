"""Architecture-invariant guard: the language-agnostic layers must not bind to
language backends at module-load time.

The whole point of the engine layer is that core workflow/task/report code is
language-agnostic and reaches Java/Python only through dispatch seams. These
tests fail the build if that boundary is crossed, so a regression like a
JaCoCo/Maven runner being imported straight into the task manager (the
project-coverage recompute leak) cannot silently return.

Boundaries enforced here:

- ``uta/engine`` — the shared engine surface stays language-free. Backends may
  only be reached through the lazy dispatch factories
  (``default_*_registry`` / ``make_*_provider``), i.e. *function-local* imports,
  or via typing-only ``TYPE_CHECKING`` imports. A top-level
  ``import uta.language.*`` is a layering leak.
- ``uta/tasks`` — the task layer must not reference any language backend at all
  (module-level or lazy).

Out of scope: ``uta/testgen/graph`` is intentionally not scanned. The LangGraph
workflow in ``uta/testgen/graph/nodes.py`` is currently the Java backend's workflow and
``uta/testgen/graph/state.py`` carries the Java ``CodeGraph`` type, so that package is a
known language coupling rather than an agnostic layer.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

import uta

UTA_ROOT = Path(uta.__file__).resolve().parent
LANGUAGE_PREFIX = "uta.language"


def _iter_py_files(package: str) -> List[Path]:
    return sorted((UTA_ROOT / package).rglob("*.py"))


def _language_modules(node: ast.AST) -> List[str]:
    modules: List[str] = []
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        if module == LANGUAGE_PREFIX or module.startswith(LANGUAGE_PREFIX + "."):
            modules.append(module)
    elif isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == LANGUAGE_PREFIX or alias.name.startswith(LANGUAGE_PREFIX + "."):
                modules.append(alias.name)
    return modules


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _module_level_language_imports(tree: ast.Module) -> List[Tuple[int, str]]:
    """Language imports that execute at import time.

    Descends through module-level compound statements (``if``/``try``/``with``/
    class bodies) but never into function bodies — function-local imports are the
    sanctioned lazy-dispatch seam — and skips ``if TYPE_CHECKING:`` blocks, whose
    imports never run.
    """
    violations: List[Tuple[int, str]] = []

    def scan(body: List[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(stmt, ast.If) and _is_type_checking_guard(stmt):
                continue
            for module in _language_modules(stmt):
                violations.append((stmt.lineno, module))
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list):
                    scan(inner)
            for handler in getattr(stmt, "handlers", []) or []:
                scan(handler.body)

    scan(tree.body)
    return violations


def _all_language_imports(tree: ast.Module) -> List[Tuple[int, str]]:
    violations: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        for module in _language_modules(node):
            violations.append((getattr(node, "lineno", 0), module))
    return violations


def _format(offenders: dict) -> str:
    lines = []
    for rel_path, hits in sorted(offenders.items()):
        for lineno, module in hits:
            lines.append(f"  {rel_path}:{lineno} -> {module}")
    return "\n".join(lines)


def test_engine_layer_has_no_module_level_language_imports():
    offenders = {}
    for path in _iter_py_files("engine"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        violations = _module_level_language_imports(tree)
        if violations:
            offenders[path.relative_to(UTA_ROOT).as_posix()] = violations
    assert not offenders, (
        "uta/engine must not import uta.language.* at module load time. "
        "Reach backends through the lazy dispatch factories "
        "(default_*_registry / make_*_provider) or a TYPE_CHECKING import:\n"
        + _format(offenders)
    )


def test_tasks_layer_has_no_language_imports():
    offenders = {}
    for path in _iter_py_files("tasks"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        violations = _all_language_imports(tree)
        if violations:
            offenders[path.relative_to(UTA_ROOT).as_posix()] = violations
    assert not offenders, (
        "uta/tasks must not reference any language backend (uta.language.*). "
        "Dispatch language behavior through an engine registry instead:\n"
        + _format(offenders)
    )

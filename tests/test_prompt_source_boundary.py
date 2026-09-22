"""Keep product prompt construction behind agent-core's public API."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
# `uta/app/reporting.py` is deliberately outside these roots: its Jinja HTML
# rendering is a product view, not construction of text sent to an agent.
PROMPT_SOURCE_ROOTS = (ROOT / "uta" / "testgen", ROOT / "uta" / "language")


def _production_modules():
    for source_root in PROMPT_SOURCE_ROOTS:
        yield from source_root.rglob("*.py")


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def test_agent_prompt_sources_do_not_import_jinja_directly():
    offenders = []
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            if any(name == "jinja2" or name.startswith("jinja2.") for name in imported):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert offenders == [], "direct Jinja prompt rendering:\n  " + "\n  ".join(offenders)


def test_agent_prompt_sources_do_not_write_prompt_files_directly():
    offenders = []
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "write_text":
                continue
            receiver = _qualified_name(node.func.value).lower()
            ancestor = parents.get(node)
            while ancestor is not None and not isinstance(
                ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                ancestor = parents.get(ancestor)
            function_name = ancestor.name.lower() if ancestor is not None else ""
            if "prompt" in receiver or "prompt" in function_name:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert offenders == [], "direct prompt artifact writes:\n  " + "\n  ".join(offenders)

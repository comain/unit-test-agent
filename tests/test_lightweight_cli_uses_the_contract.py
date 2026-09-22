"""The local CLI must dispatch through the contract, not run its own gate.

Success criterion 4: `tool/CLI -> uta_enforce_core -> selected binding`, with no
tool-local parallel orchestration path. The CLI used to import `run_coverage`,
`run_mutation` and `strict_test_candidates` and drive them itself, which is a
second implementation of the thing the contract exists to own -- and the one
local developers actually run.

These tests are about *wiring*, so they assert on imports and on the dispatch
being reached. The behaviour they must not change is covered by the existing
standalone-enforcer suite and the frozen evidence baselines.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = REPO_ROOT / "tools" / "python-enforcement"
CLI = TOOLS_ROOT / "uta_py_enforce" / "cli.py"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_the_cli_does_not_import_the_execution_modules():
    """Importing them is how a composition adapter becomes an orchestrator."""
    forbidden = {
        "uta_py_enforce.coverage",
        "uta_py_enforce.mutation",
        "uta_py_enforce.test_selection",
        "uta_py_enforce.mutation_workspace",
    }
    assert _imported_modules(CLI) & forbidden == set()


def test_the_cli_reaches_the_binding_through_the_contract():
    modules = _imported_modules(CLI)
    assert "uta_enforce_core.dispatch" in modules or "uta_enforce_core" in modules
    assert "uta_py_enforce.api" in modules


def test_the_cli_builds_a_registry_and_dispatches_once():
    source = CLI.read_text(encoding="utf-8")
    assert "enforce(" in source, "the CLI never calls the contract's dispatch"
    assert source.count("create_python_enforcement_binding") >= 1


def test_dispatch_has_a_production_caller():
    """`uta_enforce_core.dispatch` had zero non-test importers, which made the
    contract ornamental -- present, tested, and bypassed by everything real."""
    callers = subprocess.run(
        ["grep", "-rln", "uta_enforce_core.dispatch", "uta", "tools"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.split()
    # Not the contract package importing itself -- that proves nothing.
    production = [
        c for c in callers
        if not c.startswith("tests/") and "uta_enforce_core" not in c
    ]
    assert production, "no consumer outside the contract imports the dispatch"


@pytest.mark.parametrize("flag", ["--help"])
def test_the_cli_still_runs_from_a_sparse_tree(tmp_path, flag):
    """The distribution boundary is the point: this must work with only
    `tools/python-enforcement` on disk and no UTA importable."""
    sparse = tmp_path / "sparse"
    subprocess.run(["cp", "-R", str(TOOLS_ROOT), str(sparse)], check=True)

    completed = subprocess.run(
        [sys.executable, str(sparse / "uta_python_test_enforce.py"), flag],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(sparse), "HOME": str(tmp_path)},
    )
    assert completed.returncode == 0, completed.stderr

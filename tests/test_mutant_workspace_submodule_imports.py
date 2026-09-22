"""`from package import submodule` has to reach the submodule.

The import extractor yielded only `node.module` for an `ImportFrom`, so
`from pkg.sub import mod` resolved to `pkg/sub/__init__.py` and never to
`pkg/sub/mod.py`. The submodule was left out of mutmut's copied tree, and every
mutant for the target died at collection with an ImportError -- reported as
`mutation_backend_failed`. Thirteen targets in one production report were lost
this way, holding the report's mutation score ~9 points below production's.
"""

from __future__ import annotations

import ast
from pathlib import Path

from uta_py_enforce.mutation_workspace import (
    _imported_modules,
    mutation_support_copy_paths,
    mutmut_pytest_add_cli_args,
)


def test_selected_test_conftest_and_its_imports_are_copied(tmp_path):
    files = {
        "conftest.py": (
            "import os\n"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'staff_service.settings.test')\n"
        ),
        "staff_service/settings/__init__.py": "\n",
        "staff_service/settings/base.py": (
            "MIDDLEWARE = ['common.middleware.RequestMetricsMiddleware']\n"
        ),
        "staff_service/settings/test.py": "from .base import *\n",
        "common/auth.py": "def trusted_user(): return True\n",
        "common/middleware.py": "class RequestMetricsMiddleware: pass\n",
        "tests/test_auth.py": "from common.auth import trusted_user\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    paths = mutation_support_copy_paths(
        tmp_path,
        "common/auth.py",
        ("tests/test_auth.py",),
    )

    assert "conftest.py" in paths
    assert "staff_service/settings/test.py" in paths
    assert "staff_service/settings/base.py" in paths
    assert "staff_service/settings/__init__.py" in paths
    assert "common/middleware.py" in paths


def test_package_local_conftest_is_kept_when_selected_test_requires_its_fixture(tmp_path):
    source = tmp_path / "pipecat" / "sop" / "engine.py"
    test_file = tmp_path / "pipecat" / "tests" / "test_engine.py"
    conftest = test_file.parent / "conftest.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_text("def run():\n    return True\n", encoding="utf-8")
    test_file.write_text(
        "def test_run(sop_engine):\n"
        "    assert sop_engine.run()\n",
        encoding="utf-8",
    )
    conftest.write_text(
        "import pytest\n\n"
        "@pytest.fixture\n"
        "def sop_engine():\n"
        "    from pipecat.sop import engine\n"
        "    return engine\n",
        encoding="utf-8",
    )

    paths = mutation_support_copy_paths(
        tmp_path,
        "pipecat/sop/engine.py",
        ("pipecat/tests/test_engine.py",),
    )
    pytest_args = mutmut_pytest_add_cli_args(
        tmp_path,
        ("pipecat/tests/test_engine.py",),
    )

    assert "pipecat/tests/conftest.py" in paths
    assert pytest_args == ()


def test_unneeded_package_local_conftest_remains_isolated(tmp_path):
    source = tmp_path / "pipecat" / "sop" / "engine.py"
    test_file = tmp_path / "pipecat" / "tests" / "test_engine.py"
    conftest = test_file.parent / "conftest.py"
    source.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source.write_text("def run():\n    return True\n", encoding="utf-8")
    test_file.write_text("def test_run():\n    assert True\n", encoding="utf-8")
    conftest.write_text("import unavailable_project_bootstrap\n", encoding="utf-8")

    paths = mutation_support_copy_paths(
        tmp_path,
        "pipecat/sop/engine.py",
        ("pipecat/tests/test_engine.py",),
    )
    pytest_args = mutmut_pytest_add_cli_args(
        tmp_path,
        ("pipecat/tests/test_engine.py",),
    )

    assert "pipecat/tests/conftest.py" not in paths
    assert pytest_args == ("--noconftest",)


def test_ancestor_import_root_support_is_copied_without_overwriting_target(tmp_path):
    import shutil
    import subprocess
    import sys

    for name, content in {"app/config.py": "settings = 1", "app/nested/target.py": "from config import settings", "app/skeleton/__init__.py": ""}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    paths = mutation_support_copy_paths(tmp_path, "app/nested/target.py")
    assert "app/config.py" in paths
    assert "app/skeleton" not in paths
    assert "app" not in paths
    assert "app/nested" not in paths
    sandbox = tmp_path / "mutants"
    target = sandbox / "app/nested/target.py"
    target.parent.mkdir(parents=True)
    target.write_text("from config import settings\nMUTANT = True\n")
    for path in paths:
        src, dst = tmp_path / path, sandbox / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'app'); "
         "from app.nested import target; assert target.settings == 1; assert target.MUTANT"],
        cwd=sandbox, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_mutation_support_does_not_copy_unrelated_python_trees(tmp_path):
    """A small target must not rebuild a large repository for every mutant run."""
    files = {
        "main/config.py": "settings = 1\n",
        "main/shared/target.py": "from config import settings\n",
        "main/unrelated/worker.py": "VALUE = 1\n",
        "vendor/large_framework/module.py": "VALUE = 2\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    paths = mutation_support_copy_paths(tmp_path, "main/shared/target.py")

    assert "main/config.py" in paths
    assert "main/unrelated" not in paths
    assert "vendor" not in paths


def test_imported_module_copies_the_file_not_its_whole_package(tmp_path):
    files = {
        "app/target.py": "from support.helper import VALUE\n",
        "support/__init__.py": "\n",
        "support/helper.py": "VALUE = 1\n",
        "support/large_unrelated.py": "VALUE = 2\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    paths = mutation_support_copy_paths(tmp_path, "app/target.py")

    assert "support/helper.py" in paths
    assert "support/__init__.py" in paths
    assert "support" not in paths
    assert "support/large_unrelated.py" not in paths


def test_package_local_tests_do_not_copy_their_common_source_root(tmp_path):
    files = {
        "main/__init__.py": "\n",
        "main/handlers/__init__.py": "\n",
        "main/handlers/target.py": "def run(): return 1\n",
        "main/test/test_target.py": "from main.handlers.target import run\n",
        "main/unrelated/large_model.py": "VALUE = 1\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    paths = mutation_support_copy_paths(
        tmp_path,
        "main/handlers/target.py",
        ("main/test/test_target.py",),
    )

    assert "main/test/test_target.py" in paths
    assert "main" not in paths
    assert "main/unrelated" not in paths
    assert "main/handlers/target.py" not in paths


def test_incomplete_mutation_evidence_does_not_show_success_headline():
    from uta.enforcement.evidence import _structured_mutation_detail
    detail = _structured_mutation_detail(
        {"scope": "changed_lines", "gateScope": "report", "passed": True,
         "killed": 2, "generated": 10, "changedLineMutantsScored": 2},
        target_results=[{"reasonCode": "mutation_backend_failed", "mutation": {
            "generated": 8, "changedLineMutantsScored": 0, "passed": False}}],
    )
    assert detail["formattedRate"].startswith("Incomplete")
    assert detail["passed"] is False
    assert detail["unexecuted"] == 8


def test_a_submodule_import_names_the_submodule():
    tree = ast.parse("from pkg.sub import mod\n")

    assert ("pkg.sub.mod", 0) in set(_imported_modules(tree))


def test_the_package_itself_is_still_yielded():
    """The package's `__init__` may carry the import the module relies on."""
    tree = ast.parse("from pkg.sub import mod\n")

    assert ("pkg.sub", 0) in set(_imported_modules(tree))


def test_relative_submodule_imports_keep_their_level():
    tree = ast.parse("from . import sibling\nfrom .rel import inner\n")
    found = set(_imported_modules(tree))

    assert ("sibling", 1) in found
    assert ("rel.inner", 1) in found


def test_a_star_import_does_not_invent_a_module():
    tree = ast.parse("from pkg import *\n")

    assert all(name != "pkg.*" for name, _ in _imported_modules(tree))


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    pkg = repo / "app" / "adaptors"
    pkg.mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "target.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_target.py").write_text(
        "from app.adaptors import helper\nfrom app.adaptors import target\n\n\n"
        "def test_run():\n    assert target.run() == helper.VALUE\n",
        encoding="utf-8",
    )
    return repo


def test_the_sibling_module_a_test_imports_is_copied(tmp_path):
    """End to end: the copy set must carry the module, not just its package."""
    repo = _repo(tmp_path)

    paths = mutation_support_copy_paths(
        repo, "app/adaptors/target.py", ["tests/test_target.py"]
    )

    assert "app/adaptors/helper.py" in paths
    # The target itself is mutated, never copied beside itself.
    assert "app/adaptors/target.py" not in paths


def test_an_imported_attribute_does_not_become_a_copy_path(tmp_path):
    """`from module import CONSTANT` must not fabricate `module/CONSTANT.py`."""
    repo = _repo(tmp_path)
    (repo / "tests" / "test_target.py").write_text(
        "from app.adaptors.helper import VALUE\nfrom app.adaptors import target\n\n\n"
        "def test_run():\n    assert target.run() == VALUE\n",
        encoding="utf-8",
    )

    paths = mutation_support_copy_paths(
        repo, "app/adaptors/target.py", ["tests/test_target.py"]
    )

    assert "app/adaptors/helper.py" in paths
    assert not any(path.endswith("VALUE.py") for path in paths)

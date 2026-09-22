"""Tests for promoted strict Python test selection across src, flat, and namespace layouts."""

from __future__ import annotations

from pathlib import Path

from uta_py_enforce.test_selection import (
    is_strict_test_path,
    select_python_test_destination,
    strict_test_candidates,
)
from uta.language.python.test_selection import (
    select_python_test_destination as uta_select_destination,
)
from uta.shared.targets import TargetRef


def test_test_selection_flat_layout(tmp_path: Path):
    (tmp_path / "foo.py").write_text("def bar(): pass")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("import foo\ndef test_bar(): pass")
    (tmp_path / "tests" / "test_smoke.py").write_text("def test_smoke(): pass")

    candidates = strict_test_candidates(tmp_path, "foo.py")
    assert candidates == ["tests/test_foo.py"]

    # Broad test excluded
    assert not is_strict_test_path("tests/test_smoke.py", "foo", "tests/uta_generated/test_foo.py")


def test_test_selection_src_layout(tmp_path: Path):
    (tmp_path / "src" / "my_pkg").mkdir(parents=True)
    (tmp_path / "src" / "my_pkg" / "__init__.py").write_text("")
    (tmp_path / "src" / "my_pkg" / "service.py").write_text("class MyService: pass")

    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_service.py").write_text("from my_pkg.service import MyService")
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    (tmp_path / "tests" / "e2e" / "test_service_e2e.py").write_text("from my_pkg.service import MyService")

    candidates = strict_test_candidates(
        tmp_path,
        "src/my_pkg/service.py",
        symbol="MyService",
    )
    assert candidates == ["tests/unit/test_service.py"]


def test_test_selection_five_result_limit(tmp_path: Path):
    (tmp_path / "math_utils.py").write_text("def add(): pass")
    (tmp_path / "tests").mkdir()
    for i in range(10):
        (tmp_path / "tests" / f"test_math_utils_{i}.py").write_text("import math_utils")

    candidates = strict_test_candidates(tmp_path, "math_utils.py", max_results=5)
    assert len(candidates) <= 5


def test_uta_facade_parity(tmp_path: Path):
    (tmp_path / "auth.py").write_text("class Auth: pass")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_auth.py").write_text("import auth\ndef test_auth(): pass")

    target = TargetRef(
        language="python",
        target_id="pyfile:auth.py",
        source_path="auth.py",
        display_name="auth.py",
        symbol="Auth",
    )
    
    # Standalone uta_py_enforce call
    legacy_dest = select_python_test_destination(tmp_path, target)
    
    # UTA facade call
    uta_dest = uta_select_destination(tmp_path, target)
    
    assert legacy_dest.path == uta_dest.path
    assert legacy_dest.path == "tests/test_auth.py"
    assert legacy_dest.uses_existing_test is True
    assert uta_dest.uses_existing_test is True


def test_generic_module_stem_requires_matching_package(tmp_path: Path):
    qa_test = tmp_path / "qa_knowledge" / "tests" / "test_urls.py"
    qa_test.parent.mkdir(parents=True)
    qa_test.write_text("from staff_service import urls\n", encoding="utf-8")

    staff = TargetRef(language="python", target_id="pyfile:staff_service/urls.py",
                      source_path="staff_service/urls.py", display_name="staff_service/urls.py")
    qa = TargetRef(language="python", target_id="pyfile:qa_knowledge/urls.py",
                   source_path="qa_knowledge/urls.py", display_name="qa_knowledge/urls.py")

    assert select_python_test_destination(tmp_path, staff).path == (
        "tests/uta_generated/test_staff_service_urls.py"
    )
    assert strict_test_candidates(tmp_path, "staff_service/urls.py", configured=["qa_knowledge/tests/test_urls.py"]) == []
    assert select_python_test_destination(tmp_path, qa).path == "qa_knowledge/tests/test_urls.py"


def test_nested_generic_module_accepts_package_local_exact_import(tmp_path: Path):
    tests_dir = tmp_path / "qa_knowledge" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_models.py").write_text(
        "from qa_knowledge.runtime.models import KnowledgeRecord\n", encoding="utf-8"
    )
    (tests_dir / "test_settings.py").write_text(
        "from qa_knowledge.runtime.settings import RAGFLOW_DATASET_ID\n", encoding="utf-8"
    )

    assert strict_test_candidates(tmp_path, "qa_knowledge/runtime/models.py") == [
        "qa_knowledge/tests/test_models.py"
    ]
    assert strict_test_candidates(tmp_path, "qa_knowledge/runtime/settings.py") == [
        "qa_knowledge/tests/test_settings.py"
    ]

    (tests_dir / "test_models.py").write_text("from qa_knowledge.other.models import Record\n", encoding="utf-8")
    assert strict_test_candidates(tmp_path, "qa_knowledge/runtime/models.py") == []

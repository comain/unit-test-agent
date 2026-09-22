"""The repository root's package marker must not reach the mutant workspace.

mutmut generates its tree at ``<repo>/mutants``. If a root ``__init__.py`` is
copied there, that directory becomes a package too, and pytest's ``prepend``
import mode walks the basedir up out of ``mutants/`` and puts the *real*
repository on ``sys.path``. Every test then imports the unmutated module, so
mutants are generated and scored but none can ever be killed -- mutmut reports
"could not find any test case for any mutant" and the target scores 0%.

Nine targets in the replay of production report 37f46d55 scored 0% this way
where production scored 100%, on identical mutant counts.
"""

from __future__ import annotations

from pathlib import Path

from uta_py_enforce.mutation_workspace import mutation_support_copy_paths


def _repo(tmp_path: Path, *, root_package: bool) -> Path:
    repo = tmp_path / "repo"
    (repo / "common").mkdir(parents=True)
    (repo / "common" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "common" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "common" / "target.py").write_text(
        "from common import helper\n\n\ndef run():\n    return helper.VALUE\n",
        encoding="utf-8",
    )
    tests_dir = repo / "test"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_target.py").write_text(
        "from common import target\n\n\ndef test_run():\n    assert target.run() == 1\n",
        encoding="utf-8",
    )
    if root_package:
        (repo / "__init__.py").write_text("", encoding="utf-8")
    return repo


def test_the_root_package_marker_is_not_copied(tmp_path):
    repo = _repo(tmp_path, root_package=True)

    paths = mutation_support_copy_paths(repo, "common/target.py", ["test/test_target.py"])

    assert "__init__.py" not in paths


def test_package_markers_inside_the_tree_are_still_copied(tmp_path):
    """Only the root marker is dangerous; nested ones make imports work."""
    repo = _repo(tmp_path, root_package=True)

    paths = mutation_support_copy_paths(repo, "common/target.py", ["test/test_target.py"])

    assert "common/__init__.py" in paths


def test_a_repository_without_a_root_package_is_unaffected(tmp_path):
    repo = _repo(tmp_path, root_package=False)

    paths = mutation_support_copy_paths(repo, "common/target.py", ["test/test_target.py"])

    assert "__init__.py" not in paths
    assert "common/__init__.py" in paths
    assert "common/helper.py" in paths

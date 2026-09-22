"""A rename must be checked against the path that now exists.

`git status --porcelain=1 -z` emits a rename as `R  <new>\0<old>\0`. Reading
the field after the entry gives the *old* name, which no longer exists -- so
the guard asked whether a vanished path was allowed to change while the path
that now does went unexamined.

The consequence is the reason this has its own test: an agent could move a
file out of a directory it is permitted to write into one it is not, and the
workspace guard would see nothing wrong.
"""

from __future__ import annotations

import subprocess

import pytest

from uta.shared.workspace_policy import git_status_paths


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("original\n")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return tmp_path


def test_a_rename_reports_the_new_path(repo):
    subprocess.run(
        ["git", "mv", "tests/test_app.py", "src/smuggled.py"],
        cwd=repo, check=True, capture_output=True,
    )

    paths = git_status_paths(str(repo))

    assert "src/smuggled.py" in paths, "the guard examined a path that no longer exists"


def test_the_old_path_is_not_reported(repo):
    """It is gone; asking whether it was allowed to change means nothing."""
    subprocess.run(
        ["git", "mv", "tests/test_app.py", "src/smuggled.py"],
        cwd=repo, check=True, capture_output=True,
    )

    assert "tests/test_app.py" not in git_status_paths(str(repo))


def test_an_ordinary_modification_still_works(repo):
    (repo / "tests" / "test_app.py").write_text("changed\n")

    assert git_status_paths(str(repo)) == {"tests/test_app.py"}


def test_a_path_with_a_space_survives_the_z_parsing(repo):
    (repo / "tests" / "test with space.py").write_text("x\n")

    assert "tests/test with space.py" in git_status_paths(str(repo))

from __future__ import annotations

import subprocess

import pytest

from uta.shared.workspace_policy import TaskUnsafeDiffError, git_status_snapshot
from uta.testgen.task_guard import llm_guard_after


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "uta@example.test")
    _git(tmp_path, "config", "user.name", "UTA")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    _git(tmp_path, "add", "pyproject.toml")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _state(repo):
    return {
        "task_id": "1",
        "task_db_path": str(repo / "unused.db"),
        "repo_path": str(repo),
        "language": "python",
    }


def _snapshot(repo):
    return {
        "repo_path": str(repo),
        "before": git_status_snapshot(str(repo)),
        "phase": "python_fix_coverage",
        "batch": ["pyfile:pkg/worker.py"],
    }


def test_llm_guard_removes_new_untracked_uv_lock_runtime_residue(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    snapshot = _snapshot(repo)
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr("uta.testgen.task_guard.task_ports_from_state", lambda _state: None)

    llm_guard_after(_state(repo), snapshot)

    assert not (repo / "uv.lock").exists()


def test_llm_guard_still_rejects_changes_to_tracked_uv_lock(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _git(repo, "add", "uv.lock")
    _git(repo, "commit", "-q", "-m", "track lock")
    snapshot = _snapshot(repo)
    (repo / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    monkeypatch.setattr("uta.testgen.task_guard.task_ports_from_state", lambda _state: None)

    with pytest.raises(TaskUnsafeDiffError, match="uv.lock"):
        llm_guard_after(_state(repo), snapshot)

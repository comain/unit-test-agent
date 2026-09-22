"""Delivery may publish generated assets, never model prompt artifacts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from uta.testgen.delivery import commit_to_branch


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_delivery_stages_cache_deliverables_but_never_prompt_artifacts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "UTA Test")
    _git(repo, "config", "user.email", "uta@example.invalid")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "fixture")
    assert not (repo / ".gitignore").exists()

    generated_test = repo / "src" / "test" / "java" / "pkg" / "ATest.java"
    generated_test.parent.mkdir(parents=True)
    generated_test.write_text("class ATest {}\n", encoding="utf-8")
    legitimate_inputs = repo / "src" / "test" / "resources" / "inputs.json"
    legitimate_inputs.parent.mkdir(parents=True)
    legitimate_inputs.write_text('{"fixture": true}\n', encoding="utf-8")

    allowed = (
        repo / ".uta_cache" / "context" / "project_summary.md",
        repo / ".uta_cache" / "python" / "dependencies" / "marker.txt",
    )
    forbidden = (
        repo / ".uta_cache" / "agent_turns" / "legacy" / "prompt.md",
        repo / ".uta_cache" / "cycle-prompts" / "generate" / "prompt.md",
        repo / "workflow-state" / "prompts" / "managed" / "prompt.md",
        repo / ".uta_cache" / "context" / "inputs.json",
    )
    for path in (*allowed, *forbidden):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sensitive\n", encoding="utf-8")

    # Leave the real index intact for inspection by making the real commit
    # fail after staging. No Git calls are mocked.
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(hook, 0o755)

    commit_to_branch(
        {
            "repo_path": str(repo),
            "current_batch": ["pkg.A"],
            "results": {
                "pkg.A": {
                    "test_file_path": "src/test/java/pkg/ATest.java",
                    "status": "PASS",
                    "coverage": 100.0,
                }
            },
            "deterministic_change_paths": [
                "workflow-state",
                "src/test/resources/inputs.json",
            ],
        }
    )

    staged = set(_git(repo, "diff", "--cached", "--name-only").stdout.splitlines())
    assert "src/test/java/pkg/ATest.java" in staged
    assert "src/test/resources/inputs.json" in staged
    assert {path.relative_to(repo).as_posix() for path in allowed} <= staged
    assert staged.isdisjoint(path.relative_to(repo).as_posix() for path in forbidden)

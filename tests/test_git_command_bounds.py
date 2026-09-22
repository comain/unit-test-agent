"""Git commands in the Java generation backend are bounded.

Twenty-odd call sites reach `_git_run`, and they are not all local reads:
`fetch`, `push` and `rebase` talk to a remote, where a stalled connection or a
server that accepts and never answers hangs indefinitely. Unbounded, the turn
hangs with it, holding a task slot until someone kills the process by hand.
"""

from __future__ import annotations

import subprocess

import pytest

from agent_core.git import GitTimeout
from uta.language.java import generation


def test_a_timeout_is_applied_by_default(monkeypatch):
    seen = {}

    def record(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", record)
    generation._git_run("/repo", "status")

    assert seen.get("timeout") == 600


def test_a_caller_may_set_its_own(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (seen.update(kw), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
    )

    generation._git_run("/repo", "status", timeout=5)

    assert seen["timeout"] == 5


def test_a_hung_command_raises_a_typed_error(monkeypatch):
    """`GitTimeout`, not a bare TimeoutExpired: a caller can tell a hang from a
    command that ran and failed."""
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hang)

    with pytest.raises(GitTimeout) as caught:
        generation._git_run("/repo", "fetch", "origin")

    assert "fetch origin" in str(caught.value)
    assert "/repo" in str(caught.value)


def test_the_typed_error_is_still_a_runtime_error(monkeypatch):
    """Existing handlers must be unaffected."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 1)),
    )

    with pytest.raises(RuntimeError):
        generation._git_run("/repo", "status")


def test_a_zero_budget_means_no_limit(monkeypatch):
    """For an operator running a clone that legitimately takes longer than any
    default anyone would pick."""
    from uta.shared.config import settings

    monkeypatch.setattr(settings, "ci_git_command_timeout_seconds", 0)
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (seen.update(kw), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
    )

    generation._git_run("/repo", "status")

    assert seen["timeout"] is None


def test_it_uses_the_projects_single_budget():
    """`_git_command_timeout` is gone: there is one answer, in `uta.shared.git`,
    rather than one per feature that has to be kept in step by hand."""
    from uta.shared.config import settings
    from uta.shared.git import git_timeout

    assert git_timeout() == settings.ci_git_command_timeout_seconds
    assert not hasattr(generation, "_git_command_timeout")

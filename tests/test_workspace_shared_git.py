"""The workspace manager on agent-core's shared git workspace.

Branch validation, URL-host extraction, repo naming, credentials, retry and
the clone/fetch/checkout sequence itself were all duplicated here. They now
come from `agent_core.git.GitWorkspace`, and this class is the policy on top:
a local branch rather than a detached HEAD, a fresh tree per task, and full
history.

What used to keep the two apart was `prepare`. agent-core's workspace kept one
cached clone per repository and reused it, and tasks here *edit* the repository
and push, so two concurrent repair tasks would have overwritten each other.
That is no longer true: 0.6.7 gave the shared class per-task scopes and 0.6.9
gave it the three properties above, so there is one implementation again.

Failures stay typed, so a caller can still tell a transient transport error
from a real one -- which this project could not do before agent-core owned it,
and so retried everything or nothing.
"""

from __future__ import annotations


import pytest

from agent_core.git import GitCommandError, GitTimeout, is_retryable_git_failure
from agent_core.git import git_url_host, validate_branch
from fake_git import calls, fake_git
from uta.app.workspace import GitWorkspaceManager


def manager(tmp_path, **fake):
    return GitWorkspaceManager(
        workspace_root=tmp_path,
        git_bin=fake_git(tmp_path, **fake),
        allowed_hosts=["git.example.com"],
        command_retry_times=0,
        command_retry_delay_seconds=0.0,
    )


def prepare(mgr, branch="main"):
    return mgr.prepare(
        git_url="git@git.example.com:group/demo.git",
        branch=branch,
        task_id="task-1",
    )


# -- the helpers now come from agent-core ----------------------------------

def test_branch_validation_is_agent_cores():
    assert GitWorkspaceManager._validate_branch is validate_branch


def test_url_host_extraction_is_agent_cores():
    assert GitWorkspaceManager._git_url_host is git_url_host


@pytest.mark.parametrize(
    "branch",
    ["-x", "/x", ".x", "x/", "x.", "x.lock", "a..b", "a//b", "a@{b", "a\\b", "", "a b"],
)
def test_the_same_branches_are_still_rejected(branch):
    with pytest.raises(ValueError):
        GitWorkspaceManager._validate_branch(branch)


@pytest.mark.parametrize("branch", ["main", "TASK-40989-20260807", "feature/a_b.c-d"])
def test_the_same_branches_are_still_accepted(branch):
    assert GitWorkspaceManager._validate_branch(branch) == branch


def test_a_checkout_is_one_directory_under_the_task(tmp_path):
    """It becomes a path, so it must stay a single component under the task.

    Both halves come from a request body. agent-core refuses a name that would
    traverse out of the cache or be read as an option; what is pinned here is
    that the shape did not change with the implementation.
    """
    workspace = prepare(manager(tmp_path))

    assert workspace == tmp_path / "task-1" / "demo"


def test_a_host_outside_the_allow_list_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not allowed"):
        manager(tmp_path)._validate_git_url("git@evil.example.com:g/r.git")


def test_a_repository_on_a_disallowed_host_is_never_cloned(tmp_path):
    """The check has to happen before git is handed the URL, not after."""
    log = tmp_path / "git.jsonl"
    mgr = GitWorkspaceManager(
        workspace_root=tmp_path,
        git_bin=fake_git(tmp_path, log=log),
        allowed_hosts=["git.example.com"],
    )

    with pytest.raises(ValueError, match="not allowed"):
        mgr.prepare(git_url="git@evil.example.com:g/r.git", branch="main", task_id="task-1")

    assert calls(log) == []


# -- failures are typed ----------------------------------------------------

def test_a_failed_command_raises_a_typed_error(tmp_path):
    with pytest.raises(GitCommandError):
        prepare(manager(tmp_path, fail_on={"clone": "fatal: could not read from remote"}))


def test_typed_errors_are_still_runtime_errors(tmp_path):
    """Every existing `except RuntimeError` must keep working."""
    with pytest.raises(RuntimeError):
        prepare(manager(tmp_path, fail_on={"clone": "fatal: could not read from remote"}))


def test_a_timeout_raises_a_typed_error(tmp_path):
    mgr = manager(tmp_path, sleep=5)
    mgr.workspace.timeout = 0.8
    mgr.workspace.poll_interval = 0.05

    with pytest.raises(GitTimeout):
        prepare(mgr)


def test_a_transient_transport_failure_is_now_recognisable(tmp_path):
    """The capability this project did not have: telling a blip from a real error."""
    stderr = "fatal: unable to access: Connection reset by peer"
    with pytest.raises(GitCommandError) as caught:
        prepare(manager(tmp_path, fail_on={"clone": stderr}))

    assert is_retryable_git_failure(caught.value)


def test_a_real_error_is_not_mistaken_for_a_blip(tmp_path):
    with pytest.raises(GitCommandError) as caught:
        prepare(manager(tmp_path, fail_on={"clone": "fatal: repository not found"}))

    assert not is_retryable_git_failure(caught.value)


def test_a_timeout_is_retryable(tmp_path):
    mgr = manager(tmp_path, sleep=5)
    mgr.workspace.timeout = 0.8
    mgr.workspace.poll_interval = 0.05

    with pytest.raises(GitTimeout) as caught:
        prepare(mgr)

    assert is_retryable_git_failure(caught.value)


def test_a_timed_out_clone_leaves_no_git_behind(tmp_path):
    """The whole process group is killed. git delegates to ssh, and killing
    only git orphans the ssh it spawned -- which a `subprocess.run` timeout,
    what this class used before, does not handle."""
    marker = tmp_path / "child-alive"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(f"#!/bin/sh\n( sleep 30; echo alive > {marker} ) &\nsleep 30\n")
    fake.chmod(0o755)

    mgr = GitWorkspaceManager(
        workspace_root=tmp_path,
        git_bin=str(fake),
        allowed_hosts=["git.example.com"],
        command_retry_times=0,
    )
    mgr.workspace.timeout = 0.5
    mgr.workspace.poll_interval = 0.05

    with pytest.raises(GitTimeout):
        prepare(mgr)

    import time
    time.sleep(0.4)
    assert not marker.exists(), "the grandchild survived the kill"

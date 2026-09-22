"""How `uta tasks enqueue` gets the repository.

It used to shell out directly:

    subprocess.run(["git", "clone", git_url, dest], check=True, timeout=...)

with no environment, so git authenticated with whatever ambient ssh identity
the operator happened to have. This deployment authenticates with an access
token, and a token reaches git two ways at once: an `http.<host>.extraheader`
in the environment, and an `https://` clone URL for it to apply to. Neither
was present, so enqueueing a private repository failed on any host without a
usable key, and the failure was a bare `CalledProcessError` from a command
line the caller could not see.

It is one `GitWorkspace.prepare` now. The properties below are agent-core's,
not this project's -- what these tests pin is that the CLI actually gets them,
because the first fix ran `clone` and `fetch` here and reassembled the URL
rewrite and the remote repoint alongside, which is how they drift.
"""

from __future__ import annotations

import pytest

from agent_core.git import GitCommandError, GitCredentials, GitWorkspace
from fake_git import calls, envs, fake_git
from uta.app.task_commands import legacy_clone_path, prepare_cli_clone

SSH_URL = "git@git.example.com:group/demo.git"
TOKEN_URL = "https://git.example.com/group/demo.git"
DEFAULT_BRANCH = "ref: refs/heads/master\tHEAD"


@pytest.fixture
def workspace_with(tmp_path, monkeypatch):
    """Install a workspace whose git is a fake, and hand back its log."""

    def install(*, access_token="", ssh_key_path="", responses=None, **fake):
        log = tmp_path / "git.jsonl"
        workspace = GitWorkspace(
            tmp_path / "clones",
            GitCredentials(
                ssh_key_path=ssh_key_path,
                access_token=access_token,
                token_host="git.example.com",
            ),
            clone_depth=None,
            git_bin=fake_git(
                tmp_path,
                log=log,
                responses={"ls-remote": DEFAULT_BRANCH, **(responses or {})},
                **fake,
            ),
        )
        monkeypatch.setattr("uta.shared.git.git_workspace", lambda *a, **kw: workspace)
        return log

    return install


def test_a_token_deployment_clones_over_https_with_the_header(tmp_path, workspace_with):
    """The defect: an ssh URL cloned with a token configured cannot use it."""
    log = workspace_with(access_token="secret-token")

    dest = prepare_cli_clone(SSH_URL)

    clone = next(argv for argv in calls(log) if argv[0] == "clone")
    assert TOKEN_URL in clone, "the ssh URL must be rewritten for the token"
    assert str(dest) == clone[-1]

    env = envs(log)[0]
    assert env["GIT_CONFIG_KEY_0"] == "http.https://git.example.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "secret-token" not in env["GIT_CONFIG_VALUE_0"], "the token must not be readable"


def test_without_a_token_the_url_is_left_alone_and_the_key_is_used(tmp_path, workspace_with):
    log = workspace_with(ssh_key_path="/opt/app/uta-ci-data/runner/ssh/uta ci key")

    prepare_cli_clone(SSH_URL)

    clone = next(argv for argv in calls(log) if argv[0] == "clone")
    assert SSH_URL in clone
    assert "'/opt/app/uta-ci-data/runner/ssh/uta ci key'" in envs(log)[0]["GIT_SSH_COMMAND"]


def test_the_checkout_keeps_its_history(tmp_path, workspace_with):
    """This project reads commit ranges -- enforcement diffs, commit-message
    context -- which a shallow clone cannot answer."""
    log = workspace_with()

    prepare_cli_clone(SSH_URL)

    clone = next(argv for argv in calls(log) if argv[0] == "clone")
    assert "--depth" not in clone


def test_no_branch_asks_the_remote_which_one_it_defaults_to(tmp_path, workspace_with):
    """It used to clone and let git pick, which is the same answer only when
    the clone is the thing being pointed at a branch afterwards."""
    log = workspace_with()

    prepare_cli_clone(SSH_URL)

    assert any(argv[0] == "ls-remote" for argv in calls(log))
    checkout = next(argv for argv in calls(log) if "checkout" in argv)
    assert "master" in checkout


def test_a_branch_is_checked_out_as_a_local_branch(tmp_path, workspace_with):
    """A local branch, not a detached HEAD: the daemon commits and pushes it
    by name."""
    log = workspace_with()

    prepare_cli_clone(SSH_URL, branch="TASK-40990-20260810")

    checkout = next(argv for argv in calls(log) if "checkout" in argv)
    assert checkout[2:] == [
        "checkout", "--force", "-B", "TASK-40990-20260810", "origin/TASK-40990-20260810",
    ]
    assert not any(argv[0] == "ls-remote" for argv in calls(log)), "no need to ask the remote"


def test_an_existing_checkout_is_fetched_rather_than_recloned(tmp_path, workspace_with):
    log = workspace_with(access_token="secret-token")

    dest = prepare_cli_clone(SSH_URL)
    before = len(calls(log))

    assert prepare_cli_clone(SSH_URL) == dest
    again = calls(log)[before:]
    assert not any(argv[0] == "clone" for argv in again)
    assert any("fetch" in argv for argv in again)


def test_an_existing_checkout_is_repointed_at_a_url_the_credentials_can_use(tmp_path, workspace_with):
    """A clone root outlives a credential change -- cloned over ssh, now
    running with a token -- and the stale remote would fail to fetch."""
    log = workspace_with(access_token="secret-token", responses={"remote": SSH_URL})

    prepare_cli_clone(SSH_URL)
    prepare_cli_clone(SSH_URL)

    assert any(argv[2:] == ["remote", "set-url", "origin", TOKEN_URL] for argv in calls(log))


def test_a_remote_already_pointing_the_right_way_is_left_alone(tmp_path, workspace_with):
    log = workspace_with(access_token="secret-token", responses={"remote": TOKEN_URL})

    prepare_cli_clone(SSH_URL)
    prepare_cli_clone(SSH_URL)

    assert not any(argv[2:4] == ["remote", "set-url"] for argv in calls(log))


def test_a_branch_that_is_not_a_branch_is_refused_before_git_sees_it(tmp_path, workspace_with):
    """`--upload-pack=...` is an option, not a branch, and would run a command."""
    log = workspace_with()

    with pytest.raises(ValueError):
        prepare_cli_clone(SSH_URL, branch="--upload-pack=touch /tmp/x")

    assert calls(log) == []


def test_a_failed_clone_raises_a_typed_error(tmp_path, workspace_with):
    """It used to be a bare CalledProcessError naming a command line the
    caller could not see."""
    workspace_with(fail_on={"clone": "fatal: repository not found"})

    with pytest.raises(GitCommandError):
        prepare_cli_clone(SSH_URL)


# -- the path moved --------------------------------------------------------


def test_the_checkout_is_named_by_agent_cores_rule(tmp_path, workspace_with):
    workspace_with()

    assert prepare_cli_clone(SSH_URL) == tmp_path / "clones" / "demo"


def test_the_old_path_is_still_derivable_for_an_already_running_task(tmp_path):
    """A task enqueued before this is recorded against the old directory, and
    enqueue has to recognise it or it schedules the same work twice."""
    assert legacy_clone_path(SSH_URL, tmp_path) == tmp_path / "demo.git"
    assert legacy_clone_path("https://host/g/dm-core.git", tmp_path) == tmp_path / "dm-core.git"

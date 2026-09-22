"""Task-isolated checkouts, on agent-core's shared git workspace.

This class used to run git itself: `subprocess.run(["git", "-C", ...])` through
an injectable `run_command`, with its own retry loop, its own credential
plumbing and its own clone/fetch/checkout sequence. All of that now comes from
`agent_core.git.GitWorkspace`, and what is left here is the *policy* this
product needs, which the shared class did not have until it was given it:

* **A local branch, not a detached HEAD.** Delivery runs its own
  `git push -u origin <branch>` so it can rebase and retry on rejection, and
  that needs a local branch of that name -- without one the push fails with
  `src refspec <branch> does not match any` and strands work already
  committed.
* **A fresh tree per task.** `clean -fd` keeps ignored files, so a task that
  compiles would inherit the previous task's `target/` as its baseline.
* **Full history.** Enforcement and commit-message context both read commit
  ranges (`diff base...HEAD`, `log base..HEAD`), which a shallow clone cannot
  answer.

What this buys is what a hand-rolled `subprocess.run` could not do:
cancellation interrupts a *running* clone rather than being noticed between
commands, and a timeout kills the process group -- git delegates to ssh, and
killing only git orphans the ssh it spawned.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

from agent_core.git import (
    GitCommandError,
    GitCredentials,
    GitTimeout,
    GitWorkspace,
    git_url_host,
    is_retryable_git_failure,
    repo_name_from_url,
    ssh_command_for_key,
    validate_branch,
)


LOGGER = logging.getLogger(__name__)


class GitWorkspaceManager:
    def __init__(
        self,
        workspace_root: Path,
        allowed_hosts: Optional[List[str]] = None,
        git_ssh_key_path: str = "",
        git_access_token: str = "",
        command_timeout_seconds: int = 600,
        command_retry_times: int = 1,
        command_retry_delay_seconds: float = 2.0,
        git_bin: str = "git",
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.allowed_hosts = tuple(allowed_hosts or ["git.example.com"])
        self.git_access_token = git_access_token.strip()
        self.git_ssh_key_path = git_ssh_key_path
        self.command_timeout_seconds = command_timeout_seconds
        self.workspace = GitWorkspace(
            self.workspace_root,
            GitCredentials(
                ssh_key_path=git_ssh_key_path,
                access_token=self.git_access_token,
                token_host=self.allowed_hosts[0],
            ),
            # Not shallow: enforcement diffs and commit-message context both
            # read commit ranges a `--depth 1` clone cannot answer.
            clone_depth=None,
            timeout=command_timeout_seconds,
            allowed_hosts=list(self.allowed_hosts),
            retry_times=max(0, command_retry_times),
            retry_delay_seconds=max(0.0, command_retry_delay_seconds),
            git_bin=git_bin,
        )

    # -- preparing a task's checkout ---------------------------------------

    def prepare(self, *, git_url: str, branch: str, task_id: str) -> Path:
        """A clean checkout of ``branch``, isolated to this task."""
        self._validate_git_url(git_url)
        self._validate_branch(branch)
        return self.workspace.prepare(
            git_url,
            branch=branch,
            scope=self._slug(task_id),
            local_branch=True,
            fresh=True,
        )

    def refresh_branch(self, repo_path: Path, *, branch: str) -> str:
        """Reset an existing workspace to the current remote branch head."""
        self._validate_branch(branch)
        repo_path = Path(repo_path)
        self._pin_ssh_command(repo_path)
        # Repair workspaces are disposable mirrors. `refresh_ref` forces the
        # tracking ref, so a rebased or force-pushed source branch can still be
        # refreshed and reset.
        if not self.workspace.refresh_ref(repo_path, branch):
            raise GitCommandError(
                ["git", "fetch", branch], 1, f"remote has no branch {branch}"
            )
        self._execute(repo_path, "checkout", "--force", "-B", branch, f"origin/{branch}")
        self._execute(repo_path, "reset", "--hard", f"origin/{branch}")
        self._execute(repo_path, "clean", "-fd")
        return self.workspace.query(repo_path, "rev-parse", "HEAD")

    def remote_branch_head(self, repo_path: Path, *, branch: str) -> str:
        """The current remote branch head, without changing the checkout."""
        self._validate_branch(branch)
        repo_path = Path(repo_path)
        self._pin_ssh_command(repo_path)
        if not self.workspace.refresh_ref(repo_path, branch):
            raise GitCommandError(
                ["git", "fetch", branch], 1, f"remote has no branch {branch}"
            )
        return self.workspace.query(repo_path, "rev-parse", f"origin/{branch}")

    # -- internals ---------------------------------------------------------

    def _execute(self, repo_path: Path, *args: str) -> None:
        self.workspace.execute(repo_path, *args, check=True)

    def _pin_ssh_command(self, repo_path: Path) -> None:
        """Record the key in the repo's own config.

        `prepare` already does this for a workspace it built, but these two
        also run against checkouts prepared elsewhere, and the pin is what lets
        a build or an agent run git in the tree with the same identity.
        """
        command = ssh_command_for_key(self.git_ssh_key_path)
        if not command or self.git_access_token:
            return
        self._execute(repo_path, "config", "core.sshCommand", command)

    @staticmethod
    def _slug(value: str) -> str:
        # The task id becomes a directory under the workspace root.
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")

    def _validate_git_url(self, git_url: str) -> None:
        host = self._git_url_host(git_url)
        if host not in self.allowed_hosts:
            raise ValueError(f"git_url host is not allowed: {host or 'unknown'}")

    # Duplicates of agent-core's, rule for rule. Kept as names because they are
    # called from here and from the tests that pin this class's contract.
    _validate_branch = staticmethod(validate_branch)
    _git_url_host = staticmethod(git_url_host)


__all__ = [
    "GitWorkspaceManager",
    "GitCommandError",
    "GitTimeout",
    "is_retryable_git_failure",
    "repo_name_from_url",
]

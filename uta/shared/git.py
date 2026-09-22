"""The one place this project invokes git.

There were four. `tasks/auto_push.py`, `app/workspace.py`,
`language/java/generation.py` and `shared/workspace_policy.py` each had their
own helper, and each had independently worked out some subset of the same four
concerns -- credentials, a timeout, typed failures, retry on a transient one.
None had all four. They disagreed on which they needed, which is what happens
when a concern has no owner: every feature that needs git grows its own way of
running it, and a fix to one is invisible to the other three.

The mechanism is agent-core's `GitRunner`, which owns invocation. What belongs
here, and only here, is where this project keeps its credentials and what it
considers a reasonable budget.

Retry is agent-core's too. `retry_git_operation` runs an operation again only
for a failure `is_retryable_git_failure` classifies as transient -- which is
what the hand-rolled loop in `app/workspace.py` was approximating by retrying
everything, including a repository that does not exist.
"""

from __future__ import annotations

from typing import Callable, Optional, TypeVar

from agent_core.git import GitCredentials, GitRunner, GitWorkspace, runner_for
from agent_core.git.retry import retry_git_operation

T = TypeVar("T")


def git_timeout() -> Optional[float]:
    """The budget for one git command, or None when disabled.

    Zero or negative disables it, for an operator with a clone or a push that
    legitimately takes longer than any default anyone would pick.
    """
    from uta.shared.config import settings

    configured = float(getattr(settings, "ci_git_command_timeout_seconds", 0) or 0)
    return configured if configured > 0 else None


def git(*, timeout: Optional[float] = -1.0, token_host: str = "") -> GitRunner:
    """A runner carrying this deployment's credentials.

    Built per call rather than cached: the token and key path are settings, and
    an operator or a test changing one mid-process should take effect.
    Construction is a dictionary and no I/O.

    ``token_host`` defaults to the first entry of `ci_allowed_git_hosts`, the
    setting that already says which hosts this service may reach.
    """
    from uta.shared.config import settings

    hosts = (settings.ci_allowed_git_hosts or "").split(",")
    return runner_for(
        ssh_key_path=settings.ci_git_ssh_key_path,
        access_token=settings.ci_git_access_token,
        token_host=token_host or (hosts[0].strip() if hosts else ""),
        timeout=git_timeout() if timeout == -1.0 else timeout,
    )


def with_git_retry(operation: Callable[[], T], *, attempts: int = 3) -> T:
    """Run a git operation again only for a transient failure.

    Named here so a caller does not have to know that the classification lives
    in agent-core, and so there is one answer to "how many times" rather than
    one per feature.
    """
    return retry_git_operation(operation, attempts=attempts)


def git_workspace(cache_dir, *, timeout: Optional[float] = -1.0, token_host: str = "", **kwargs) -> GitWorkspace:
    """A workspace carrying this deployment's credentials.

    The counterpart to `git()` for the operations agent-core states as
    operations rather than as verbs -- cloning, refreshing, publishing. A
    caller reaching for `GitRunner` and a bare `clone` is re-deriving what this
    already knows: which URL the credentials can actually use, and how to
    repoint a checkout whose remote predates a credential change.

    `clone_depth=None` by default: this project reads commit ranges, which a
    shallow clone cannot answer.
    """
    from uta.shared.config import settings

    hosts = (settings.ci_allowed_git_hosts or "").split(",")
    kwargs.setdefault("clone_depth", None)
    return GitWorkspace(
        cache_dir,
        GitCredentials(
            ssh_key_path=settings.ci_git_ssh_key_path,
            access_token=settings.ci_git_access_token,
            token_host=token_host or (hosts[0].strip() if hosts else ""),
        ),
        timeout=git_timeout() if timeout == -1.0 else timeout,
        **kwargs,
    )

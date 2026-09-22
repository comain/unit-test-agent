"""UTA deployment configuration for agent-core Git execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_core.git import GitCredentials, GitWorkspace

from uta.shared.config import settings


def configured_git_workspace(repo_path: str) -> GitWorkspace:
    """Build agent-core's Git client from this product's deployment settings."""
    token_host = next(
        (host.strip() for host in (settings.ci_allowed_git_hosts or "").split(",") if host.strip()),
        "",
    )
    credentials = GitCredentials(
        ssh_key_path=settings.ci_git_ssh_key_path,
        access_token=settings.ci_git_access_token,
        token_host=token_host,
    )
    return GitWorkspace(
        Path(repo_path).parent,
        credentials=credentials,
        clone_depth=None,
        timeout=max(0, int(settings.ci_git_command_timeout_seconds or 0)),
    )


def run_git(repo_path: str, *args: str, **kwargs: Any):
    """Run Git through agent-core while retaining caller-owned failure policy."""
    supported = {"capture_output", "check", "text"}
    unexpected = set(kwargs) - supported
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise TypeError(f"unsupported Git execution options: {names}")
    return configured_git_workspace(repo_path).execute(
        Path(repo_path),
        *args,
        check=bool(kwargs.get("check", False)),
    )


def output_text(value: Any) -> str:
    """Normalize legacy byte results and agent-core text results."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value or "")

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlparse

from uta.ci_plugin.git_identity import git_env_with_ssh_key, git_ssh_command_for_key


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
LOGGER = logging.getLogger(__name__)


class GitWorkspaceManager:
    def __init__(
        self,
        workspace_root: Path,
        run_command: Optional[RunCommand] = None,
        allowed_hosts: Optional[List[str]] = None,
        git_ssh_key_path: str = "",
        command_timeout_seconds: int = 600,
        command_retry_times: int = 1,
        command_retry_delay_seconds: float = 2.0,
    ) -> None:
        self.workspace_root = workspace_root
        self.run_command = run_command or subprocess.run
        self.allowed_hosts = tuple(allowed_hosts or ["git.example.com"])
        self.git_ssh_command = git_ssh_command_for_key(git_ssh_key_path)
        self.git_env = git_env_with_ssh_key(git_ssh_key_path)
        self.command_timeout_seconds = command_timeout_seconds
        self.command_retry_times = max(0, command_retry_times)
        self.command_retry_delay_seconds = max(0.0, command_retry_delay_seconds)

    def prepare(self, *, git_url: str, branch: str, task_id: str) -> Path:
        self._validate_git_url(git_url)
        self._validate_branch(branch)
        repo_name = self._repo_name(git_url)
        task_root = self.workspace_root / self._slug(task_id)
        repo_path = task_root / repo_name
        if task_root.exists():
            shutil.rmtree(task_root)
        task_root.mkdir(parents=True, exist_ok=True)

        self._run(["git", "clone", git_url, str(repo_path)], stage="clone")
        self._configure_repo_ssh_command(repo_path)
        remote_ref = f"refs/heads/{branch}:refs/remotes/origin/{branch}"
        self._run(["git", "-C", str(repo_path), "fetch", "origin", remote_ref, "--prune"], stage="fetch")
        self._run(["git", "-C", str(repo_path), "checkout", "--force", "-B", branch, f"origin/{branch}"], stage="checkout")
        self._run(["git", "-C", str(repo_path), "clean", "-fd"], stage="clean")
        return repo_path

    def _run(self, cmd: List[str], *, stage: str = "git") -> None:
        attempts = self.command_retry_times + 1
        last_error: str = ""
        for attempt in range(1, attempts + 1):
            LOGGER.info(
                "ci_workspace_git_%s_started attempt=%s/%s cmd=%s",
                stage,
                attempt,
                attempts,
                self._safe_cmd(cmd),
            )
            try:
                completed = self.run_command(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=self.git_env,
                    timeout=self.command_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                last_error = (
                    f"git command timed out during {stage} after {self.command_timeout_seconds}s: "
                    f"{self._safe_cmd(cmd)}\nstdout={self._decode_output(exc.stdout)}\nstderr={self._decode_output(exc.stderr)}"
                )
                LOGGER.warning(
                    "ci_workspace_git_%s_timeout attempt=%s/%s timeout_seconds=%s cmd=%s",
                    stage,
                    attempt,
                    attempts,
                    self.command_timeout_seconds,
                    self._safe_cmd(cmd),
                )
            else:
                if completed.returncode == 0:
                    LOGGER.info("ci_workspace_git_%s_finished attempt=%s/%s", stage, attempt, attempts)
                    return
                last_error = (
                    f"git command failed during {stage}: {self._safe_cmd(cmd)}\n"
                    f"stdout={completed.stdout}\nstderr={completed.stderr}"
                )
                LOGGER.warning(
                    "ci_workspace_git_%s_failed attempt=%s/%s returncode=%s cmd=%s",
                    stage,
                    attempt,
                    attempts,
                    completed.returncode,
                    self._safe_cmd(cmd),
                )
            if attempt < attempts and self.command_retry_delay_seconds:
                time.sleep(self.command_retry_delay_seconds)
        raise RuntimeError(last_error)

    def _configure_repo_ssh_command(self, repo_path: Path) -> None:
        if not self.git_ssh_command:
            return
        self._run(["git", "-C", str(repo_path), "config", "core.sshCommand", self.git_ssh_command], stage="configure_ssh")

    @staticmethod
    def _safe_cmd(cmd: List[str]) -> str:
        return " ".join(cmd)

    @staticmethod
    def _decode_output(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _repo_name(git_url: str) -> str:
        name = git_url.rstrip("/").split("/")[-1].removesuffix(".git")
        return GitWorkspaceManager._slug(name) or "repo"

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")

    def _validate_git_url(self, git_url: str) -> None:
        host = self._git_url_host(git_url)
        if host not in self.allowed_hosts:
            raise ValueError(f"git_url host is not allowed: {host or 'unknown'}")

    @staticmethod
    def _validate_branch(branch: str) -> None:
        if not branch or len(branch) > 255:
            raise ValueError("branch is invalid")
        invalid = (
            branch.startswith(("-", "/", ".")),
            branch.endswith(("/", ".", ".lock")),
            ".." in branch,
            "//" in branch,
            "@{" in branch,
            "\\" in branch,
            not re.fullmatch(r"[A-Za-z0-9._/-]+", branch),
        )
        if any(invalid):
            raise ValueError("branch is invalid")

    @staticmethod
    def _git_url_host(git_url: str) -> str:
        if "://" in git_url:
            parsed = urlparse(git_url)
            if parsed.scheme not in {"ssh", "https"}:
                return ""
            return parsed.hostname or ""
        match = re.match(r"^[^@\s]+@([^:\s]+):\S+$", git_url)
        return match.group(1) if match else ""

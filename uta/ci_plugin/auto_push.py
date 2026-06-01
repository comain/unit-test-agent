from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from uta.ci_plugin.git_identity import git_env_with_ssh_key, git_ssh_command_for_key
from uta.config import settings


class AutoPushPolicyError(RuntimeError):
    pass


class AutoPushConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class AutoPushContext:
    branch_name: str
    repo_task_id: Optional[int] = None
    ci_task_id: str = ""
    ci_record_id: str = ""
    jira_key: str = ""
    class_fqns: Optional[List[str]] = None
    commit_paths: Optional[List[str]] = None


@dataclass(frozen=True)
class AutoPushResult:
    commit_sha: str
    remote_ref: str
    changed_paths: List[str]
    pushed_at: str


class CiAutoPusher:
    def __init__(
        self,
        repo_path: Path | str,
        *,
        user_name: Optional[str] = None,
        user_email: Optional[str] = None,
    ) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.user_name = user_name or settings.ci_git_user_name
        self.user_email = user_email or settings.ci_git_user_email
        self.git_ssh_command = git_ssh_command_for_key(settings.ci_git_ssh_key_path)
        self.git_env = git_env_with_ssh_key(settings.ci_git_ssh_key_path)

    def commit_and_push(self, context: AutoPushContext) -> AutoPushResult:
        changed_paths = self.changed_paths()
        ignored_paths = [path for path in changed_paths if is_ignored_ci_runtime_artifact(path)]
        committable_candidates = [path for path in changed_paths if path not in set(ignored_paths)]
        disallowed = [path for path in committable_candidates if not is_allowed_ci_test_path(path)]
        if disallowed:
            raise AutoPushPolicyError(
                "CI repair auto-push refused non-test or UTA artifact diffs: "
                + ", ".join(disallowed)
            )
        requested_paths = {_normalize_path(path) for path in context.commit_paths or [] if path}
        if requested_paths:
            committable_paths = [
                path for path in committable_candidates if _normalize_path(path) in requested_paths
            ]
        else:
            committable_paths = committable_candidates
        if not committable_paths:
            raise AutoPushPolicyError("CI repair auto-push found no test changes to commit")

        self._run_git("config", "user.name", self.user_name)
        self._run_git("config", "user.email", self.user_email)
        if self.git_ssh_command:
            self._run_git("config", "core.sshCommand", self.git_ssh_command)
        if ignored_paths:
            self._discard_ignored_paths(ignored_paths)
        self._run_git("add", "--", *committable_paths)
        self._run_git("commit", "-m", self._commit_subject(context), "-m", self._commit_body(context))

        fetch = self._run_git("fetch", "origin", context.branch_name, "--prune", check=False)
        if fetch.returncode != 0:
            raise AutoPushConflictError(f"fetch failed before push: {_combined_output(fetch)}")
        rebase = self._run_git("rebase", f"origin/{context.branch_name}", check=False)
        if rebase.returncode != 0:
            self._run_git("rebase", "--abort", check=False)
            raise AutoPushConflictError(f"rebase failed before push: {_combined_output(rebase)}")

        commit_sha = self._git_stdout("rev-parse", "HEAD")
        push = self._run_git("push", "-u", "origin", context.branch_name, check=False)
        if push.returncode != 0:
            raise AutoPushConflictError(f"push failed: {_combined_output(push)}")
        remote_ref = self._remote_ref(context.branch_name)
        if remote_ref != commit_sha:
            raise AutoPushConflictError(
                f"remote ref mismatch after push: local={commit_sha} remote={remote_ref}"
            )
        return AutoPushResult(
            commit_sha=commit_sha,
            remote_ref=remote_ref,
            changed_paths=committable_paths,
            pushed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def changed_paths(self) -> List[str]:
        status = self._git_stdout("status", "--porcelain", "--untracked-files=all")
        paths: List[str] = []
        for line in status.splitlines():
            if not line:
                continue
            raw_path = line[2:].strip()
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", 1)[1].strip()
            if raw_path:
                paths.append(raw_path)
        return sorted(dict.fromkeys(paths))

    def _remote_ref(self, branch_name: str) -> str:
        output = self._git_stdout("ls-remote", "origin", f"refs/heads/{branch_name}")
        if not output:
            return ""
        return output.split()[0].strip()

    def _git_stdout(self, *args: str) -> str:
        return self._run_git(*args).stdout.strip()

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_path,
            check=check,
            capture_output=True,
            text=True,
            env=self.git_env,
        )

    def _discard_ignored_paths(self, paths: Iterable[str]) -> None:
        specs = _cleanup_specs(paths)
        if not specs:
            return
        tracked_specs = self._tracked_specs(specs)
        if tracked_specs:
            self._run_git("checkout", "--", *tracked_specs, check=False)
        self._run_git("clean", "-fd", "--", *specs, check=False)

    def _tracked_specs(self, specs: Iterable[str]) -> List[str]:
        tracked: List[str] = []
        for spec in specs:
            result = self._run_git("ls-files", "--", spec, check=False)
            if result.stdout.strip():
                tracked.append(spec)
        return tracked

    @staticmethod
    def _commit_subject(context: AutoPushContext) -> str:
        jira = context.jira_key or context.ci_task_id or "ci"
        return f"uta: repair unit tests for {jira}"

    @staticmethod
    def _commit_body(context: AutoPushContext) -> str:
        class_fqns = ", ".join(context.class_fqns or [])
        trailers = [
            "Generated-by: unit-test-agent",
            f"CI-Task-Id: {context.ci_task_id or '-'}",
            f"CI-Record-Id: {context.ci_record_id or '-'}",
            f"Jira: {context.jira_key or '-'}",
            f"UTA-Repo-Task-Id: {context.repo_task_id if context.repo_task_id is not None else '-'}",
            f"UTA-Classes: {class_fqns or '-'}",
        ]
        return "\n".join(trailers)


def is_allowed_ci_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    parts = normalized.split("/")
    if normalized.startswith("."):
        return False
    return _contains_segment_sequence(parts, ("src", "test")) or (
        bool(parts) and parts[0] in {"tests", "test"}
    )


def is_ignored_ci_runtime_artifact(path: str) -> bool:
    normalized = _normalize_path(path)
    parts = normalized.split("/")
    if any(
        part in {".sisyphus", ".uta_cache", ".uta_reports", "target", "build", ".gradle", "mutants"}
        for part in parts
    ):
        return True
    return normalized in {".coverage", ".uta_summary.md", "opencode.json", "pom.xml"}


def _cleanup_specs(paths: Iterable[str]) -> List[str]:
    specs: List[str] = []
    for path in paths:
        normalized = path.replace("\\", "/").lstrip("/")
        parts = normalized.split("/")
        spec = normalized
        for marker in (".sisyphus", ".uta_cache", ".uta_reports", "target", "build", ".gradle", "mutants"):
            if marker in parts:
                spec = "/".join(parts[: parts.index(marker) + 1])
                break
        specs.append(spec)
    return sorted(dict.fromkeys(specs))


def _normalize_path(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("/")


def _contains_segment_sequence(parts: Sequence[str], expected: Sequence[str]) -> bool:
    width = len(expected)
    return any(tuple(parts[idx : idx + width]) == tuple(expected) for idx in range(len(parts) - width + 1))


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout or ''}\n{result.stderr or ''}".strip()

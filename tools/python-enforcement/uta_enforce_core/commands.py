"""Safe distributed command-runner port and process execution protocols."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable


HEAD_LIMIT_BYTES = 2 * 1024 * 1024  # 2 MiB
TAIL_LIMIT_BYTES = 6 * 1024 * 1024  # 6 MiB


@dataclass(frozen=True)
class CommandRequest:
    """Specification of a command to be executed in a child process."""

    args: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] | None = None
    timeout_seconds: float = 1800.0
    is_cancelled: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.args, list):
            object.__setattr__(self, "args", tuple(self.args))
        if isinstance(self.cwd, str):
            object.__setattr__(self, "cwd", Path(self.cwd))


@dataclass(frozen=True)
class CommandResult:
    """Result of a command execution."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float = 0.0
    timed_out: bool = False
    cancelled: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.args, list):
            object.__setattr__(self, "args", tuple(self.args))


@runtime_checkable
class CommandRunner(Protocol):
    """Protocol for executing sub-process commands safely."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CommandResult: ...


class CommandExecutionError(RuntimeError):
    """Raised when command execution fails safety preflight."""


class SafeProcessRunner:
    """Default safe distributed process runner.

    Provides:
    - No shell invocation (execve direct argv)
    - Confinement to repository / working directory
    - Safe environment filtering
    - Process-group isolation and guaranteed orphan teardown on timeout/cancel
    - Cancellation polling
    - Bounded head/tail output buffering (2 MiB head, 6 MiB tail)

    Waiver Note (ADR-011 / C-3):
    This distributed default runner does NOT claim or enforce OS RSS memory limits.
    UTA's server-side injected runner preserves the 3 GiB RSS guard independently.
    """

    SAFE_ENV_PASSTHROUGH = (
        "PATH",
        "PYTHONPATH",
        "HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "LOGNAME",
        "VIRTUAL_ENV",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        # Toolchain locations, not credentials. The allow-list exists to keep
        # provider tokens and CI secrets out of a mutation subprocess; dropping
        # JAVA_HOME instead breaks Maven, which is a different failure wearing
        # the same clothes. Anything here names *where a tool lives*, never
        # what it is allowed to do.
        "JAVA_HOME",
        "MAVEN_OPTS",
        "M2_HOME",
        "MAVEN_HOME",
    )

    def __init__(self, allowed_repo_root: Path | None = None) -> None:
        self._allowed_repo_root = allowed_repo_root.resolve() if allowed_repo_root else None

    def _sanitize_env(self, custom_env: Mapping[str, str] | None) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in self.SAFE_ENV_PASSTHROUGH:
            if key in os.environ:
                env[key] = os.environ[key]
        if custom_env:
            for k, v in custom_env.items():
                if str(k).strip():
                    env[str(k)] = str(v)
        return env

    def _validate_preflight(self, args: Sequence[str], cwd: Path) -> tuple[tuple[str, ...], Path]:
        if not args:
            raise CommandExecutionError("Command arguments cannot be empty")
        executable = str(args[0]).strip()
        if not executable:
            raise CommandExecutionError("Command executable cannot be empty")

        resolved_cwd = Path(cwd).resolve()
        if not resolved_cwd.exists() or not resolved_cwd.is_dir():
            raise CommandExecutionError(f"Command working directory does not exist or is not a directory: {resolved_cwd}")

        if self._allowed_repo_root:
            try:
                resolved_cwd.relative_to(self._allowed_repo_root)
            except ValueError:
                raise CommandExecutionError(
                    f"Command working directory '{resolved_cwd}' escapes allowed root '{self._allowed_repo_root}'"
                )

        return tuple(str(a) for a in args), resolved_cwd

    def _read_bounded_output(self, file_obj: Any) -> str:
        try:
            file_obj.seek(0, os.SEEK_END)
            size = file_obj.tell()
            if size <= (HEAD_LIMIT_BYTES + TAIL_LIMIT_BYTES):
                file_obj.seek(0)
                raw = file_obj.read()
                return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

            file_obj.seek(0)
            head_raw = file_obj.read(HEAD_LIMIT_BYTES)
            file_obj.seek(max(0, size - TAIL_LIMIT_BYTES))
            tail_raw = file_obj.read(TAIL_LIMIT_BYTES)

            head = head_raw.decode("utf-8", errors="replace") if isinstance(head_raw, bytes) else str(head_raw)
            tail = tail_raw.decode("utf-8", errors="replace") if isinstance(tail_raw, bytes) else str(tail_raw)
            truncated_bytes = size - (len(head_raw) + len(tail_raw))
            return f"{head}\n... [output truncated: {truncated_bytes} bytes omitted] ...\n{tail}"
        except Exception:
            return ""

    def _kill_process_group(self, proc: subprocess.Popen[Any]) -> None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        # Give 0.2s for graceful termination, then SIGKILL
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.02)

        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> CommandResult:
        validated_args, resolved_cwd = self._validate_preflight(args, cwd)
        timeout = float(timeout_seconds) if timeout_seconds is not None else 1800.0
        sanitized_env = self._sanitize_env(env)

        start_time = time.monotonic()

        with tempfile.TemporaryFile() as stdout_f, tempfile.TemporaryFile() as stderr_f:
            try:
                proc = subprocess.Popen(
                    list(validated_args),
                    cwd=str(resolved_cwd),
                    env=sanitized_env,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    start_new_session=True,  # Isolate in own process group
                )
            except OSError as exc:
                duration = time.monotonic() - start_time
                return CommandResult(
                    args=validated_args,
                    returncode=127,
                    stdout="",
                    stderr=f"Failed to spawn process: {exc}",
                    duration_seconds=duration,
                )

            cancelled = False
            timed_out = False
            poll_interval = 0.05
            deadline = start_time + timeout

            while proc.poll() is None:
                now = time.monotonic()
                if is_cancelled is not None and is_cancelled():
                    cancelled = True
                    self._kill_process_group(proc)
                    break
                if now >= deadline:
                    timed_out = True
                    self._kill_process_group(proc)
                    break
                time.sleep(poll_interval)

            try:
                proc.wait(timeout=2.0)
            except (subprocess.TimeoutExpired, Exception):
                self._kill_process_group(proc)

            duration = time.monotonic() - start_time
            stdout_text = self._read_bounded_output(stdout_f)
            stderr_text = self._read_bounded_output(stderr_f)

            if timed_out:
                timeout_msg = f"\n[command timed out after {timeout:.1f}s]"
                stderr_text = (stderr_text + timeout_msg).strip()
                return CommandResult(
                    args=validated_args,
                    returncode=124,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    duration_seconds=duration,
                    timed_out=True,
                )

            if cancelled:
                cancel_msg = "\n[command cancelled by user/agent]"
                stderr_text = (stderr_text + cancel_msg).strip()
                return CommandResult(
                    args=validated_args,
                    returncode=130,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    duration_seconds=duration,
                    cancelled=True,
                )

            return CommandResult(
                args=validated_args,
                returncode=proc.returncode if proc.returncode is not None else 0,
                stdout=stdout_text,
                stderr=stderr_text,
                duration_seconds=duration,
            )

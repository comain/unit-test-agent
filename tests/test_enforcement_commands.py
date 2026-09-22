"""Tests for safe distributed command-runner port and process runner conformance."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import pytest

from uta_enforce_core.commands import (
    CommandExecutionError,
    SafeProcessRunner,
)


def test_command_runner_runs_command_successfully(tmp_path: Path):
    runner = SafeProcessRunner()
    result = runner.run([sys.executable, "-c", "print('hello from runner')"], cwd=tmp_path)
    assert result.returncode == 0
    assert "hello from runner" in result.stdout
    assert not result.timed_out
    assert not result.cancelled
    assert result.duration_seconds >= 0.0


def test_command_runner_path_confinement(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    runner = SafeProcessRunner(allowed_repo_root=repo_root)

    # Within root is fine
    res_ok = runner.run([sys.executable, "-c", "print('ok')"], cwd=repo_root)
    assert res_ok.returncode == 0

    # Outside root is rejected pre-spawn
    with pytest.raises(CommandExecutionError, match="escapes allowed root"):
        runner.run([sys.executable, "-c", "print('fail')"], cwd=outside_dir)


def test_command_runner_empty_args_rejection(tmp_path: Path):
    runner = SafeProcessRunner()
    with pytest.raises(CommandExecutionError, match="Command arguments cannot be empty"):
        runner.run([], cwd=tmp_path)


def test_command_runner_timeout(tmp_path: Path):
    runner = SafeProcessRunner()
    # Run sleep for 5 seconds with a 0.2s timeout
    result = runner.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=0.2,
    )
    assert result.timed_out
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_command_runner_cancellation(tmp_path: Path):
    runner = SafeProcessRunner()
    start = time.monotonic()

    # Cancel after 0.15s
    def is_cancelled():
        return (time.monotonic() - start) > 0.15

    result = runner.run(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=5.0,
        is_cancelled=is_cancelled,
    )
    assert result.cancelled
    assert result.returncode == 130
    assert "cancelled" in result.stderr


def test_command_runner_kills_child_process_group(tmp_path: Path):
    """Ensure descendant child processes are killed when parent command times out."""
    runner = SafeProcessRunner()
    pid_file = tmp_path / "child.pid"

    # Python script that spawns a background worker writing its PID and sleeping
    script = f"""
import subprocess, sys, time
proc = subprocess.Popen([sys.executable, "-c", "import os, time, pathlib; pathlib.Path('{pid_file}').write_text(str(os.getpid())); time.sleep(30)"])
time.sleep(10)
"""
    result = runner.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        timeout_seconds=0.3,
    )
    assert result.timed_out

    # Verify that if child PID was written, the process was killed
    if pid_file.exists():
        child_pid = int(pid_file.read_text().strip())
        # Give OS a moment to process kill
        time.sleep(0.1)
        try:
            os.kill(child_pid, 0)
            is_alive = True
        except (ProcessLookupError, OSError):
            is_alive = False
        assert not is_alive, f"Child process {child_pid} was orphaned and is still running!"


def test_command_runner_output_truncation(tmp_path: Path):
    runner = SafeProcessRunner()
    # Output 10MB of data to test truncation bounding
    script = "import sys; sys.stdout.write('A' * (10 * 1024 * 1024))"
    result = runner.run([sys.executable, "-c", script], cwd=tmp_path)
    assert result.returncode == 0
    # Length should be bounded around head + tail limits
    assert len(result.stdout) < 10 * 1024 * 1024
    assert "[output truncated:" in result.stdout

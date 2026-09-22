"""Subprocess execution and process-tree control for Python verification.

Every verifier command goes through here, so the three properties that matter
are owned in one place: each command is launched in its own session, a
timeout kills the whole process group rather than orphaning the mutant
children mutmut spawns, and the tree is held under a memory bound.
``_run_command`` is the only place that turns a completed process into
``CommandEvidence``, which is what keeps the evidence payload identical no
matter which phase issued the command.

The memory bound is here rather than only in the CI lane because generation
and repair build their own runners: a generated test that allocates without
limit ran unbounded on this path, and on a node with no swap the kernel
answered by OOM-killing whatever it liked -- three times in one hour, taking
the machine down with it. Enforcement's ceiling has to hold for every lane
that runs generated code, not just the one that reports a gate.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time
from typing import List, Mapping, Optional, Sequence

from uta.enforcement.enforcement import process_tree_memory_guard
from uta.language.python.verification.models import CommandEvidence, RunCommand
from uta.language.python.verification.mutmut_runtime import _decode_output
from uta.shared.config import settings


def _memory_limit_bytes() -> int:
    """The verification lane's RSS ceiling; 0 or less disables the bound."""
    limit_mb = int(getattr(settings, "python_verification_memory_limit_mb", 0) or 0)
    return limit_mb * 1024 * 1024 if limit_mb > 0 else 0


def _subprocess_run(cmd: Sequence[str], cwd: Optional[Path] = None, timeout: Optional[int] = None, env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        with process_tree_memory_guard(proc, memory_limit_bytes=_memory_limit_bytes()) as guard:
            stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(proc)
        stdout = exc.stdout if exc.stdout is not None else b""
        stderr = exc.stderr if exc.stderr is not None else b""
        stderr_text = _decode_output(stderr)
        if "command timed out" not in stderr_text.lower():
            stderr_text = (stderr_text + "\ncommand timed out").strip()
        return subprocess.CompletedProcess(
            list(cmd),
            124,
            stdout=_decode_output(stdout),
            stderr=stderr_text or "command timed out",
        )
    exhausted_rss = int(guard.get("exhausted_rss") or 0)
    if exhausted_rss:
        # 137 and the CI lane's marker string, so a kill is distinguishable in
        # the command evidence from an ordinary failing phase. Only the CI lane
        # currently branches on this marker; here it is evidence, so a reader
        # can tell "we killed it" from "the test failed".
        marker = (
            f"UTA_RESOURCE_EXHAUSTED process-group-rss={exhausted_rss} "
            f"limit={_memory_limit_bytes()}"
        )
        stderr_text = (_decode_output(stderr) + "\n" + marker).strip()
        return subprocess.CompletedProcess(
            list(cmd), 137, stdout=_decode_output(stdout), stderr=stderr_text
        )
    return subprocess.CompletedProcess(
        list(cmd),
        int(proc.returncode or 0),
        stdout=_decode_output(stdout),
        stderr=_decode_output(stderr),
    )


def _terminate_process_tree(proc: subprocess.Popen, *, grace_seconds: float = 2.0) -> None:
    """Terminate the process group created for verifier subprocesses.

    mutmut starts child Python processes for mutant execution. Killing only the
    direct mutmut process on timeout leaves those children orphaned, so all
    verifier subprocesses are launched in a new session and timed-out commands
    terminate the whole process group.
    """
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=grace_seconds)
        return
    except Exception:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=grace_seconds)
    except Exception:
        pass


def _run_command(
    name: str,
    cmd: List[str],
    repo: Path,
    timeout: int,
    runner: RunCommand,
    *,
    env_overrides: Optional[Mapping[str, str]] = None,
) -> CommandEvidence:
    env = os.environ.copy()
    if env_overrides:
        env.update({key: str(value) for key, value in env_overrides.items()})
    started = time.monotonic()
    try:
        result = runner(cmd, cwd=repo, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        return CommandEvidence(
            name=name,
            command=cmd,
            exit_code=127,
            elapsed_seconds=_elapsed_since(started),
            stderr=str(exc),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        return CommandEvidence(
            name=name,
            command=cmd,
            exit_code=124,
            elapsed_seconds=_elapsed_since(started),
            stdout=stdout,
            stderr=stderr or "command timed out",
        )
    return CommandEvidence(
        name=name,
        command=list(cmd),
        exit_code=int(result.returncode),
        elapsed_seconds=_elapsed_since(started),
        stdout=_decode_output(result.stdout),
        stderr=_decode_output(result.stderr),
    )


def _elapsed_since(started: float) -> float:
    return round(max(time.monotonic() - started, 0.0), 4)


def _prepend_env_path(value: str, current: str) -> str:
    return value if not current else f"{value}{os.pathsep}{current}"


def _runner_with_env(runner: RunCommand, overrides: Mapping[str, str]) -> RunCommand:
    def run(cmd, *, cwd=None, timeout=None, env=None):
        merged = dict(env or {})
        for key, value in overrides.items():
            if key == "PYTHONPATH":
                merged[key] = _prepend_env_path(str(value), str(merged.get(key) or ""))
            else:
                merged[key] = str(value)
        return runner(cmd, cwd=cwd, timeout=timeout, env=merged)

    return run

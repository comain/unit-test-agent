"""Runtime resolution, interpreter discovery, and compatibility precheck for Python enforcement."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class PythonRuntimeResolution:
    """Resolved Python runtime binaries and lane."""

    lane: str  # "mutmut-modern" or "mutmut-legacy-py2"
    python_bin: str
    mutmut_bin: str
    is_python2: bool = False
    environment_profile: str = "python"


def resolve_python_runtime(
    syntax_version: str | None = None,
    *,
    python_bin: str | None = None,
    mutmut_bin: str | None = None,
    python2_bin: str | None = None,
    python2_mutmut_bin: str | None = None,
) -> PythonRuntimeResolution:
    """Resolve the active Python interpreter, mutmut binary, and execution lane."""
    is_py2 = str(syntax_version or "").strip().lower().startswith("python2")
    lane = "mutmut-legacy-py2" if is_py2 else "mutmut-modern"

    if is_py2:
        resolved_python = (python2_bin or python_bin or "python2").strip()
        resolved_mutmut = (python2_mutmut_bin or mutmut_bin or "mutmut").strip()
        profile = "python2"
    else:
        candidates = [
            python_bin,
            os.environ.get("UTA_SERVICE_PYTHON_BIN"),
            sys.executable,
            "python3",
        ]
        resolved_python = "python3"
        for candidate in candidates:
            if candidate and str(candidate).strip():
                resolved_python = str(candidate).strip()
                break

        if mutmut_bin and str(mutmut_bin).strip():
            resolved_mutmut = str(mutmut_bin).strip()
        else:
            sibling = Path(resolved_python).expanduser().parent / "mutmut"
            resolved_mutmut = sibling.as_posix() if sibling.exists() else "mutmut"
        profile = "python3"

    return PythonRuntimeResolution(
        lane=lane,
        python_bin=resolved_python,
        mutmut_bin=resolved_mutmut,
        is_python2=is_py2,
        environment_profile=profile,
    )


def check_python_syntax_compatibility(
    repo: Path,
    source_paths: Sequence[str],
    *,
    target_syntax: str = "python3",
) -> Optional[tuple[str, str]]:
    """Check if source files contain syntax incompatible with the target interpreter.

    Returns (failed_path, error_message) or None if all files pass syntax check.
    """
    for path_str in source_paths:
        file_path = repo / path_str
        if not file_path.is_file():
            continue
        try:
            source = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return path_str, f"Could not read source file: {exc}"

        try:
            ast.parse(source, filename=path_str)
        except SyntaxError as exc:
            return path_str, f"SyntaxError under {target_syntax}: {exc}"
    return None


def run_command(
    name: str,
    command: Sequence[str],
    cwd: Path,
    timeout: int,
    commands: list[dict[str, Any]],
    env: Mapping[str, str] | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute a sub-process command and record structured execution evidence."""
    if runner is not None:
        try:
            completed = runner(
                command,
                cwd=cwd,
                timeout=timeout,
                env=env,
            )
            exit_code = getattr(completed, "returncode", getattr(completed, "exit_code", 0))
            stdout = str(getattr(completed, "stdout", "") or "")
            stderr = str(getattr(completed, "stderr", "") or "")
            timed_out = getattr(completed, "timed_out", False)
            if timed_out:
                exit_code = 124
                stderr = (stderr + "\ncommand timed out").strip()
            payload = {
                "name": name,
                "command": list(command),
                "exitCode": exit_code,
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
            }
        except Exception as exc:
            payload = {
                "name": name,
                "command": list(command),
                "exitCode": 127,
                "stdout": "",
                "stderr": f"Error running command: {exc}",
            }
        commands.append(payload)
        return payload

    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            env=dict(env) if env else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        payload = {
            "name": name,
            "command": list(command),
            "exitCode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        payload = {
            "name": name,
            "command": list(command),
            "exitCode": 124,
            "stdout": str(exc.stdout or "")[-4000:],
            "stderr": (str(exc.stderr or "") + "\ncommand timed out")[-4000:],
        }
    commands.append(payload)
    return payload

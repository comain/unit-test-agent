"""Targeted Maven commands shared by Java verification and agent prompts."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Optional

from uta.language.java.enforcement_runner.execution import _with_maven_central_mirror
from uta.language.java.maven_project import with_default_profile_args
from uta.shared.config import settings as uta_settings


def targeted_maven_command(
    repo_path: str,
    goal: str,
    *,
    module: Optional[str] = None,
    test_selector: Optional[str] = None,
    quality_gate_command: str = "",
) -> list[str]:
    """Keep Maven repository/profile selection aligned with the configured gate."""
    cmd = [uta_settings.maven_bin, goal]
    cmd.extend(_gate_environment_args(quality_gate_command))
    if goal == "test":
        if not test_selector:
            raise ValueError("targeted Maven test requires a test selector")
        cmd.extend(
            [
                f"-Dtest={test_selector}",
                "-DfailIfNoTests=false",
                "-Dsurefire.failIfNoSpecifiedTests=false",
                "-DskipTests=false",
                "-Dmaven.test.skip=false",
            ]
        )
    elif goal == "test-compile":
        cmd.append("-DskipTests")
    else:
        raise ValueError(f"unsupported targeted Maven goal: {goal}")
    if module:
        cmd.extend(["-pl", module, "-am"])
    repo = Path(repo_path)
    cmd = with_default_profile_args(cmd, repo)
    return _with_maven_central_mirror(cmd, repo, uta_settings.maven_central_mirror_url)


def targeted_maven_command_text(*args, **kwargs) -> str:
    """Render the exact command executed by UTA for an agent prompt."""
    return shlex.join(targeted_maven_command(*args, **kwargs))


def _gate_environment_args(quality_gate_command: str) -> list[str]:
    tokens = shlex.split(quality_gate_command or "")
    args: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-P", "--activate-profiles", "-s", "--settings"}:
            if index + 1 < len(tokens):
                args.extend((token, tokens[index + 1]))
                index += 2
                continue
        elif token in {"-U", "--update-snapshots", "-o", "--offline"}:
            args.append(token)
        elif token.startswith(("-P", "--activate-profiles=", "--settings=", "-Dmaven.repo.local=", "-Dprofile.active=")):
            args.append(token)
        index += 1
    return args

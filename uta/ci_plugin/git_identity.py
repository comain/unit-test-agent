from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Dict, Optional


def git_ssh_command_for_key(key_path: str) -> Optional[str]:
    if not key_path.strip():
        return None
    resolved = str(Path(key_path).expanduser())
    return (
        f"ssh -F /dev/null -i {shlex.quote(resolved)} "
        "-o IdentitiesOnly=yes -o PreferredAuthentications=publickey "
        "-o StrictHostKeyChecking=accept-new"
    )


def git_env_with_ssh_key(key_path: str) -> Optional[Dict[str, str]]:
    ssh_command = git_ssh_command_for_key(key_path)
    if not ssh_command:
        return None
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = ssh_command
    return env

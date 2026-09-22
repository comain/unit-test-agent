"""Process-bound operations for CI report tasks.

CI report workers run inside API threads, while Maven, pytest, PIT, and mutmut
run as child process groups.  Stopping a record therefore has to terminate the
groups whose cwd or command line belongs to that record's workspace; changing
the JSON status alone would leave the expensive work alive.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path


def stop_workspace_processes(workspace: Path, *, grace_seconds: float = 1.0) -> int:
    """Terminate Linux process groups executing inside ``workspace``.

    Returns the number of distinct process groups signalled.  Non-Linux hosts
    have no ``/proc`` inventory and safely return zero.
    """
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return 0
    workspace = Path(workspace).expanduser().resolve()
    current_pid = os.getpid()
    current_group = os.getpgrp()
    groups: set[int] = set()
    for entry in proc_root.glob("[0-9]*"):
        try:
            pid = int(entry.name)
            if pid == current_pid or not _process_matches_workspace(entry, workspace):
                continue
            group = os.getpgid(pid)
            if group != current_group:
                groups.add(group)
        except (OSError, ValueError):
            continue
    for group in groups:
        _signal_group(group, signal.SIGTERM)
    signalled_count = len(groups)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while groups and time.monotonic() < deadline:
        groups = {group for group in groups if _group_is_live(group)}
        if groups:
            time.sleep(0.05)
    for group in groups:
        _signal_group(group, signal.SIGKILL)
    return signalled_count


def _process_matches_workspace(proc_entry: Path, workspace: Path) -> bool:
    try:
        cwd = (proc_entry / "cwd").resolve()
        if cwd == workspace or workspace in cwd.parents:
            return True
    except OSError:
        pass
    try:
        command = (proc_entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return False
    return str(workspace) in command


def _group_is_live(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(group: int, sig: signal.Signals) -> None:
    try:
        os.killpg(group, sig)
    except OSError:
        pass

"""The file lock that keeps one rerun per repair session.

A rerun re-executes the whole enforcement command against a working copy, so
two of them in the same checkout would fight over the same build outputs. The
lock is a file in the repository's own cache directory rather than an
in-process primitive, because the second contender is usually a different
process -- an API worker and a daemon looking at the same session.

It lives apart from the session lane because its correctness argument is about
processes and stale files, not about repair: a lock left behind by a killed
process must eventually be reclaimed, which is what the staleness window is
for, and that reasoning is easier to review when it is not interleaved with
session state transitions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator

from uta.shared.ci_models import CiTaskRecord, utc_now

# The service's logger, not this module's -- see `repair.service` for why the
# whole repair lane logs into one stream.
LOGGER = logging.getLogger("uta.app.service")

RERUN_LOCK_STALE_SECONDS = 6 * 60 * 60


class RepairRerunLockMixin:
    """Cross-process exclusion for the post-repair enforcement rerun."""

    @contextmanager
    def _repair_rerun_lock(
        self,
        record: CiTaskRecord,
        session: Dict[str, object],
        repo_path: Path,
    ) -> Iterator[bool]:
        lock_dir = repo_path / ".uta_cache" / "ci_rerun_locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        session_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session.get("sessionId") or "session")).strip("-")
        task_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(record.task_id or "task")).strip("-")
        lock_path = lock_dir / f"{task_id}-{session_id}.lock"
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self._remove_stale_repair_rerun_lock(lock_path):
                try:
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    LOGGER.info(
                        "ci_repair_rerun_already_running task_id=%s session_id=%s lock=%s",
                        record.task_id,
                        session.get("sessionId"),
                        lock_path,
                    )
                    yield False
                    return
            else:
                LOGGER.info(
                    "ci_repair_rerun_already_running task_id=%s session_id=%s lock=%s",
                    record.task_id,
                    session.get("sessionId"),
                    lock_path,
                )
                yield False
                return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "taskId": record.task_id,
                            "sessionId": session.get("sessionId"),
                            "pid": os.getpid(),
                            "createdAt": utc_now().isoformat(),
                        },
                        ensure_ascii=False,
                    )
                )
            yield True
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
    @staticmethod
    def _remove_stale_repair_rerun_lock(lock_path: Path) -> bool:
        try:
            age_seconds = time.time() - lock_path.stat().st_mtime
        except FileNotFoundError:
            return True
        if age_seconds < RERUN_LOCK_STALE_SECONDS:
            return False
        try:
            lock_path.unlink()
            LOGGER.warning("ci_repair_rerun_stale_lock_removed lock=%s age_seconds=%.1f", lock_path, age_seconds)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            LOGGER.warning("ci_repair_rerun_stale_lock_remove_failed lock=%s", lock_path, exc_info=True)
            return False

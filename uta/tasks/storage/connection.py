"""SQLite connection and outer-transaction ownership for task storage."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
from pathlib import Path
from typing import Iterator, Optional, Union

from uta.tasks.storage.base import default_db_path


class SQLiteConnectionOwner:
    """Own the only connection factory and transaction boundary repositories use."""

    def __init__(self, path: Optional[Union[os.PathLike[str], str]] = None):
        self.path = Path(path).expanduser().resolve() if path else default_db_path()

    def _enforce_file_modes(self) -> None:
        for candidate in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
            self.path.with_name(f"{self.path.name}-journal"),
        ):
            if candidate.exists():
                try:
                    os.chmod(candidate, 0o600)
                    actual_mode = stat.S_IMODE(candidate.stat().st_mode)
                except OSError as exc:
                    raise PermissionError(
                        f"cannot enforce owner-only SQLite permissions for {candidate}"
                    ) from exc
                if actual_mode != 0o600:
                    raise PermissionError(
                        "cannot enforce owner-only SQLite permissions for "
                        f"{candidate}: mode is {actual_mode:o}"
                    )

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
                os.close(fd)
            except OSError as exc:
                raise PermissionError(
                    f"cannot create owner-only SQLite database {self.path}"
                ) from exc
        conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            self._enforce_file_modes()
        except Exception:
            conn.close()
            raise
        return conn

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            self._enforce_file_modes()


__all__ = ["SQLiteConnectionOwner"]

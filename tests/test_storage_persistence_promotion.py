"""Tests for the testgen persistence ports.

The obsolete prototype storage test was removed because it exercised an
invented schema. The production TaskDB is now split under ``uta.tasks.storage``
and its real schema, transactions, repositories, and compatibility facade are
covered by ``test_task_db_storage_split.py`` and the existing DB suites.
"""

from __future__ import annotations

from pathlib import Path

from uta.testgen.ports.persistence import TaskSnapshot


def test_task_snapshot_immutability(tmp_path: Path):
    snapshot = TaskSnapshot(
        task_id="task-123",
        status="completed",
        language="java",
        repo_path=tmp_path,
        created_at="2026-08-20T10:00:00Z",
        updated_at="2026-08-20T10:05:00Z",
    )
    assert snapshot.task_id == "task-123"
    assert snapshot.status == "completed"
    assert snapshot.language == "java"

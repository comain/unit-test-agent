"""Structural contract for the TaskDB storage decomposition."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import uta.tasks.storage.connection as connection_module
from uta.tasks.db import TaskDB
from uta.tasks.storage.connection import SQLiteConnectionOwner


REPO_ROOT = Path(__file__).resolve().parents[1]
STORAGE_ROOT = REPO_ROOT / "uta" / "tasks" / "storage"


def test_task_db_keeps_one_connection_and_transaction_owner():
    assert issubclass(TaskDB, SQLiteConnectionOwner)
    assert TaskDB.connect is SQLiteConnectionOwner.connect
    assert TaskDB.transaction is SQLiteConnectionOwner.transaction

    for path in STORAGE_ROOT.glob("*_repository.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sqlite3"
            and node.func.attr == "connect"
        ]
        assert calls == [], f"{path.name} opened its own SQLite connection"


def test_connection_fails_when_owner_only_mode_cannot_be_enforced(
    tmp_path, monkeypatch
):
    database = tmp_path / "tasks.sqlite"
    database.touch(mode=0o644)
    database.chmod(0o644)

    def reject_chmod(path, mode):
        raise PermissionError(f"cannot chmod {path} to {mode:o}")

    monkeypatch.setattr(connection_module.os, "chmod", reject_chmod)

    with pytest.raises(PermissionError, match="owner-only SQLite permissions"):
        SQLiteConnectionOwner(database).connect()


def test_task_db_facade_keeps_the_repository_method_signatures():
    expected = {
        "init",
        "create_repo_task",
        "create_class_task",
        "add_event",
        "start_workflow_operation",
        "acquire_next_task",
        "upsert_heartbeat",
    }
    assert expected <= set(dir(TaskDB))
    assert list(inspect.signature(TaskDB.create_repo_task).parameters) == [
        "self",
        "fields",
    ]
    assert list(inspect.signature(TaskDB.add_event).parameters)[:5] == [
        "self",
        "repo_task_id",
        "class_task_id",
        "event_type",
        "message",
    ]


def test_storage_modules_stay_within_the_decomposition_budget():
    line_counts = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in STORAGE_ROOT.glob("*.py")
    }
    assert line_counts["connection.py"] < 100
    assert max(line_counts.values()) < 600

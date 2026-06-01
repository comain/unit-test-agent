import sqlite3

from uta.tasks.db import TaskDB


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_task_db_init_backfills_target_identity_columns_on_old_db(tmp_path):
    db_path = tmp_path / "old_tasks.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE schema_version(version INTEGER NOT NULL);
        INSERT INTO schema_version(version) VALUES (1);

        CREATE TABLE repo_tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_path TEXT NOT NULL,
            repo_slug TEXT NOT NULL,
            module_filter TEXT,
            selection_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO repo_tasks(repo_path, repo_slug, module_filter, selection_json, created_at, updated_at)
        VALUES ('/repo', 'repo', NULL, '{"class_fqns":["pkg.Foo"]}', 'now', 'now');
        INSERT INTO repo_tasks(repo_path, repo_slug, module_filter, selection_json, created_at, updated_at)
        VALUES ('/pyrepo', 'pyrepo', NULL, '{"language":"python","targets":[]}', 'now', 'now');

        CREATE TABLE class_tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_task_id INTEGER NOT NULL,
            class_fqn TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO class_tasks(repo_task_id, class_fqn, created_at, updated_at)
        VALUES (1, 'pkg.Foo', 'now', 'now');
        """
    )
    conn.close()

    db = TaskDB(db_path)
    db.init()
    db.init()

    with db.connect() as conn:
        repo_cols = _columns(conn, "repo_tasks")
        class_cols = _columns(conn, "class_tasks")
        assert {"language", "estimate_snapshot_json", "total_classes"}.issubset(repo_cols)
        assert {
            "language",
            "target_id",
            "source_path",
            "symbol",
            "target_granularity",
            "display_name",
        }.issubset(class_cols)

        repo = conn.execute("SELECT language FROM repo_tasks WHERE id=1").fetchone()
        py_repo = conn.execute("SELECT language FROM repo_tasks WHERE id=2").fetchone()
        row = conn.execute("SELECT * FROM class_tasks WHERE id=1").fetchone()
        versions = [item["version"] for item in conn.execute("SELECT version FROM schema_version ORDER BY version")]
        indexes = {item["name"] for item in conn.execute("PRAGMA index_list(class_tasks)")}

    assert repo["language"] == "java"
    assert py_repo["language"] == "python"
    assert row["language"] == "java"
    assert row["target_id"] == "pkg.Foo"
    assert row["symbol"] == "pkg.Foo"
    assert row["target_granularity"] == "class"
    assert row["display_name"] == "pkg.Foo"
    assert versions == [1, 2]
    assert "idx_class_tasks_language_target" in indexes


def test_task_db_init_clears_stale_python_file_symbol(tmp_path):
    db = TaskDB(tmp_path / "tasks.db")
    db.init()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO repo_tasks(repo_path, repo_slug, selection_json, language, created_at, updated_at)
            VALUES ('/repo', 'repo', '{"language":"python"}', 'python', 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO class_tasks(
                repo_task_id, class_fqn, language, target_id, source_path, symbol,
                target_granularity, display_name, created_at, updated_at
            )
            VALUES (
                1, 'pyfile:jobs/forecast.py', 'python', 'pyfile:jobs/forecast.py',
                'jobs/forecast.py', 'pyfile:jobs/forecast.py', 'file', 'jobs/forecast.py', 'now', 'now'
            )
            """
        )

    db.init()

    with db.connect() as conn:
        row = conn.execute("SELECT symbol FROM class_tasks WHERE id=1").fetchone()
        incomplete = TaskDB._has_incomplete_target_identity(conn)
    assert row["symbol"] is None
    assert incomplete is False


def test_task_db_init_treats_python_file_without_symbol_as_complete(tmp_path):
    db = TaskDB(tmp_path / "tasks.db")
    db.init()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO repo_tasks(repo_path, repo_slug, selection_json, language, created_at, updated_at)
            VALUES ('/repo', 'repo', '{"language":"python"}', 'python', 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO class_tasks(
                repo_task_id, class_fqn, language, target_id, source_path, symbol,
                target_granularity, display_name, created_at, updated_at
            )
            VALUES (
                1, 'pyfile:jobs/forecast.py', 'python', 'pyfile:jobs/forecast.py',
                'jobs/forecast.py', NULL, 'file', 'jobs/forecast.py', 'now', 'now'
            )
            """
        )

    db.init()

    with db.connect() as conn:
        row = conn.execute("SELECT symbol FROM class_tasks WHERE id=1").fetchone()
        incomplete = TaskDB._has_incomplete_target_identity(conn)
    assert row["symbol"] is None
    assert incomplete is False

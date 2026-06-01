import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Union

from uta.tasks.models import DEFAULT_PRIORITY, json_dumps, json_loads, now_iso


def _storage_symbol(*, language: str, target_granularity: str, class_fqn: str, symbol: Optional[str]) -> Optional[str]:
    if symbol:
        return symbol
    if (language or "java") == "java" and (target_granularity or "class") == "class":
        return class_fqn
    return None


REPO_TASK_EXTRA_COLUMNS: Dict[str, str] = {
    "language": "TEXT NOT NULL DEFAULT 'java'",
    "budget_config_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
    "estimate_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
    "total_classes": "INTEGER NOT NULL DEFAULT 0",
    "completed_classes": "INTEGER NOT NULL DEFAULT 0",
    "passed_classes": "INTEGER NOT NULL DEFAULT 0",
    "failed_classes": "INTEGER NOT NULL DEFAULT 0",
    "skipped_classes": "INTEGER NOT NULL DEFAULT 0",
    "coverage_avg": "REAL",
    "coverage_min": "REAL",
    "mutation_avg": "REAL",
    "mutation_min": "REAL",
    "input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_read_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
    "output_tokens": "INTEGER NOT NULL DEFAULT 0",
    "reasoning_tokens": "INTEGER NOT NULL DEFAULT 0",
    "total_tokens": "INTEGER NOT NULL DEFAULT 0",
    "estimated_total_tokens": "INTEGER",
    "estimated_cost_usd": "REAL",
    "provider_cost_usd": "REAL",
    "estimated_elapsed_seconds": "REAL",
    "actual_elapsed_seconds": "REAL",
    "budget_used_ratio": "REAL",
    "latest_report_path": "TEXT",
    "latest_live_status_path": "TEXT",
    "latest_commit": "TEXT",
    "remote_ref": "TEXT",
    "ci_context_json": "TEXT NOT NULL DEFAULT '{}'",
    "ci_context_path": "TEXT",
    "error": "TEXT",
    "session_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    "hard_cap_usd": "REAL",
}


CLASS_TASK_EXTRA_COLUMNS: Dict[str, str] = {
    "language": "TEXT NOT NULL DEFAULT 'java'",
    "target_id": "TEXT",
    "source_path": "TEXT",
    "symbol": "TEXT",
    "target_granularity": "TEXT",
    "display_name": "TEXT",
    "batch_key": "TEXT",
    "stage": "TEXT",
    "attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "llm_turn_count": "INTEGER NOT NULL DEFAULT 0",
    "coverage": "REAL",
    "surviving_mutants": "INTEGER",
    "total_mutants": "INTEGER",
    "phase_token_usage_json": "TEXT NOT NULL DEFAULT '{}'",
    "input_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_read_tokens": "INTEGER NOT NULL DEFAULT 0",
    "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
    "output_tokens": "INTEGER NOT NULL DEFAULT 0",
    "reasoning_tokens": "INTEGER NOT NULL DEFAULT 0",
    "total_tokens": "INTEGER NOT NULL DEFAULT 0",
    "estimated_cache_read_tokens": "INTEGER",
    "estimated_reasoning_tokens": "INTEGER",
    "estimated_total_tokens": "INTEGER",
    "estimated_cost_usd": "REAL",
    "provider_cost_usd": "REAL",
    "actual_elapsed_seconds": "REAL",
    "commit_sha": "TEXT",
    "pushed_at": "TEXT",
    "error": "TEXT",
}


RUNNER_HEARTBEAT_EXTRA_COLUMNS: Dict[str, str] = {
    "started_at": "TEXT",
    "heartbeat_at": "TEXT",
    "current_repo_task_id": "INTEGER",
    "loaded_config_hash": "TEXT",
}

TASK_EVENTS_EXTRA_COLUMNS: Dict[str, str] = {"ts": "TEXT"}
TASK_CONTROL_EXTRA_COLUMNS: Dict[str, str] = {
    "requested_action": "TEXT",
    "handled_at": "TEXT",
}


def default_db_path() -> Path:
    configured = os.environ.get("UTA_TASK_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    runner_home = os.environ.get("UTA_RUNNER_HOME")
    if runner_home:
        return (Path(runner_home).expanduser() / "uta_tasks.db").resolve()
    return (Path.home() / ".local" / "share" / "uta" / "uta_tasks.db").resolve()


class TaskDB:
    def __init__(self, path: Optional[Union[os.PathLike[str], str]] = None):
        self.path = Path(path).expanduser().resolve() if path else default_db_path()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
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

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS repo_branches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_path TEXT NOT NULL,
                    repo_slug TEXT NOT NULL,
                    base_ref TEXT NOT NULL DEFAULT 'origin/master',
                    branch_profile TEXT NOT NULL DEFAULT 'default',
                    branch_name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_task_id INTEGER,
                    UNIQUE(repo_path, base_ref, branch_profile, branch_name)
                );

                CREATE TABLE IF NOT EXISTS repo_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_path TEXT NOT NULL,
                    repo_slug TEXT NOT NULL,
                    module_filter TEXT,
                    selection_json TEXT NOT NULL DEFAULT '{}',
                    language TEXT NOT NULL DEFAULT 'java',
                    branch_id INTEGER,
                    branch_name TEXT,
                    base_ref TEXT NOT NULL DEFAULT 'origin/master',
                    priority INTEGER NOT NULL DEFAULT 100,
                    status TEXT NOT NULL DEFAULT 'CREATED',
                    current_stage TEXT,
                    current_detail TEXT,
                    coverage_gate REAL,
                    mutation_gate REAL,
                    estimated_input_tokens INTEGER,
                    estimated_output_tokens INTEGER,
                    estimated_cache_read_tokens INTEGER,
                    estimated_cost REAL,
                    estimated_seconds REAL,
                    actual_input_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_output_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_cost REAL,
                    elapsed_seconds REAL,
                    report_path TEXT,
                    run_log_path TEXT,
                    config_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    budget_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    budget_config_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    estimate_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    total_classes INTEGER NOT NULL DEFAULT 0,
                    completed_classes INTEGER NOT NULL DEFAULT 0,
                    passed_classes INTEGER NOT NULL DEFAULT 0,
                    failed_classes INTEGER NOT NULL DEFAULT 0,
                    skipped_classes INTEGER NOT NULL DEFAULT 0,
                    coverage_avg REAL,
                    coverage_min REAL,
                    mutation_avg REAL,
                    mutation_min REAL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_total_tokens INTEGER,
                    estimated_cost_usd REAL,
                    provider_cost_usd REAL,
                    estimated_elapsed_seconds REAL,
                    actual_elapsed_seconds REAL,
                    budget_used_ratio REAL,
                    latest_report_path TEXT,
                    latest_live_status_path TEXT,
                    latest_commit TEXT,
                    remote_ref TEXT,
                    ci_context_json TEXT NOT NULL DEFAULT '{}',
                    ci_context_path TEXT,
                    error TEXT,
                    session_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    stop_requested_at TEXT,
                    resume_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    FOREIGN KEY(branch_id) REFERENCES repo_branches(id)
                );

                CREATE TABLE IF NOT EXISTS class_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_task_id INTEGER NOT NULL,
                    class_fqn TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT 'java',
                    target_id TEXT,
                    source_path TEXT,
                    symbol TEXT,
                    target_granularity TEXT,
                    display_name TEXT,
                    module TEXT,
                    priority INTEGER NOT NULL DEFAULT 100,
                    status TEXT NOT NULL DEFAULT 'CREATED',
                    current_stage TEXT,
                    current_detail TEXT,
                    batch_key TEXT,
                    stage TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    llm_turn_count INTEGER NOT NULL DEFAULT 0,
                    coverage_line REAL,
                    coverage REAL,
                    mutation_score REAL,
                    surviving_mutants INTEGER,
                    total_mutants INTEGER,
                    test_count INTEGER,
                    test_file_path TEXT,
                    test_file_lines INTEGER,
                    session_ids_json TEXT NOT NULL DEFAULT '[]',
                    phase_token_usage_json TEXT NOT NULL DEFAULT '{}',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_input_tokens INTEGER,
                    estimated_cache_read_tokens INTEGER,
                    estimated_output_tokens INTEGER,
                    estimated_reasoning_tokens INTEGER,
                    estimated_total_tokens INTEGER,
                    estimated_cost REAL,
                    estimated_cost_usd REAL,
                    provider_cost_usd REAL,
                    estimated_seconds REAL,
                    actual_input_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_output_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_cost REAL,
                    elapsed_seconds REAL,
                    actual_elapsed_seconds REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    commit_sha TEXT,
                    pushed_at TEXT,
                    error TEXT,
                    last_error TEXT,
                    UNIQUE(repo_task_id, class_fqn),
                    FOREIGN KEY(repo_task_id) REFERENCES repo_tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_task_id INTEGER,
                    class_task_id INTEGER,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'INFO',
                    stage TEXT,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    ts TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(repo_task_id) REFERENCES repo_tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(class_task_id) REFERENCES class_tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS task_control (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_task_id INTEGER NOT NULL,
                    class_task_id INTEGER,
                    action TEXT NOT NULL,
                    requested_action TEXT,
                    reason TEXT,
                    acknowledged_at TEXT,
                    handled_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(repo_task_id) REFERENCES repo_tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(class_task_id) REFERENCES class_tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runner_heartbeats (
                    runner_id TEXT PRIMARY KEY,
                    repo_task_id INTEGER,
                    current_repo_task_id INTEGER,
                    pid INTEGER,
                    hostname TEXT,
                    status TEXT NOT NULL,
                    message TEXT,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    loaded_config_hash TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_columns(conn, "repo_tasks", REPO_TASK_EXTRA_COLUMNS)
            self._ensure_columns(conn, "class_tasks", CLASS_TASK_EXTRA_COLUMNS)
            self._ensure_columns(conn, "task_events", TASK_EVENTS_EXTRA_COLUMNS)
            self._ensure_columns(conn, "task_control", TASK_CONTROL_EXTRA_COLUMNS)
            self._ensure_columns(conn, "runner_heartbeats", RUNNER_HEARTBEAT_EXTRA_COLUMNS)
            current_version = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
            if current_version < 2 or self._has_incomplete_target_identity(conn):
                self._migrate_target_identity(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_repo_tasks_language ON repo_tasks(language)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_class_tasks_language_target ON class_tasks(language, target_id)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_class_tasks_source_path ON class_tasks(source_path)")
            version = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
            if not version:
                conn.execute("INSERT INTO schema_version(version) VALUES (1)")
            has_v2 = conn.execute("SELECT 1 FROM schema_version WHERE version >= 2 LIMIT 1").fetchone()
            if not has_v2:
                conn.execute("INSERT INTO schema_version(version) VALUES (2)")

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @staticmethod
    def _has_incomplete_target_identity(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            """
            SELECT 1 FROM repo_tasks
            WHERE language IS NULL OR language = ''
            LIMIT 1
            """
        ).fetchone()
        if row:
            return True
        row = conn.execute(
            """
            SELECT 1 FROM class_tasks
            WHERE target_id IS NULL
               OR target_id = ''
               OR language IS NULL
               OR language = ''
               OR target_granularity IS NULL
               OR target_granularity = ''
               OR display_name IS NULL
               OR display_name = ''
               OR (
                    COALESCE(NULLIF(language, ''), 'java') = 'java'
                    AND COALESCE(NULLIF(target_granularity, ''), 'class') = 'class'
                    AND (symbol IS NULL OR symbol = '')
               )
               OR (
                    COALESCE(NULLIF(language, ''), 'java') != 'java'
                    AND COALESCE(NULLIF(target_granularity, ''), 'class') NOT IN ('file', 'module')
                    AND (symbol IS NULL OR symbol = '')
               )
               OR (
                    language != 'java'
                    AND target_granularity IN ('file', 'module')
                    AND symbol = target_id
               )
            LIMIT 1
            """
        ).fetchone()
        return bool(row)

    @staticmethod
    def _migrate_target_identity(conn: sqlite3.Connection) -> None:
        for row in conn.execute("SELECT id, selection_json, language FROM repo_tasks"):
            selection = json_loads(row["selection_json"])
            selected_language = selection.get("language") if isinstance(selection, dict) else None
            language = selected_language or row["language"] or "java"
            if language != row["language"]:
                conn.execute("UPDATE repo_tasks SET language=? WHERE id=?", (language, row["id"]))
        conn.execute(
            """
            UPDATE class_tasks
            SET
                language = COALESCE(NULLIF(language, ''), 'java'),
                target_id = COALESCE(NULLIF(target_id, ''), class_fqn),
                target_granularity = COALESCE(NULLIF(target_granularity, ''), 'class'),
                display_name = COALESCE(NULLIF(display_name, ''), class_fqn),
                symbol = COALESCE(
                    NULLIF(symbol, ''),
                    CASE
                        WHEN COALESCE(NULLIF(language, ''), 'java') = 'java'
                          OR COALESCE(NULLIF(target_granularity, ''), 'class') NOT IN ('file', 'module')
                        THEN class_fqn
                        ELSE NULL
                    END
                )
            WHERE target_id IS NULL
               OR target_id = ''
               OR language IS NULL
               OR language = ''
               OR target_granularity IS NULL
               OR target_granularity = ''
               OR display_name IS NULL
               OR display_name = ''
               OR (
                    COALESCE(NULLIF(language, ''), 'java') = 'java'
                    AND COALESCE(NULLIF(target_granularity, ''), 'class') = 'class'
                    AND (symbol IS NULL OR symbol = '')
               )
               OR (
                    COALESCE(NULLIF(language, ''), 'java') != 'java'
                    AND COALESCE(NULLIF(target_granularity, ''), 'class') NOT IN ('file', 'module')
                    AND (symbol IS NULL OR symbol = '')
               )
            """
        )
        conn.execute(
            """
            UPDATE class_tasks
            SET symbol = NULL
            WHERE language != 'java'
              AND target_granularity IN ('file', 'module')
              AND symbol = target_id
            """
        )

    def add_event(
        self,
        repo_task_id: Optional[int],
        class_task_id: Optional[int],
        event_type: str,
        message: str,
        *,
        stage: Optional[str] = None,
        severity: str = "INFO",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_events(repo_task_id, class_task_id, event_type, severity, stage, message, payload_json, ts, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (repo_task_id, class_task_id, event_type, severity, stage, message, json_dumps(payload), now, now),
            )

    def create_or_reuse_branch(
        self,
        *,
        repo_path: str,
        repo_slug: str,
        base_ref: str = "origin/master",
        branch_profile: str = "default",
        branch_name: Optional[str] = None,
        new_branch: bool = False,
    ) -> sqlite3.Row:
        now = now_iso()
        with self.transaction() as conn:
            if not branch_name and not new_branch:
                existing = conn.execute(
                    """
                    SELECT * FROM repo_branches
                    WHERE repo_path=? AND base_ref=? AND branch_profile=? AND active=1
                    ORDER BY updated_at DESC, id DESC LIMIT 1
                    """,
                    (repo_path, base_ref, branch_profile),
                ).fetchone()
                if existing:
                    return existing
            if not branch_name:
                date = now[:10].replace("-", "")
                base = f"uta/{repo_slug}/{date}"
                branch_name = base
                suffix = 2
                while conn.execute(
                    "SELECT 1 FROM repo_branches WHERE repo_path=? AND branch_name=?",
                    (repo_path, branch_name),
                ).fetchone():
                    branch_name = f"{base}-{suffix}"
                    suffix += 1
            conn.execute(
                """
                INSERT OR IGNORE INTO repo_branches(
                    repo_path, repo_slug, base_ref, branch_profile, branch_name, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (repo_path, repo_slug, base_ref, branch_profile, branch_name, now, now),
            )
            return conn.execute(
                """
                SELECT * FROM repo_branches
                WHERE repo_path=? AND base_ref=? AND branch_profile=? AND branch_name=?
                """,
                (repo_path, base_ref, branch_profile, branch_name),
            ).fetchone()

    def create_repo_task(self, fields: Dict[str, Any]) -> int:
        now = now_iso()
        values = {
            "repo_path": fields["repo_path"],
            "repo_slug": fields["repo_slug"],
            "module_filter": fields.get("module_filter"),
            "selection_json": json_dumps(fields.get("selection")),
            "language": fields.get("language", "java"),
            "branch_id": fields.get("branch_id"),
            "branch_name": fields.get("branch_name"),
            "base_ref": fields.get("base_ref", "origin/master"),
            "priority": int(fields.get("priority", DEFAULT_PRIORITY)),
            "status": fields.get("status", "CREATED"),
            "coverage_gate": fields.get("coverage_gate"),
            "mutation_gate": fields.get("mutation_gate"),
            "estimated_input_tokens": fields.get("estimated_input_tokens"),
            "estimated_output_tokens": fields.get("estimated_output_tokens"),
            "estimated_cache_read_tokens": fields.get("estimated_cache_read_tokens"),
            "estimated_total_tokens": fields.get("estimated_total_tokens"),
            "estimated_cost": fields.get("estimated_cost"),
            "estimated_cost_usd": fields.get("estimated_cost_usd", fields.get("estimated_cost")),
            "estimated_seconds": fields.get("estimated_seconds"),
            "estimated_elapsed_seconds": fields.get("estimated_elapsed_seconds", fields.get("estimated_seconds")),
            "config_snapshot_json": json_dumps(fields.get("config_snapshot")),
            "budget_snapshot_json": json_dumps(fields.get("budget_snapshot")),
            "budget_config_snapshot_json": json_dumps(fields.get("budget_snapshot")),
            "estimate_snapshot_json": json_dumps(fields.get("estimate_snapshot")),
            "ci_context_json": json_dumps(fields.get("ci_context")),
            "ci_context_path": fields.get("ci_context_path"),
            "total_classes": int(fields.get("total_classes") or 0),
            "created_at": now,
            "updated_at": now,
        }
        if "hard_cap_usd" in fields:
            values["hard_cap_usd"] = fields["hard_cap_usd"]
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"INSERT INTO repo_tasks({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            task_id = int(cursor.lastrowid)
            if values["branch_id"]:
                conn.execute(
                    "UPDATE repo_branches SET last_task_id=?, updated_at=? WHERE id=?",
                    (task_id, now, values["branch_id"]),
                )
            return task_id

    def create_class_task(
        self,
        repo_task_id: int,
        class_fqn: str,
        *,
        module: Optional[str],
        priority: int,
        language: str = "java",
        target_id: Optional[str] = None,
        source_path: Optional[str] = None,
        symbol: Optional[str] = None,
        target_granularity: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> int:
        now = now_iso()
        target_id = target_id or class_fqn
        target_granularity = target_granularity or "class"
        display_name = display_name or class_fqn
        symbol = _storage_symbol(language=language, target_granularity=target_granularity, class_fqn=class_fqn, symbol=symbol)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO class_tasks(
                    repo_task_id, class_fqn, language, target_id, source_path, symbol, target_granularity,
                    display_name, module, priority, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', ?, ?)
                """,
                (
                    repo_task_id,
                    class_fqn,
                    language or "java",
                    target_id,
                    source_path,
                    symbol,
                    target_granularity,
                    display_name,
                    module,
                    int(priority),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE class_tasks
                SET
                    language=COALESCE(NULLIF(language, ''), ?),
                    target_id=COALESCE(NULLIF(target_id, ''), ?),
                    source_path=COALESCE(source_path, ?),
                    symbol=COALESCE(NULLIF(symbol, ''), ?),
                    target_granularity=COALESCE(NULLIF(target_granularity, ''), ?),
                    display_name=COALESCE(NULLIF(display_name, ''), ?)
                WHERE repo_task_id=? AND class_fqn=?
                """,
                (
                    language or "java",
                    target_id,
                    source_path,
                    symbol,
                    target_granularity,
                    display_name,
                    repo_task_id,
                    class_fqn,
                ),
            )
            row = conn.execute(
                "SELECT id FROM class_tasks WHERE repo_task_id=? AND class_fqn=?",
                (repo_task_id, class_fqn),
            ).fetchone()
            return int(row["id"])

    def get_repo_task(self, task_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM repo_tasks WHERE id=?", (task_id,)).fetchone()

    def count_task_failures(self, task_id: int) -> int:
        """Count how many times this repo task has been marked FAILED (task_failed events)."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE repo_task_id=? AND event_type='task_failed'",
                (task_id,),
            ).fetchone()
            return int(row["n"] or 0) if row else 0

    def total_provider_cost_usd(self) -> float:
        """Sum provider_cost_usd across all repo tasks for global batch cap enforcement."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(provider_cost_usd), 0.0) AS total FROM repo_tasks"
            ).fetchone()
            return float(row["total"] or 0.0) if row else 0.0

    def list_repo_tasks(
        self,
        *,
        status: Optional[str] = None,
        repo_path: Optional[str] = None,
        limit: int = 50,
    ) -> List[sqlite3.Row]:
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if repo_path:
            clauses.append("repo_path=?")
            params.append(repo_path)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            return list(conn.execute(
                f"""
                SELECT * FROM repo_tasks {where}
                ORDER BY
                    CASE status WHEN 'RUNNING' THEN 0 WHEN 'QUEUED' THEN 1 WHEN 'CREATED' THEN 2 ELSE 3 END,
                    priority ASC, created_at DESC
                LIMIT ?
                """,
                (*params, int(limit)),
            ))

    def list_class_tasks(self, repo_task_id: int) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                "SELECT * FROM class_tasks WHERE repo_task_id=? ORDER BY priority ASC, id ASC",
                (repo_task_id,),
            ))

    def get_class_task(self, class_task_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM class_tasks WHERE id=?", (class_task_id,)).fetchone()

    def find_class_task(self, repo_task_id: int, class_fqn: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM class_tasks WHERE repo_task_id=? AND class_fqn=?",
                (repo_task_id, class_fqn),
            ).fetchone()

    def update_repo_task(self, task_id: int, **fields: Any) -> None:
        self._update("repo_tasks", task_id, fields)

    def update_class_task(self, class_task_id: int, **fields: Any) -> None:
        self._update("class_tasks", class_task_id, fields)

    def _update(self, table: str, row_id: int, fields: Dict[str, Any]) -> None:
        if not fields:
            return
        now = now_iso()
        data = dict(fields)
        data["updated_at"] = now
        assignments = ", ".join(f"{key}=?" for key in data)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE {table} SET {assignments} WHERE id=?",
                (*data.values(), row_id),
            )

    def aggregate_repo_task(self, task_id: int) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS class_count,
                    SUM(CASE WHEN status IN ('PASS','FAIL','MUTATION_FAIL','BUDGET_EXCEEDED','PROVIDER_ERROR','PROVIDER_RATE_LIMITED','PUSH_FAILED','UNSAFE_DIFF','LLM_STALLED','CANCELLED') THEN 1 ELSE 0 END) AS completed_classes,
                    SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) AS passed_classes,
                    SUM(CASE WHEN status NOT IN ('PASS','CREATED','PENDING','QUEUED','RUNNING','STOPPED','GENERATED') THEN 1 ELSE 0 END) AS failed_classes,
                    SUM(actual_input_tokens) AS actual_input_tokens,
                    SUM(actual_output_tokens) AS actual_output_tokens,
                    SUM(actual_cache_read_tokens) AS actual_cache_read_tokens,
                    SUM(actual_cache_write_tokens) AS actual_cache_write_tokens,
                    SUM(input_tokens) AS input_tokens,
                    SUM(output_tokens) AS output_tokens,
                    SUM(cache_read_tokens) AS cache_read_tokens,
                    SUM(cache_write_tokens) AS cache_write_tokens,
                    SUM(reasoning_tokens) AS reasoning_tokens,
                    SUM(total_tokens) AS total_tokens,
                    SUM(COALESCE(actual_cost, 0)) AS actual_cost,
                    SUM(COALESCE(provider_cost_usd, 0)) AS provider_cost_usd,
                    AVG(COALESCE(coverage_line, coverage)) AS coverage_line_avg,
                    MIN(COALESCE(coverage_line, coverage)) AS coverage_line_min,
                    AVG(mutation_score) AS mutation_score_avg,
                    MIN(mutation_score) AS mutation_score_min
                FROM class_tasks
                WHERE repo_task_id=?
                """,
                (task_id,),
            ).fetchone()
            statuses = {
                row["status"]: row["count"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM class_tasks WHERE repo_task_id=? GROUP BY status",
                    (task_id,),
                )
            }
        return {**dict(row), "statuses": statuses}

    def request_control(
        self,
        repo_task_id: int,
        action: str,
        *,
        reason: Optional[str] = None,
        class_task_id: Optional[int] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO task_control(repo_task_id, class_task_id, action, requested_action, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (repo_task_id, class_task_id, action, action, reason, now_iso()),
            )

    def latest_events(self, repo_task_id: int, *, limit: int = 20) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(
                """
                SELECT * FROM task_events
                WHERE repo_task_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (repo_task_id, int(limit)),
            ))

    def acquire_next_task(self, *, allow_same_repo_concurrency: bool = False, include_failed: bool = False) -> Optional[sqlite3.Row]:
        now = now_iso()
        statuses = "'CREATED', 'QUEUED'"
        if include_failed:
            statuses += ", 'FAILED'"
        with self.transaction() as conn:
            rows = list(conn.execute(
                f"""
                SELECT * FROM repo_tasks
                WHERE status IN ({statuses})
                ORDER BY priority ASC, created_at ASC
                LIMIT 20
                """
            ))
            for row in rows:
                if not allow_same_repo_concurrency:
                    active = conn.execute(
                        """
                        SELECT 1 FROM repo_tasks
                        WHERE repo_path=? AND status IN ('RUNNING', 'STOP_REQUESTED') AND id<>?
                        LIMIT 1
                        """,
                        (row["repo_path"], row["id"]),
                    ).fetchone()
                    if active:
                        continue
                conn.execute(
                    """
                    UPDATE repo_tasks
                    SET status='RUNNING', started_at=COALESCE(started_at, ?), updated_at=?, current_stage='acquired'
                    WHERE id=?
                    """,
                    (now, now, row["id"]),
                )
                return conn.execute("SELECT * FROM repo_tasks WHERE id=?", (row["id"],)).fetchone()
        return None

    def upsert_heartbeat(
        self,
        runner_id: str,
        *,
        repo_task_id: Optional[int],
        pid: int,
        hostname: str,
        status: str,
        message: Optional[str] = None,
        loaded_config_hash: Optional[str] = None,
    ) -> None:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runner_heartbeats(runner_id, repo_task_id, current_repo_task_id, pid, hostname, status, message, started_at, heartbeat_at, loaded_config_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(runner_id) DO UPDATE SET
                    repo_task_id=excluded.repo_task_id,
                    current_repo_task_id=excluded.current_repo_task_id,
                    pid=excluded.pid,
                    hostname=excluded.hostname,
                    status=excluded.status,
                    message=excluded.message,
                    heartbeat_at=excluded.heartbeat_at,
                    loaded_config_hash=excluded.loaded_config_hash,
                    updated_at=excluded.updated_at
                """,
                (runner_id, repo_task_id, repo_task_id, pid, hostname, status, message, now, now, loaded_config_hash, now, now),
            )

    def latest_control(self, repo_task_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM task_control
                WHERE repo_task_id=? AND acknowledged_at IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (repo_task_id,),
            ).fetchone()

    def acknowledge_controls(self, repo_task_id: int, *, action: Optional[str] = None) -> None:
        now = now_iso()
        clauses = ["repo_task_id=?", "acknowledged_at IS NULL"]
        params: List[Any] = [repo_task_id]
        if action:
            clauses.append("action=?")
            params.append(action)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE task_control SET acknowledged_at=?, handled_at=? WHERE {' AND '.join(clauses)}",
                (now, now, *params),
            )

    def latest_heartbeat(self) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM runner_heartbeats ORDER BY updated_at DESC LIMIT 1").fetchone()

    def class_tasks_by_fqn(self, repo_task_id: int, fqns: Iterable[str]) -> Dict[str, sqlite3.Row]:
        values = list(dict.fromkeys(fqns))
        if not values:
            return {}
        placeholders = ",".join("?" for _ in values)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM class_tasks WHERE repo_task_id=? AND class_fqn IN ({placeholders})",
                (repo_task_id, *values),
            ).fetchall()
        return {row["class_fqn"]: row for row in rows}

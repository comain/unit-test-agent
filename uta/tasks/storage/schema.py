"""Creating the tables and migrating them forward.

Initialization retains the original connection context and SQL verbatim, so
existing databases see the same migration and idempotency behavior.

A mixin rather than a collaborator: `TaskDB` owns the one connection and the
one transaction, and these read and write through it. Handing each its own
connection is exactly how an atomic transition becomes several.
"""

from __future__ import annotations

import sqlite3
from typing import Dict

from uta.tasks.models import json_loads
from uta.tasks.storage.base import (
    CLASS_TASK_EXTRA_COLUMNS,
    REPO_TASK_EXTRA_COLUMNS,
    RUNNER_HEARTBEAT_EXTRA_COLUMNS,
    TASK_CONTROL_EXTRA_COLUMNS,
    TASK_EVENTS_EXTRA_COLUMNS,
)


class SchemaMixin:
    """The schema half of `TaskDB`."""

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS java_hanging_tests (
                    repo_slug TEXT NOT NULL,
                    test_class TEXT NOT NULL,
                    module TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    observations INTEGER NOT NULL,
                    PRIMARY KEY (repo_slug, test_class)
                );

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
                    rdc_context_json TEXT NOT NULL DEFAULT '{}',
                    rdc_context_path TEXT,
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
                    mutation_detail TEXT,
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

                CREATE TABLE IF NOT EXISTS workflow_operations (
                    operation_id TEXT PRIMARY KEY,
                    repo_task_id INTEGER NOT NULL,
                    workflow_run_id TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    operation_step TEXT NOT NULL
                        CHECK(operation_step IN ('turn','interpret','deterministic')),
                    attempt INTEGER NOT NULL CHECK(attempt >= 0),
                    execution_ordinal INTEGER NOT NULL DEFAULT 0
                        CHECK(execution_ordinal >= 0),
                    status TEXT NOT NULL
                        CHECK(status IN ('STARTED','COMPLETED','FAILED','CANCELLED')),
                    input_fingerprint TEXT NOT NULL,
                    resulting_workspace_fingerprint TEXT,
                    result_artifact_path TEXT,
                    result_artifact_sha256 TEXT,
                    output_fingerprints_json TEXT NOT NULL DEFAULT '{}',
                    prerequisite_operation_ids_json TEXT NOT NULL DEFAULT '[]',
                    schema_version INTEGER NOT NULL,
                    session_id TEXT,
                    superseded_by_operation_id TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    error_kind TEXT,
                    error TEXT,
                    FOREIGN KEY(repo_task_id) REFERENCES repo_tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS workflow_operations_recovery_idx
                    ON workflow_operations(
                        repo_task_id, workflow_run_id, unit_id, phase,
                        operation_step, status
                    );

                CREATE TABLE IF NOT EXISTS workflow_operation_accounting (
                    operation_id TEXT NOT NULL,
                    paid_attempt_ordinal INTEGER NOT NULL CHECK(paid_attempt_ordinal > 0),
                    provider_cost_usd REAL,
                    cost_provenance TEXT NOT NULL
                        CHECK(cost_provenance IN ('recorded','unavailable')),
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(operation_id, paid_attempt_ordinal),
                    FOREIGN KEY(operation_id) REFERENCES workflow_operations(operation_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS workflow_operation_accounting_cost_idx
                    ON workflow_operation_accounting(operation_id, cost_provenance);

                CREATE TABLE IF NOT EXISTS workflow_run_supersessions (
                    supersession_id TEXT PRIMARY KEY,
                    repo_task_id INTEGER NOT NULL,
                    prior_identity_kind TEXT NOT NULL
                        CHECK(prior_identity_kind IN ('known','missing')),
                    old_workflow_run_id TEXT,
                    new_workflow_run_id TEXT NOT NULL,
                    old_unit_ids_json TEXT NOT NULL,
                    new_unit_ids_json TEXT NOT NULL,
                    affected_class_ids_json TEXT NOT NULL,
                    requeue_policy TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    CHECK(
                        (prior_identity_kind='known' AND old_workflow_run_id IS NOT NULL)
                        OR
                        (prior_identity_kind='missing' AND old_workflow_run_id IS NULL)
                    ),
                    FOREIGN KEY(repo_task_id) REFERENCES repo_tasks(id) ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS workflow_run_supersessions_known_idx
                    ON workflow_run_supersessions(
                        repo_task_id, old_workflow_run_id, new_workflow_run_id
                    )
                    WHERE old_workflow_run_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS legacy_prompt_scopes (
                    repo_task_id INTEGER NOT NULL,
                    legacy_run_id TEXT NOT NULL,
                    language TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    superseded_at TEXT,
                    superseded_by_workflow_run_id TEXT,
                    PRIMARY KEY(repo_task_id, legacy_run_id),
                    FOREIGN KEY(repo_task_id) REFERENCES repo_tasks(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS legacy_prompt_scopes_retention_idx
                    ON legacy_prompt_scopes(superseded_at, repo_task_id);

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
            self._ensure_columns(
                conn, "runner_heartbeats", RUNNER_HEARTBEAT_EXTRA_COLUMNS
            )
            current_version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version"
            ).fetchone()[0]
            if current_version < 2 or self._has_incomplete_target_identity(conn):
                self._migrate_target_identity(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_repo_tasks_language ON repo_tasks(language)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_class_tasks_language_target ON class_tasks(language, target_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_class_tasks_source_path ON class_tasks(source_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS task_events_cursor_idx "
                "ON task_events(repo_task_id, id)"
            )
            version = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
            if not version:
                conn.execute("INSERT INTO schema_version(version) VALUES (1)")
            has_v2 = conn.execute(
                "SELECT 1 FROM schema_version WHERE version >= 2 LIMIT 1"
            ).fetchone()
            if not has_v2:
                conn.execute("INSERT INTO schema_version(version) VALUES (2)")
            has_v3 = conn.execute(
                "SELECT 1 FROM schema_version WHERE version >= 3 LIMIT 1"
            ).fetchone()
            if not has_v3:
                conn.execute("INSERT INTO schema_version(version) VALUES (3)")
            has_v4 = conn.execute(
                "SELECT 1 FROM schema_version WHERE version >= 4 LIMIT 1"
            ).fetchone()
            if not has_v4:
                conn.execute("INSERT INTO schema_version(version) VALUES (4)")
            has_v5 = conn.execute(
                "SELECT 1 FROM schema_version WHERE version >= 5 LIMIT 1"
            ).fetchone()
            if not has_v5:
                conn.execute("INSERT INTO schema_version(version) VALUES (5)")
            has_v6 = conn.execute(
                "SELECT 1 FROM schema_version WHERE version >= 6 LIMIT 1"
            ).fetchone()
            if not has_v6:
                conn.execute("INSERT INTO schema_version(version) VALUES (6)")
            has_v7 = conn.execute(
                "SELECT 1 FROM schema_version WHERE version >= 7 LIMIT 1"
            ).fetchone()
            if not has_v7:
                conn.execute("INSERT INTO schema_version(version) VALUES (7)")

    @staticmethod
    def _ensure_columns(
        conn: sqlite3.Connection, table: str, columns: Dict[str, str]
    ) -> None:
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
            selected_language = (
                selection.get("language") if isinstance(selection, dict) else None
            )
            language = selected_language or row["language"] or "java"
            if language != row["language"]:
                conn.execute(
                    "UPDATE repo_tasks SET language=? WHERE id=?", (language, row["id"])
                )
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

"""Repo tasks, class tasks, and the branch they run on.

`find_class_tasks` takes a batch and returns a map because the per-class
version opened a connection each time, on the hot path of every phase.

A mixin rather than a collaborator: `TaskDB` owns the one connection and the
one transaction, and these read and write through it. Handing each its own
connection is exactly how an atomic transition becomes several.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from uta.tasks.models import DEFAULT_PRIORITY, json_dumps, now_iso
from uta.tasks.storage.base import _MAX_QUERY_PARAMS, _storage_symbol


class TaskRepositoryMixin:
    """The task repository half of `TaskDB`."""

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
        from uta.tasks.generation_engine import durable_generation_snapshot

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
            "estimated_cost_usd": fields.get(
                "estimated_cost_usd", fields.get("estimated_cost")
            ),
            "estimated_seconds": fields.get("estimated_seconds"),
            "estimated_elapsed_seconds": fields.get(
                "estimated_elapsed_seconds", fields.get("estimated_seconds")
            ),
            "config_snapshot_json": json_dumps(
                durable_generation_snapshot(fields.get("config_snapshot"))
            ),
            "budget_snapshot_json": json_dumps(fields.get("budget_snapshot")),
            "budget_config_snapshot_json": json_dumps(fields.get("budget_snapshot")),
            "estimate_snapshot_json": json_dumps(fields.get("estimate_snapshot")),
            "rdc_context_json": json_dumps(fields.get("rdc_context")),
            "rdc_context_path": fields.get("rdc_context_path"),
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
        symbol = _storage_symbol(
            language=language,
            target_granularity=target_granularity,
            class_fqn=class_fqn,
            symbol=symbol,
        )
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
            return conn.execute(
                "SELECT * FROM repo_tasks WHERE id=?", (task_id,)
            ).fetchone()

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
            return list(
                conn.execute(
                    f"""
                SELECT * FROM repo_tasks {where}
                ORDER BY
                    CASE status WHEN 'RUNNING' THEN 0 WHEN 'QUEUED' THEN 1 WHEN 'CREATED' THEN 2 ELSE 3 END,
                    priority ASC, created_at DESC
                LIMIT ?
                """,
                    (*params, int(limit)),
                )
            )

    def list_class_tasks(self, repo_task_id: int) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM class_tasks WHERE repo_task_id=? ORDER BY priority ASC, id ASC",
                    (repo_task_id,),
                )
            )

    def get_class_task(self, class_task_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM class_tasks WHERE id=?", (class_task_id,)
            ).fetchone()

    def find_class_task(
        self, repo_task_id: int, class_fqn: str
    ) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM class_tasks WHERE repo_task_id=? AND class_fqn=?",
                (repo_task_id, class_fqn),
            ).fetchone()

    def find_class_tasks(
        self, repo_task_id: int, class_fqns: Iterable[str]
    ) -> Dict[str, sqlite3.Row]:
        """Look up a whole batch at once, keyed by class name.

        `find_class_task` opens its own connection, so calling it in a loop
        costs a connection *and* a query per class. `llm_guard_before` did that
        three times over per batch, on the hot path of every phase of every
        target.

        A class with no row is absent from the result rather than mapped to
        `None`: callers already branch on "no row", and absence keeps that from
        ever being confused with a row that exists but is empty.
        """
        names = list(dict.fromkeys(str(fqn) for fqn in class_fqns if fqn))
        if not names:
            return {}

        found: Dict[str, sqlite3.Row] = {}
        with self.connect() as conn:
            for start in range(0, len(names), _MAX_QUERY_PARAMS):
                chunk = names[start : start + _MAX_QUERY_PARAMS]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT * FROM class_tasks "
                    f"WHERE repo_task_id=? AND class_fqn IN ({placeholders})",
                    (repo_task_id, *chunk),
                ).fetchall()
                for row in rows:
                    found[row["class_fqn"]] = row
        return found

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
        with self.transaction() as conn:
            if table == "repo_tasks" and data.get("status") in {
                "CREATED",
                "QUEUED",
                "RUNNING",
                "STOP_REQUESTED",
                "STOPPED",
            }:
                from uta.tasks.generation_engine import require_durable_generation_task

                row = conn.execute(
                    "SELECT * FROM repo_tasks WHERE id=?", (int(row_id),)
                ).fetchone()
                if row is not None:
                    require_durable_generation_task(row, action="be reactivated")
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

    def class_tasks_by_fqn(
        self, repo_task_id: int, fqns: Iterable[str]
    ) -> Dict[str, sqlite3.Row]:
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

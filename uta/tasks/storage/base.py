"""Small primitives shared by task-storage modules.

Shared exception identity matters: raising one conflict class and catching a
copy with the same spelling would silently stop matching.
"""

import os
from pathlib import Path
from typing import Dict, Optional


#: SQLite compiles in a host-parameter ceiling per statement -- 999 on older
#: builds. Batches are chunked well under it, because discovering that ceiling
#: would mean an exception inside a guard on the hot path of every phase.
_MAX_QUERY_PARAMS = 900


class WorkflowOperationConflict(RuntimeError):
    """Stored workflow evidence disagrees with an attempted transition."""


def _storage_symbol(
    *, language: str, target_granularity: str, class_fqn: str, symbol: Optional[str]
) -> Optional[str]:
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
    "rdc_context_json": "TEXT NOT NULL DEFAULT '{}'",
    "rdc_context_path": "TEXT",
    "error": "TEXT",
    "session_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    "hard_cap_usd": "REAL",
    "progress_event_count": "INTEGER NOT NULL DEFAULT 0",
    "progress_event_bytes": "INTEGER NOT NULL DEFAULT 0",
    "progress_truncated": "INTEGER NOT NULL DEFAULT 0",
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
    "mutation_detail": "TEXT",
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
    # A CI repair unit's equivalent-mutant review, read back by the fix
    # session when it decides whether its fresh gate rerun may be excused.
    "equivalence_review_json": "TEXT",
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

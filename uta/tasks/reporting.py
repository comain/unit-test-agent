"""Task-ledger-only status and summary projections."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from uta.shared.backends import UnknownBackendError, make_backend
from uta.tasks.db import TaskDB
from uta.tasks.models import TERMINAL_CLASS_STATUSES


class TaskReportingService:
    def __init__(self, db: TaskDB) -> None:
        self.db = db

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            result = float(value)
            return None if math.isnan(result) else result
        except (TypeError, ValueError):
            return None

    @classmethod
    def _metric_stats(cls, values: Iterable[Any]) -> Dict[str, Optional[float]]:
        series = [value for value in map(cls._safe_float, values) if value is not None]
        if not series:
            return {"count": 0, "total": None, "avg": None, "max": None, "min": None}
        return {
            "count": len(series),
            "total": sum(series),
            "avg": sum(series) / len(series),
            "max": max(series),
            "min": min(series),
        }

    def status_payload(self, repo_task_id: int) -> Dict[str, Any]:
        from uta.tasks.render import build_status_payload

        return build_status_payload(self.db, repo_task_id)

    def build_summary(
        self, repo_task_id: int, *, recalc_project_coverage: bool = False
    ) -> Dict[str, Any]:
        task_row = self.db.get_repo_task(repo_task_id)
        if task_row is None:
            raise KeyError(f"Task {repo_task_id} not found")
        task = dict(task_row)
        class_rows = [dict(row) for row in self.db.list_class_tasks(repo_task_id)]
        aggregate = self.db.aggregate_repo_task(repo_task_id)
        coverage = self._metric_stats(
            row.get("coverage_line")
            if row.get("coverage_line") is not None
            else row.get("coverage")
            for row in class_rows
        )
        mutation = self._metric_stats(row.get("mutation_score") for row in class_rows)
        total_mutants = sum(int(row.get("total_mutants") or 0) for row in class_rows)
        surviving_mutants = sum(
            int(row.get("surviving_mutants") or 0) for row in class_rows
        )
        mutation["total"] = (
            ((total_mutants - surviving_mutants) / total_mutants) * 100.0
            if total_mutants > 0
            else None
        )
        coverage_recalc: Dict[str, Any] = {"ran": False}
        coverage_total = coverage.get("avg")
        if recalc_project_coverage:
            language = task.get("language") or "java"
            try:
                recomputer = make_backend(language, "coverage_recompute")
            except UnknownBackendError:
                recomputer = None
            if recomputer is not None:
                outcome = recomputer.recompute(
                    str(task["repo_path"]), class_rows, current_total=coverage_total
                )
                coverage_recalc = outcome.recalc
                coverage_total = outcome.coverage_total
        coverage["total"] = coverage_total
        started_at, finished_at = task.get("started_at"), task.get("finished_at")
        elapsed = self._safe_float(task.get("actual_elapsed_seconds"))
        if elapsed is None:
            elapsed = self._safe_float(task.get("elapsed_seconds"))
        if elapsed is None and started_at and finished_at:
            try:
                start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
                elapsed = max((end - start).total_seconds(), 0.0)
            except (TypeError, ValueError):
                elapsed = None
        tokens = {
            "input": int(
                aggregate.get("input_tokens")
                or aggregate.get("actual_input_tokens")
                or 0
            ),
            "output": int(
                aggregate.get("output_tokens")
                or aggregate.get("actual_output_tokens")
                or 0
            ),
            "cache_read": int(
                aggregate.get("cache_read_tokens")
                or aggregate.get("actual_cache_read_tokens")
                or 0
            ),
            "cache_write": int(
                aggregate.get("cache_write_tokens")
                or aggregate.get("actual_cache_write_tokens")
                or 0
            ),
            "reasoning": int(aggregate.get("reasoning_tokens") or 0),
        }
        tokens["total"] = sum(tokens.values())
        cost = self.db.workflow_operation_cost_total(repo_task_id)
        recorded = {
            **tokens,
            "cost_usd": cost["provider_cost_usd"],
            "cost_provenance": cost["cost_provenance"],
        }
        generated = sum(
            1
            for row in class_rows
            if row.get("test_file_path")
            or row.get("test_count")
            or row["status"] in TERMINAL_CLASS_STATUSES
        )
        return {
            "task": task,
            "aggregate": aggregate,
            "classes": {
                "total": len(class_rows),
                "generated": generated,
                "completed": int(aggregate.get("completed_classes") or 0),
                "passed": int(aggregate.get("passed_classes") or 0),
                "failed": int(aggregate.get("failed_classes") or 0),
                "statuses": aggregate.get("statuses") or {},
            },
            "coverage": coverage,
            "mutation": mutation,
            "project_coverage_recalc": coverage_recalc,
            "project_mutation_totals": {
                "total_mutants": total_mutants,
                "surviving_mutants": surviving_mutants,
            },
            "timing": {
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_seconds": elapsed,
            },
            "tokens": {
                "recorded": recorded,
                "coverage": {
                    "classes_with_usage": sum(
                        1 for row in class_rows if int(row.get("total_tokens") or 0) > 0
                    ),
                    "classes_without_usage": sum(
                        1
                        for row in class_rows
                        if int(row.get("total_tokens") or 0) == 0
                    ),
                },
            },
        }

    def write_live_status(self, repo_task_id: int) -> Dict[str, str]:
        from uta.tasks.render import write_live_status

        task = self.db.get_repo_task(repo_task_id)
        if task is None:
            raise KeyError(f"Task {repo_task_id} not found")
        return write_live_status(self.db, repo_task_id, repo_path=task["repo_path"])

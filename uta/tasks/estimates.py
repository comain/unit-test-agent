"""Measured task estimates without provider- or model-specific pricing."""

from __future__ import annotations

from typing import Any, Dict, Optional

from uta.tasks.db import TaskDB


class TaskEstimateService:
    def __init__(self, db: TaskDB) -> None:
        self.db = db

    @staticmethod
    def unavailable(*, target_count: int, language: str) -> Dict[str, Any]:
        return {
            "estimate_source": "unavailable",
            "estimate_language": language or "java",
            "target_count": int(target_count),
        }

    def from_history(
        self,
        *,
        repo_path: str,
        module: Optional[str],
        language: str,
        target_count: int,
    ) -> Optional[Dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    AVG(NULLIF(input_tokens, 0) / CAST(total_classes AS REAL)) AS avg_input,
                    AVG(NULLIF(output_tokens, 0) / CAST(total_classes AS REAL)) AS avg_output,
                    AVG(NULLIF(cache_read_tokens, 0) / CAST(total_classes AS REAL)) AS avg_cache,
                    AVG(NULLIF(reasoning_tokens, 0) / CAST(total_classes AS REAL)) AS avg_reasoning,
                    AVG(NULLIF(provider_cost_usd, 0) / CAST(total_classes AS REAL)) AS avg_cost,
                    AVG(NULLIF(actual_elapsed_seconds, 0) / CAST(total_classes AS REAL)) AS avg_seconds,
                    COUNT(*) AS samples
                FROM repo_tasks
                WHERE repo_path=?
                  AND COALESCE(module_filter, '')=COALESCE(?, '')
                  AND language=? AND status='COMPLETED'
                  AND total_classes > 0 AND input_tokens > 0
                """,
                (repo_path, module, language or "java"),
            ).fetchone()
        if not row or int(row["samples"] or 0) < 1:
            return None
        count = max(1, int(target_count))
        estimated_input = int(float(row["avg_input"] or 0.0) * count)
        estimated_output = int(float(row["avg_output"] or 0.0) * count)
        if estimated_input <= 0 and estimated_output <= 0:
            return None
        estimated_cache = int(float(row["avg_cache"] or 0.0) * count)
        estimated_reasoning = int(float(row["avg_reasoning"] or 0.0) * count)
        estimated_cost = (
            float(row["avg_cost"] or 0.0) * count
            if float(row["avg_cost"] or 0.0) > 0
            else None
        )
        estimated_seconds = float(row["avg_seconds"] or 0.0) * count
        estimate = {
            "estimate_source": "historical_recorded",
            "estimate_language": language or "java",
            "estimate_samples": int(row["samples"] or 0),
            "target_count": count,
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": estimated_output,
            "estimated_cache_read_tokens": estimated_cache,
            "estimated_reasoning_tokens": estimated_reasoning,
            "estimated_total_tokens": (
                estimated_input
                + estimated_output
                + estimated_cache
                + estimated_reasoning
            ),
            "estimated_seconds": estimated_seconds,
            "estimated_elapsed_seconds": estimated_seconds,
        }
        if estimated_cost is not None:
            estimate.update(
                estimated_cost=estimated_cost,
                estimated_cost_usd=estimated_cost,
            )
        return estimate

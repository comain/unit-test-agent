"""The spend ceiling a workflow run is held to."""

from __future__ import annotations



from typing import Any


class WorkflowCostGate:
    """Fail closed on persisted currency caps before a provider turn."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def allow_attempt(self, *, operation_id: str, paid_attempt_ordinal: int) -> None:
        del paid_attempt_ordinal
        with self.db.connect() as conn:
            operation = conn.execute(
                "SELECT repo_task_id FROM workflow_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise RuntimeError(f"unknown workflow operation: {operation_id}")
            task = conn.execute(
                "SELECT hard_cap_usd, estimated_cost_usd FROM repo_tasks WHERE id=?",
                (int(operation["repo_task_id"]),),
            ).fetchone()
            accounting = conn.execute(
                """
                SELECT COUNT(*) AS attempts,
                       SUM(CASE WHEN cost_provenance='unavailable' THEN 1 ELSE 0 END) AS unavailable,
                       SUM(provider_cost_usd) AS cost
                FROM workflow_operation_accounting AS a
                JOIN workflow_operations AS o ON o.operation_id=a.operation_id
                WHERE o.repo_task_id=?
                """,
                (int(operation["repo_task_id"]),),
            ).fetchone()
        if task is None:
            raise RuntimeError(
                f"task for workflow operation is missing: {operation_id}"
            )
        explicit = task["hard_cap_usd"]
        estimated = task["estimated_cost_usd"]
        cap = float(explicit) if explicit is not None else None
        if cap is None and estimated is not None:
            from uta.shared.config import settings

            cap = float(estimated) * float(settings.budget_hard_cap_multiplier or 4.0)
        if cap is None or cap <= 0:
            return
        if int(accounting["unavailable"] or 0) > 0:
            from uta.testgen.task_guard import TaskBudgetExceeded

            raise TaskBudgetExceeded(
                "currency cap cannot continue after a provider returned unknown cost"
            )
        if float(accounting["cost"] or 0.0) >= cap:
            from uta.testgen.task_guard import TaskBudgetExceeded

            raise TaskBudgetExceeded(
                f"currency cap reached before provider turn: "
                f"${float(accounting['cost'] or 0.0):.4f} >= ${cap:.4f}"
            )


__all__ = ["WorkflowCostGate"]

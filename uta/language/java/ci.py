from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from uta.ci_plugin.fix_sessions import CreateFixSessionRequest
from uta.ci_plugin.models import CiTaskRecord
from uta.engine.ci import BaseCiLanguageHandler
from uta.tasks.manager import TaskManager


class JavaCiLanguageHandler(BaseCiLanguageHandler):
    language = "java"
    quality_gate_backend = "maven_enforcer"

    def create_repair_task(
        self,
        *,
        task_manager: TaskManager,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        priority: int,
        base_ref: str,
        coverage_gate: float,
        mutation_gate: float,
        ci_context: Dict[str, Any],
        ci_context_path: Optional[str],
    ) -> int:
        return task_manager.create_task(
            repo_path=str(repo_path),
            class_fqns=self._repair_class_fqns(record, request),
            select_all=False,
            priority=priority,
            branch_name=record.request.branch,
            base_ref=base_ref,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            quality_mode="ci_incremental",
            quality_gate_backend=self.quality_gate_backend,
            quality_gate_command=self._quality_gate_command(record),
            ci_context=ci_context,
            ci_context_path=ci_context_path,
        )

    @staticmethod
    def _repair_class_fqns(record: CiTaskRecord, request: CreateFixSessionRequest) -> list[str]:
        classes = [
            target_id.split(":", 1)[1]
            for target_id in request.target_ids
            if target_id.startswith("class:") and target_id.split(":", 1)[1]
        ]
        enforcement = record.enforcement_result or {}
        for key in ("failedClasses", "failed_classes", "changedClasses", "changed_classes"):
            values = enforcement.get(key)
            if isinstance(values, list):
                classes.extend(str(value) for value in values if value)
        return list(dict.fromkeys(classes))

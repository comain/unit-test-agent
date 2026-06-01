from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from uta.ci_plugin.enforcement import EnforcementResult, EnforcementResultStatus
from uta.ci_plugin.fix_sessions import CreateFixSessionRequest
from uta.ci_plugin.models import CiTaskRecord
from uta.engine.ci import BaseCiLanguageHandler
from uta.engine.languages import RawTargetSelection, default_registry
from uta.config import settings
from uta.language.python.enforcement import PYTHON_ENFORCEMENT_BACKEND, PYTHON_ENFORCEMENT_SCHEMA_VERSION
from uta.tasks.manager import TaskManager


class PythonCiLanguageHandler(BaseCiLanguageHandler):
    language = "python"
    quality_gate_backend = "python_enforcer"

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
        return task_manager.create_task_targets(
            repo_path=str(repo_path),
            targets=self._repair_targets(record, request),
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
            language=self.language,
        )

    @staticmethod
    def _repair_targets(record: CiTaskRecord, request: CreateFixSessionRequest):
        adapter = default_registry().adapter_for("python")
        values: list[str] = []
        for target_id in request.target_ids:
            if target_id.startswith("python:"):
                values.append(target_id.split(":", 1)[1])
            elif (
                target_id.startswith("pyfile:")
                or target_id.startswith("pysymbol:")
                or target_id.endswith(".py")
                or ".py::" in target_id
            ):
                values.append(target_id)
        enforcement = record.enforcement_result or {}
        evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
        for item in evidence.get("targetResults") or []:
            target = item.get("target") if isinstance(item, dict) else None
            if isinstance(target, dict):
                value = target.get("target") or target.get("target_id") or target.get("source_path")
                if value:
                    values.append(str(value))
        for target in evidence.get("targets") or []:
            if isinstance(target, dict):
                value = target.get("target") or target.get("target_id") or target.get("source_path")
                if value:
                    values.append(str(value))
        for path in evidence.get("changedProductionFiles") or []:
            if path:
                values.append(str(path))
        values = list(dict.fromkeys(values))
        return [adapter.normalize_target(RawTargetSelection(target=value)) for value in values]

    def completed_task_enforcement_result(
        self,
        *,
        record: CiTaskRecord,
        task_manager: TaskManager,
        repo_task: Dict[str, Any],
    ) -> Optional[EnforcementResult]:
        if not self.matches(record) and str(repo_task.get("language") or "") != self.language:
            return None
        if repo_task.get("status") != "COMPLETED":
            return None
        rows = task_manager.list_class_tasks(int(repo_task["id"]))
        if not rows or any(str(row.get("status") or "").upper() != "PASS" for row in rows):
            return None

        original = record.enforcement_result if isinstance(record.enforcement_result, dict) else {}
        original_evidence = original.get("evidence") if isinstance(original.get("evidence"), dict) else {}
        original_coverage = original_evidence.get("coverage") if isinstance(original_evidence.get("coverage"), dict) else {}
        original_mutation = original_evidence.get("mutation") if isinstance(original_evidence.get("mutation"), dict) else {}
        coverage_gate = float(repo_task.get("coverage_gate") or original_coverage.get("gate") or settings.coverage_gate)
        mutation_gate = float(repo_task.get("mutation_gate") or original_mutation.get("gate") or settings.mutation_gate)
        target_results = [self._target_evidence_from_task_row(row, coverage_gate, mutation_gate) for row in rows]
        coverage = self._aggregate_task_coverage(target_results, coverage_gate)
        mutation = self._aggregate_task_mutation(target_results, mutation_gate)
        if not coverage.get("passed") or not mutation.get("passed"):
            return None

        head_commit = str(repo_task.get("remote_ref") or repo_task.get("latest_commit") or "")
        evidence = {
            "schemaVersion": PYTHON_ENFORCEMENT_SCHEMA_VERSION,
            "evidenceId": f"uta-python-repair-task:{record.task_id}:{repo_task['id']}",
            "language": "python",
            "backend": PYTHON_ENFORCEMENT_BACKEND,
            "repo": str(repo_task.get("repo_path") or record.workspace_path or ""),
            "baseRef": str(repo_task.get("base_ref") or original_evidence.get("baseRef") or "origin/master"),
            "baseCommit": original_evidence.get("baseCommit") or "",
            "headRef": record.request.branch or "HEAD",
            "headCommit": head_commit,
            "changedProductionFiles": original_evidence.get("changedProductionFiles") or [],
            "changedLines": original_evidence.get("changedLines") or {},
            "targets": [item["target"] for item in target_results],
            "status": "passed",
            "passed": True,
            "reasonCode": "passed",
            "summary": "Python enforcement passed from completed repair task evidence",
            "targetResults": target_results,
            "coverage": coverage,
            "mutation": mutation,
            "commands": [],
            "artifacts": {},
            "setup": {"source": "uta_repair_task"},
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        return EnforcementResult(
            status=EnforcementResultStatus.passed,
            passed=True,
            command=["uta", "tasks", "show", str(repo_task["id"])],
            returncode=0,
            stdout=json.dumps(evidence, ensure_ascii=False),
            summary=evidence["summary"],
            language="python",
            backend=PYTHON_ENFORCEMENT_BACKEND,
            evidence=evidence,
        )

    @staticmethod
    def _target_evidence_from_task_row(row: Dict[str, Any], coverage_gate: float, mutation_gate: float) -> Dict[str, Any]:
        coverage_rate = float(row.get("coverage_line") if row.get("coverage_line") is not None else row.get("coverage") or 0.0)
        generated = int(row.get("total_mutants") or 0)
        survived = int(row.get("surviving_mutants") or 0)
        killed = max(0, generated - survived)
        mutation_rate = 100.0 if generated == 0 and survived == 0 else float(row.get("mutation_score") or 0.0)
        return {
            "target": {
                "language": "python",
                "target_id": row.get("target_id") or row.get("class_fqn") or row.get("source_path") or "",
                "display_name": row.get("display_name") or row.get("class_fqn") or row.get("target_id") or "",
                "source_path": row.get("source_path") or None,
                "symbol": row.get("symbol") or None,
                "granularity": row.get("target_granularity") or "file",
            },
            "status": "passed",
            "reasonCode": "passed",
            "testsPass": True,
            "message": row.get("current_detail") or "PASS",
            "coverage": {
                "rate": coverage_rate,
                "covered": 0,
                "total": 0,
                "gate": coverage_gate,
                "passed": coverage_rate >= coverage_gate,
            },
            "mutation": {
                "generated": generated,
                "killed": killed,
                "survived": survived,
                "rate": mutation_rate,
                "gate": mutation_gate,
                "passed": survived == 0 and mutation_rate >= mutation_gate,
            },
            "commands": [],
            "artifacts": {"test_file_path": row.get("test_file_path") or ""},
            "setup": {"source": "uta_repair_task"},
        }

    @staticmethod
    def _aggregate_task_coverage(target_results: list[Dict[str, Any]], gate: float) -> Dict[str, Any]:
        summaries = [item["coverage"] for item in target_results if isinstance(item.get("coverage"), dict)]
        rate = min((float(item.get("rate") or 0.0) for item in summaries), default=0.0)
        return {
            "covered": sum(int(item.get("covered") or 0) for item in summaries),
            "total": sum(int(item.get("total") or 0) for item in summaries),
            "rate": rate,
            "gate": gate,
            "passed": bool(summaries) and all(item.get("passed") is True for item in summaries),
        }

    @staticmethod
    def _aggregate_task_mutation(target_results: list[Dict[str, Any]], gate: float) -> Dict[str, Any]:
        summaries = [item["mutation"] for item in target_results if isinstance(item.get("mutation"), dict)]
        generated = sum(int(item.get("generated") or 0) for item in summaries)
        killed = sum(int(item.get("killed") or 0) for item in summaries)
        survived = sum(int(item.get("survived") or 0) for item in summaries)
        rate = 100.0 if survived == 0 else round((killed / max(killed + survived, 1)) * 100.0, 4)
        return {
            "generated": generated,
            "killed": killed,
            "survived": survived,
            "rate": rate,
            "gate": gate,
            "passed": bool(summaries) and survived == 0 and rate >= gate,
        }

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from uta.enforcement.enforcement import QualityGateResult, QualityGateStatus
from uta.shared.fix_sessions import CreateFixSessionRequest
from uta.shared.ci_models import CiTaskRecord
from uta.enforcement.ci import BaseCiLanguageHandler
from uta.shared.languages import RawTargetSelection, default_registry
from uta.enforcement.evidence import ADVISORY_SKIP_REASONS
from uta.enforcement.mutation_candidates import candidate_plan_requires_mutation_execution
from uta.shared.config import settings
from uta.shared.opencode_snapshot import opencode_config_snapshot
from uta.language.python.enforcement import PYTHON_ENFORCEMENT_BACKEND, PYTHON_ENFORCEMENT_SCHEMA_VERSION
from uta.tasks.manager import TaskManager
from uta.language.python.environment_recipes import (
    PYTHON_ENVIRONMENT_RECIPE_SNAPSHOT_KEY,
    PythonEnvironmentRecipeResolver,
)


PYTHON_REPAIR_MISSING_CHANGED_LINES_ERROR = (
    "Python CI incremental repair requires changedLines evidence for every selected repair target; "
    "refusing to create a repair session because changed-line context is missing"
)


def _repair_scope_key(target_id: Any, source_path: Any) -> str:
    """One key per source file, whatever granularity named it."""
    path = str(source_path or "").replace("\\", "/").strip()
    if path:
        return path
    value = str(target_id or "").strip()
    if ":" in value:
        value = value.split(":", 1)[1]
    return value.split("::", 1)[0]


def _repair_scope_keys(targets) -> set:
    keys = {
        _repair_scope_key(
            getattr(target, "target_id", ""), getattr(target, "source_path", "")
        )
        for target in targets
    }
    keys.discard("")
    return keys


class PythonCiLanguageHandler(BaseCiLanguageHandler):
    language = "python"
    quality_gate_backend = "python_enforcer"

    def __init__(self, runner, runtime_resolver: Optional[PythonEnvironmentRecipeResolver] = None) -> None:
        super().__init__(runner)
        self.runtime_resolver = runtime_resolver

    def runner_for(self, record: CiTaskRecord):
        if self.runner is None or self.runtime_resolver is None:
            return self.runner
        recipe = self.runtime_resolver.resolve(record.request.app_name)
        if recipe is None:
            return self.runner
        bind = getattr(self.runner, "with_runtime_overrides", None)
        return bind(recipe.runtime_overrides()) if callable(bind) else self.runner

    def should_rerun_after_repair_workspace_refresh(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
    ) -> bool:
        evidence = (record.enforcement_result or {}).get("evidence") or {}
        mutation = evidence.get("mutation") if isinstance(evidence, dict) else {}
        return (
            isinstance(mutation, dict)
            and mutation.get("gateScope") == "report"
            and mutation.get("passed") is False
        )

    def run_repair_preflight(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
    ) -> Optional[QualityGateResult]:
        """Replace sampled CI diagnostics with authoritative full-cap evidence.

        A report-aggregate failure says the report missed the gate; it does not
        say which targets are weak. Every target's own score was computed from a
        one- or two-mutant sample and reported as diagnostic, so deriving repair
        targets from it would send the agent at whichever file drew an unlucky
        mutant. The full-cap rerun is what makes the targets real.
        """
        if not self.should_rerun_after_repair_workspace_refresh(record=record, request=request):
            return None
        selected_runner = self.runner_for(record)
        run_full = getattr(selected_runner, "run_full", None)
        if not callable(run_full):
            raise ValueError(
                "Python sampled CI failure requires a full-cap enforcement runner before repair target creation"
            )
        return run_full(repo_path)

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
        rdc_context: Dict[str, Any],
        rdc_context_path: Optional[str],
    ) -> int:
        targets = self._repair_targets(record, request)
        if not targets:
            raise ValueError(
                "Python CI incremental repair requires explicit or Python enforcement target evidence; "
                "no safe repair target files were found"
            )
        return task_manager.create_task_targets(
            repo_path=str(repo_path),
            targets=targets,
            select_all=False,
            priority=priority,
            branch_name=record.request.branch,
            base_ref=base_ref,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            quality_mode="ci_incremental",
            quality_gate_backend=self.quality_gate_backend,
            quality_gate_command=self._quality_gate_command(record),
            # Repair tasks pin their model like any other task. Without this the
            # snapshot is empty and the run follows whatever the daemon's
            # settings resolve to at execution time.
            config_snapshot=self._repair_config_snapshot(record),
            rdc_context=rdc_context,
            rdc_context_path=rdc_context_path,
            language=self.language,
        )

    def _repair_config_snapshot(self, record: CiTaskRecord) -> Dict[str, Any]:
        snapshot = opencode_config_snapshot()
        if self.runtime_resolver is None:
            return snapshot
        recipe = self.runtime_resolver.resolve(record.request.app_name)
        if recipe is not None:
            snapshot[PYTHON_ENVIRONMENT_RECIPE_SNAPSHOT_KEY] = recipe.as_dict()
        return snapshot

    def scoring_survivors(self, *, record: CiTaskRecord, result, repo_path: Path):
        from uta.language.python.equivalence import python_scoring_survivors

        payload = result.model_dump(mode="json")
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        return python_scoring_survivors(evidence.get("mutation"), repo_path)

    def gate_failure_flags(self, *, record: CiTaskRecord, result):
        from uta.language.python.equivalence import python_gate_failure_flags

        payload = result.model_dump(mode="json")
        return python_gate_failure_flags(payload)

    def repair_target_ids(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        base_ref: str,
    ) -> tuple[str, ...]:
        return tuple(target.target_id for target in self._repair_targets(record, request))

    def validate_repair_context(
        self,
        *,
        record: CiTaskRecord,
        request: CreateFixSessionRequest,
        repo_path: Path,
        base_ref: str,
        rdc_context: Dict[str, Any],
        target_ids: tuple[str, ...],
    ) -> None:
        self._validate_changed_lines_for_targets(rdc_context, target_ids)

    @staticmethod
    def _validate_changed_lines_for_targets(context: Dict[str, Any], target_ids: tuple[str, ...]) -> None:
        if not target_ids:
            return
        enforcement = context.get("enforcement") if isinstance(context.get("enforcement"), dict) else {}
        evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
        changed_lines = _normalized_changed_lines(evidence)
        if not changed_lines:
            raise ValueError(PYTHON_REPAIR_MISSING_CHANGED_LINES_ERROR)

        target_paths = [
            path
            for path in (_python_source_path_from_target_id(target_id) for target_id in target_ids)
            if path
        ]
        missing_paths = [
            path
            for path in target_paths
            if not _changed_lines_for_path(changed_lines, path)
        ]
        if missing_paths:
            raise ValueError(
                f"{PYTHON_REPAIR_MISSING_CHANGED_LINES_ERROR}: {', '.join(sorted(set(missing_paths)))}"
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
        if values:
            values = list(dict.fromkeys(values))
            return [adapter.normalize_target(RawTargetSelection(target=value)) for value in values]

        enforcement = record.enforcement_result or {}
        evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else {}
        target_results = [item for item in evidence.get("targetResults") or [] if isinstance(item, dict)]
        for item in target_results:
            if not PythonCiLanguageHandler._target_result_needs_repair(item):
                continue
            target = item.get("target") if isinstance(item, dict) else None
            if isinstance(target, dict):
                value = target.get("target") or target.get("target_id") or target.get("source_path")
                if value:
                    values.append(str(value))
        if not values and not target_results:
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

    @staticmethod
    def _target_result_needs_repair(item: Dict[str, Any]) -> bool:
        # Checked first: a skip is not a failure, so there is nothing here for
        # repair to fix, and a skipped target trips the zero-mutant check below
        # for the same reason it was skipped. The `status` check further down
        # already tolerates "skipped"; without the same tolerance on the reason
        # code, every advisory skip fell through to repair -- UTA declined to
        # verify the target as too expensive, then spent LLM turns writing tests
        # the next run would skip again.
        if str(item.get("reasonCode") or "").strip().lower() in ADVISORY_SKIP_REASONS:
            return False
        coverage = item.get("coverage")
        if isinstance(coverage, dict) and coverage.get("passed") is False:
            return True
        mutation = item.get("mutation")
        if isinstance(mutation, dict) and mutation.get("passed") is False:
            return True
        if isinstance(mutation, dict):
            candidate_plan = mutation.get("candidatePlan") or {}
            if (
                int(mutation.get("changedLineMutantsGenerated") or mutation.get("generated") or 0) <= 0
                and candidate_plan_requires_mutation_execution(candidate_plan)
            ):
                return True
        if item.get("testsPass") is False:
            return True
        status = str(item.get("status") or "").strip().lower()
        if status and status not in {"pass", "passed", "success", "succeeded", "skipped"}:
            return True
        reason_code = str(item.get("reasonCode") or "").strip().lower()
        if reason_code and reason_code not in {"pass", "passed", "success"}:
            return True
        has_gate_signal = (
            isinstance(coverage, dict)
            or isinstance(mutation, dict)
            or "testsPass" in item
            or bool(status)
            or bool(reason_code)
        )
        return not has_gate_signal

    def completed_task_enforcement_result(
        self,
        *,
        record: CiTaskRecord,
        task_manager: TaskManager,
        repo_task: Dict[str, Any],
    ) -> Optional[QualityGateResult]:
        if not self.matches(record) and str(repo_task.get("language") or "") != self.language:
            return None
        if repo_task.get("status") != "COMPLETED":
            return None
        rows = task_manager.list_class_tasks(int(repo_task["id"]))
        if not rows or any(str(row.get("status") or "").upper() != "PASS" for row in rows):
            return None

        original = record.enforcement_result if isinstance(record.enforcement_result, dict) else {}
        # A scoped repair task proves only its own target rows. Keep the
        # original full-diff failure authoritative until every failed
        # coverage/mutation target has actually been verified.
        #
        # Compared by source file rather than by target id, which is where
        # this differs from the same guard on `main`. This branch's target
        # model yields both `pyfile:jobs/forecast.py` and
        # `pysymbol:jobs/forecast.py::forecast_for_store` for one file, so an
        # id comparison demands a file-granularity row that a symbol-scoped
        # repair will never produce, and the gate could then never clear. Two
        # ids naming one file are one obligation; a genuinely unrepaired
        # *other* file still fails this check, which is the protection that
        # matters.
        required_sources = _repair_scope_keys(
            self._repair_targets(record, CreateFixSessionRequest())
        )
        completed_sources = {
            _repair_scope_key(
                row.get("target_id") or row.get("class_fqn"), row.get("source_path")
            )
            for row in rows
        }
        completed_sources.discard("")
        if required_sources - completed_sources:
            return None

        original_evidence = original.get("evidence") if isinstance(original.get("evidence"), dict) else {}
        original_coverage = original_evidence.get("coverage") if isinstance(original_evidence.get("coverage"), dict) else {}
        original_mutation = original_evidence.get("mutation") if isinstance(original_evidence.get("mutation"), dict) else {}
        coverage_gate = float(repo_task.get("coverage_gate") or original_coverage.get("gate") or settings.coverage_gate)
        mutation_gate = float(repo_task.get("mutation_gate") or original_mutation.get("gate") or settings.mutation_gate)
        coverage_summaries = self._coverage_summaries_from_latest_report(repo_task)
        target_results = [
            self._target_evidence_from_task_row(
                row,
                coverage_gate,
                mutation_gate,
                coverage_summary=self._coverage_summary_for_task_row(row, coverage_summaries),
            )
            for row in rows
        ]
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
        return QualityGateResult(
            status=QualityGateStatus.passed,
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
    def _target_evidence_from_task_row(
        row: Dict[str, Any],
        coverage_gate: float,
        mutation_gate: float,
        *,
        coverage_summary: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        coverage_rate = float(row.get("coverage_line") if row.get("coverage_line") is not None else row.get("coverage") or 0.0)
        coverage = {
            "rate": coverage_rate,
            "covered": 0,
            "total": 0,
            "gate": coverage_gate,
            "passed": coverage_rate >= coverage_gate,
        }
        if coverage_summary:
            coverage.update(
                {
                    "covered": int(coverage_summary.get("covered") or 0),
                    "total": int(coverage_summary.get("total") or 0),
                    "rate": float(coverage_summary.get("rate") if coverage_summary.get("rate") is not None else coverage_rate),
                    "gate": float(coverage_summary.get("gate") if coverage_summary.get("gate") is not None else coverage_gate),
                    "passed": coverage_summary.get("passed") is True,
                }
            )
            for key in ("scope", "no_executable_changed_lines", "changed_lines", "xml_path"):
                if key in coverage_summary:
                    coverage[key] = coverage_summary[key]
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
            "coverage": coverage,
            "mutation": {
                "generated": generated,
                "killed": killed,
                "survived": survived,
                "rate": mutation_rate,
                "gate": mutation_gate,
                "passed": mutation_rate >= mutation_gate,
            },
            "commands": [],
            "artifacts": {"test_file_path": row.get("test_file_path") or ""},
            "setup": {"source": "uta_repair_task"},
        }

    @staticmethod
    def _coverage_summaries_from_latest_report(repo_task: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        report_path = Path(str(repo_task.get("latest_report_path") or ""))
        if not report_path.is_file():
            return {}
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, dict):
            return {}
        summaries: Dict[str, Dict[str, Any]] = {}
        for result_key, result in results.items():
            if not isinstance(result, dict) or not isinstance(result.get("coverage_summary"), dict):
                continue
            coverage = dict(result["coverage_summary"])
            for key in PythonCiLanguageHandler._coverage_summary_keys(str(result_key), result):
                summaries.setdefault(key, coverage)
        return summaries

    @staticmethod
    def _coverage_summary_for_task_row(
        row: Dict[str, Any],
        summaries: Dict[str, Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for key in PythonCiLanguageHandler._coverage_summary_keys("", row):
            if key in summaries:
                return summaries[key]
        return None

    @staticmethod
    def _coverage_summary_keys(result_key: str, payload: Dict[str, Any]) -> tuple[str, ...]:
        values = [
            result_key,
            str(payload.get("target_id") or ""),
            str(payload.get("class_fqn") or ""),
            str(payload.get("source_path") or ""),
        ]
        target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
        values.extend(
            [
                str(target.get("target_id") or ""),
                str(target.get("source_path") or ""),
                str(target.get("display_name") or ""),
            ]
        )
        keys: list[str] = []
        for value in values:
            normalized = value.replace("\\", "/").lstrip("./").strip()
            if not normalized:
                continue
            keys.append(normalized)
            if normalized.endswith(".py") and not normalized.startswith(("pyfile:", "pysymbol:")):
                keys.append(f"pyfile:{normalized}")
        return tuple(dict.fromkeys(keys))

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
        rate = min((float(item.get("rate") or 0.0) for item in summaries), default=0.0)
        return {
            "generated": generated,
            "killed": killed,
            "survived": survived,
            "rate": rate,
            "gate": gate,
            "passed": bool(summaries) and all(item.get("passed") is True for item in summaries),
        }


def _normalized_changed_lines(evidence: Dict[str, Any]) -> Dict[str, list[int]]:
    raw = evidence.get("changedLines") or evidence.get("changed_lines")
    if not isinstance(raw, dict):
        return {}
    normalized: Dict[str, list[int]] = {}
    for path, lines in raw.items():
        if not path or not isinstance(lines, (list, tuple, set)):
            continue
        valid_lines = sorted(
            {int(line) for line in lines if str(line).strip().isdigit() and int(line) > 0}
        )
        if valid_lines:
            normalized[str(path).replace("\\", "/").lstrip("./")] = valid_lines
    return normalized


def _python_source_path_from_target_id(target_id: str) -> Optional[str]:
    value = str(target_id or "").strip()
    if value.startswith("pyfile:"):
        value = value.split(":", 1)[1]
    elif value.startswith("pysymbol:"):
        value = value.split(":", 1)[1].split("::", 1)[0]
    elif "::" in value:
        value = value.split("::", 1)[0]
    if not value.endswith(".py"):
        return None
    return value.replace("\\", "/").lstrip("./")


def _changed_lines_for_path(changed_lines: Dict[str, list[int]], source_path: str) -> list[int]:
    normalized = source_path.replace("\\", "/").lstrip("./")
    return changed_lines.get(normalized) or changed_lines.get(Path(normalized).name) or []

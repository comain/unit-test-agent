import os
import re
import hashlib
import json
import sqlite3
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

from uta.config import settings
from uta.maven.jacoco import find_jacoco_report, parse_jacoco_line_coverage_for_classes, run_tests_with_jacoco_batch
from uta.opencode.tiered_router import (
    available_provider_candidates,
    parse_provider_chain,
    parse_provider_tokens,
    provider_candidates,
    provider_token_statuses,
)
from uta.tasks.db import TaskDB
from uta.tasks.models import (
    CLASS_TASK_STATUSES,
    DEFAULT_PRIORITY,
    TERMINAL_CLASS_STATUSES,
    json_dumps,
    json_loads,
    normalize_status,
    now_iso,
)
from uta.tasks.targets import (
    TargetIdentity,
    TargetRef,
    coerce_targets,
    event_payload_for_targets,
    legacy_class_fqn_for_storage,
    result_key,
)


def _repo_slug(repo_path: str) -> str:
    name = Path(repo_path).resolve().name or "repo"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "repo"


def _normalize_class_status(status: str) -> str:
    candidate = (status or "").strip().upper()
    if "RATE" in candidate and "LIMIT" in candidate:
        return "PROVIDER_RATE_LIMITED"
    if "TIMEOUT" in candidate or "STALLED" in candidate:
        return "LLM_STALLED"
    if "BUDGET" in candidate:
        return "BUDGET_EXCEEDED"
    if "UNSAFE" in candidate:
        return "UNSAFE_DIFF"
    if "PROVIDER" in candidate and "ERROR" in candidate:
        return "PROVIDER_ERROR"
    if "MODEL" in candidate and ("NOT_FOUND" in candidate or "NOT FOUND" in candidate):
        return "PROVIDER_ERROR"
    return normalize_status(candidate, CLASS_TASK_STATUSES, fallback="FAIL")


def config_hash(value: Dict[str, Any]) -> str:
    raw = json.dumps(value or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _group_provider_chain(candidates: Iterable[Any]) -> List[Dict[str, Any]]:
    grouped: List[Dict[str, Any]] = []
    by_provider: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        entry = by_provider.get(candidate.provider)
        if entry is None:
            entry = {"provider": candidate.provider, "models": []}
            by_provider[candidate.provider] = entry
            grouped.append(entry)
        entry["models"].append(candidate.model)
    return grouped


def _with_opencode_routing_metadata(config_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    snapshot = dict(config_snapshot or {})
    chain = parse_provider_chain(settings.opencode_provider_chain)
    fallback_history = list(snapshot.get("opencode_fallback_history") or [])[-20:]
    exhausted_indexes = {
        int(item["candidate_index"])
        for item in fallback_history
        if isinstance(item, dict) and item.get("candidate_index") is not None
    }
    configured_candidates = provider_candidates(
        fallback_enabled=settings.opencode_provider_fallback_enabled
    )
    candidates = available_provider_candidates(
        fallback_enabled=settings.opencode_provider_fallback_enabled
    )
    probe_status = "checked" if candidates != configured_candidates else "not_filtered"
    selected_candidates = [
        candidate for candidate in candidates if candidate.index not in exhausted_indexes
    ]
    selected = selected_candidates[0] if selected_candidates else None
    token_status = provider_token_statuses(
        chain,
        parse_provider_tokens(settings.opencode_provider_tokens),
    )
    snapshot.update(
        {
            "opencode_provider_chain": _group_provider_chain(chain),
            "opencode_selected_provider": selected.provider if selected else "",
            "opencode_selected_model": selected.model if selected else "",
            "opencode_candidate_index": selected.index if selected else None,
            "opencode_provider_tokens": token_status,
            "opencode_model_probe": {
                "status": probe_status,
                "cache": "process_local",
                "configured_candidates": len(configured_candidates),
                "available_candidates": len(candidates),
            },
            "opencode_fallback_history": fallback_history,
        }
    )
    return snapshot


def _selected_model_event_payload(config_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "provider": config_snapshot.get("opencode_selected_provider"),
        "model": config_snapshot.get("opencode_selected_model"),
        "candidate_index": config_snapshot.get("opencode_candidate_index"),
        "provider_tokens": config_snapshot.get("opencode_provider_tokens") or {},
        "model_probe": config_snapshot.get("opencode_model_probe") or {},
    }


TOKEN_TOTAL_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")
DEFAULT_OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _estimate_cost_from_tokens(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> float:
    model_lower = (model or "").lower()
    # Default: gpt-5.4 standard rates
    input_rate = 2.50
    cache_rate = 0.25
    output_rate = 15.0
    if "kimi-k2.6" in model_lower or "kimi/k2.6" in model_lower:
        input_rate = 0.7448
        cache_rate = input_rate * 0.25
        output_rate = 4.655
    elif "gpt-5.5" in model_lower:
        input_rate = 5.00
        cache_rate = 0.50
        output_rate = 30.0
    elif "gpt-5.4-mini" in model_lower:
        input_rate = 0.75
        cache_rate = 0.075
        output_rate = 4.50
    elif "gpt-5.4-nano" in model_lower:
        input_rate = 0.20
        cache_rate = 0.02
        output_rate = 1.25
    elif "gpt-5.4" in model_lower:
        input_rate = 2.50
        cache_rate = 0.25
        output_rate = 15.0
    elif "gpt-5.3-codex" in model_lower or "gpt-5.3-chat" in model_lower or "gpt-5.3" in model_lower:
        input_rate = 1.75
        cache_rate = 0.175
        output_rate = 14.0
    return (
        ((input_tokens * input_rate) / 1_000_000)
        + ((cache_read_tokens * cache_rate) / 1_000_000)
        + (((output_tokens + reasoning_tokens) * output_rate) / 1_000_000)
    )


class TaskManager:
    def __init__(self, db_path: Optional[Union[os.PathLike[str], str]] = None):
        self.db = TaskDB(db_path)
        self.db.init()

    @property
    def db_path(self) -> Path:
        return self.db.path

    def create_task(
        self,
        *,
        repo_path: str,
        module: Optional[str] = None,
        class_fqns: Optional[Iterable[str]] = None,
        select_all: bool = False,
        priority: int = DEFAULT_PRIORITY,
        branch_name: Optional[str] = None,
        new_branch: bool = False,
        base_ref: str = "origin/master",
        branch_profile: str = "default",
        coverage_gate: Optional[float] = None,
        mutation_gate: Optional[float] = None,
        hard_cap_usd: Optional[float] = None,
        config_snapshot: Optional[Dict[str, Any]] = None,
        budget_snapshot: Optional[Dict[str, Any]] = None,
        estimate_snapshot: Optional[Dict[str, Any]] = None,
        ci_context: Optional[Dict[str, Any]] = None,
        ci_context_path: Optional[str] = None,
        quality_mode: str = "class_batch",
        quality_gate_backend: str = "builtin",
        quality_gate_command: Optional[str] = None,
    ) -> int:
        resolved_repo = str(Path(repo_path).expanduser().resolve())
        slug = _repo_slug(resolved_repo)
        classes = list(dict.fromkeys(class_fqns or []))
        branch = self.db.create_or_reuse_branch(
            repo_path=resolved_repo,
            repo_slug=slug,
            base_ref=base_ref,
            branch_profile=branch_profile,
            branch_name=branch_name,
            new_branch=new_branch,
        )
        selection = {
            "language": "java",
            "module": module,
            "class_fqns": classes,
            "all": bool(select_all),
            "quality_mode": quality_mode,
            "quality_gate_backend": quality_gate_backend,
        }
        if quality_gate_command:
            selection["quality_gate_command"] = quality_gate_command
        if estimate_snapshot is None:
            estimate_snapshot = self._historical_estimate_snapshot(
                repo_path=resolved_repo,
                module=module,
                language="java",
                class_count=max(1, len(classes) or 1),
            )
        if estimate_snapshot is None:
            estimate_snapshot = self._fallback_estimate_snapshot(
                class_count=max(1, len(classes) or 1),
                language="java",
            )
        config_snapshot = _with_opencode_routing_metadata(config_snapshot)
        row_fields: Dict[str, Any] = {
            "repo_path": resolved_repo,
            "repo_slug": slug,
            "module_filter": module,
            "language": "java",
            "selection": selection,
            "branch_id": branch["id"],
            "branch_name": branch["branch_name"],
            "base_ref": base_ref,
            "priority": priority,
            "coverage_gate": coverage_gate,
            "mutation_gate": mutation_gate,
            "config_snapshot": config_snapshot,
            "budget_snapshot": budget_snapshot,
            "estimate_snapshot": estimate_snapshot,
            "ci_context": ci_context,
            "ci_context_path": ci_context_path,
            "total_classes": len(classes),
            **(estimate_snapshot or {}),
        }
        if hard_cap_usd is not None:
            row_fields["hard_cap_usd"] = hard_cap_usd
        task_id = self.db.create_repo_task(row_fields)
        for class_fqn in classes:
            self.db.create_class_task(task_id, class_fqn, module=module, priority=priority)
        self.db.add_event(
            task_id,
            None,
            "task_created",
            f"Created repo task on branch {branch['branch_name']}",
            payload={"selection": selection, "branch_name": branch["branch_name"]},
        )
        self.db.add_event(
            task_id,
            None,
            "opencode_model_selected",
            f"Selected OpenCode model {config_snapshot.get('opencode_selected_model')}",
            stage="routing",
            payload=_selected_model_event_payload(config_snapshot),
        )
        return task_id

    def create_task_targets(
        self,
        *,
        repo_path: str,
        targets: Iterable[TargetRef],
        module: Optional[str] = None,
        select_all: bool = False,
        priority: int = DEFAULT_PRIORITY,
        branch_name: Optional[str] = None,
        new_branch: bool = False,
        base_ref: str = "origin/master",
        branch_profile: str = "default",
        coverage_gate: Optional[float] = None,
        mutation_gate: Optional[float] = None,
        hard_cap_usd: Optional[float] = None,
        config_snapshot: Optional[Dict[str, Any]] = None,
        budget_snapshot: Optional[Dict[str, Any]] = None,
        estimate_snapshot: Optional[Dict[str, Any]] = None,
        ci_context: Optional[Dict[str, Any]] = None,
        ci_context_path: Optional[str] = None,
        quality_mode: str = "target_batch",
        quality_gate_backend: str = "builtin",
        quality_gate_command: Optional[str] = None,
        language: Optional[str] = None,
    ) -> int:
        resolved_repo = str(Path(repo_path).expanduser().resolve())
        slug = _repo_slug(resolved_repo)
        target_refs = coerce_targets(targets)
        languages = {target.language for target in target_refs}
        language = language or (next(iter(languages)) if len(languages) == 1 else ("mixed" if languages else "java"))
        branch = self.db.create_or_reuse_branch(
            repo_path=resolved_repo,
            repo_slug=slug,
            base_ref=base_ref,
            branch_profile=branch_profile,
            branch_name=branch_name,
            new_branch=new_branch,
        )
        selection: Dict[str, Any] = {
            "language": language,
            "module": module,
            "targets": [target.as_selection() for target in target_refs],
            "all": bool(select_all),
            "quality_mode": quality_mode,
            "quality_gate_backend": quality_gate_backend,
        }
        java_classes = [target.target_id for target in target_refs if target.language == "java"]
        if java_classes and len(java_classes) == len(target_refs):
            selection["class_fqns"] = java_classes
        if quality_gate_command:
            selection["quality_gate_command"] = quality_gate_command
        target_total = max(1, len(target_refs) or 1)
        if estimate_snapshot is None:
            estimate_snapshot = self._historical_estimate_snapshot(
                repo_path=resolved_repo,
                module=module,
                language=language,
                class_count=target_total,
            )
        if estimate_snapshot is None:
            estimate_snapshot = self._fallback_estimate_snapshot(class_count=target_total, language=language)
        config_snapshot = _with_opencode_routing_metadata(config_snapshot)
        row_fields: Dict[str, Any] = {
            "repo_path": resolved_repo,
            "repo_slug": slug,
            "module_filter": module,
            "language": language,
            "selection": selection,
            "branch_id": branch["id"],
            "branch_name": branch["branch_name"],
            "base_ref": base_ref,
            "priority": priority,
            "coverage_gate": coverage_gate,
            "mutation_gate": mutation_gate,
            "config_snapshot": config_snapshot,
            "budget_snapshot": budget_snapshot,
            "estimate_snapshot": estimate_snapshot,
            "ci_context": ci_context,
            "ci_context_path": ci_context_path,
            "total_classes": len(target_refs),
            **(estimate_snapshot or {}),
        }
        if hard_cap_usd is not None:
            row_fields["hard_cap_usd"] = hard_cap_usd
        task_id = self.db.create_repo_task(row_fields)
        self.ensure_target_tasks(task_id, target_refs, module=module, priority=priority)
        self.db.add_event(
            task_id,
            None,
            "task_created",
            f"Created repo task on branch {branch['branch_name']}",
            payload={"selection": selection, "branch_name": branch["branch_name"]},
        )
        self.db.add_event(
            task_id,
            None,
            "opencode_model_selected",
            f"Selected OpenCode model {config_snapshot.get('opencode_selected_model')}",
            stage="routing",
            payload=_selected_model_event_payload(config_snapshot),
        )
        return task_id

    @staticmethod
    def _fallback_estimate_snapshot(*, class_count: int, language: str = "java") -> Dict[str, Any]:
        per_class_input = 350_000
        per_class_output = 18_000
        per_class_cache = 40_000
        estimated_input = per_class_input * class_count
        estimated_output = per_class_output * class_count
        estimated_cache = per_class_cache * class_count
        estimated_cost = _estimate_cost_from_tokens(
            model="",
            input_tokens=estimated_input,
            output_tokens=estimated_output,
            cache_read_tokens=estimated_cache,
        )
        return {
            "estimate_source": "fallback_default",
            "estimate_language": language or "java",
            "target_count": class_count,
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": estimated_output,
            "estimated_cache_read_tokens": estimated_cache,
            "estimated_total_tokens": estimated_input + estimated_output + estimated_cache,
            "estimated_cost": estimated_cost,
            "estimated_cost_usd": estimated_cost,
            "estimated_seconds": 1800 * class_count,
            "estimated_elapsed_seconds": 1800 * class_count,
        }

    def _historical_estimate_snapshot(
        self,
        *,
        repo_path: str,
        module: Optional[str],
        language: str,
        class_count: int,
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
                  AND language=?
                  AND status='COMPLETED'
                  AND total_classes > 0
                  AND input_tokens > 0
                """,
                (repo_path, module, language or "java"),
            ).fetchone()
        if not row or int(row["samples"] or 0) < 1:
            return None
        per_task_classes = max(1, class_count)
        estimated_input = int(float(row["avg_input"] or 0.0) * per_task_classes)
        estimated_output = int(float(row["avg_output"] or 0.0) * per_task_classes)
        estimated_cache = int(float(row["avg_cache"] or 0.0) * per_task_classes)
        estimated_reasoning = int(float(row["avg_reasoning"] or 0.0) * per_task_classes)
        estimated_cost = float(row["avg_cost"] or 0.0) * per_task_classes
        estimated_seconds = float(row["avg_seconds"] or 0.0) * per_task_classes
        if estimated_input <= 0 and estimated_output <= 0:
            return None
        if estimated_cost <= 0:
            estimated_cost = _estimate_cost_from_tokens(
                model="",
                input_tokens=estimated_input,
                output_tokens=estimated_output,
                cache_read_tokens=estimated_cache,
                reasoning_tokens=estimated_reasoning,
            )
        return {
            "estimate_source": "historical_db",
            "estimate_language": language or "java",
            "estimate_samples": int(row["samples"] or 0),
            "target_count": class_count,
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": estimated_output,
            "estimated_cache_read_tokens": estimated_cache,
            "estimated_reasoning_tokens": estimated_reasoning,
            "estimated_total_tokens": estimated_input + estimated_output + estimated_cache + estimated_reasoning,
            "estimated_cost": estimated_cost,
            "estimated_cost_usd": estimated_cost,
            "estimated_seconds": estimated_seconds,
            "estimated_elapsed_seconds": estimated_seconds,
        }

    def get_task(self, task_id: int) -> Dict[str, Any]:
        row = self.db.get_repo_task(task_id)
        if not row:
            raise KeyError(f"Task {task_id} not found")
        return dict(row)

    def list_tasks(self, *, status: Optional[str] = None, repo_path: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        normalized_status = status.upper() if status else None
        return [dict(row) for row in self.db.list_repo_tasks(status=normalized_status, repo_path=repo_path, limit=limit)]

    def list_class_tasks(self, task_id: int) -> List[Dict[str, Any]]:
        return [dict(row) for row in self.db.list_class_tasks(task_id)]

    def ensure_class_tasks(
        self,
        repo_task_id: int,
        class_fqns: Iterable[str],
        *,
        module: Optional[str] = None,
        priority: int = DEFAULT_PRIORITY,
    ) -> None:
        for class_fqn in dict.fromkeys(class_fqns):
            self.db.create_class_task(repo_task_id, class_fqn, module=module, priority=priority)
        self._refresh_repo_counts(repo_task_id)

    def ensure_target_tasks(
        self,
        repo_task_id: int,
        targets: Iterable[TargetRef],
        *,
        module: Optional[str] = None,
        priority: int = DEFAULT_PRIORITY,
    ) -> None:
        for target in coerce_targets(targets):
            self.db.create_class_task(
                repo_task_id,
                legacy_class_fqn_for_storage(target),
                module=module,
                priority=priority,
                language=target.language,
                target_id=target.target_id,
                source_path=target.source_path,
                symbol=target.symbol,
                target_granularity=target.granularity,
                display_name=target.display_name,
            )
        self._refresh_repo_counts(repo_task_id)

    def find_target_task(self, repo_task_id: int, target: TargetRef) -> Optional[Dict[str, Any]]:
        target_ref = coerce_targets([target])[0]
        for row in self.db.list_class_tasks(repo_task_id):
            row_target_id = row["target_id"] if "target_id" in row.keys() else row["class_fqn"]
            row_language = row["language"] if "language" in row.keys() else "java"
            if row_language == target_ref.language and row_target_id == target_ref.target_id:
                return dict(row)
        return None

    def _refresh_repo_counts(self, task_id: int) -> None:
        aggregate = self.db.aggregate_repo_task(task_id)
        self.db.update_repo_task(
            task_id,
            total_classes=aggregate.get("class_count") or 0,
            completed_classes=aggregate.get("completed_classes") or 0,
            passed_classes=aggregate.get("passed_classes") or 0,
            failed_classes=aggregate.get("failed_classes") or 0,
        )

    def start_task(self, task_id: int) -> None:
        self.db.update_repo_task(
            task_id,
            status="QUEUED",
            current_stage="queued",
            started_at=None,
            finished_at=None,
            last_error=None,
            error=None,
        )
        self.db.add_event(task_id, None, "task_queued", "Task queued for daemon execution", stage="queued")

    def mark_running(self, task_id: int, *, stage: str = "startup", detail: Optional[str] = None) -> None:
        row = self.db.get_repo_task(task_id)
        started_at = row["started_at"] if row and row["started_at"] else now_iso()
        self.db.update_repo_task(
            task_id,
            status="RUNNING",
            current_stage=stage,
            current_detail=detail,
            started_at=started_at,
            finished_at=None,
            last_error=None,
            error=None,
        )
        self.db.add_event(task_id, None, "task_running", detail or "Task running", stage=stage)

    def stop_task(self, task_id: int, *, reason: Optional[str] = None) -> None:
        self.db.request_control(task_id, "stop", reason=reason)
        self.db.update_repo_task(task_id, status="STOP_REQUESTED", stop_requested_at=now_iso(), current_detail=reason)
        self.db.add_event(task_id, None, "task_stopped", reason or "Stop requested", severity="WARNING")

    def mark_stopped(self, task_id: int, *, reason: Optional[str] = None, stage: str = "stopped") -> None:
        self.db.acknowledge_controls(task_id, action="stop")
        self.db.update_repo_task(
            task_id,
            status="STOPPED",
            current_stage=stage,
            current_detail=reason,
            finished_at=now_iso(),
            last_error=reason,
            error=reason,
        )
        self.db.add_event(task_id, None, "task_stopped", reason or "Task stopped cooperatively", stage=stage, severity="WARNING")

    def check_stop_requested(self, task_id: int) -> Optional[str]:
        task = self.db.get_repo_task(task_id)
        if task and task["status"] == "STOP_REQUESTED":
            return task["current_detail"] or "stop requested"
        control = self.db.latest_control(task_id)
        if control and control["action"] in {"stop", "cancel"}:
            return control["reason"] or f"{control['action']} requested"
        return None

    def cancel_task(self, task_id: int, *, reason: Optional[str] = None) -> None:
        self.db.request_control(task_id, "cancel", reason=reason)
        self.db.update_repo_task(task_id, status="CANCELLED", finished_at=now_iso(), current_detail=reason)
        for row in self.db.list_class_tasks(task_id):
            if row["status"] in {"CREATED", "PENDING", "QUEUED", "STOPPED"}:
                self.db.update_class_task(row["id"], status="CANCELLED", finished_at=now_iso(), last_error=reason, error=reason)
        self.db.add_event(task_id, None, "task_cancelled", reason or "Task cancelled", severity="WARNING")

    def resume_task(self, task_id: int, *, force_rerun_failed: bool = False, force_rerun_all: bool = False) -> None:
        row = self.db.get_repo_task(task_id)
        if not row:
            raise KeyError(f"Task {task_id} not found")
        config_snapshot = _with_opencode_routing_metadata(
            json_loads(row["config_snapshot_json"])
        )
        self.db.acknowledge_controls(task_id)
        self.db.update_repo_task(
            task_id,
            status="QUEUED",
            config_snapshot_json=json_dumps(config_snapshot),
            stop_requested_at=None,
            finished_at=None,
            resume_count=int(row["resume_count"] or 0) + 1,
            current_stage="queued",
            current_detail="resume requested",
            last_error=None,
            error=None,
        )
        resumable = {"CREATED", "PENDING", "QUEUED", "RUNNING", "STOPPED"}
        if force_rerun_failed:
            resumable.update(TERMINAL_CLASS_STATUSES - {"PASS", "CANCELLED"})
        if force_rerun_all:
            resumable.update(TERMINAL_CLASS_STATUSES)
        for row in self.db.list_class_tasks(task_id):
            if row["status"] in resumable:
                self.db.update_class_task(
                    row["id"], status="QUEUED", stage="queued", current_stage="queued",
                    finished_at=None, last_error=None, error=None,
                )
        self._refresh_repo_counts(task_id)
        self.db.add_event(
            task_id,
            None,
            "task_resumed",
            "Task resumed and queued",
            stage="queued",
            payload={"force_rerun_failed": force_rerun_failed, "force_rerun_all": force_rerun_all},
        )
        self.db.add_event(
            task_id,
            None,
            "opencode_model_selected",
            f"Selected OpenCode model {config_snapshot.get('opencode_selected_model')}",
            stage="routing",
            payload=_selected_model_event_payload(config_snapshot),
        )

    def stop_and_resume_for_provider_fallback(
        self,
        task_id: int,
        *,
        provider: str,
        model: str,
        candidate_index: int,
        reason: str,
        phase: str,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        row = self.db.get_repo_task(task_id)
        if not row:
            raise KeyError(f"Task {task_id} not found")
        snapshot = json_loads(row["config_snapshot_json"])
        history = list(snapshot.get("opencode_fallback_history") or [])
        event = {
            "provider": provider,
            "model": model,
            "candidate_index": candidate_index,
            "reason": reason,
            "phase": phase,
        }
        if retry_after_seconds is not None:
            event["retry_after_seconds"] = retry_after_seconds
        history.append(event)
        snapshot["opencode_fallback_history"] = history[-20:]
        snapshot = _with_opencode_routing_metadata(snapshot)
        self.db.update_repo_task(task_id, config_snapshot_json=json_dumps(snapshot))
        self.db.add_event(
            task_id,
            None,
            "opencode_model_unavailable",
            f"OpenCode model unavailable: {model} ({reason})",
            stage=phase,
            severity="WARNING",
            payload=event,
        )

        next_model = snapshot.get("opencode_selected_model")
        if not next_model:
            message = "No OpenCode provider candidates remain after fallback"
            self.mark_failed(task_id, message, stage="provider_fallback")
            self.db.add_event(
                task_id,
                None,
                "opencode_provider_fallback_exhausted",
                message,
                stage="provider_fallback",
                severity="ERROR",
                payload={"last_failure": event},
            )
            return

        self.mark_stopped(
            task_id,
            reason=f"OpenCode provider fallback: {model} {reason}",
            stage="provider_fallback",
        )
        self.db.add_event(
            task_id,
            None,
            "opencode_provider_fallback_stop",
            f"Stopped task after {model} {reason}",
            stage="provider_fallback",
            severity="WARNING",
            payload={"failed": event, "next_model": next_model},
        )
        self.resume_task(task_id)
        self.db.add_event(
            task_id,
            None,
            "opencode_provider_fallback_resume",
            f"Queued task to resume with {next_model}",
            stage="provider_fallback",
            payload={"failed": event, "next_model": next_model},
        )

    def reprioritize_task(self, task_id: int, priority: int) -> None:
        self.db.update_repo_task(task_id, priority=int(priority))
        self.db.add_event(task_id, None, "task_reprioritized", f"Priority set to {priority}")

    def reprioritize_class(self, class_task_id: int, priority: int) -> None:
        row = self.db.get_class_task(class_task_id)
        if not row:
            raise KeyError(f"Class task {class_task_id} not found")
        self.db.update_class_task(class_task_id, priority=int(priority))
        self.db.add_event(row["repo_task_id"], class_task_id, "class_reprioritized", f"Class priority set to {priority}")

    def preempt_running_same_repo_for_urgent(self, urgent_task_id: int) -> List[int]:
        urgent = self.db.get_repo_task(urgent_task_id)
        if not urgent:
            raise KeyError(f"Task {urgent_task_id} not found")
        with self.db.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT id FROM repo_tasks
                    WHERE repo_path=? AND status='RUNNING' AND id<>? AND priority>=100
                    ORDER BY started_at ASC, id ASC
                    """,
                    (urgent["repo_path"], urgent_task_id),
                )
            )
        preempted_ids: List[int] = []
        for row in rows:
            preempted_id = int(row["id"])
            reason = f"preempted by urgent CI repair task {urgent_task_id}"
            self.stop_task(preempted_id, reason=reason)
            payload = {"preempted_by_task_id": urgent_task_id, "preempted_task_id": preempted_id}
            self.db.add_event(
                preempted_id,
                None,
                "task_preempted",
                reason,
                stage="preempted",
                severity="WARNING",
                payload=payload,
            )
            self.db.add_event(
                urgent_task_id,
                None,
                "task_preempting",
                f"Requested stop for batch task {preempted_id}",
                stage="preempted",
                payload=payload,
            )
            preempted_ids.append(preempted_id)
        return preempted_ids

    def resume_preempted_tasks(self, urgent_task_id: int) -> List[int]:
        with self.db.connect() as conn:
            rows = list(
                conn.execute(
                    """
                    SELECT repo_task_id, payload_json FROM task_events
                    WHERE event_type='task_preempted'
                    ORDER BY id ASC
                    """
                )
            )
        resumed: List[int] = []
        for row in rows:
            payload = json_loads(row["payload_json"])
            if int(payload.get("preempted_by_task_id") or 0) != int(urgent_task_id):
                continue
            preempted_id = int(row["repo_task_id"])
            task = self.db.get_repo_task(preempted_id)
            if not task or task["status"] not in {"STOPPED", "STOP_REQUESTED"}:
                continue
            self.resume_task(preempted_id)
            self.db.add_event(
                urgent_task_id,
                None,
                "preempted_task_resumed",
                f"Resumed preempted batch task {preempted_id}",
                stage="finished",
                payload={"preempted_task_id": preempted_id},
            )
            resumed.append(preempted_id)
        return resumed

    def record_stage(
        self,
        repo_task_id: int,
        stage: str,
        *,
        detail: Optional[str] = None,
        class_fqns: Optional[Iterable[str]] = None,
    ) -> None:
        self.db.update_repo_task(repo_task_id, current_stage=stage, current_detail=detail)
        updated_classes = []
        for class_fqn in dict.fromkeys(class_fqns or []):
            row = self.db.find_class_task(repo_task_id, class_fqn)
            if not row:
                continue
            status = row["status"]
            if status in TERMINAL_CLASS_STATUSES:
                continue
            updates = {"current_stage": stage, "current_detail": detail}
            updates["stage"] = stage
            updates["status"] = "RUNNING"
            if not row["started_at"]:
                updates["started_at"] = now_iso()
            self.db.update_class_task(row["id"], **updates)
            updated_classes.append(row["id"])
        self.db.add_event(
            repo_task_id,
            updated_classes[0] if len(updated_classes) == 1 else None,
            "stage_started",
            detail or stage,
            stage=stage,
            payload=self._target_event_payload(repo_task_id, class_fqns or []),
        )

    def record_stage_for_targets(
        self,
        repo_task_id: int,
        stage: str,
        *,
        detail: Optional[str] = None,
        targets: Optional[Iterable[TargetRef]] = None,
    ) -> None:
        target_keys = [result_key(target) for target in targets or []]
        self.record_stage(repo_task_id, stage, detail=detail, class_fqns=target_keys)

    def record_stage_completed(
        self,
        repo_task_id: int,
        stage: str,
        *,
        detail: Optional[str] = None,
        class_fqns: Optional[Iterable[str]] = None,
        class_task_id: Optional[int] = None,
    ) -> None:
        if class_task_id is None:
            fqns = list(dict.fromkeys(class_fqns or []))
            if len(fqns) == 1:
                row = self.db.find_class_task(repo_task_id, fqns[0])
                class_task_id = row["id"] if row else None
        self.db.add_event(
            repo_task_id,
            class_task_id,
            "stage_completed",
            detail or stage,
            stage=stage,
            payload=self._target_event_payload(repo_task_id, class_fqns or []),
        )

    def record_stage_completed_for_targets(
        self,
        repo_task_id: int,
        stage: str,
        *,
        detail: Optional[str] = None,
        targets: Optional[Iterable[TargetRef]] = None,
        class_task_id: Optional[int] = None,
    ) -> None:
        target_keys = [result_key(target) for target in targets or []]
        self.record_stage_completed(
            repo_task_id,
            stage,
            detail=detail,
            class_fqns=target_keys,
            class_task_id=class_task_id,
        )

    def _target_event_payload(self, repo_task_id: int, storage_keys: Iterable[str]) -> Dict[str, Any]:
        keys = list(dict.fromkeys(storage_keys or []))
        if not keys:
            return {"class_fqns": []}
        rows = [self.db.find_class_task(repo_task_id, key) for key in keys]
        rows = [row for row in rows if row]
        if not rows:
            return {"class_fqns": keys}
        targets = [
            TargetIdentity(
                language=row["language"] if "language" in row.keys() else "java",
                target_id=row["target_id"] if "target_id" in row.keys() and row["target_id"] else row["class_fqn"],
                display_name=row["display_name"] if "display_name" in row.keys() and row["display_name"] else row["class_fqn"],
                source_path=row["source_path"] if "source_path" in row.keys() else None,
                symbol=row["symbol"] if "symbol" in row.keys() else row["class_fqn"],
                granularity=row["target_granularity"] if "target_granularity" in row.keys() and row["target_granularity"] else "class",
            )
            for row in rows
        ]
        payload = event_payload_for_targets(targets)
        payload["class_fqns"] = keys
        return payload

    def sync_results(
        self,
        repo_task_id: int,
        results: Dict[str, Any],
        *,
        module: Optional[str] = None,
        session_token_usage: Optional[Dict[str, Any]] = None,
        phase_token_usage: Optional[Dict[str, Any]] = None,
        report_path: Optional[str] = None,
        run_log_path: Optional[str] = None,
        elapsed_seconds: Optional[float] = None,
        final_error: Optional[str] = None,
    ) -> None:
        results = results or {}
        task = self.db.get_repo_task(repo_task_id)
        config_snapshot = json_loads(task["config_snapshot_json"]) if task else {}
        model = config_snapshot.get("opencode_model") or ""
        self.ensure_class_tasks(repo_task_id, results.keys(), module=module)
        for class_fqn, result in results.items():
            row = self.db.find_class_task(repo_task_id, class_fqn)
            if not row:
                continue
            status = _normalize_class_status(str(result.get("status") or "FAIL"))
            if row["status"] in {"PUSH_FAILED", "UNSAFE_DIFF", "BUDGET_EXCEEDED"}:
                status = row["status"]
            session_ids = result.get("session_ids") or []
            if not isinstance(session_ids, list):
                session_ids = []
            existing_session_ids = self._session_ids_from_row(row)
            merged_session_ids = self._merge_session_ids(existing_session_ids, session_ids)
            has_new_sessions = any(session_id not in existing_session_ids for session_id in session_ids)
            test_file_content = result.get("test_file_content") or ""
            test_count = result.get("test_count")
            if test_count is None and isinstance(test_file_content, str):
                test_count = test_file_content.count("@Test")
                if result.get("language") == "python":
                    test_count = len(re.findall(r"^\s*def\s+test_", test_file_content, flags=re.MULTILINE))
            test_file_lines = len(test_file_content.splitlines()) if isinstance(test_file_content, str) and test_file_content else None
            class_phase_usage = result.get("phase_token_usage") or result.get("token_usage") or {}
            if not isinstance(class_phase_usage, dict):
                class_phase_usage = {}
            existing_phase_usage = json_loads(row["phase_token_usage_json"]) if row["phase_token_usage_json"] else {}
            existing_totals = self._token_totals_from_row(row)
            incoming_totals = self._token_totals({}, class_phase_usage)
            if class_phase_usage and has_new_sessions and any(existing_totals.values()):
                stored_phase_usage = self._merge_phase_token_usage(existing_phase_usage, class_phase_usage)
                class_totals = self._add_token_totals(existing_totals, incoming_totals)
            elif class_phase_usage:
                stored_phase_usage = class_phase_usage
                class_totals = incoming_totals
            else:
                stored_phase_usage = existing_phase_usage
                class_totals = existing_totals
            class_cost = _estimate_cost_from_tokens(
                model=model,
                input_tokens=class_totals["input"],
                output_tokens=class_totals["output"],
                cache_read_tokens=class_totals["cache_read"],
                reasoning_tokens=class_totals["reasoning"],
            )
            self.db.update_class_task(
                row["id"],
                status=status,
                current_stage="finished",
                stage="finished",
                current_detail=status,
                coverage_line=result.get("coverage", result.get("line_coverage")),
                coverage=result.get("coverage", result.get("line_coverage")),
                mutation_score=result.get("mutation_score"),
                surviving_mutants=result.get("surviving_mutants"),
                total_mutants=result.get("total_mutants"),
                test_count=test_count,
                test_file_path=result.get("test_file_path"),
                test_file_lines=test_file_lines,
                session_ids_json=json_dumps(merged_session_ids),
                phase_token_usage_json=json_dumps(stored_phase_usage),
                input_tokens=class_totals["input"],
                output_tokens=class_totals["output"],
                cache_read_tokens=class_totals["cache_read"],
                cache_write_tokens=class_totals["cache_write"],
                reasoning_tokens=class_totals["reasoning"],
                total_tokens=sum(class_totals.values()),
                actual_input_tokens=class_totals["input"],
                actual_output_tokens=class_totals["output"],
                actual_cache_read_tokens=class_totals["cache_read"],
                actual_cache_write_tokens=class_totals["cache_write"],
                actual_cost=class_cost,
                provider_cost_usd=class_cost,
                finished_at=now_iso(),
                last_error=result.get("error"),
                error=result.get("error"),
                elapsed_seconds=result.get("elapsed_seconds"),
                actual_elapsed_seconds=result.get("elapsed_seconds"),
            )
            self.db.add_event(
                repo_task_id,
                row["id"],
                "stage_completed",
                status,
                stage="finished",
                payload={**self._target_event_payload(repo_task_id, [class_fqn]), "result_status": status},
            )

        observed_totals = self._token_totals(session_token_usage or {}, phase_token_usage or {})
        result_rows = {class_fqn: self.db.find_class_task(repo_task_id, class_fqn) for class_fqn in results.keys()}
        missing_token_fqns = [
            class_fqn
            for class_fqn, row in result_rows.items()
            if row and not any(self._token_totals_from_row(row).values())
        ]
        if any(observed_totals.values()) and missing_token_fqns:
            split_totals = self._split_token_totals(observed_totals, len(missing_token_fqns))
            for class_fqn, split in zip(missing_token_fqns, split_totals):
                row = self.db.find_class_task(repo_task_id, class_fqn)
                if not row:
                    continue
                split_cost = _estimate_cost_from_tokens(
                    model=model,
                    input_tokens=split["input"],
                    output_tokens=split["output"],
                    cache_read_tokens=split["cache_read"],
                    reasoning_tokens=split["reasoning"],
                )
                self.db.update_class_task(
                    row["id"],
                    phase_token_usage_json=json_dumps({"batch_shared": split}),
                    input_tokens=split["input"],
                    output_tokens=split["output"],
                    cache_read_tokens=split["cache_read"],
                    cache_write_tokens=split["cache_write"],
                    reasoning_tokens=split["reasoning"],
                    total_tokens=sum(split.values()),
                    actual_input_tokens=split["input"],
                    actual_output_tokens=split["output"],
                    actual_cache_read_tokens=split["cache_read"],
                    actual_cache_write_tokens=split["cache_write"],
                    actual_cost=split_cost,
                    provider_cost_usd=split_cost,
                )
        remaining_missing_token_fqns = [
            class_fqn
            for class_fqn in results.keys()
            if (row := self.db.find_class_task(repo_task_id, class_fqn)) and not any(self._token_totals_from_row(row).values())
        ]
        if remaining_missing_token_fqns:
            try:
                self.recover_missing_class_tokens_from_opencode_db(
                    repo_task_id,
                    class_fqns=remaining_missing_token_fqns,
                )
            except Exception:
                pass
        class_rows = self.db.list_class_tasks(repo_task_id)
        repo_session_ids = self._merge_session_ids(
            self._session_ids_from_json(task["session_ids_json"] if task and "session_ids_json" in task.keys() else "[]"),
            self._repo_session_ids_from_class_rows(class_rows),
        )
        session_totals = self._repo_token_totals_from_sessions(repo_session_ids)
        aggregate = self.db.aggregate_repo_task(repo_task_id)
        class_totals = {
            "input": int(aggregate.get("input_tokens") or aggregate.get("actual_input_tokens") or 0),
            "output": int(aggregate.get("output_tokens") or aggregate.get("actual_output_tokens") or 0),
            "cache_read": int(aggregate.get("cache_read_tokens") or aggregate.get("actual_cache_read_tokens") or 0),
            "cache_write": int(aggregate.get("cache_write_tokens") or aggregate.get("actual_cache_write_tokens") or 0),
            "reasoning": int(aggregate.get("reasoning_tokens") or 0),
            "total": int(aggregate.get("total_tokens") or 0),
        }
        totals = session_totals if any(session_totals.values()) else class_totals
        if not any(totals.values()):
            totals = {**observed_totals, "total": sum(observed_totals.values())}
        
        provider_cost = self._extract_provider_cost(session_token_usage or {}, phase_token_usage or {})
        token_cost = _estimate_cost_from_tokens(
            model=model,
            input_tokens=totals["input"],
            output_tokens=totals["output"],
            cache_read_tokens=totals["cache_read"],
            reasoning_tokens=totals["reasoning"],
        )
        estimated_cost = provider_cost if provider_cost is not None else token_cost
        # Always record a non-NULL provider_cost_usd — fall back to token estimate when the
        # stream does not include an explicit cost field (deepseek/openrouter provider).
        if provider_cost is None and (totals["input"] or totals["output"]):
            provider_cost = token_cost
        total_tokens = totals["total"]
        budget_used_ratio = None
        if task and task["estimated_cost_usd"]:
            budget_used_ratio = float(estimated_cost or 0.0) / max(float(task["estimated_cost_usd"]), 0.000001)
        prior_status = task["status"] if task else None
        prior_error = task["error"] if task and "error" in task.keys() else None
        repo_status = "COMPLETED"
        preserved_terminal_statuses = {"FAILED", "POISONED", "BUDGET_EXCEEDED", "CANCELLED"}
        if prior_status in preserved_terminal_statuses and (final_error or prior_error):
            repo_status = prior_status
            final_error = final_error or prior_error
        elif final_error and not class_rows:
            repo_status = "FAILED"
        elif class_rows:
            in_progress = any(row["status"] in {"RUNNING", "QUEUED", "PENDING", "GENERATED"} for row in class_rows)
            any_failed = any(row["status"] in TERMINAL_CLASS_STATUSES - {"PASS", "CANCELLED"} for row in class_rows)
            
            if in_progress:
                repo_status = "RUNNING"
            elif any_failed or final_error:
                repo_status = "FAILED"
            elif all(row["status"] in {"PASS", "CANCELLED"} for row in class_rows):
                repo_status = "COMPLETED"
            else:
                repo_status = "FAILED" if (final_error or any_failed) else "RUNNING"

        is_terminal_repo_status = repo_status in {"COMPLETED", "FAILED", "POISONED", "BUDGET_EXCEEDED", "CANCELLED"}
        repo_current_stage = "finished" if is_terminal_repo_status else (task["current_stage"] if task else None)
        repo_current_detail = (
            final_error or repo_status
            if is_terminal_repo_status
            else f"{int(aggregate.get('completed_classes') or 0)}/{int(aggregate.get('class_count') or len(class_rows) or 0)} targets completed"
        )

        self.db.update_repo_task(

            repo_task_id,
            status=repo_status,
            current_stage=repo_current_stage,
            current_detail=repo_current_detail,
            actual_input_tokens=totals["input"],
            actual_output_tokens=totals["output"],
            actual_cache_read_tokens=totals["cache_read"],
            actual_cache_write_tokens=totals["cache_write"],
            input_tokens=totals["input"],
            output_tokens=totals["output"],
            cache_read_tokens=totals["cache_read"],
            cache_write_tokens=totals["cache_write"],
            reasoning_tokens=totals["reasoning"],
            total_tokens=total_tokens,
            actual_cost=estimated_cost,
            provider_cost_usd=provider_cost,
            budget_used_ratio=budget_used_ratio,
            total_classes=aggregate.get("class_count") or len(class_rows),
            completed_classes=aggregate.get("completed_classes") or 0,
            passed_classes=aggregate.get("passed_classes") or 0,
            failed_classes=aggregate.get("failed_classes") or 0,
            coverage_avg=aggregate.get("coverage_line_avg"),
            coverage_min=aggregate.get("coverage_line_min"),
            mutation_avg=aggregate.get("mutation_score_avg"),
            mutation_min=aggregate.get("mutation_score_min"),
            elapsed_seconds=elapsed_seconds,
            actual_elapsed_seconds=elapsed_seconds,
            report_path=report_path,
            latest_report_path=report_path,
            run_log_path=run_log_path,
            session_ids_json=json_dumps(repo_session_ids),
            finished_at=now_iso() if is_terminal_repo_status else None,
            last_error=final_error,
            error=final_error,
        )
        event_type = (
            "task_failed"
            if repo_status == "FAILED"
            else ("task_completed" if repo_status == "COMPLETED" else "task_progress")
        )
        self.db.add_event(
            repo_task_id,
            None,
            event_type,
            final_error or (f"Task finished as {repo_status}" if is_terminal_repo_status else repo_current_detail),
            stage="finished" if is_terminal_repo_status else repo_current_stage,
            severity="ERROR" if repo_status == "FAILED" else "INFO",
            payload={"token_totals": totals},
        )
        if is_terminal_repo_status:
            self.resume_preempted_tasks(repo_task_id)

    def sync_target_results(
        self,
        repo_task_id: int,
        results: Dict[Union[str, Mapping[str, Any], TargetIdentity], Any],
        *,
        targets: Optional[Iterable[TargetRef]] = None,
        module: Optional[str] = None,
        session_token_usage: Optional[Dict[str, Any]] = None,
        phase_token_usage: Optional[Dict[str, Any]] = None,
        report_path: Optional[str] = None,
        run_log_path: Optional[str] = None,
        elapsed_seconds: Optional[float] = None,
        final_error: Optional[str] = None,
    ) -> None:
        target_inputs: List[TargetRef] = list(targets or [])
        target_inputs.extend(
            key for key in (results or {}).keys() if isinstance(key, TargetIdentity) or isinstance(key, Mapping)
        )
        if target_inputs:
            self.ensure_target_tasks(repo_task_id, target_inputs, module=module)
        normalized_results = {result_key(key): value for key, value in (results or {}).items()}
        self.sync_results(
            repo_task_id,
            normalized_results,
            module=module,
            session_token_usage=session_token_usage,
            phase_token_usage=phase_token_usage,
            report_path=report_path,
            run_log_path=run_log_path,
            elapsed_seconds=elapsed_seconds,
            final_error=final_error,
        )

    def mark_failed(self, repo_task_id: int, message: str, *, stage: str = "failed") -> None:
        self.db.update_repo_task(
            repo_task_id,
            status="FAILED",
            current_stage=stage,
            current_detail=message,
            finished_at=now_iso(),
            last_error=message,
            error=message,
        )
        self.db.add_event(repo_task_id, None, "task_failed", message, stage=stage, severity="ERROR")
        self.resume_preempted_tasks(repo_task_id)
        from uta.config import settings
        threshold = int(settings.quarantine_threshold)
        if threshold > 0 and self.db.count_task_failures(repo_task_id) >= threshold:
            self.mark_poisoned(repo_task_id, f"Auto-quarantined after {threshold} failures. Last: {message}")

    def mark_completed(self, repo_task_id: int, *, message: str = "Task completed") -> None:
        self.db.update_repo_task(
            repo_task_id,
            status="COMPLETED",
            current_stage="finished",
            current_detail=message,
            finished_at=now_iso(),
        )
        self.db.add_event(repo_task_id, None, "task_completed", message, stage="finished")
        self.resume_preempted_tasks(repo_task_id)

    def mark_poisoned(self, repo_task_id: int, message: str) -> None:
        self.db.update_repo_task(
            repo_task_id,
            status="POISONED",
            current_stage="quarantined",
            current_detail=message,
            finished_at=now_iso(),
            last_error=message,
            error=message,
        )
        self.db.add_event(repo_task_id, None, "task_poisoned", message, stage="quarantined", severity="ERROR")

    def mark_budget_exceeded(self, repo_task_id: int, message: str = "Budget cap exceeded") -> None:
        self.db.update_repo_task(
            repo_task_id,
            status="BUDGET_EXCEEDED",
            current_stage="budget_exceeded",
            current_detail=message,
            finished_at=now_iso(),
            last_error=message,
            error=message,
        )
        self.db.add_event(repo_task_id, None, "task_budget_exceeded", message, stage="budget_exceeded", severity="ERROR")
        self.resume_preempted_tasks(repo_task_id)

    def unblock(self, repo_task_id: int) -> None:
        """Reset a POISONED or BUDGET_EXCEEDED task to QUEUED so the daemon can pick it up."""
        task = self.db.get_repo_task(repo_task_id)
        if task is None:
            raise ValueError(f"Task {repo_task_id} not found")
        status = task["status"]
        if status not in ("POISONED", "BUDGET_EXCEEDED", "FAILED"):
            raise ValueError(f"Task {repo_task_id} has status {status!r}; only POISONED/BUDGET_EXCEEDED/FAILED can be unblocked")
        self.db.acknowledge_controls(repo_task_id)
        self.db.update_repo_task(
            repo_task_id,
            status="QUEUED",
            current_stage="unblocked",
            current_detail=f"Unblocked from {status}",
            finished_at=None,
            error=None,
        )
        self.db.add_event(repo_task_id, None, "task_unblocked", f"Unblocked from {status}", stage="unblocked")

    @staticmethod
    def _token_totals(session_token_usage: Dict[str, Any], phase_token_usage: Dict[str, Any]) -> Dict[str, int]:
        total_tokens = session_token_usage.get("total_tokens") if isinstance(session_token_usage, dict) else None
        if isinstance(total_tokens, dict) and total_tokens:
            return {
                "input": int(total_tokens.get("input") or 0),
                "output": int(total_tokens.get("output") or 0),
                "cache_read": int(total_tokens.get("cache_read") or 0),
                "cache_write": int(total_tokens.get("cache_write") or 0),
                "reasoning": int(total_tokens.get("reasoning") or 0),
            }
        totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}
        for stats in (phase_token_usage or {}).values():
            if not isinstance(stats, dict):
                continue
            for key in totals:
                totals[key] += int(stats.get(key) or 0)
        return totals

    @staticmethod
    def _session_ids_from_json(raw: str) -> List[str]:
        try:
            parsed = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            parsed = []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed if item]

    @classmethod
    def _session_ids_from_row(cls, row: Any) -> List[str]:
        if not row or "session_ids_json" not in row.keys():
            return []
        return cls._session_ids_from_json(row["session_ids_json"])

    @staticmethod
    def _merge_session_ids(*groups: Iterable[str]) -> List[str]:
        merged: List[str] = []
        seen = set()
        for group in groups:
            for session_id in group or []:
                if not session_id or session_id in seen:
                    continue
                seen.add(session_id)
                merged.append(str(session_id))
        return merged

    @staticmethod
    def _token_totals_from_row(row: Any) -> Dict[str, int]:
        return {
            "input": int(row["input_tokens"] or row["actual_input_tokens"] or 0),
            "output": int(row["output_tokens"] or row["actual_output_tokens"] or 0),
            "cache_read": int(row["cache_read_tokens"] or row["actual_cache_read_tokens"] or 0),
            "cache_write": int(row["cache_write_tokens"] or row["actual_cache_write_tokens"] or 0),
            "reasoning": int(row["reasoning_tokens"] or 0),
        }

    @staticmethod
    def _add_token_totals(left: Dict[str, int], right: Dict[str, int]) -> Dict[str, int]:
        return {key: int(left.get(key) or 0) + int(right.get(key) or 0) for key in TOKEN_TOTAL_KEYS}

    @staticmethod
    def _split_token_totals(totals: Dict[str, int], count: int) -> List[Dict[str, int]]:
        count = max(1, int(count or 1))
        splits = [{key: 0 for key in TOKEN_TOTAL_KEYS} for _ in range(count)]
        for key in TOKEN_TOTAL_KEYS:
            value = int(totals.get(key) or 0)
            base, remainder = divmod(value, count)
            for index in range(count):
                splits[index][key] = base + (1 if index < remainder else 0)
        return splits

    @staticmethod
    def _merge_phase_token_usage(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
        merged: Dict[str, Dict[str, int]] = {}
        for phase in list((existing or {}).keys()) + [phase for phase in (incoming or {}).keys() if phase not in (existing or {})]:
            left = existing.get(phase) if isinstance(existing, dict) else {}
            right = incoming.get(phase) if isinstance(incoming, dict) else {}
            if not isinstance(left, dict):
                left = {}
            if not isinstance(right, dict):
                right = {}
            merged[phase] = {key: int(left.get(key) or 0) + int(right.get(key) or 0) for key in TOKEN_TOTAL_KEYS}
            if "total" in left or "total" in right:
                merged[phase]["total"] = int(left.get("total") or 0) + int(right.get("total") or 0)
        return merged

    def _repo_token_totals_from_sessions(self, session_ids: List[str], opencode_db_path: Optional[Union[os.PathLike[str], str]] = None) -> Dict[str, int]:
        db_path = Path(opencode_db_path).expanduser().resolve() if opencode_db_path else DEFAULT_OPENCODE_DB
        totals = {key: 0 for key in (*TOKEN_TOTAL_KEYS, "total")}
        if not db_path.exists() or not session_ids:
            return totals
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            for session_id in session_ids:
                for msg in conn.execute(
                    "SELECT data FROM message WHERE session_id = ? ORDER BY time_created ASC",
                    (session_id,),
                ):
                    is_assistant, token_totals = self._message_token_totals(msg["data"])
                    if not is_assistant:
                        continue
                    for key, value in token_totals.items():
                        if key in totals:
                            totals[key] += int(value or 0)
            totals["total"] = sum(totals[k] for k in TOKEN_TOTAL_KEYS)
            conn.close()
        except Exception:
            pass
        return totals

    @classmethod
    def _repo_session_ids_from_class_rows(cls, class_rows: Iterable[Any]) -> List[str]:

        merged: List[str] = []
        for row in class_rows:
            merged = cls._merge_session_ids(merged, cls._session_ids_from_row(row))
        return merged

    @staticmethod
    def _extract_provider_cost(session_token_usage: Dict[str, Any], phase_token_usage: Dict[str, Any]) -> Optional[float]:
        for source in (session_token_usage, phase_token_usage):
            if not isinstance(source, dict):
                continue
            if source.get("cost") is not None:
                return float(source["cost"])
            if source.get("total_cost") is not None:
                return float(source["total_cost"])
            for value in source.values():
                if isinstance(value, dict):
                    if value.get("cost") is not None:
                        return float(value["cost"])
                    if value.get("total_cost") is not None:
                        return float(value["total_cost"])
        return None

    def record_commit(
        self,
        repo_task_id: int,
        *,
        class_fqns: Iterable[str],
        commit_sha: Optional[str],
        pushed_at: Optional[str] = None,
        remote_ref: Optional[str] = None,
    ) -> None:
        for class_fqn in class_fqns:
            row = self.db.find_class_task(repo_task_id, class_fqn)
            if row:
                self.db.update_class_task(row["id"], commit_sha=commit_sha, pushed_at=pushed_at)
        self.db.update_repo_task(repo_task_id, latest_commit=commit_sha, remote_ref=remote_ref)
        self.db.add_event(
            repo_task_id,
            None,
            "commit_created" if commit_sha else "commit_skipped",
            f"Commit {commit_sha}" if commit_sha else "No commit sha recorded",
            payload={"class_fqns": list(class_fqns), "remote_ref": remote_ref},
        )

    def record_push_verified(self, repo_task_id: int, *, branch_name: str, local_head: str, remote_head: Optional[str]) -> None:
        ok = bool(local_head and remote_head and local_head == remote_head)
        updates = {
            "remote_ref": remote_head,
            "latest_commit": local_head,
            "error": None if ok else f"Remote ref mismatch for {branch_name}: local={local_head} remote={remote_head}",
        }
        if not ok:
            updates["status"] = "FAILED"
        self.db.update_repo_task(repo_task_id, **updates)
        self.db.add_event(
            repo_task_id,
            None,
            "push_verified" if ok else "push_failed",
            f"{branch_name}: local={local_head} remote={remote_head}",
            severity="INFO" if ok else "ERROR",
        )

    def record_push_failed(
        self,
        repo_task_id: int,
        *,
        branch_name: str,
        message: str,
        class_fqns: Optional[Iterable[str]] = None,
    ) -> None:
        for class_fqn in class_fqns or []:
            row = self.db.find_class_task(repo_task_id, class_fqn)
            if row:
                self.db.update_class_task(row["id"], status="PUSH_FAILED", error=message, last_error=message)
        self.db.update_repo_task(
            repo_task_id,
            status="FAILED",
            current_stage="push_failed",
            current_detail=message,
            error=message,
            last_error=message,
        )
        self.db.add_event(
            repo_task_id,
            None,
            "push_failed",
            f"{branch_name}: {message}",
            stage="push",
            severity="ERROR",
            payload={"class_fqns": list(class_fqns or [])},
        )

    def status_payload(self, repo_task_id: int) -> Dict[str, Any]:
        from uta.tasks.render import build_status_payload

        return build_status_payload(self.db, repo_task_id)

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            result = float(value)
            if math.isnan(result):
                return None
            return result
        except Exception:
            return None

    @classmethod
    def _metric_stats(cls, values: Iterable[Any]) -> Dict[str, Optional[float]]:
        series = [value for value in (cls._safe_float(item) for item in values) if value is not None]
        if not series:
            return {"count": 0, "total": None, "avg": None, "max": None, "min": None}
        return {
            "count": len(series),
            "total": sum(series),
            "avg": sum(series) / len(series),
            "max": max(series),
            "min": min(series),
        }

    def build_task_summary(
        self,
        repo_task_id: int,
        *,
        opencode_db_path: Optional[Union[os.PathLike[str], str]] = None,
        recalc_project_coverage: bool = False,
    ) -> Dict[str, Any]:
        task = self.get_task(repo_task_id)
        class_rows = self.list_class_tasks(repo_task_id)
        aggregate = self.db.aggregate_repo_task(repo_task_id)
        config_snapshot = json_loads(task.get("config_snapshot_json") or "{}")
        model = config_snapshot.get("opencode_model") or ""

        coverage_stats = self._metric_stats(
            row.get("coverage_line") if row.get("coverage_line") is not None else row.get("coverage")
            for row in class_rows
        )
        mutation_stats = self._metric_stats(row.get("mutation_score") for row in class_rows)
        total_mutants = sum(int(row.get("total_mutants") or 0) for row in class_rows)
        surviving_mutants = sum(int(row.get("surviving_mutants") or 0) for row in class_rows)
        mutation_total_pct = None
        if total_mutants > 0:
            mutation_total_pct = ((total_mutants - surviving_mutants) / total_mutants) * 100.0
        mutation_stats["total"] = mutation_total_pct

        coverage_total = coverage_stats.get("avg")
        coverage_recalc: Dict[str, Any] = {"ran": False}
        if recalc_project_coverage:
            module_batches: Dict[str, Dict[str, List[str]]] = {}
            for row in class_rows:
                test_file_path = row.get("test_file_path")
                if not test_file_path:
                    continue
                module = str(row.get("module") or "")
                bucket = module_batches.setdefault(module, {"tests": [], "classes": []})
                test_selector = Path(str(test_file_path)).stem
                if test_selector and test_selector not in bucket["tests"]:
                    bucket["tests"].append(test_selector)
                class_fqn = str(row.get("class_fqn") or "")
                if class_fqn and class_fqn not in bucket["classes"]:
                    bucket["classes"].append(class_fqn)
            overall_covered = 0
            overall_missed = 0
            overall_matched_classes = 0
            module_results: List[Dict[str, Any]] = []
            for module, payload in module_batches.items():
                tests = payload["tests"]
                target_classes = payload["classes"]
                if not tests or not target_classes:
                    continue
                ok, output = run_tests_with_jacoco_batch(str(task["repo_path"]), tests, module or None)
                jacoco_path = find_jacoco_report(str(task["repo_path"]), module or None)
                result = {
                    "module": module,
                    "test_count": len(tests),
                    "ok": ok,
                    "output": output[-500:] if isinstance(output, str) else "",
                    "jacoco_path": jacoco_path,
                }
                if jacoco_path:
                    module_cov = parse_jacoco_line_coverage_for_classes(jacoco_path, target_classes)
                    result.update(module_cov)
                    overall_covered += int(module_cov.get("covered_lines", 0) or 0)
                    overall_missed += int(module_cov.get("missed_lines", 0) or 0)
                    overall_matched_classes += int(module_cov.get("matched_classes", 0) or 0)
                module_results.append(result)
            if overall_covered or overall_missed:
                coverage_total = (overall_covered / (overall_covered + overall_missed) * 100.0) if (overall_covered + overall_missed) > 0 else 100.0
            coverage_recalc = {
                "ran": bool(module_results),
                "modules": module_results,
                "covered_lines": overall_covered if overall_matched_classes > 0 else None,
                "missed_lines": overall_missed if overall_matched_classes > 0 else None,
                "matched_classes": overall_matched_classes,
            }
        coverage_stats["total"] = coverage_total

        started_at = task.get("started_at")
        finished_at = task.get("finished_at")
        elapsed_seconds = self._safe_float(task.get("actual_elapsed_seconds"))
        if elapsed_seconds is None:
            elapsed_seconds = self._safe_float(task.get("elapsed_seconds"))
        if elapsed_seconds is None and started_at and finished_at:
            try:
                start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
                elapsed_seconds = max((end - start).total_seconds(), 0.0)
            except Exception:
                elapsed_seconds = None

        db_totals = {
            "input": int(aggregate.get("input_tokens") or aggregate.get("actual_input_tokens") or 0),
            "output": int(aggregate.get("output_tokens") or aggregate.get("actual_output_tokens") or 0),
            "cache_read": int(aggregate.get("cache_read_tokens") or aggregate.get("actual_cache_read_tokens") or 0),
            "cache_write": int(aggregate.get("cache_write_tokens") or aggregate.get("actual_cache_write_tokens") or 0),
            "reasoning": int(aggregate.get("reasoning_tokens") or 0),
        }
        db_totals["total"] = sum(db_totals.values())

        verified_totals = {key: 0 for key in (*TOKEN_TOTAL_KEYS, "total")}
        verification_rows: List[Dict[str, Any]] = []
        session_verified_classes = 0
        recovered_verified_classes = 0
        mismatched_classes = 0
        missing_verified_classes = 0

        for row in class_rows:
            class_db_totals = self._token_totals_from_row(row)
            class_db_total = sum(class_db_totals.values())
            db_compare_totals = {**class_db_totals, "total": class_db_total}
            session_ids = self._session_ids_from_row(row)
            session_totals = self._repo_token_totals_from_sessions(session_ids, opencode_db_path=opencode_db_path)
            source = "task_db"
            chosen = dict(class_db_totals)
            chosen["total"] = class_db_total
            if session_totals.get("total"):
                source = "opencode_sessions"
                chosen = session_totals
                session_verified_classes += 1
            else:
                recovered = self._recover_class_tokens_from_opencode_db(
                    repo_path=str(task["repo_path"]),
                    class_fqn=str(row["class_fqn"]),
                    started_at=row.get("started_at"),
                    finished_at=row.get("finished_at") or row.get("updated_at"),
                    opencode_db_path=opencode_db_path,
                )
                if recovered and int((recovered.get("total_tokens") or {}).get("total", 0) or 0) > 0:
                    source = "opencode_recovery"
                    chosen = dict(recovered["total_tokens"])
                    recovered_verified_classes += 1
                elif not class_db_total:
                    source = "missing"
                    missing_verified_classes += 1
            for key in verified_totals:
                verified_totals[key] += int(chosen.get(key, 0) or 0)
            if any(int(db_compare_totals.get(key, 0) or 0) != int(chosen.get(key, 0) or 0) for key in (*TOKEN_TOTAL_KEYS, "total")):
                mismatched_classes += 1
            verification_rows.append(
                {
                    "class_fqn": row["class_fqn"],
                    "status": row["status"],
                    "source": source,
                    "db_total_tokens": class_db_total,
                    "verified_total_tokens": int(chosen.get("total", 0) or 0),
                    "session_ids": session_ids,
                }
            )

        db_actual_cost = float(aggregate.get("provider_cost_usd") or 0.0)
        if db_actual_cost <= 0.0:
            db_actual_cost = float(aggregate.get("actual_cost") or 0.0)
        verified_cost = _estimate_cost_from_tokens(
            model=model,
            input_tokens=int(verified_totals["input"]),
            output_tokens=int(verified_totals["output"]),
            cache_read_tokens=int(verified_totals["cache_read"]),
            reasoning_tokens=int(verified_totals["reasoning"]),
        )

        generated_classes = sum(1 for row in class_rows if row.get("test_file_path") or row.get("test_count") or row["status"] in TERMINAL_CLASS_STATUSES)
        summary = {
            "task": task,
            "aggregate": aggregate,
            "classes": {
                "total": len(class_rows),
                "generated": generated_classes,
                "completed": int(aggregate.get("completed_classes") or 0),
                "passed": int(aggregate.get("passed_classes") or 0),
                "failed": int(aggregate.get("failed_classes") or 0),
                "statuses": aggregate.get("statuses") or {},
            },
            "coverage": coverage_stats,
            "mutation": mutation_stats,
            "project_coverage_recalc": coverage_recalc,
            "project_mutation_totals": {
                "total_mutants": total_mutants,
                "surviving_mutants": surviving_mutants,
            },
            "timing": {
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_seconds": elapsed_seconds,
            },
            "tokens": {
                "task_db": {
                    **db_totals,
                    "cost_usd": db_actual_cost,
                },
                "verified": {
                    **verified_totals,
                    "cost_usd": verified_cost,
                },
                "comparison": {
                    "delta": {key: int(verified_totals.get(key, 0) or 0) - int(db_totals.get(key, 0) or 0) for key in (*TOKEN_TOTAL_KEYS, "total")},
                    "classes_from_sessions": session_verified_classes,
                    "classes_from_recovery": recovered_verified_classes,
                    "classes_missing": missing_verified_classes,
                    "classes_mismatched": mismatched_classes,
                    "opencode_db_path": str(Path(opencode_db_path).expanduser().resolve()) if opencode_db_path else str(DEFAULT_OPENCODE_DB),
                },
                "per_class_verification": verification_rows,
            },
        }
        return summary

    def write_live_status(self, repo_task_id: int) -> Dict[str, str]:
        from uta.tasks.render import write_live_status

        task = self.get_task(repo_task_id)
        return write_live_status(self.db, repo_task_id, repo_path=task["repo_path"])

    @staticmethod
    def _iso_to_epoch_ms(value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        try:
            normalized = str(value).strip().replace("Z", "+00:00")
            return int(datetime.fromisoformat(normalized).timestamp() * 1000)
        except Exception:
            return None

    @staticmethod
    def _message_token_totals(raw_data: str) -> tuple[bool, Dict[str, int]]:
        try:
            data = json.loads(raw_data or "{}")
        except Exception:
            data = {}
        info = data.get("info") or {}
        role = info.get("role") or data.get("role")
        if role != "assistant":
            return False, {key: 0 for key in (*TOKEN_TOTAL_KEYS, "total")}
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        if not tokens:
            tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
        cache = tokens.get("cache") or {}
        totals = {
            "input": int(tokens.get("input", 0) or 0),
            "output": int(tokens.get("output", 0) or 0),
            "reasoning": int(tokens.get("reasoning", 0) or 0),
            "cache_read": int(cache.get("read", tokens.get("cache_read", 0) or 0) or 0),
            "cache_write": int(cache.get("write", tokens.get("cache_write", 0) or 0) or 0),
        }
        totals["total"] = int(
            tokens.get("total", 0)
            or (
                totals["input"]
                + totals["output"]
                + totals["reasoning"]
            )
        )
        return True, totals

    @classmethod
    def _recover_class_tokens_from_opencode_db(
        cls,
        *,
        repo_path: str,
        class_fqn: str,
        started_at: Optional[str],
        finished_at: Optional[str],
        opencode_db_path: Optional[Union[os.PathLike[str], str]] = None,
    ) -> Optional[Dict[str, Any]]:
        db_path = Path(opencode_db_path).expanduser().resolve() if opencode_db_path else DEFAULT_OPENCODE_DB
        if not db_path.exists():
            return None
        simple_name = class_fqn.split(".")[-1]
        start_ms = cls._iso_to_epoch_ms(started_at)
        finish_ms = cls._iso_to_epoch_ms(finished_at) or start_ms
        if finish_ms is None:
            return None
        if start_ms is None:
            start_ms = finish_ms - (30 * 60 * 1000)
        window_start = start_ms - (15 * 60 * 1000)
        window_end = finish_ms + (15 * 60 * 1000)
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = list(
                conn.execute(
                    """
                    SELECT id, title, time_created
                    FROM session
                    WHERE directory = ?
                      AND title LIKE ?
                      AND time_created BETWEEN ? AND ?
                    ORDER BY time_created ASC
                    """,
                    (repo_path, f"%{simple_name}%", int(window_start), int(window_end)),
                )
            )
            if not rows:
                rows = list(
                    conn.execute(
                        """
                        SELECT id, title, time_created
                        FROM session
                        WHERE directory = ?
                          AND title LIKE ?
                        ORDER BY ABS(time_created - ?) ASC
                        LIMIT 12
                        """,
                        (repo_path, f"%{simple_name}%", int(finish_ms)),
                    )
                )
            selected_ids: List[str] = []
            totals = {key: 0 for key in (*TOKEN_TOTAL_KEYS, "total")}
            assistant_messages = 0
            for row in rows:
                session_id = str(row["id"])
                session_totals = {key: 0 for key in (*TOKEN_TOTAL_KEYS, "total")}
                session_assistant = 0
                for msg in conn.execute(
                    "SELECT data FROM message WHERE session_id = ? ORDER BY time_created ASC",
                    (session_id,),
                ):
                    is_assistant, token_totals = cls._message_token_totals(msg["data"])
                    if not is_assistant:
                        continue
                    session_assistant += 1
                    for key, value in token_totals.items():
                        session_totals[key] += int(value or 0)
                if not session_assistant:
                    continue
                selected_ids.append(session_id)
                assistant_messages += session_assistant
                for key in totals:
                    totals[key] += session_totals[key]
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not selected_ids:
            return None
        return {
            "session_ids": selected_ids,
            "assistant_messages": assistant_messages,
            "total_tokens": totals,
        }

    def recover_missing_class_tokens_from_opencode_db(
        self,
        repo_task_id: int,
        *,
        class_fqns: Optional[Iterable[str]] = None,
        opencode_db_path: Optional[Union[os.PathLike[str], str]] = None,
    ) -> int:
        task = self.db.get_repo_task(repo_task_id)
        if not task:
            raise KeyError(f"Task {repo_task_id} not found")
        config_snapshot = json_loads(task["config_snapshot_json"]) if task else {}
        model = config_snapshot.get("opencode_model") or ""
        target_fqns = set(class_fqns or [])
        updated = 0
        for row in self.db.list_class_tasks(repo_task_id):
            class_fqn = str(row["class_fqn"])
            if target_fqns and class_fqn not in target_fqns:
                continue
            if row["status"] not in TERMINAL_CLASS_STATUSES:
                continue
            if any(self._token_totals_from_row(row).values()):
                continue
            recovered = self._recover_class_tokens_from_opencode_db(
                repo_path=str(task["repo_path"]),
                class_fqn=class_fqn,
                started_at=row["started_at"],
                finished_at=row["finished_at"] or row["updated_at"],
                opencode_db_path=opencode_db_path,
            )
            if not recovered:
                continue
            totals = recovered["total_tokens"]
            merged_session_ids = self._merge_session_ids(
                self._session_ids_from_row(row),
                recovered.get("session_ids") or [],
            )
            existing_phase_usage = json_loads(row["phase_token_usage_json"]) if row["phase_token_usage_json"] else {}
            existing_phase_usage["opencode_db_recovery"] = {key: int(totals.get(key, 0) or 0) for key in (*TOKEN_TOTAL_KEYS, "total")}
            class_cost = _estimate_cost_from_tokens(
                model=model,
                input_tokens=int(totals["input"]),
                output_tokens=int(totals["output"]),
                cache_read_tokens=int(totals["cache_read"]),
                reasoning_tokens=int(totals["reasoning"]),
            )
            self.db.update_class_task(
                row["id"],
                session_ids_json=json_dumps(merged_session_ids),
                phase_token_usage_json=json_dumps(existing_phase_usage),
                input_tokens=int(totals["input"]),
                output_tokens=int(totals["output"]),
                cache_read_tokens=int(totals["cache_read"]),
                cache_write_tokens=int(totals["cache_write"]),
                reasoning_tokens=int(totals["reasoning"]),
                total_tokens=int(totals["total"]),
                actual_input_tokens=int(totals["input"]),
                actual_output_tokens=int(totals["output"]),
                actual_cache_read_tokens=int(totals["cache_read"]),
                actual_cache_write_tokens=int(totals["cache_write"]),
                actual_cost=class_cost,
            )
            updated += 1
        if updated:
            aggregate = self.db.aggregate_repo_task(repo_task_id)
            existing_provider_cost = float(task["provider_cost_usd"] or 0.0)
            aggregate_provider_cost = float(aggregate.get("provider_cost_usd") or 0.0)
            self.db.update_repo_task(
                repo_task_id,
                actual_input_tokens=int(aggregate.get("actual_input_tokens") or aggregate.get("input_tokens") or 0),
                actual_output_tokens=int(aggregate.get("actual_output_tokens") or aggregate.get("output_tokens") or 0),
                actual_cache_read_tokens=int(aggregate.get("actual_cache_read_tokens") or aggregate.get("cache_read_tokens") or 0),
                actual_cache_write_tokens=int(aggregate.get("actual_cache_write_tokens") or aggregate.get("cache_write_tokens") or 0),
                input_tokens=int(aggregate.get("input_tokens") or aggregate.get("actual_input_tokens") or 0),
                output_tokens=int(aggregate.get("output_tokens") or aggregate.get("actual_output_tokens") or 0),
                cache_read_tokens=int(aggregate.get("cache_read_tokens") or aggregate.get("actual_cache_read_tokens") or 0),
                cache_write_tokens=int(aggregate.get("cache_write_tokens") or aggregate.get("actual_cache_write_tokens") or 0),
                reasoning_tokens=int(aggregate.get("reasoning_tokens") or 0),
                total_tokens=int(aggregate.get("total_tokens") or 0),
                actual_cost=float(aggregate.get("actual_cost") or 0.0),
                provider_cost_usd=aggregate_provider_cost if aggregate_provider_cost > 0.0 else existing_provider_cost,
            )
        return updated

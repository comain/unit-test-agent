from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from uta.tasks.domain_support import (
    repo_slug as _repo_slug,
)
from uta.tasks.models import (
    DEFAULT_PRIORITY,
)
from uta.shared.targets import (
    TargetRef,
    coerce_targets,
    legacy_class_fqn_for_storage,
)


def _selected_model_event_payload(config_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """What model a task will run on, recorded when the task is created.

    Without this the model appears in the event log only when a provider
    fallback fires -- so a run that never falls back has no record of which
    model produced it, and an operator reading the events cannot tell a
    claude-opus-5 run from a gpt-5.5 one. Restored from `main`, which this
    branch dropped when task creation moved out of `uta/tasks/manager.py`.
    """
    return {
        "provider": config_snapshot.get("opencode_selected_provider"),
        "model": config_snapshot.get("opencode_selected_model"),
        "candidate_index": config_snapshot.get("opencode_candidate_index"),
        "provider_tokens": config_snapshot.get("opencode_provider_tokens") or {},
        "model_probe": config_snapshot.get("opencode_model_probe") or {},
    }


class TaskCreationMixin:
    def _record_model_selection(self, task_id: int, snapshot: Dict[str, Any]) -> None:
        # Discovery resolves at invocation, not creation. Never announce a blank
        # selection or pin a model merely to populate the progress page.
        model = snapshot.get("opencode_selected_model")
        self.db.add_event(
            task_id, None,
            "opencode_model_selected" if model else "model_selection_deferred",
            f"Selected OpenCode model {model}" if model else "Model selection deferred until execution",
            stage="routing",
            payload=_selected_model_event_payload(snapshot) if model else {},
        )

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
        rdc_context: Optional[Dict[str, Any]] = None,
        rdc_context_path: Optional[str] = None,
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
            estimate_snapshot = self.estimates.from_history(
                repo_path=resolved_repo,
                module=module,
                language="java",
                target_count=max(1, len(classes) or 1),
            )
        if estimate_snapshot is None:
            estimate_snapshot = self.estimates.unavailable(
                target_count=max(1, len(classes) or 1),
                language="java",
            )
        config_snapshot = dict(config_snapshot or {})
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
            "rdc_context": rdc_context,
            "rdc_context_path": rdc_context_path,
            "total_classes": len(classes),
            **(estimate_snapshot or {}),
        }
        if hard_cap_usd is not None:
            row_fields["hard_cap_usd"] = hard_cap_usd
        task_id = self.db.create_repo_task(row_fields)
        for class_fqn in classes:
            self.db.create_class_task(
                task_id, class_fqn, module=module, priority=priority
            )
        self.db.add_event(
            task_id,
            None,
            "task_created",
            f"Created repo task on branch {branch['branch_name']}",
            payload={"selection": selection, "branch_name": branch["branch_name"]},
        )
        self._record_model_selection(task_id, config_snapshot)
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
        rdc_context: Optional[Dict[str, Any]] = None,
        rdc_context_path: Optional[str] = None,
        quality_mode: str = "target_batch",
        quality_gate_backend: str = "builtin",
        quality_gate_command: Optional[str] = None,
        language: Optional[str] = None,
    ) -> int:
        resolved_repo = str(Path(repo_path).expanduser().resolve())
        slug = _repo_slug(resolved_repo)
        target_refs = coerce_targets(targets)
        languages = {target.language for target in target_refs}
        language = language or (
            next(iter(languages))
            if len(languages) == 1
            else ("mixed" if languages else "java")
        )
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
        java_classes = [
            target.target_id for target in target_refs if target.language == "java"
        ]
        if java_classes and len(java_classes) == len(target_refs):
            selection["class_fqns"] = java_classes
        if quality_gate_command:
            selection["quality_gate_command"] = quality_gate_command
        target_total = max(1, len(target_refs) or 1)
        if estimate_snapshot is None:
            estimate_snapshot = self.estimates.from_history(
                repo_path=resolved_repo,
                module=module,
                language=language,
                target_count=target_total,
            )
        if estimate_snapshot is None:
            estimate_snapshot = self.estimates.unavailable(
                target_count=target_total, language=language
            )
        config_snapshot = dict(config_snapshot or {})
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
            "rdc_context": rdc_context,
            "rdc_context_path": rdc_context_path,
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
        self._record_model_selection(task_id, config_snapshot)
        return task_id

    def get_task(self, task_id: int) -> Dict[str, Any]:
        row = self.db.get_repo_task(task_id)
        if not row:
            raise KeyError(f"Task {task_id} not found")
        return dict(row)

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        repo_path: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        normalized_status = status.upper() if status else None
        return [
            dict(row)
            for row in self.db.list_repo_tasks(
                status=normalized_status, repo_path=repo_path, limit=limit
            )
        ]

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
            self.db.create_class_task(
                repo_task_id, class_fqn, module=module, priority=priority
            )
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

    def find_target_task(
        self, repo_task_id: int, target: TargetRef
    ) -> Optional[Dict[str, Any]]:
        target_ref = coerce_targets([target])[0]
        for row in self.db.list_class_tasks(repo_task_id):
            row_target_id = (
                row["target_id"] if "target_id" in row.keys() else row["class_fqn"]
            )
            row_language = row["language"] if "language" in row.keys() else "java"
            if (
                row_language == target_ref.language
                and row_target_id == target_ref.target_id
            ):
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

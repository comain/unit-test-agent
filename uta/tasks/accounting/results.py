from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

from uta.tasks.domain_support import (
    mutation_display_detail as _mutation_display_detail,
    normalize_class_status as _normalize_class_status,
)
from uta.tasks.models import (
    TERMINAL_CLASS_STATUSES,
    json_dumps,
    json_loads,
    now_iso,
)
from uta.shared.targets import (
    TargetIdentity,
    TargetRef,
    result_key,
)


#: The task error is one column read by humans scanning a list of runs, so the
#: aggregate names the first few failing classes rather than all of them.
_SUMMARY_ERROR_MAX_CLASSES = 4

#: Enough to carry a gate verdict ("Mutation gate failed: 93% < 100%") without
#: one class's stack trace crowding out the others.
_SUMMARY_ERROR_MAX_CHARS_PER_CLASS = 160


def _all_failures_equivalence_reviewed(class_rows: Iterable[Mapping[str, Any]]) -> bool:
    """Whether only mutation failures remain and each has an all-equivalent review."""
    failures = [row for row in class_rows if str(row["status"]).upper() != "PASS"]
    if not failures:
        return False
    for row in failures:
        if str(row["status"]).upper() != "MUTATION_FAIL":
            return False
        review = json_loads(row["equivalence_review_json"])
        if review.get("outcome") != "all_equivalent":
            return False
    return True


class TaskResultSyncMixin:
    """Result synchronization and batch aggregation accounting."""

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
            merged_session_ids = self._merge_session_ids(
                existing_session_ids, session_ids
            )
            has_new_sessions = any(
                session_id not in existing_session_ids for session_id in session_ids
            )
            test_file_content = result.get("test_file_content") or ""
            test_count = result.get("test_count")
            if test_count is None and isinstance(test_file_content, str):
                test_count = test_file_content.count("@Test")
                if result.get("language") == "python":
                    test_count = len(
                        re.findall(
                            r"^\s*def\s+test_", test_file_content, flags=re.MULTILINE
                        )
                    )
            test_file_lines = (
                len(test_file_content.splitlines())
                if isinstance(test_file_content, str) and test_file_content
                else None
            )
            class_phase_usage = (
                result.get("phase_token_usage") or result.get("token_usage") or {}
            )
            if not isinstance(class_phase_usage, dict):
                class_phase_usage = {}
            existing_phase_usage = (
                json_loads(row["phase_token_usage_json"])
                if row["phase_token_usage_json"]
                else {}
            )
            existing_totals = self._token_totals_from_row(row)
            incoming_totals = self._token_totals({}, class_phase_usage)
            if class_phase_usage and has_new_sessions and any(existing_totals.values()):
                stored_phase_usage = self._merge_phase_token_usage(
                    existing_phase_usage, class_phase_usage
                )
                class_totals = self._add_token_totals(existing_totals, incoming_totals)
            elif class_phase_usage:
                stored_phase_usage = class_phase_usage
                class_totals = incoming_totals
            else:
                stored_phase_usage = existing_phase_usage
                class_totals = existing_totals
            self.db.update_class_task(
                row["id"],
                status=status,
                current_stage="finished",
                stage="finished",
                current_detail=status,
                coverage_line=result.get("coverage", result.get("line_coverage")),
                coverage=result.get("coverage", result.get("line_coverage")),
                mutation_score=result.get("mutation_score"),
                mutation_detail=_mutation_display_detail(result),
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
                finished_at=now_iso(),
                last_error=result.get("error"),
                error=result.get("error"),
                equivalence_review_json=(
                    json_dumps(result["equivalence_review"])
                    if isinstance(result.get("equivalence_review"), dict)
                    else None
                ),
                elapsed_seconds=result.get("elapsed_seconds"),
                actual_elapsed_seconds=result.get("elapsed_seconds"),
            )
            self.db.add_event(
                repo_task_id,
                row["id"],
                "stage_completed",
                status,
                stage="finished",
                payload={
                    **self._target_event_payload(repo_task_id, [class_fqn]),
                    "result_status": status,
                    "testQuality": result.get("testQuality")
                    or result.get("test_quality")
                    or {},
                },
            )

        observed_totals = self._token_totals(
            session_token_usage or {}, phase_token_usage or {}
        )
        result_rows = {
            class_fqn: self.db.find_class_task(repo_task_id, class_fqn)
            for class_fqn in results.keys()
        }
        missing_token_fqns = [
            class_fqn
            for class_fqn, row in result_rows.items()
            if row and not any(self._token_totals_from_row(row).values())
        ]
        if any(observed_totals.values()) and missing_token_fqns:
            split_totals = self._split_token_totals(
                observed_totals, len(missing_token_fqns)
            )
            for class_fqn, split in zip(missing_token_fqns, split_totals):
                row = self.db.find_class_task(repo_task_id, class_fqn)
                if not row:
                    continue
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
                )
        class_rows = self.db.list_class_tasks(repo_task_id)
        repo_session_ids = self._merge_session_ids(
            self._session_ids_from_json(
                task["session_ids_json"]
                if task and "session_ids_json" in task.keys()
                else "[]"
            ),
            *(self._session_ids_from_row(row) for row in class_rows),
        )
        aggregate = self.db.aggregate_repo_task(repo_task_id)
        class_totals = {
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
        if any(observed_totals.values()):
            repo_totals = {
                key: max(class_totals[key], observed_totals[key])
                for key in class_totals
            }
        else:
            repo_totals = class_totals

        class_statuses = {row["status"] for row in class_rows}
        unresolved_statuses = {
            "CREATED",
            "PENDING",
            "QUEUED",
            "RUNNING",
            "STOP_REQUESTED",
            "STOPPED",
        }
        all_resolved = bool(class_rows) and not any(
            status in unresolved_statuses for status in class_statuses
        )
        has_failed = any(
            status in TERMINAL_CLASS_STATUSES and status != "PASS"
            for status in class_statuses
        )
        all_passed = bool(class_rows) and all(
            row["status"] == "PASS" for row in class_rows
        )
        equivalence_reviews_ready = _all_failures_equivalence_reviewed(class_rows)
        current_status = task["status"] if task else "RUNNING"
        if current_status in {"POISONED", "BUDGET_EXCEEDED"}:
            repo_status = current_status
            detail = (task["current_detail"] if task and task["current_detail"] else None) or final_error or current_status
        elif final_error:
            repo_status = "FAILED"
            detail = final_error
        elif all_passed:
            repo_status = "COMPLETED"
            detail = "All classes passed"
        elif equivalence_reviews_ready and all_resolved:
            # A review never passes the mutation gate. It only lets the repair
            # task finish so the CI service can run a fresh gate and compare
            # its survivors against the saved, source-bound review evidence.
            repo_status = "COMPLETED"
            detail = "Equivalent-mutant reviews ready for fresh gate rerun"
        elif has_failed and all_resolved:
            repo_status = "FAILED"
            detail = "One or more classes failed verification"
        elif all_resolved:
            repo_status = "FAILED"
            detail = "All classes resolved with terminal failures"
        else:
            repo_status = current_status
            detail = task["current_detail"] if task else "Class progress updated"

        is_terminal_repo_status = repo_status in {
            "COMPLETED",
            "FAILED",
            "POISONED",
            "BUDGET_EXCEEDED",
        }
        task_error = final_error
        if task_error is None and is_terminal_repo_status:
            task_error = self._summary_error_message(class_rows)

        repo_finished_at = now_iso() if is_terminal_repo_status else None
        self.db.update_repo_task(
            repo_task_id,
            status=repo_status,
            current_stage="finished" if is_terminal_repo_status else "running",
            current_detail=detail,
            session_ids_json=json_dumps(repo_session_ids),
            input_tokens=repo_totals["input"],
            output_tokens=repo_totals["output"],
            cache_read_tokens=repo_totals["cache_read"],
            cache_write_tokens=repo_totals["cache_write"],
            reasoning_tokens=repo_totals["reasoning"],
            total_tokens=sum(repo_totals.values()),
            actual_input_tokens=class_totals["input"],
            actual_output_tokens=class_totals["output"],
            actual_cache_read_tokens=class_totals["cache_read"],
            actual_cache_write_tokens=class_totals["cache_write"],
            report_path=report_path,
            run_log_path=run_log_path,
            elapsed_seconds=elapsed_seconds,
            actual_elapsed_seconds=elapsed_seconds,
            finished_at=repo_finished_at,
            last_error=task_error,
            error=task_error,
        )
        self._refresh_repo_counts(repo_task_id)
        if is_terminal_repo_status:
            summary_type = (
                "task_completed" if repo_status == "COMPLETED" else "task_failed"
            )
            self.db.add_event(
                repo_task_id,
                None,
                summary_type,
                detail,
                stage="finished",
                severity="INFO" if repo_status == "COMPLETED" else "ERROR",
                payload={"class_statuses": list(class_statuses)},
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
            key
            for key in (results or {}).keys()
            if isinstance(key, TargetIdentity) or isinstance(key, Mapping)
        )
        if target_inputs:
            self.ensure_target_tasks(repo_task_id, target_inputs, module=module)
        normalized_results = {
            result_key(key): value for key, value in (results or {}).items()
        }
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

    @staticmethod
    def _row_field(row: Any, name: str) -> Optional[str]:
        if hasattr(row, "get"):
            value = row.get(name)
        else:
            value = row[name] if name in row.keys() else None
        return str(value) if value else None

    @classmethod
    def _summary_error_message(cls, class_rows: Iterable[Any]) -> Optional[str]:
        """Summarise every failing class, not just the first one carrying an error.

        Returning the first error made a task read as whatever its first failure
        happened to be. One production run reported only a PIT tooling fault
        while three other classes had missed the mutation and coverage gates
        outright, so the real work was invisible from the task record and the
        run looked like pure infrastructure breakage.
        """
        failures: list[tuple[str, str]] = []
        for row in class_rows or []:
            message = cls._row_field(row, "error") or cls._row_field(row, "last_error")
            if not message:
                continue
            name = cls._row_field(row, "display_name") or cls._row_field(row, "class_fqn") or "?"
            failures.append((name.rsplit(".", 1)[-1], " ".join(message.split())))
        if not failures:
            return None
        if len(failures) == 1:
            return failures[0][1]
        shown = failures[:_SUMMARY_ERROR_MAX_CLASSES]
        parts = "; ".join(
            f"{name}: {message[:_SUMMARY_ERROR_MAX_CHARS_PER_CLASS]}" for name, message in shown
        )
        if len(failures) > len(shown):
            parts += f"; +{len(failures) - len(shown)} more"
        return f"{len(failures)} classes failed: {parts}"

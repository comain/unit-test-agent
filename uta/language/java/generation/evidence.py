"""Projection of a delegated gate result into per-class Java results.

This is the only place that reads normalized enforcement evidence (coverage,
mutation, PIT) and shapes it into the per-class result records the rest of the
product stores and reports. Keeping it apart from ``quality`` means the
evidence contract can be read without reading Maven command construction.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from uta.language.java.ci_evidence import java_gate_failure_summary as _java_gate_failure_summary
from uta.language.java.generation.quality import _delegated_quality_gate_feedback
from uta.language.java.test_quality import attach_java_test_quality as _attach_java_test_quality
from uta.language.java.workspace import (
    discover_ci_incremental_java_test_files as _discover_ci_incremental_java_test_files,
)
from uta.language.java.workspace import expected_test_file_rel as _expected_test_file_rel

logger = logging.getLogger("uta")


def _enforcement_evidence_detail(result: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from uta.enforcement.evidence import evidence_detail

        return evidence_detail(result)
    except Exception:
        logger.debug("Failed to normalize enforcement evidence", exc_info=True)
        return {"coverage": None, "mutation": None, "pitMutation": None}



def _delegated_gate_batch_results(
    *,
    state: Dict[str, Any],
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    gate_result: Dict[str, Any],
    gate_seconds: float,
    status: Optional[str] = None,
    session_id: Optional[str] = None,
    session_ids: Optional[List[str]] = None,
    precheck_existing_tests: bool = False,
) -> Dict[str, Dict[str, Any]]:
    evidence = _enforcement_evidence_detail(gate_result)
    coverage = evidence.get("coverage") if isinstance(evidence.get("coverage"), dict) else {}
    mutation = evidence.get("mutation") if isinstance(evidence.get("mutation"), dict) else {}
    if not mutation and isinstance(evidence.get("pitMutation"), dict):
        mutation = evidence.get("pitMutation") or {}
    gate_output = _delegated_quality_gate_feedback(gate_result)
    batch_results = state.get("results", {}).copy()
    resolved_status = status or ("PASS" if gate_result.get("passed") else "FAIL")
    gate_error = None if resolved_status == "PASS" else _java_gate_failure_summary(gate_result)
    for class_fqn in batch:
        discovered_tests = _discover_ci_incremental_java_test_files(state, repo_path, class_fqn, module)
        test_file_rel = discovered_tests[0] if discovered_tests else _expected_test_file_rel(module, class_fqn)
        test_file_abs = Path(repo_path) / test_file_rel
        try:
            test_file_content = test_file_abs.read_text(encoding="utf-8", errors="replace")
        except Exception:
            test_file_content = ""
        batch_results[class_fqn] = {
            "status": resolved_status,
            "coverage": coverage.get("rate") if coverage else None,
            "tests_pass": bool(gate_result.get("passed")),
            "mutation_score": mutation.get("rate") if mutation else None,
            "surviving_mutants": int(mutation.get("survived") or 0) if mutation else 0,
            "total_mutants": int(mutation.get("generated") or 0) if mutation else 0,
            "killed_mutants": int(mutation.get("killed") or 0) if mutation else 0,
            "output": gate_output[:2000],
            "error": gate_error,
            "test_file_path": test_file_rel,
            "test_file_content": test_file_content,
            "elapsed_seconds": gate_seconds,
            "generation_seconds": 0.0,
            "compile_seconds": 0.0,
            "test_seconds": 0.0,
            "mutation_seconds": gate_seconds,
            "session_id": session_id,
            "session_ids": list(session_ids or []),
            "delegated_quality_gate": gate_result,
            "precheck_existing_tests": precheck_existing_tests,
            "candidate_test_file_paths": discovered_tests,
        }
    return batch_results



def _sync_task_results_if_available(state: Dict[str, Any], results: Dict[str, Any], batch: List[str]) -> None:
    task_id = state.get("task_id")
    task_db_path = state.get("task_db_path")
    if not task_id or not task_db_path:
        return
    scoped_results = {fqn: results[fqn] for fqn in batch if fqn in results}
    if not scoped_results:
        return
    try:
        _attach_java_test_quality(str(state.get("repo_path") or ""), scoped_results)
    except Exception:
        logger.debug("Failed to attach test-quality evidence before sync", exc_info=True)
    try:
        from uta.tasks.manager import TaskManager

        TaskManager(task_db_path).sync_results(
            int(task_id),
            scoped_results,
            module=state.get("module"),
            phase_token_usage=state.get("phase_token_usage"),
            elapsed_seconds=None,
        )
    except Exception:
        logger.debug("Failed to sync delegated precheck results", exc_info=True)



__all__ = [
    "_delegated_gate_batch_results",
    "_enforcement_evidence_detail",
    "_sync_task_results_if_available",
]

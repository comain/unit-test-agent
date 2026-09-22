"""Existing-test precheck for the Java generation cycle.

The algorithm is isolated from the legacy composite, while its tool calls are
ports. That keeps compatibility monkeypatch seams working during rollout and
lets the durable backend supply the same Maven/PIT capabilities directly.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger("uta")


@dataclass(frozen=True)
class JavaPrecheckPorts:
    expected_test_file_rel: Callable[..., str]
    set_stage: Callable[..., Any]
    run_tests_with_jacoco_batch: Callable[..., Any]
    find_jacoco_report: Callable[..., Any]
    parse_surefire_results: Callable[..., Any]
    parse_jacoco_report: Callable[..., Mapping[str, Any]]
    run_pitest: Callable[..., Any]
    find_latest_pitest_report: Callable[..., Any]
    compute_mutation_stats: Callable[..., Mapping[str, Any]]
    merge_phase_timings: Callable[..., Dict[str, float]]
    run_delegated_quality_gate: Callable[..., Dict[str, Any]]
    delegated_gate_batch_results: Callable[..., Dict[str, Any]]
    sync_task_results: Callable[..., Any]
    delegated_gate_failure_stage: Callable[[Dict[str, Any]], str]


def precheck_existing_tests(
    *,
    state: Dict[str, Any],
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    coverage_gate: int,
    mutation_gate_score: int,
    node_started: float,
    ports: JavaPrecheckPorts,
) -> Optional[Dict[str, Any]]:
    """Return proceed, delegated repair, or a completed existing-test result."""
    if (
        state.get("quality_mode") == "ci_incremental"
        and state.get("quality_gate_backend") == "maven_enforcer"
    ):
        return _diff_enforcer_precheck(
            state=state,
            repo_path=repo_path,
            module=module,
            batch=batch,
            node_started=node_started,
            ports=ports,
        )
    return _class_level_precheck(
        state=state,
        repo_path=repo_path,
        module=module,
        batch=batch,
        coverage_gate=coverage_gate,
        mutation_gate_score=mutation_gate_score,
        node_started=node_started,
        ports=ports,
    )


def _class_level_precheck(
    *,
    state: Dict[str, Any],
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    coverage_gate: int,
    mutation_gate_score: int,
    node_started: float,
    ports: JavaPrecheckPorts,
) -> Optional[Dict[str, Any]]:
    test_files = {
        class_fqn: Path(repo_path) / ports.expected_test_file_rel(module, class_fqn)
        for class_fqn in batch
    }
    missing = [str(path) for path in test_files.values() if not path.exists()]
    if missing:
        logger.info("Existing-test precheck skipped; missing test file(s): %s", missing)
        return None

    logger.info("Prechecking existing tests before LLM work for batch=%d", len(batch))
    ports.set_stage(state, "precheck_existing_tests", f"batch={len(batch)}")
    precheck_started = time.perf_counter()
    test_names = [f"{class_fqn.split('.')[-1]}Test" for class_fqn in batch]
    test_ok, test_output = ports.run_tests_with_jacoco_batch(
        repo_path, test_names, module
    )
    test_seconds = time.perf_counter() - precheck_started
    if not test_ok:
        logger.info(
            "Existing-test precheck did not pass targeted tests; continuing with LLM workflow"
        )
        return None

    jacoco_path = ports.find_jacoco_report(repo_path, module)
    if not jacoco_path:
        logger.info(
            "Existing-test precheck did not find a JaCoCo XML report; continuing with LLM workflow"
        )
        return None

    surefire_results = ports.parse_surefire_results(repo_path, test_names, module)
    coverage_by_class: Dict[str, float] = {}
    output_by_class: Dict[str, str] = {}
    for class_fqn, test_name in zip(batch, test_names):
        class_test = surefire_results.get(test_name, {})
        if not bool(class_test.get("passed", True)):
            logger.info(
                "[%s] Existing-test precheck found failing Surefire result; continuing with LLM workflow",
                class_fqn,
            )
            return None
        output_by_class[class_fqn] = class_test.get("output") or test_output or ""
        line_cov = float(
            ports.parse_jacoco_report(jacoco_path, class_fqn).get("line", 0.0)
            or 0.0
        )
        coverage_by_class[class_fqn] = line_cov
        if line_cov < coverage_gate:
            logger.info(
                "[%s] Existing-test precheck coverage %.1f%% < gate %d%%; continuing with LLM workflow",
                class_fqn,
                line_cov,
                coverage_gate,
            )
            return None

    mutation_started = time.perf_counter()
    mutation_by_class: Dict[str, Dict[str, Any]] = {}
    if mutation_gate_score > 0:
        for class_fqn, test_name in zip(batch, test_names):
            test_class_fqn = f"{'.'.join(class_fqn.split('.')[:-1])}.{test_name}"
            pitest_ok, pitest_output = ports.run_pitest(
                repo_path, class_fqn, test_class_fqn, module
            )
            if not pitest_ok:
                logger.info(
                    "[%s] Existing-test precheck PIT failed; continuing with LLM workflow: %s",
                    class_fqn,
                    pitest_output[:200],
                )
                return None
            report_path = ports.find_latest_pitest_report(repo_path, module)
            if not report_path:
                logger.info(
                    "[%s] Existing-test precheck did not find a PIT report; continuing with LLM workflow",
                    class_fqn,
                )
                return None
            stats = dict(ports.compute_mutation_stats(report_path, class_fqn))
            score = float(stats.get("score", 0.0) or 0.0)
            if score < mutation_gate_score:
                logger.info(
                    "[%s] Existing-test precheck mutation %.1f%% < gate %d%%; continuing with LLM workflow",
                    class_fqn,
                    score,
                    mutation_gate_score,
                )
                return None
            mutation_by_class[class_fqn] = stats
    mutation_seconds = time.perf_counter() - mutation_started

    logger.info(
        "Existing-test precheck satisfied all gates for batch=%s; skipping LLM workflow",
        batch,
    )
    new_results = state["results"].copy()
    session_ids = list(state.get("session_ids", []) or [])
    per_class_test_seconds = test_seconds / max(len(batch), 1)
    per_class_mutation_seconds = mutation_seconds / max(len(batch), 1)
    for class_fqn in batch:
        stats = mutation_by_class.get(class_fqn, {})
        test_file_rel = ports.expected_test_file_rel(module, class_fqn)
        test_file_abs = Path(repo_path) / test_file_rel
        try:
            test_file_content = test_file_abs.read_text(
                encoding="utf-8", errors="replace"
            )
        except Exception:
            test_file_content = ""
        new_results[class_fqn] = {
            "status": "PASS",
            "coverage": coverage_by_class[class_fqn],
            "tests_pass": True,
            "mutation_score": float(stats.get("score", 0.0) or 0.0),
            "surviving_mutants": int(stats.get("survived", 0) or 0),
            "total_mutants": int(stats.get("total", 0) or 0),
            "killed_mutants": int(stats.get("killed", 0) or 0),
            "no_coverage_mutants": int(stats.get("no_coverage", 0) or 0),
            "timed_out_mutants": int(stats.get("timed_out", 0) or 0),
            "non_viable_mutants": int(stats.get("non_viable", 0) or 0),
            "memory_error_mutants": int(stats.get("memory_error", 0) or 0),
            "run_error_mutants": int(stats.get("run_error", 0) or 0),
            "mutation_status_counts": stats.get("status_counts", {}),
            "output": output_by_class.get(class_fqn, "")[:2000],
            "test_file_path": test_file_rel,
            "test_file_content": test_file_content,
            "elapsed_seconds": per_class_test_seconds + per_class_mutation_seconds,
            "generation_seconds": 0.0,
            "compile_seconds": 0.0,
            "test_seconds": per_class_test_seconds,
            "mutation_seconds": per_class_mutation_seconds,
            "session_id": session_ids[-1] if session_ids else None,
            "session_ids": session_ids,
            "precheck_existing_tests": True,
        }

    return {
        "results": new_results,
        "current_batch": [],
        "current_class": None,
        "current_stage": "precheck_existing_tests",
        "phase_timings": ports.merge_phase_timings(
            state,
            generate_validate_seconds=time.perf_counter() - node_started,
            test_execution_seconds=test_seconds,
            mutation_seconds=mutation_seconds,
        ),
    }


def _diff_enforcer_precheck(
    *,
    state: Dict[str, Any],
    repo_path: str,
    module: Optional[str],
    batch: List[str],
    node_started: float,
    ports: JavaPrecheckPorts,
) -> Optional[Dict[str, Any]]:
    logger.info(
        "Prechecking existing tests with Maven diff enforcer before LLM work for batch=%d",
        len(batch),
    )
    ports.set_stage(
        state, "precheck_existing_tests", f"maven_enforcer batch={len(batch)}"
    )
    started = time.perf_counter()
    gate_result = ports.run_delegated_quality_gate(state, repo_path)
    gate_seconds = time.perf_counter() - started
    if gate_result.get("passed"):
        logger.info("Maven diff enforcer precheck passed; skipping LLM workflow")
        results = ports.delegated_gate_batch_results(
            state=state,
            repo_path=repo_path,
            module=module,
            batch=batch,
            gate_result=gate_result,
            gate_seconds=gate_seconds,
            status="PASS",
            precheck_existing_tests=True,
        )
        ports.sync_task_results(state, results, batch)
        return {
            "results": results,
            "current_batch": [],
            "current_class": None,
            "current_stage": "precheck_existing_tests",
            "phase_timings": ports.merge_phase_timings(
                state,
                generate_validate_seconds=time.perf_counter() - node_started,
                mutation_seconds=gate_seconds,
            ),
        }
    logger.info(
        "Maven diff enforcer precheck failed; routing repair from %s",
        ports.delegated_gate_failure_stage(gate_result),
    )
    return {
        "_precheck_action": "delegated_repair",
        "delegated_quality_gate": gate_result,
        "delegated_quality_gate_seconds": gate_seconds,
    }


def phase_result(prechecked: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize the legacy-shaped result into the cycle's phase vocabulary."""
    if prechecked is None:
        return {"phase_outcome": "proceed"}
    projection = dict(prechecked)
    action = projection.pop("_precheck_action", None)
    if action == "delegated_repair":
        return {**projection, "phase_outcome": "delegated_repair"}
    return {**projection, "phase_outcome": "skip_target"}


__all__ = ["JavaPrecheckPorts", "phase_result", "precheck_existing_tests"]

"""Assemble the legacy-compatible Java target result from durable evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from uta.language.java.workspace import expected_test_file_rel
from uta.testgen.turn_accounting import aggregate_turn_accounting


def status_with_mutation_gate(
    test_ok: bool,
    line_cov: float,
    coverage_gate: int,
    mutation_gate_score: int,
    mutation_score: float,
) -> str:
    """Map deterministic verification evidence to the public Java status."""
    if not test_ok or line_cov < coverage_gate:
        return "FAIL"
    if mutation_gate_score > 0 and mutation_score < mutation_gate_score:
        return "MUTATION_FAIL"
    return "PASS"


def complete_generation(state: Mapping[str, Any], *, ports) -> Dict[str, Any]:
    batch = list(state.get("batch") or [])
    existing = dict(state.get("results") or {})
    precheck = _phase(state, "precheck_existing_tests")
    if precheck.get("phase_outcome") == "skip_target" and precheck.get("results"):
        return {
            "phase_outcome": "passed",
            "results": dict(precheck["results"]),
            "current_batch": [],
            "current_class": None,
            "current_stage": "precheck_existing_tests",
        }

    turns = list(state.get("turn_history") or [])
    accounting = aggregate_turn_accounting(turns)
    session_ids = list(accounting["session_ids"])
    session_refs = list(accounting["session_refs"])
    plan = _phase(state, "plan_tests")
    generated = _phase(state, "generate_tests")
    compiled = _phase(state, "verify_compile")
    tested = _phase(state, "verify_tests")
    coverage = _evidence(state, "measure_coverage")
    mutation = _evidence(state, "measure_mutation")
    delegated = _evidence(state, "delegated_quality_gate_verify")
    from uta.enforcement.equivalent_mutants import REVIEW_EVIDENCE_KEY
    from uta.testgen.equivalence_review import review_payload_for_results

    review = review_payload_for_results(state)

    terminal = _terminal_status(plan, generated, compiled, tested, coverage, mutation, delegated)
    if delegated.get("quality_gate_result"):
        gate = dict(delegated["quality_gate_result"])
        results = ports.delegated_gate_batch_results(
            state=dict(state),
            repo_path=str(state["repo_path"]),
            module=state.get("module"),
            batch=batch,
            gate_result=gate,
            gate_seconds=float(delegated.get("gate_seconds") or 0.0),
            status="PASS" if gate.get("passed") else "FAIL",
            session_id=session_ids[-1] if session_ids else None,
            session_ids=session_ids,
        )
        for result in results.values():
            if isinstance(result, dict):
                result["session_refs"] = session_refs
                result[REVIEW_EVIDENCE_KEY] = review
        return _completion_projection(state, results, turns, delegated=True)

    coverage_by_class = dict(coverage.get("coverage_by_class") or {})
    mutation_by_class = dict(mutation.get("mutation_stats_by_class") or {})
    tests_pass = bool((tested.get("evidence") or {}).get("tests_pass"))
    results = dict(existing)
    for class_fqn in batch:
        line_cov = float(coverage_by_class.get(class_fqn, 0.0) or 0.0)
        stats = dict(mutation_by_class.get(class_fqn) or {})
        mutation_score = float(stats.get("score", 0.0) or 0.0)
        status = terminal or ports.status_with_mutation_gate(
            tests_pass,
            line_cov,
            int(state.get("coverage_gate") or 0),
            int(state.get("mutation_gate") or 0),
            mutation_score,
        )
        test_file_rel = expected_test_file_rel(state.get("module"), class_fqn)
        test_file = Path(str(state["repo_path"])) / test_file_rel
        try:
            content = test_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = ""
        results[class_fqn] = {
            "status": status,
            "coverage": line_cov,
            "tests_pass": tests_pass,
            "mutation_score": mutation_score,
            "surviving_mutants": int(stats.get("survived", 0) or 0),
            "total_mutants": int(stats.get("total", 0) or 0),
            "killed_mutants": int(stats.get("killed", 0) or 0),
            "no_coverage_mutants": int(stats.get("no_coverage", 0) or 0),
            "timed_out_mutants": int(stats.get("timed_out", 0) or 0),
            "non_viable_mutants": int(stats.get("non_viable", 0) or 0),
            "memory_error_mutants": int(stats.get("memory_error", 0) or 0),
            "run_error_mutants": int(stats.get("run_error", 0) or 0),
            "mutation_status_counts": dict(stats.get("status_counts") or {}),
            "test_file_path": test_file_rel,
            "test_file_content": content,
            "elapsed_seconds": _elapsed(turns),
            "generation_seconds": _turn_elapsed(turns, "generate_tests"),
            "compile_seconds": float((compiled.get("evidence") or {}).get("elapsed_seconds") or 0.0),
            "test_seconds": float((tested.get("evidence") or {}).get("elapsed_seconds") or 0.0),
            "mutation_seconds": float(mutation.get("elapsed_seconds") or 0.0),
            "session_id": session_ids[-1] if session_ids else None,
            "session_ids": session_ids,
            "session_refs": session_refs,
            REVIEW_EVIDENCE_KEY: review,
        }
    return _completion_projection(state, results, turns, delegated=False)


def _completion_projection(state, results, turns, *, delegated):
    accounting = aggregate_turn_accounting(turns)
    return {
        "phase_outcome": "passed",
        "results": results,
        "current_batch": [],
        "current_class": None,
        "current_stage": "delegated_quality_gate" if delegated else "complete_generation",
        "session_ids": accounting["session_ids"],
        "session_refs": accounting["session_refs"],
        "session_token_usage": accounting["session_token_usage"],
        "phase_token_usage": accounting["phase_token_usage"],
        "session_retrospect": accounting["session_retrospect"],
        "session_diagnostics": accounting["session_diagnostics"],
        "session_patch_count": accounting["session_patch_count"],
        "phase_timings": {
            **dict(state.get("phase_timings") or {}),
            **accounting["agent_phase_timings"],
            "generate_validate_seconds": _elapsed(turns),
        },
    }


def _terminal_status(*results) -> str | None:
    for result in results:
        evidence = result.get("evidence") if isinstance(result, Mapping) else result
        if isinstance(evidence, Mapping) and evidence.get("terminal_status"):
            return str(evidence["terminal_status"])
    compile_result, test_result = results[2], results[3]
    if compile_result.get("phase_outcome") == "failed":
        return "COMPILE_FAIL"
    if test_result.get("phase_outcome") == "failed":
        return "TEST_FAIL"
    return None


def _phase(state: Mapping[str, Any], phase: str) -> Dict[str, Any]:
    return dict((state.get("phase_results") or {}).get(phase) or {})


def _evidence(state: Mapping[str, Any], phase: str) -> Dict[str, Any]:
    return dict(_phase(state, phase).get("evidence") or {})


def _elapsed(turns) -> float:
    return sum(float(turn.get("elapsed_seconds") or 0.0) for turn in turns)


def _turn_elapsed(turns, phase) -> float:
    return sum(
        float(turn.get("elapsed_seconds") or 0.0)
        for turn in turns
        if turn.get("phase") == phase
    )


__all__ = ["complete_generation", "status_with_mutation_gate"]

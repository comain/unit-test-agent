"""Java coverage measurement; the repair-progress policy lives above it."""

from __future__ import annotations

from uta.language.repair_attempts import advance_verification_attempts
from uta.testgen.repair_progress import SCORES_EVIDENCE_KEY

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping

from uta.testgen.project_summary_artifacts import ensure_stage_introspect_file
from uta.language.java.workspace import expected_test_file_rel
from uta.language.java.maven.invocation import targeted_maven_command_text
from uta.testgen.prompts.loader import render_prompt_split
from uta.shared.languages import PromptBundle
from uta.testgen.repair import RepairKind, repair_prompt_for
from uta.testgen.turn_accounting import last_session_locator, session_refs


@dataclass(frozen=True)
class JavaCoveragePorts:
    prompt_bundle: PromptBundle
    run_tests_with_jacoco_batch: Callable[..., tuple[bool, str]]
    find_jacoco_report: Callable[..., Any]
    parse_jacoco_report: Callable[..., Mapping[str, Any]]
    extract_uncovered_clusters: Callable[..., Mapping[str, Any]]
    format_uncovered_clusters: Callable[..., str]


def measure_coverage(
    state: Mapping[str, Any], *, ports: JavaCoveragePorts
) -> Dict[str, Any]:
    """Produce one JaCoCo snapshot for every class in the stable batch."""
    batch = list(state.get("batch") or [])
    if not batch:
        return {
            "phase_outcome": "failed",
            "evidence": {"failure_reason": "measure_coverage_requires_stable_batch"},
        }
    repo_path = str(state["repo_path"])
    module = state.get("module")
    gate = int(state.get("coverage_gate") or 0)
    test_names = [f"{fqn.split('.')[-1]}Test" for fqn in batch]
    started = time.perf_counter()
    tests_pass, output = ports.run_tests_with_jacoco_batch(
        repo_path, test_names, module
    )
    elapsed = time.perf_counter() - started
    evidence: Dict[str, Any] = {
        "tests_pass": bool(tests_pass),
        "output": str(output or "")[-6000:],
        "coverage_gate": gate,
        "test_names": test_names,
        "elapsed_seconds": elapsed,
    }
    if not tests_pass:
        evidence["failure_reason"] = "jacoco_test_execution_failed"
        return {"phase_outcome": "failed", "evidence": evidence}

    report = ports.find_jacoco_report(repo_path, module)
    if not report:
        evidence["failure_reason"] = "jacoco_report_missing"
        return {"phase_outcome": "failed", "evidence": evidence}

    coverage_by_class: Dict[str, float] = {}
    uncovered_by_class: Dict[str, str] = {}
    for class_fqn in batch:
        stats = ports.parse_jacoco_report(report, class_fqn)
        coverage_by_class[class_fqn] = float(stats.get("line", 0.0) or 0.0)
        uncovered = ports.extract_uncovered_clusters(report, class_fqn)
        uncovered_by_class[class_fqn] = ports.format_uncovered_clusters(uncovered)
    failing = [fqn for fqn in batch if coverage_by_class[fqn] < gate]
    evidence.update(
        {
            "report_path": str(report),
            "coverage_by_class": coverage_by_class,
            "uncovered_by_class": uncovered_by_class,
            "failing_classes": failing,
            # Whether another repair round is worth running is decided above
            # the languages, from these scores -- the below-gate classes only,
            # so a passing class drifting up cannot pass for progress on a
            # class that is stuck. See `testgen.repair_progress`.
            SCORES_EVIDENCE_KEY: {fqn: coverage_by_class[fqn] for fqn in failing},
        }
    )
    if not failing:
        return {"phase_outcome": "passed", "evidence": evidence}
    evidence["failure_reason"] = "coverage_below_gate"
    return {"phase_outcome": "repair", "evidence": evidence}


def render_fix_coverage_prompt(
    state: Mapping[str, Any], *, ports: JavaCoveragePorts
) -> str:
    """Render one coverage-hardening request; agent-core owns the turn."""
    evidence = _phase_evidence(state, "measure_coverage")
    failing = list(evidence.get("failing_classes") or state.get("batch") or [])
    if not failing:
        raise ValueError("fix_coverage requires at least one below-gate class")
    class_fqn = str(failing[0])
    repo_path = str(state["repo_path"])
    module = state.get("module")
    contexts = dict(state.get("target_context_paths") or {})
    paths = dict(contexts.get(class_fqn) or {})
    coverage_by_class = dict(evidence.get("coverage_by_class") or {})
    current = float(coverage_by_class.get(class_fqn, 0.0) or 0.0)
    gate = int(evidence.get("coverage_gate") or state.get("coverage_gate") or 0)
    test_name = f"{class_fqn.split('.')[-1]}Test"
    module_flag = f" -pl {module} -am" if module else ""
    stable, volatile = render_prompt_split(
        repair_prompt_for(ports.prompt_bundle, RepairKind.coverage),
        class_fqn=class_fqn,
        current_coverage=current,
        coverage_gate=gate,
        source_path=paths.get("source_abs", ""),
        test_file_path=expected_test_file_rel(module, class_fqn),
        test_class_name=test_name,
        maven_module_flag=module_flag,
        targeted_compile_command=targeted_maven_command_text(
            repo_path, "test-compile", module=module,
            quality_gate_command=str(state.get("quality_gate_command") or ""),
        ),
        targeted_test_command=targeted_maven_command_text(
            repo_path, "test", module=module, test_selector=test_name,
            quality_gate_command=str(state.get("quality_gate_command") or ""),
        ),
        target_context_abs=paths.get("context_abs", ""),
        target_symbols_abs=paths.get("symbols_abs", ""),
        uncovered_summary=str(
            (evidence.get("uncovered_by_class") or {}).get(class_fqn, "")
        ),
        roi_abs=paths.get("roi_abs", ""),
        stage_introspect_abs=ensure_stage_introspect_file(repo_path, "coverage_fix"),
    )
    if len(failing) > 1:
        volatile += "\n\n### OTHER BELOW-GATE CLASSES IN THIS BATCH\n" + "\n".join(
            f"- `{fqn}`: {coverage_by_class.get(fqn, 0.0):.1f}%" for fqn in failing[1:]
        )
    return f"{stable}{volatile}"


def interpret_fix_coverage(
    state: Mapping[str, Any], turn: Mapping[str, Any]
) -> Dict[str, Any]:
    attempts = advance_verification_attempts(state.get("attempts_by_phase") or {})
    attempts["fix_coverage"] = int(attempts.get("fix_coverage", 0)) + 1
    return {
        "phase_outcome": "passed",
        "attempts_by_phase": attempts,
        "evidence": {
            "turn_status": str(state.get("turn_status") or turn.get("status") or "unknown"),
            "session_id": state.get("turn_session_id") or last_session_locator(turn),
            "session_refs": session_refs(turn),
        },
    }


def _phase_evidence(state: Mapping[str, Any], phase: str) -> Dict[str, Any]:
    result = dict((state.get("phase_results") or {}).get(phase) or {})
    evidence = result.get("evidence")
    return dict(evidence) if isinstance(evidence, Mapping) else {}


__all__ = [
    "JavaCoveragePorts",
    "interpret_fix_coverage",
    "measure_coverage",
    "render_fix_coverage_prompt",
]

"""Java PIT measurement; the repair-progress policy lives above it."""

from __future__ import annotations

from uta.language.repair_attempts import advance_verification_attempts
from uta.testgen.repair_progress import SCORES_EVIDENCE_KEY

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping

from uta.testgen.project_summary_artifacts import ensure_stage_introspect_file
from uta.enforcement.mutation_repair import plan_mutation_repair_round
from uta.shared.config import settings as uta_settings
from uta.language.java.maven.pitest import java_pit_families_to_repair_context
from uta.language.java.workspace import expected_test_file_rel
from uta.testgen.prompts.loader import render_prompt_split
from uta.shared.languages import PromptBundle
from uta.testgen.repair import RepairKind, repair_prompt_for
from uta.testgen.turn_accounting import last_session_locator, session_refs


@dataclass(frozen=True)
class JavaMutationPorts:
    prompt_bundle: PromptBundle
    run_pitest: Callable[..., tuple[bool, str]]
    find_latest_pitest_report: Callable[..., Any]
    parse_pitest_green_suite_failure: Callable[..., Any]
    compute_mutation_stats: Callable[..., Mapping[str, Any]]
    summarize_surviving_mutants: Callable[..., list[Dict[str, Any]]]
    format_mutation_families: Callable[..., str]
    write_context_artifact: Callable[..., str]
    filter_mutation_families: Callable[..., list[Dict[str, Any]]]
    flatten_mutation_examples: Callable[..., list[Dict[str, Any]]]


def measure_mutation(
    state: Mapping[str, Any], *, ports: JavaMutationPorts
) -> Dict[str, Any]:
    """Run one stable PIT snapshot per class and select the next repair."""
    batch = list(state.get("batch") or [])
    gate = int(state.get("mutation_gate") or 0)
    if not batch:
        return {
            "phase_outcome": "failed",
            "evidence": {"failure_reason": "measure_mutation_requires_stable_batch"},
        }
    if gate <= 0:
        return {
            "phase_outcome": "skipped",
            "evidence": {
                "reason": "mutation_gate_disabled",
                "mutation_gate": gate,
            },
        }

    repo_path = str(state["repo_path"])
    module = state.get("module")
    attempts = int((state.get("attempts_by_phase") or {}).get("fix_mutation", 0))
    previous = _phase_evidence(state, "measure_mutation")
    previous_stats = dict(previous.get("mutation_stats_by_class") or {})
    previous_families = dict(previous.get("families_by_class") or {})
    method_efforts = dict(state.get("method_efforts_by_class") or {})
    roi_enabled = bool(state.get("mutation_roi_enabled", True))
    skip_expensive = bool(state.get("mutation_roi_skip_expensive", False))
    started = time.perf_counter()

    stats_by_class: Dict[str, Dict[str, Any]] = {}
    reports_by_class: Dict[str, str] = {}
    families_by_class: Dict[str, list[Dict[str, Any]]] = {}
    family_summaries: Dict[str, str] = {}
    family_summary_paths: Dict[str, str] = {}
    examples_by_class: Dict[str, list[Dict[str, Any]]] = {}
    repair_round_by_class: Dict[str, str] = {}
    repair_flags_by_class: Dict[str, Dict[str, Any]] = {}
    failing: list[str] = []

    for class_fqn in batch:
        package = ".".join(class_fqn.split(".")[:-1])
        test_class_fqn = ".".join(
            part for part in (package, f"{class_fqn.split('.')[-1]}Test") if part
        )
        pitest_ok, output = ports.run_pitest(
            repo_path, class_fqn, test_class_fqn, module
        )
        if not pitest_ok:
            green_failure = ports.parse_pitest_green_suite_failure(output)
            evidence = {
                "mutation_gate": gate,
                "failing_classes": [class_fqn],
                "output": str(output or "")[-6000:],
                "attempt": attempts,
                "elapsed_seconds": time.perf_counter() - started,
            }
            if green_failure:
                evidence.update(
                    {
                        "failure_reason": "mutation_requires_green_suite",
                        "green_suite_failure": dict(green_failure),
                        "test_selector": f"{class_fqn.split('.')[-1]}Test",
                    }
                )
                return {"phase_outcome": "tests_failed", "evidence": evidence}
            evidence["failure_reason"] = "pitest_execution_failed"
            return {"phase_outcome": "failed", "evidence": evidence}

        report = ports.find_latest_pitest_report(repo_path, module)
        if not report:
            return {
                "phase_outcome": "failed",
                "evidence": {
                    "failure_reason": "pitest_report_missing",
                    "mutation_gate": gate,
                    "failing_classes": [class_fqn],
                    "attempt": attempts,
                    "elapsed_seconds": time.perf_counter() - started,
                },
            }

        stats = dict(ports.compute_mutation_stats(report, class_fqn))
        families = ports.summarize_surviving_mutants(
            report,
            class_fqn,
            method_efforts=method_efforts.get(class_fqn) if roi_enabled else None,
        )
        if roi_enabled:
            families = ports.filter_mutation_families(
                families, skip_expensive=skip_expensive
            )
        paths = dict((state.get("target_context_paths") or {}).get(class_fqn) or {})
        repair_context = java_pit_families_to_repair_context(
            target_id=class_fqn,
            source_path=str(paths.get("source_abs") or ""),
            test_paths=(expected_test_file_rel(module, class_fqn),),
            reproduce_command="mvn org.pitest:pitest-maven:mutationCoverage",
            families=families,
        )
        # What the last round started from and targeted, so the planner can
        # tell a repair that moved survivors from one that changed nothing.
        # Passing the attempt index alone left `no_progress` false forever, and
        # `focused_group_count = len(groups)` made every "focused" round select
        # the whole survivor set -- so a later round repeated round one with its
        # ROI guidance dropped instead of narrowing onto the top groups.
        repair_plan = plan_mutation_repair_round(
            repair_context,
            attempt_index=attempts + 1,
            previous_survivor_count=(
                previous_stats.get(class_fqn) or {}
            ).get("survived"),
            previous_selected_group_ids=[
                family.get("method", "")
                for family in previous_families.get(class_fqn) or ()
            ],
            edited_test_paths=(expected_test_file_rel(module, class_fqn),),
            focused_group_count=int(
                uta_settings.mutation_repair_groups_per_round or 1
            ),
        )
        selected_symbols = {group.symbol for group in repair_plan.selected_groups}
        selected_families = [
            family
            for family in families
            if family.get("method", "(unknown)") in selected_symbols
        ]
        summary = ports.format_mutation_families(selected_families)
        summary_path = ports.write_context_artifact(
            repo_path,
            f"{class_fqn.split('.')[-1]}.mutation_families.md",
            "# Mutation Survivor Families\n\n" + summary,
        )
        score = float(stats.get("score", 0.0) or 0.0)
        survived = int(stats.get("survived", 0) or 0)
        stats_by_class[class_fqn] = stats
        reports_by_class[class_fqn] = str(report)
        families_by_class[class_fqn] = selected_families
        family_summaries[class_fqn] = summary
        family_summary_paths[class_fqn] = summary_path
        examples_by_class[class_fqn] = ports.flatten_mutation_examples(
            selected_families
        )
        repair_round_by_class[class_fqn] = repair_plan.round_kind
        repair_flags_by_class[class_fqn] = dict(repair_plan.prompt_flags)
        if score < gate and survived > 0:
            failing.append(class_fqn)

    evidence = {
        "mutation_gate": gate,
        "mutation_stats_by_class": stats_by_class,
        "mutation_score_by_class": {
            fqn: float(stats.get("score", 0.0) or 0.0)
            for fqn, stats in stats_by_class.items()
        },
        "reports_by_class": reports_by_class,
        "families_by_class": families_by_class,
        "family_summaries": family_summaries,
        "family_summary_paths": family_summary_paths,
        "examples_by_class": examples_by_class,
        "repair_round_by_class": repair_round_by_class,
        "repair_flags_by_class": repair_flags_by_class,
        "failing_classes": failing,
        "attempt": attempts,
        # Whether another repair round is worth running is decided above the
        # languages, from these scores -- the below-gate classes only, so a
        # passing class drifting up cannot pass for progress on a class that
        # is stuck. See `testgen.repair_progress`.
        SCORES_EVIDENCE_KEY: {
            fqn: float((stats_by_class.get(fqn) or {}).get("score", 0.0) or 0.0)
            for fqn in failing
        },
        "mutation_roi_enabled": roi_enabled,
        "mutation_roi_skip_expensive": skip_expensive,
        "elapsed_seconds": time.perf_counter() - started,
    }
    if not failing:
        return {"phase_outcome": "passed", "evidence": evidence}
    evidence["failure_reason"] = "mutation_below_gate"
    return {"phase_outcome": "repair", "evidence": evidence}


def render_fix_mutation_prompt(
    state: Mapping[str, Any], *, ports: JavaMutationPorts
) -> str:
    """Render a focused survivor-family request; agent-core owns the turn."""
    evidence = _phase_evidence(state, "measure_mutation")
    failing = list(evidence.get("failing_classes") or [])
    if not failing:
        raise ValueError("fix_mutation requires at least one below-gate class")
    class_fqn = str(failing[0])
    contexts = dict(state.get("target_context_paths") or {})
    paths = dict(contexts.get(class_fqn) or {})
    stats = dict((evidence.get("mutation_stats_by_class") or {}).get(class_fqn) or {})
    coverage = _phase_evidence(state, "measure_coverage")
    current_coverage = float(
        (coverage.get("coverage_by_class") or {}).get(class_fqn, 0.0) or 0.0
    )
    repair_flags = dict(
        (evidence.get("repair_flags_by_class") or {}).get(class_fqn) or {}
    )
    stable, volatile = render_prompt_split(
        repair_prompt_for(ports.prompt_bundle, RepairKind.mutation),
        class_fqn=class_fqn,
        current_coverage=current_coverage,
        mutation_gate=int(evidence.get("mutation_gate") or state.get("mutation_gate") or 0),
        current_mutation_score=float(stats.get("score", 0.0) or 0.0),
        mutation_stats=stats,
        surviving_mutants=list(
            (evidence.get("examples_by_class") or {}).get(class_fqn) or []
        ),
        mutation_family_summary=str(
            (evidence.get("family_summaries") or {}).get(class_fqn) or ""
        ),
        mutation_family_summary_abs=str(
            (evidence.get("family_summary_paths") or {}).get(class_fqn) or ""
        ),
        source_path=paths.get("source_abs", ""),
        test_file_path=expected_test_file_rel(state.get("module"), class_fqn),
        target_context_abs=paths.get("context_abs", ""),
        target_symbols_abs=paths.get("symbols_abs", ""),
        stage_introspect_abs=ensure_stage_introspect_file(
            str(state["repo_path"]), "mutation_fix"
        ),
        mutation_roi_enabled=bool(evidence.get("mutation_roi_enabled", True)),
        mutation_roi_skip_expensive=bool(
            evidence.get("mutation_roi_skip_expensive", False)
        ),
        mutation_repair_round_kind=str(
            (evidence.get("repair_round_by_class") or {}).get(
                class_fqn, "full_roi"
            )
        ),
        mutation_repair_roi_guided_full=bool(
            repair_flags.get("mutation_repair_roi_guided_full", True)
        ),
    )
    if len(failing) > 1:
        scores = dict(evidence.get("mutation_score_by_class") or {})
        volatile += "\n\n### OTHER BELOW-GATE CLASSES IN THIS BATCH\n" + "\n".join(
            f"- `{fqn}`: {float(scores.get(fqn, 0.0) or 0.0):.1f}%"
            for fqn in failing[1:]
        )
    return f"{stable}{volatile}"


def interpret_fix_mutation(
    state: Mapping[str, Any], turn: Mapping[str, Any]
) -> Dict[str, Any]:
    attempts = advance_verification_attempts(state.get("attempts_by_phase") or {})
    attempts["fix_mutation"] = int(attempts.get("fix_mutation", 0)) + 1
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
    "JavaMutationPorts",
    "interpret_fix_mutation",
    "measure_mutation",
    "render_fix_mutation_prompt",
]

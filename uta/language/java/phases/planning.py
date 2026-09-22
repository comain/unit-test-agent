"""Java test-plan prompt construction and deterministic interpretation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

from uta.testgen.project_summary_artifacts import ensure_stage_introspect_file
from uta.testgen.prompts.loader import render_prompt_split
from uta.testgen.turn_accounting import last_session_locator, session_refs


def render_plan_tests_prompt(state: Mapping[str, Any]) -> str:
    batch = list(state.get("batch") or [])
    contexts = dict(state.get("target_context_paths") or {})
    context_lines = []
    wave_section = ""
    for class_fqn in batch:
        paths = dict(contexts.get(class_fqn) or {})
        part = (
            f"- `{class_fqn}`\n"
            f"  - target context: `{paths.get('context_abs', '')}`\n"
            f"  - symbol map: `{paths.get('symbols_abs', '')}`"
        )
        if paths.get("roi_abs"):
            part += f"\n  - roi scores: `{paths['roi_abs']}`"
        context_lines.append(part)
        context_path = Path(str(paths.get("context_abs") or ""))
        if context_path.is_file():
            try:
                from uta.testgen.wave_assigner import (
                    assign_waves_from_context,
                    format_wave_table,
                )

                waves = assign_waves_from_context(
                    context_path.read_text(encoding="utf-8", errors="replace")
                )
                if waves:
                    wave_section += (
                        f"\n\n### PRE-COMPUTED WAVE TABLE — `{class_fqn}`\n"
                        + format_wave_table(waves)
                    )
            except Exception:
                pass

    stable, volatile = render_prompt_split(
        "plan_tests",
        batch=batch,
        coverage_gate=int(state.get("coverage_gate") or 0),
        quality_mode=str(state.get("quality_mode") or "class_batch"),
        ci_diff_coverage_gate=int(state.get("ci_diff_coverage_gate") or 0),
        ci_diff_mutation_gate=int(state.get("ci_diff_mutation_gate") or 0),
        strict_coverage_classes=list(state.get("strict_coverage_classes") or []),
        target_context_files="\n".join(context_lines),
        roi_enabled=bool(state.get("roi_enabled")),
        index_query_command=str(state.get("plan_index_query_command") or ""),
        stage_introspect_abs=ensure_stage_introspect_file(
            str(state["repo_path"]), "plan"
        ),
        spec_context=str(state.get("spec_context") or ""),
    )
    previous = _phase_result(state)
    reasons = list((previous.get("evidence") or {}).get("replan_reasons") or [])
    if reasons:
        volatile += (
            "\n\n### REQUIRED REPLAN CORRECTIONS\n- "
            + "\n- ".join(str(reason) for reason in reasons)
        )
    return f"{stable}{volatile}{wave_section}"


def interpret_plan_tests(
    state: Mapping[str, Any], turn: Mapping[str, Any], *, ports
) -> Dict[str, Any]:
    status = str(state.get("turn_status") or turn.get("status") or "unknown")
    text = str(state.get("turn_text") or turn.get("text") or "")
    attempts = dict(state.get("attempts_by_phase") or {})
    maximum = int((state.get("max_attempts_by_phase") or {}).get("plan_tests", 1))
    attempt = int(attempts.get("plan_tests", 0))
    session_id = last_session_locator(turn)
    evidence: Dict[str, Any] = {
        "turn_status": status,
        "session_id": session_id,
        "session_refs": session_refs(turn),
        "replan_reasons": [],
    }
    if status != "completed":
        evidence["failure_reason"] = f"planning_turn_{status}"
        evidence["terminal_status"] = _terminal_status("PLANNING", status)
        return {"phase_outcome": "failed", "evidence": evidence}
    if not text.strip():
        evidence["replan_reasons"] = ["The planning turn emitted no final plan text."]
    elif ports.plan_needs_stricter_replan(
        text, list(state.get("strict_coverage_classes") or [])
    ):
        evidence["replan_reasons"] = [
            "The plan is structurally too narrow for a strict coverage target; "
            "include methods required for gate, estimated reach, blockers, and implementation waves."
        ]

    if evidence["replan_reasons"] and attempt < maximum:
        attempts["plan_tests"] = attempt + 1
        candidate = ports.write_plan_candidate(
            repo_path=str(state["repo_path"]),
            session_id=str(session_id or "unknown"),
            batch=list(state.get("batch") or []),
            plan_text=text,
            replan_reasons=evidence["replan_reasons"],
        )
        evidence["candidate_plan_path"] = candidate
        return {
            "phase_outcome": "retry",
            "attempts_by_phase": attempts,
            "evidence": evidence,
        }
    if evidence["replan_reasons"]:
        evidence["failure_reason"] = "planning_attempts_exhausted"
        evidence["terminal_status"] = "PLANNING_TIMEOUT"
        return {"phase_outcome": "exhausted", "evidence": evidence}

    plan_path = ports.write_plan(
        str(state["repo_path"]),
        str(session_id or "unknown"),
        list(state.get("batch") or []),
        text,
    )
    if str(state.get("stop_after_stage") or "") == "plan_tests":
        evidence.update(
            {"plan_path": plan_path, "plan_text": text, "stopped_early": True}
        )
        return {"phase_outcome": "failed", "evidence": evidence}
    return {
        "phase_outcome": "passed",
        "plan_path": plan_path,
        "plan_text": text,
        "evidence": {**evidence, "plan_path": plan_path, "plan_text": text},
    }


def _terminal_status(prefix: str, status: str) -> str:
    if status == "failed":
        return "PROVIDER_ERROR"
    if status == "skipped":
        return f"{prefix}_TIMEOUT"
    return f"{prefix}_{status.upper()}"


def _phase_result(state: Mapping[str, Any]) -> Dict[str, Any]:
    return dict((state.get("phase_results") or {}).get("plan_tests") or {})


__all__ = ["interpret_plan_tests", "render_plan_tests_prompt"]

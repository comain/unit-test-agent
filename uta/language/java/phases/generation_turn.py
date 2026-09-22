"""Java generation prompt and post-turn file contract."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Mapping

from uta.testgen.project_summary_artifacts import ensure_stage_introspect_file
from uta.language.java.workspace import expected_test_file_rel
from uta.language.java.maven.invocation import targeted_maven_command_text
from uta.testgen.prompts.loader import render_prompt_split
from uta.testgen.turn_accounting import last_session_locator, session_refs


def render_generate_tests_prompt(state: Mapping[str, Any], *, ports) -> str:
    batch = list(state.get("batch") or [])
    module = state.get("module")
    contexts = dict(state.get("target_context_paths") or {})
    previous = dict((state.get("phase_results") or {}).get("generate_tests") or {})
    missing = list((previous.get("evidence") or {}).get("missing_test_files") or [])
    if missing:
        return (
            "The previous generation turn did not create every required test file. "
            "Continue from the current workspace, write only the missing files below, "
            "and do not rewrite unrelated files.\n\n"
            + "\n".join(f"- `{path}`" for path in missing)
        )

    first = batch[0]
    first_paths = dict(contexts.get(first) or {})
    test_names = [f"{fqn.split('.')[-1]}Test" for fqn in batch]
    module_flag = f" -pl {module} -am" if module else ""
    gate_command = str(state.get("quality_gate_command") or "")
    compile_command = targeted_maven_command_text(
        str(state["repo_path"]), "test-compile", module=module,
        quality_gate_command=gate_command,
    )
    test_command = targeted_maven_command_text(
        str(state["repo_path"]), "test", module=module,
        test_selector=",".join(test_names), quality_gate_command=gate_command,
    )
    project_paths = dict(state.get("project_prompt_paths") or {})
    stable, volatile = render_prompt_split(
        "generate_test",
        class_fqn=first,
        source_path=first_paths.get("source_abs", ""),
        context_dir=str(state.get("context_dir") or ""),
        target_context_abs=first_paths.get("context_abs", ""),
        target_symbols_abs=first_paths.get("symbols_abs", ""),
        index_query_command=str(state.get("generation_index_query_command") or ""),
        wave_one_only=bool(state.get("strict_coverage_classes")),
        maven_instructions=(
            "\n\nRun the commands above from the repository root. "
            "Do not change the Maven repository/profile/offline settings."
        ),
        targeted_compile_command=compile_command,
        targeted_test_command=test_command,
        maven_module_flag=module_flag,
        test_class_name=test_names[0],
        coverage_gate=int(state.get("coverage_gate") or 0),
        quality_mode=str(state.get("quality_mode") or "class_batch"),
        ci_diff_coverage_gate=int(state.get("ci_diff_coverage_gate") or 0),
        ci_diff_mutation_gate=int(state.get("ci_diff_mutation_gate") or 0),
        run_id=str(int(time.time())),
        stage_introspect_abs=ensure_stage_introspect_file(
            str(state["repo_path"]), "generate"
        ),
        mockito_api_guidance=ports.mockito_api_guidance(str(state["repo_path"])),
        spec_context=str(state.get("spec_context") or ""),
        **project_paths,
    )
    if len(batch) > 1:
        volatile += (
            f"\n\n### BATCH MODE — GENERATE TESTS FOR ALL {len(batch)} CLASSES\n"
            "Write a separate test file for every target:\n"
            + "\n".join(
                f"- `{fqn}` → `{expected_test_file_rel(module, fqn)}`; "
                f"context `{(contexts.get(fqn) or {}).get('context_abs', '')}`"
                for fqn in batch
            )
        )
    plan = dict((state.get("phase_results") or {}).get("plan_tests") or {})
    plan_text = str(plan.get("plan_text") or (plan.get("evidence") or {}).get("plan_text") or "")
    plan_path = str(plan.get("plan_path") or (plan.get("evidence") or {}).get("plan_path") or "")
    volatile += (
        "\n\n### APPROVED TEST PLAN\n"
        f"Plan file: `{plan_path}`\n\n"
        + (ports.compress_plan(plan_text) if plan_text else "_No captured plan text._")
    )
    return f"{stable}{volatile}"


def interpret_generate_tests(
    state: Mapping[str, Any], turn: Mapping[str, Any]
) -> Dict[str, Any]:
    status = str(state.get("turn_status") or turn.get("status") or "unknown")
    attempts = dict(state.get("attempts_by_phase") or {})
    evidence: Dict[str, Any] = {
        "turn_status": status,
        "session_id": last_session_locator(turn),
        "session_refs": session_refs(turn),
        "generation_seconds": float(turn.get("elapsed_seconds") or 0.0),
    }
    if status != "completed":
        evidence["failure_reason"] = f"generation_turn_{status}"
        evidence["terminal_status"] = (
            "PROVIDER_ERROR" if status == "failed" else "GENERATION_TIMEOUT"
        )
        return {"phase_outcome": "failed", "evidence": evidence}

    repo = Path(str(state["repo_path"]))
    module = state.get("module")
    missing = [
        expected_test_file_rel(module, fqn)
        for fqn in list(state.get("batch") or [])
        if not (repo / expected_test_file_rel(module, fqn)).is_file()
    ]
    evidence["missing_test_files"] = missing
    if missing:
        attempt = int(attempts.get("generate_tests", 0))
        maximum = int(
            (state.get("max_attempts_by_phase") or {}).get("generate_tests", 1)
        )
        if attempt < maximum:
            attempts["generate_tests"] = attempt + 1
            return {
                "phase_outcome": "retry",
                "attempts_by_phase": attempts,
                "evidence": evidence,
            }
        evidence.update(
            {"failure_reason": "incomplete_generation_batch", "terminal_status": "INCOMPLETE_BATCH"}
        )
        return {"phase_outcome": "exhausted", "evidence": evidence}
    if str(state.get("stop_after_stage") or "") == "generation":
        evidence["stopped_early"] = True
        return {"phase_outcome": "failed", "evidence": evidence}
    return {"phase_outcome": "passed", "evidence": evidence}


__all__ = ["interpret_generate_tests", "render_generate_tests_prompt"]

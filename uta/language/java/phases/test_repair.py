"""Java test-repair prompt and interpretation policy."""

from __future__ import annotations

from uta.language.repair_attempts import advance_verification_attempts

from dataclasses import dataclass
from typing import Any, Dict, Mapping

from uta.testgen.project_summary_artifacts import ensure_stage_introspect_file
from uta.language.java.workspace import expected_test_file_rel
from uta.language.java.maven.invocation import targeted_maven_command_text
from uta.testgen.turn_accounting import last_session_locator, session_refs


@dataclass(frozen=True)
class JavaTestRepairPorts:
    index_query_command: Any


def render_fix_tests_prompt(
    state: Mapping[str, Any], *, ports: JavaTestRepairPorts
) -> str:
    """Render one bounded repair request from deterministic test evidence."""
    batch = list(state.get("batch") or [])
    if not batch:
        raise ValueError("fix_tests requires a non-empty stable batch")
    repo_path = str(state["repo_path"])
    module = state.get("module")
    verify = _phase_evidence(state, "verify_tests")
    selector = str(
        verify.get("test_selector")
        or ",".join(f"{fqn.split('.')[-1]}Test" for fqn in batch)
    )
    failures = str(verify.get("output") or "Targeted tests failed")[-6000:]
    paths = [expected_test_file_rel(module, fqn) for fqn in batch]
    command = targeted_maven_command_text(
        repo_path, "test", module=module, test_selector=selector,
        quality_gate_command=str(state.get("quality_gate_command") or ""),
    )
    context_paths = dict(state.get("target_context_paths") or {})
    fix_index = ports.index_query_command(module, section="fix_summary")
    prompt = (
        "The generated Java tests compile, but the targeted test run still fails.\n\n"
        f"### TEST FAILURES\n```\n{failures}\n```\n\n"
        "Fix the existing test files. Do not create replacement files or edit production code. "
        "Fix the full failing suite, not only the first assertion. When failures share setup, "
        "stubbing, or fixture state, repair that shared seam first.\n\n"
        "### TARGETED COMMAND\nRun from the repository root. Use this exact command "
        f"without changing Maven flags:\n`{command}`\n\n"
        f"### REPO-LOCAL FIX INDEX\nUse `{fix_index} --class-fqn <CLASS> "
        "--method <METHOD> --symbol <SYMBOL> --json-output` before broad source hunting.\n\n"
        "### TEST FILES\n"
        + "\n".join(f"- `{path}`" for path in paths)
    )
    contexts = [
        str((context_paths.get(fqn) or {}).get("context_abs") or "")
        for fqn in batch
    ]
    contexts = [path for path in contexts if path]
    if contexts:
        prompt += "\n\n### TARGET CONTEXT\n" + "\n".join(
            f"- `{path}`" for path in contexts
        )
    gate_context = _gate_context(state)
    if gate_context:
        prompt += f"\n\n### PRECEDING QUALITY-GATE CONTEXT\n{gate_context}"
    prompt += (
        "\n\n### STAGE INTROSPECT\n"
        f"Read `{ensure_stage_introspect_file(repo_path, 'test_fix')}` before broad exploration."
    )
    return prompt


def interpret_fix_tests(
    state: Mapping[str, Any], turn: Mapping[str, Any]
) -> Dict[str, Any]:
    """Spend one existing Java test-repair attempt, then re-run tests."""
    attempts = advance_verification_attempts(state.get("attempts_by_phase") or {})
    attempts["fix_tests"] = int(attempts.get("fix_tests", 0)) + 1
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


def _gate_context(state: Mapping[str, Any]) -> str:
    for phase, label in (
        ("measure_mutation", "Mutation verification"),
        ("measure_coverage", "Coverage verification"),
    ):
        evidence = _phase_evidence(state, phase)
        output = str(evidence.get("output") or evidence.get("summary") or "").strip()
        if output:
            return f"{label}:\n{output[-1800:]}"
    return ""


__all__ = [
    "JavaTestRepairPorts",
    "interpret_fix_tests",
    "render_fix_tests_prompt",
]

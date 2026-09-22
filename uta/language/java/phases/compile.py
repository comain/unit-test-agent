"""Compile verification and compile-repair prompt policy for Java."""

from __future__ import annotations

from uta.language.repair_attempts import advance_verification_attempts

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from uta.testgen.project_summary_artifacts import ensure_stage_introspect_file
from uta.language.java.baseline import _mockito_api_guidance
from uta.language.java.compile import classify_compile_errors
from uta.language.java.workspace import expected_test_file_rel
from uta.language.java.maven.invocation import targeted_maven_command_text
from uta.testgen.prompts.loader import render_prompt_split
from uta.shared.languages import PromptBundle
from uta.testgen.repair import RepairKind, repair_prompt_for
from uta.testgen.turn_accounting import last_session_locator, session_refs


@dataclass(frozen=True)
class JavaCompilePorts:
    prompt_bundle: PromptBundle
    compile_test: Callable[..., tuple[bool, str]]
    writeback_resolved_symbols: Callable[..., Dict[str, List[str]]]


def verify_compile(
    state: Mapping[str, Any], *, ports: JavaCompilePorts
) -> Dict[str, Any]:
    """Run one compile command and return evidence for graph routing."""
    started = time.perf_counter()
    repo_path = str(state["repo_path"])
    module = state.get("module")
    batch = list(state.get("batch") or [])
    compile_ok, output = ports.compile_test(
        repo_path, module,
        quality_gate_command=str(state.get("quality_gate_command") or ""),
    )
    evidence: Dict[str, Any] = {
        "compile_ok": bool(compile_ok),
        "output": str(output or "")[-6000:],
        "elapsed_seconds": time.perf_counter() - started,
    }
    if compile_ok:
        return {"phase_outcome": "passed", "evidence": evidence}

    classified = classify_compile_errors(output)
    expected_paths = [expected_test_file_rel(module, fqn) for fqn in batch]
    scoped = classified
    if state.get("quality_mode") == "ci_incremental" and classified:
        scoped = _filter_to_paths(classified, expected_paths)
        if not scoped:
            evidence.update(
                {
                    "ignored_unrelated_errors": True,
                    "expected_test_paths": expected_paths,
                    "compile_errors": [_error_payload(item) for item in classified],
                }
            )
            return {"phase_outcome": "passed", "evidence": evidence}

    current_signatures = [str(item.signature) for item in scoped]
    previous = _prior_verify_evidence(state)
    previous_signatures = set(previous.get("error_signatures") or [])
    recurring = [item for item in scoped if str(item.signature) in previous_signatures]
    new_errors = [item for item in scoped if str(item.signature) not in previous_signatures]
    attempts = int((state.get("attempts_by_phase") or {}).get("fix_compile", 0))
    maximum = int((state.get("max_attempts_by_phase") or {}).get("fix_compile", 3))
    evidence.update(
        {
            "expected_test_paths": expected_paths,
            "compile_errors": [_error_payload(item) for item in scoped],
            "error_signatures": current_signatures,
            "feedback": _render_feedback(
                scoped,
                new_errors=new_errors if previous_signatures else None,
                recurring_errors=recurring if previous_signatures else None,
            ),
            "attempt": attempts,
            "max_attempts": maximum,
        }
    )
    if attempts > 0 and scoped and len(recurring) == len(scoped):
        evidence["failure_reason"] = "recurring_compile_errors_without_progress"
        return {"phase_outcome": "failed", "evidence": evidence}
    if attempts >= maximum:
        evidence["failure_reason"] = "compile_repair_attempts_exhausted"
        return {"phase_outcome": "failed", "evidence": evidence}
    return {"phase_outcome": "repair", "evidence": evidence}


def render_fix_compile_prompt(
    state: Mapping[str, Any], *, ports: JavaCompilePorts
) -> str:
    """Build one repair prompt; agent-core owns the session and turn."""
    evidence = _prior_verify_evidence(state)
    batch = list(state.get("batch") or [])
    if not batch:
        raise ValueError("fix_compile requires a non-empty stable batch")
    repo_path = str(state["repo_path"])
    module = state.get("module")
    first_fqn = batch[0]
    contexts = dict(state.get("target_context_paths") or {})
    first_context = dict(contexts.get(first_fqn) or {})
    ports.writeback_resolved_symbols(
        compile_errors=str(evidence.get("output") or ""),
        repo_path=repo_path,
        class_fqn=first_fqn,
        symbols_abs=first_context.get("symbols_abs"),
    )
    expected_paths = list(evidence.get("expected_test_paths") or [])
    if not expected_paths:
        expected_paths = [expected_test_file_rel(module, fqn) for fqn in batch]
    module_flag = f" -pl {module} -am" if module else ""
    stable, volatile = render_prompt_split(
        repair_prompt_for(ports.prompt_bundle, RepairKind.compile),
        class_fqn=first_fqn,
        compile_errors=str(evidence.get("feedback") or evidence.get("output") or ""),
        test_file_path=expected_paths[0],
        maven_module_flag=module_flag,
        targeted_compile_command=targeted_maven_command_text(
            repo_path, "test-compile", module=module,
            quality_gate_command=str(state.get("quality_gate_command") or ""),
        ),
        mockito_api_guidance=_mockito_api_guidance(repo_path),
        target_context_abs=first_context.get("context_abs", ""),
        target_symbols_abs=first_context.get("symbols_abs", ""),
        stage_introspect_abs=ensure_stage_introspect_file(repo_path, "compile_fix"),
    )
    tail = volatile
    if len(expected_paths) > 1:
        tail += (
            "\n\n### ALL TEST FILES IN THIS BATCH\n"
            "Fix compilation errors in ANY of these files as needed:\n"
            + "\n".join(f"- `{path}`" for path in expected_paths)
        )
    tail += (
        "\n\nContinue from the existing generated files on disk. "
        "Do not restart broad exploration. Run Maven from the repository root "
        "without changing the supplied flags."
    )
    return f"{stable}{tail}"


def interpret_fix_compile(
    state: Mapping[str, Any], turn: Mapping[str, Any]
) -> Dict[str, Any]:
    """Advance the logical repair attempt, then require deterministic recheck."""
    attempts = advance_verification_attempts(state.get("attempts_by_phase") or {})
    attempts["fix_compile"] = int(attempts.get("fix_compile", 0)) + 1
    return {
        "phase_outcome": "passed",
        "attempts_by_phase": attempts,
        "evidence": {
            "turn_status": str(state.get("turn_status") or turn.get("status") or "unknown"),
            "session_id": state.get("turn_session_id") or last_session_locator(turn),
            "session_refs": session_refs(turn),
        },
    }


def _prior_verify_evidence(state: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict((state.get("phase_results") or {}).get("verify_compile") or {})
    evidence = result.get("evidence")
    return dict(evidence) if isinstance(evidence, Mapping) else {}


def _error_payload(error: Any) -> Dict[str, Any]:
    return {
        "category": str(error.category),
        "file": str(error.file),
        "line": int(error.line or 0),
        "message": str(error.message),
        "symbol": str(error.symbol) if error.symbol else None,
        "signature": str(error.signature),
        "detail": list(error.detail or ())[:4],
    }


def _filter_to_paths(errors: List[Any], expected_paths: List[str]) -> List[Any]:
    normalized = [path.replace("\\", "/").lstrip("/") for path in expected_paths]
    return [
        error
        for error in errors
        if any(
            str(error.file or "").replace("\\", "/").endswith(path)
            for path in normalized
        )
    ]


def _render_feedback(
    current_errors: List[Any],
    *,
    new_errors: Optional[List[Any]] = None,
    recurring_errors: Optional[List[Any]] = None,
) -> str:
    if not current_errors:
        return ""
    if new_errors is None or recurring_errors is None:
        return _format_errors(current_errors, heading="ALL CURRENT COMPILATION ERRORS")
    sections = [
        "Fix the full current compile-error set below. Do not focus only on the first error."
    ]
    if new_errors:
        sections.append(_format_errors(new_errors, heading="NEW ERROR CLUSTERS"))
    if recurring_errors:
        sections.append(
            _format_errors(recurring_errors, heading="RECURRING ERROR CLUSTERS")
        )
    return "\n\n".join(section for section in sections if section.strip())


def _format_errors(errors: List[Any], *, heading: str) -> str:
    lines = [heading]
    for error in errors:
        line = f"- [{error.category.upper()}] {error.file}:{error.line}: {error.message}"
        if error.symbol:
            line += f" ({error.symbol})"
        lines.append(line)
        lines.extend(f"  {detail}" for detail in list(error.detail or ())[:4])
    return "\n".join(lines)


__all__ = [
    "JavaCompilePorts",
    "interpret_fix_compile",
    "render_fix_compile_prompt",
    "verify_compile",
]

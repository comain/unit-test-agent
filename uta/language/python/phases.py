"""Phase-sized Python generation operations for the durable cycle."""

from __future__ import annotations

from uta.language.repair_attempts import advance_verification_attempts
from uta.testgen.repair_progress import SCORES_EVIDENCE_KEY, repair_round_available

import ast
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from uta.language.python.test_artifacts import (
    generated_body_from_response_or_new_file,
    looks_like_python_test,
    module_name_from_source_path,
    source_path_from_target_id,
    write_python_test_file,
)
from uta.language.python.verification.generation import (
    enforce_generated_test,
    validate_generated_test_import_contract,
)
from uta.enforcement.mutation_repair import (
    format_mutation_repair_context,
    plan_mutation_repair_round,
)
from uta.language.python.mutation_context import build_python_mutation_repair_context
from uta.shared.config import settings
from uta.shared.targets import coerce_target
from uta.testgen.prompts.loader import render_prompt_split
from uta.testgen.turn_accounting import (
    aggregate_turn_accounting,
    last_session_locator,
    session_refs,
)


logger = logging.getLogger(__name__)

#: How much of the survivor summary travels in phase evidence. The summary is
#: checkpointed and written to a ledger artifact on every measurement, and a
#: target with many survivors renders hundreds of kilobytes of mutmut diffs --
#: the full text stays on disk, and the prompt tells the model to read it from
#: `mutation_repair_context_abs`.
SURVIVOR_SUMMARY_MAX_CHARS = 20_000

#: The repairs that spend this unit's flat repair budget. Coverage and mutation
#: repair are deliberately absent: they are bounded by whether their score is
#: still improving, which `testgen.repair_progress` decides.
BUDGETED_REPAIR_PHASES = frozenset({"fix_compile", "fix_tests"})

# These outcomes mean the verifier could not produce trustworthy mutation
# evidence. Editing tests cannot repair the mutation tool or its deterministic
# candidate plan, so do not spend an LLM round on an empty survivor set.
NON_REPAIRABLE_MUTATION_REASONS = frozenset(
    {
        "mutation_backend_failed",
        "mutation_candidate_plan_drift",
        "mutation_candidate_plan_failed",
        "mutation_candidate_plan_missing_context",
        "mutation_resource_exhausted",
    }
)


def precheck_existing_tests(state: Mapping[str, Any], *, enforcer=None):
    generated = _generated_path(state)
    if not generated.is_file():
        return {
            "phase_outcome": "plan_skipped",
            "phase_results": {
                **dict(state.get("phase_results") or {}),
                "plan_tests": {
                    "phase_outcome": "skipped",
                    "evidence": {"reason": "unsupported_by_current_python_policy"},
                },
            },
        }
    verification = _verify(state, run_mutation=True, enforcer=enforcer)
    if verification.status == "passed":
        serialized = _verification_dict(verification)
        return {
            "phase_outcome": "skip_target",
            "existing_verification": serialized,
        }
    if verification.reason_code in NON_REPAIRABLE_MUTATION_REASONS:
        return {
            "phase_outcome": "failed",
            "existing_verification": _verification_dict(verification),
            "evidence": {
                "failure_reason": verification.reason_code,
                "message": verification.message,
            },
        }
    return {
        "phase_outcome": "existing_repair",
        "existing_verification": _verification_dict(verification),
    }


def render_generate_prompt(state: Mapping[str, Any]) -> str:
    target = _target(state)
    paths = dict(state.get("target_context_paths") or {})
    stable, volatile = render_prompt_split(
        "python_generate_test",
        target_id=target.target_id,
        display_name=target.display_name,
        source_path=target.source_path or source_path_from_target_id(target.target_id),
        canonical_module=module_name_from_source_path(
            Path(str(state["repo_path"])),
            target.source_path or source_path_from_target_id(target.target_id),
        ),
        symbol=target.symbol or "",
        generated_test_path=str(state["generated_test_path"]),
        context_abs=paths.get("context_abs", ""),
        context_json_abs=paths.get("json_abs", ""),
        index_query_command=str(state.get("index_query_command") or ""),
        syntax_version=str((state.get("context_payload") or {}).get("syntax_version") or "unknown"),
        parser_backend=str((state.get("context_payload") or {}).get("parser_backend") or "tree_sitter"),
        side_effect_hints=str(state.get("side_effect_hints") or ""),
        changed_line_hints=str(state.get("changed_line_hints") or ""),
        companion_files=list(state.get("companion_files") or []),
        existing_test_path=str(state.get("existing_test_path") or ""),
        existing_test_reasons=list(state.get("existing_test_reasons") or []),
        scored_methods=list(state.get("scored_methods") or []),
        prior_hints=list(state.get("prior_hints") or []),
        spec_context=str(state.get("spec_context") or ""),
    )
    return f"{stable}{volatile}"


def _declared_output_changed(state: Mapping[str, Any], relative_path: str) -> bool:
    """Did this phase change one of its own declared outputs?

    `output_fingerprints_before` is recorded by the ledger's reconciliation
    decision, from the same `backend.output_paths` both languages already
    declare. Absent it -- an embedder driving the phase directly -- the caller
    falls back to inspecting the file.
    """
    before = state.get("output_fingerprints_before")
    if not isinstance(before, Mapping) or relative_path not in before:
        return False
    path = Path(str(state["repo_path"])) / relative_path
    if not path.is_file():
        return False
    import hashlib

    current = hashlib.sha256(path.read_bytes()).hexdigest()
    return current != str(before[relative_path])


def interpret_generate(state: Mapping[str, Any], turn: Mapping[str, Any]):
    status = str(state.get("turn_status") or turn.get("status") or "unknown")
    response = str(state.get("turn_text") or turn.get("text") or "")
    generated = _generated_path(state)
    produced_by_turn = _declared_output_changed(state, str(state["generated_test_path"]))
    if produced_by_turn:
        # The neutral ledger fingerprinted this phase's declared outputs before
        # it ran, and this one changed. Reading a "before" snapshot here would
        # sample the file *after* the agent wrote it, making its own new
        # content indistinguishable from content that was always there -- which
        # reported produced tests as "not produced" and skipped verification.
        preexisted = False
        before = None
    else:
        before = (
            generated.read_text(encoding="utf-8", errors="replace")
            if generated.is_file()
            else None
        )
        preexisted = before is not None
    body = generated_body_from_response_or_new_file(
        response,
        generated_abs=generated,
        generated_preexisted=preexisted,
        generated_before=before,
        allow_existing_non_uta=bool(state.get("allow_existing_non_uta")),
    )
    if status != "completed" and not looks_like_python_test(body):
        return {
            "phase_outcome": "failed",
            "evidence": {
                "failure_reason": f"generation_turn_{status}",
                "terminal_status": "PROVIDER_ERROR",
            },
        }
    if not looks_like_python_test(body):
        return {
            "phase_outcome": "failed",
            "evidence": {
                "failure_reason": "python_test_file_not_produced",
                "terminal_status": "PROVIDER_ERROR",
            },
        }
    written = write_python_test_file(
        Path(str(state["repo_path"])),
        _target(state),
        body,
        relative_path=str(state["generated_test_path"]),
        allow_existing_non_uta=bool(state.get("allow_existing_non_uta")),
    )
    return {
        "phase_outcome": "passed",
        "evidence": {"generated_test_path": str(written), "turn_status": status},
    }


def verify_compile(state: Mapping[str, Any]):
    generated = _generated_path(state)
    try:
        ast.parse(generated.read_text(encoding="utf-8", errors="replace"), filename=str(generated))
    except (OSError, SyntaxError) as exc:
        return _repair_or_fail(state, "fix_compile", "python_syntax_error", str(exc))
    contract = validate_generated_test_import_contract(generated)
    if contract:
        return _repair_or_fail(state, "fix_compile", "invalid_generated_test_import", contract)
    return {
        "phase_outcome": "passed",
        "evidence": {"syntax_valid": True, "import_contract_valid": True},
    }


def verify_tests(state: Mapping[str, Any], *, enforcer=None):
    verification = _verify(state, run_mutation=False, enforcer=enforcer)
    evidence = {"verification": _verification_dict(verification), "tests_pass": verification.tests_pass}
    if verification.tests_pass:
        return {"phase_outcome": "passed", "evidence": evidence}
    return _repair_or_fail(
        state, "fix_tests", verification.reason_code, verification.message, evidence=evidence
    )


def measure_coverage(state: Mapping[str, Any]):
    verification = _latest_verification(state, "verify_tests")
    coverage = dict(verification.get("coverage") or {})
    if not coverage:
        return {"phase_outcome": "failed", "evidence": {"failure_reason": "coverage_evidence_missing"}}
    scores = {_target(state).target_id: float(coverage.get("rate") or 0.0)}
    # Whether another repair round is worth running is decided above the
    # languages, from these scores. See `testgen.repair_progress`.
    evidence = {"coverage": coverage, "coverage_by_class": scores, SCORES_EVIDENCE_KEY: scores}
    if bool(coverage.get("passed")):
        return {"phase_outcome": "passed", "evidence": evidence}
    return _repair(evidence, "coverage_gate_failed", "coverage below gate")


def measure_mutation(state: Mapping[str, Any], *, enforcer=None):
    if float(state.get("mutation_gate") or 0) <= 0:
        return {"phase_outcome": "skipped", "evidence": {"reason": "mutation_gate_disabled"}}
    verification = _verify(state, run_mutation=True, enforcer=enforcer)
    mutation = asdict(verification.mutation) if verification.mutation else {}
    evidence = {
        "verification": _verification_dict(verification),
        "mutation": mutation,
        "mutation_stats_by_class": {_target(state).target_id: {
            "score": float(mutation.get("rate") or 0.0),
            "total": int(mutation.get("generated") or 0),
            "killed": int(mutation.get("killed") or 0),
            "survived": int(mutation.get("survived") or 0),
        }},
    }
    if mutation:
        # Whether another repair round is worth running is decided above the
        # languages, from this score. A run that produced no mutation evidence
        # at all reports none: a missing measurement is a failed tool run, not
        # a repair that stopped making progress.
        evidence[SCORES_EVIDENCE_KEY] = {
            _target(state).target_id: float(mutation.get("rate") or 0.0)
        }
    if verification.status == "passed":
        return {"phase_outcome": "passed", "evidence": evidence}
    if verification.reason_code in NON_REPAIRABLE_MUTATION_REASONS:
        return {
            "phase_outcome": "failed",
            "evidence": {
                **evidence,
                "failure_reason": verification.reason_code,
                "message": verification.message,
            },
        }
    evidence.update(_mutation_repair_context(state, verification))
    return _repair(evidence, verification.reason_code, verification.message)


def render_repair_prompt(state: Mapping[str, Any], phase: str) -> str:
    template = {
        "fix_compile": "python_fix_compile",
        "fix_tests": "python_fix_compile",
        "fix_coverage": "python_fix_coverage",
        "fix_mutation": "python_fix_mutations",
    }[phase]
    target = _target(state)
    paths = dict(state.get("target_context_paths") or {})
    evidence = _repair_evidence(state, phase)
    repair_flags = dict(evidence.get("mutation_repair_flags") or {})
    diagnostics = str(evidence.get("message") or evidence.get("failure_reason") or evidence)
    if phase == "fix_coverage":
        diagnostics = _coverage_repair_diagnostics(evidence)
    if phase == "fix_mutation":
        # Without the survivor summary this prompt asks the model to kill
        # mutants it was never shown, and every round re-rolls the same guess.
        diagnostics = str(evidence.get("mutation_survivor_summary") or diagnostics)
    stable, volatile = render_prompt_split(
        template,
        target_id=target.target_id,
        display_name=target.display_name,
        source_path=target.source_path or "",
        canonical_module=module_name_from_source_path(Path(str(state["repo_path"])), target.source_path or source_path_from_target_id(target.target_id)),
        symbol=target.symbol or "",
        generated_test_path=str(state["generated_test_path"]),
        test_file_path=str(state["generated_test_path"]),
        context_abs=paths.get("context_abs", ""),
        context_json_abs=paths.get("json_abs", ""),
        index_query_command=str(state.get("index_query_command") or ""),
        compile_errors=diagnostics,
        coverage_diagnostics=diagnostics,
        mutation_diagnostics=diagnostics,
        coverage_gate=float(state.get("coverage_gate") or 0),
        mutation_gate=float(state.get("mutation_gate") or 0),
        mutation_repair_context_abs=str(evidence.get("mutation_repair_context_abs") or ""),
        mutation_repair_group_abs="\n".join(evidence.get("mutation_repair_group_paths") or []),
        mutation_repair_group=", ".join(evidence.get("mutation_repair_groups") or []),
        mutation_repair_groups=list(evidence.get("mutation_repair_groups") or []),
        mutation_repair_split=bool(repair_flags.get("mutation_repair_split")),
        mutation_repair_roi_guided_full=bool(repair_flags.get("mutation_repair_roi_guided_full")),
        mutation_repair_reproduce_command=str(evidence.get("mutation_repair_reproduce_command") or ""),
        uta_python_enforcer_command="",
    )
    return f"{stable}{volatile}"


def _coverage_repair_diagnostics(evidence: Mapping[str, Any]) -> str:
    """Describe the exact gate the next agent turn must improve."""
    coverage = evidence.get("coverage")
    if not isinstance(coverage, Mapping):
        return str(evidence.get("message") or evidence.get("failure_reason") or evidence)
    covered = int(coverage.get("covered") or 0)
    total = int(coverage.get("total") or 0)
    rate = float(coverage.get("rate") or 0.0)
    gate = float(coverage.get("gate") or 0.0)
    scope = str(coverage.get("scope") or "target_file")
    label = "Changed-line coverage" if scope == "changed_lines" else "Target-file coverage"
    lines = [f"{label}: {covered}/{total} ({rate:.2f}%); gate: {gate:.2f}%."]
    misses = coverage.get("uncovered_lines")
    if isinstance(misses, Mapping):
        rendered = []
        for raw_path, values in sorted(misses.items(), key=lambda item: str(item[0])):
            rendered.append(
                f"{raw_path}: {', '.join(str(int(line)) for line in (values or []))}"
            )
        if rendered:
            lines.append("Uncovered changed lines: " + "; ".join(rendered))
    if scope == "changed_lines":
        lines.append(
            "Whole-file coverage is not the gate; verify these changed lines with the active test file."
        )
    return "\n".join(lines)


def interpret_repair(state: Mapping[str, Any], turn: Mapping[str, Any], phase: str):
    attempts = advance_verification_attempts(state.get("attempts_by_phase") or {})
    attempts[phase] = int(attempts.get(phase, 0)) + 1
    if phase in BUDGETED_REPAIR_PHASES:
        attempts["python_repair_total"] = int(attempts.get("python_repair_total", 0)) + 1
    return {
        "phase_outcome": "passed",
        "attempts_by_phase": attempts,
        "evidence": {
            "session_id": last_session_locator(turn),
            "session_refs": session_refs(turn),
            "turn_status": turn.get("status"),
        },
    }


def complete_generation(state: Mapping[str, Any]):
    from uta.enforcement.equivalent_mutants import REVIEW_EVIDENCE_KEY
    from uta.testgen.equivalence_review import review_payload_for_results

    target = _target(state)
    verification = (
        _latest_verification(state, "measure_mutation")
        or _latest_verification(state, "verify_tests")
        or _latest_verification(state, "precheck_existing_tests")
        or dict(state.get("existing_verification") or {})
    )
    fields = dict(verification.get("result_fields") or {})
    if not fields:
        fields = {"status": "FAIL", "coverage": 0.0, "tests_pass": False, "mutation_score": None}
    generated = _generated_path(state)
    accounting = aggregate_turn_accounting(state.get("turn_history") or [])
    fields.update({
        "language": "python", "target_id": target.target_id,
        "display_name": target.display_name, "source_path": target.source_path,
        "symbol": target.symbol, "target_granularity": target.granularity,
        "test_file_path": str(state["generated_test_path"]),
        "test_file_content": generated.read_text(encoding="utf-8", errors="replace") if generated.is_file() else "",
        "session_ids": accounting["session_ids"],
        "session_refs": accounting["session_refs"],
        REVIEW_EVIDENCE_KEY: review_payload_for_results(state),
    })
    return {
        "phase_outcome": "passed",
        "results": {**dict(state.get("results") or {}), target.target_id: fields},
        "current_batch": [],
        "current_stage": "complete_generation",
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
        },
    }


def _mutation_repair_context(state, verification):
    """Build the survivor context this target's next repair round needs.

    Without it the `python_fix_mutations` prompt renders a score where its
    "SURVIVING MUTANTS" block should be, and then tells the model to read a
    context artifact nobody wrote -- so a repair round is a guess, and a second
    round is the same guess again. This is the Python counterpart of what the
    Java phase already assembles from its PIT report.

    Failures here are not fatal: a repair round with a bare score is what the
    cycle did before, and it is better than abandoning the target.
    """
    if not verification.mutation:
        # The run produced no mutation evidence at all -- a failed tool run, not
        # a survivor set. There is nothing to summarize, and the repair prompt
        # should say so with the verifier's own message.
        return {}
    if not repair_round_available(state, "measure_mutation"):
        # No round can follow, so the `mutmut show` subprocesses this costs
        # would produce a survivor map nothing reads.
        return {}
    target = _target(state)
    attempt = int((state.get("attempts_by_phase") or {}).get("fix_mutation", 0)) + 1
    previous = _repair_evidence(state, "fix_mutation")
    try:
        context = build_python_mutation_repair_context(
            repo=Path(str(state["repo_path"])),
            target_id=target.target_id,
            source_path=target.source_path or source_path_from_target_id(target.target_id),
            test_paths=[str(state["generated_test_path"])],
            verification=verification,
            repair_attempt=attempt,
            method_efforts=list(state.get("scored_methods") or []) or None,
        )
        plan = plan_mutation_repair_round(
            context,
            attempt_index=attempt,
            previous_survivor_count=(previous.get("mutation") or {}).get("survived"),
            previous_selected_group_ids=previous.get("mutation_repair_groups") or (),
            edited_test_paths=(str(state["generated_test_path"]),),
            focused_group_count=int(settings.mutation_repair_groups_per_round or 1),
        )
    except Exception:  # pragma: no cover - context is an aid, never a gate
        logger.warning(
            "Could not build Python mutation repair context for %s; "
            "the repair round will see the score only",
            target.target_id,
            exc_info=True,
        )
        return {}
    return {
        "mutation_repair_context_abs": plan.context_artifact_path,
        "mutation_repair_groups": [group.symbol for group in plan.selected_groups],
        "mutation_repair_group_paths": list(plan.group_artifact_paths),
        "mutation_repair_flags": dict(plan.prompt_flags),
        "mutation_repair_reproduce_command": context.reproduce_command,
        "mutation_survivor_summary": _bounded_summary(
            format_mutation_repair_context(context, selected_group=None),
            plan.context_artifact_path,
        ),
    }


def _bounded_summary(summary: str, artifact_path: str) -> str:
    if len(summary) <= SURVIVOR_SUMMARY_MAX_CHARS:
        return summary
    return (
        summary[:SURVIVOR_SUMMARY_MAX_CHARS]
        + f"\n\n_Truncated. The full survivor map is at `{artifact_path}`._\n"
    )


def _repair(evidence, reason, message):
    """Ask for another repair round; `testgen.repair_progress` grants or refuses it."""
    return {
        "phase_outcome": "repair",
        "evidence": {**dict(evidence), "failure_reason": reason, "message": message},
    }


def _repair_or_fail(state, phase, reason, message, *, evidence=None):
    total = int((state.get("attempts_by_phase") or {}).get("python_repair_total", 0))
    maximum = int((state.get("max_attempts_by_phase") or {}).get("python_repair_total", 2))
    merged = {**dict(evidence or {}), "failure_reason": reason, "message": message, "attempt": total, "max_attempts": maximum}
    return {"phase_outcome": "repair" if total < maximum else "failed", "evidence": merged}


def _verify(state, *, run_mutation, enforcer):
    return enforce_generated_test(
        Path(str(state["repo_path"])), _target(state), _generated_path(state),
        context_payload=dict(state.get("context_payload") or {}), enforcer=enforcer,
        coverage_gate=float(state.get("coverage_gate") or 0), mutation_gate=float(state.get("mutation_gate") or 0),
        run_mutation=run_mutation,
        base_ref=str(state.get("base_ref") or "origin/master"),
    )


def _verification_dict(value):
    return {**asdict(value), "result_fields": value.as_result_fields()}


def _latest_verification(state, phase):
    phase_result = dict((state.get("phase_results") or {}).get(phase) or {})
    evidence = dict(phase_result.get("evidence") or {})
    return dict(
        evidence.get("verification")
        or phase_result.get("existing_verification")
        or {}
    )


def _repair_evidence(state, phase):
    source = {"fix_compile": "verify_compile", "fix_tests": "verify_tests", "fix_coverage": "measure_coverage", "fix_mutation": "measure_mutation"}[phase]
    return dict(((state.get("phase_results") or {}).get(source) or {}).get("evidence") or {})


def _target(state):
    return coerce_target(state["target"])


def _generated_path(state):
    return Path(str(state["repo_path"])) / str(state["generated_test_path"])


__all__ = [name for name in globals() if not name.startswith("_")]

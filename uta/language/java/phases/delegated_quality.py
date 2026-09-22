"""Target-scoped delegated Maven quality-gate verification and repair."""

from __future__ import annotations

import logging

from uta.language.repair_attempts import advance_verification_attempts

from uta.language.java.generation.quality import _delegated_gate_context
from uta.language.java.generation.evidence import _enforcement_evidence_detail
from uta.language.java.generation.mutation_context import write_delegated_survivors

import time
from dataclasses import dataclass
from typing import Optional, Any, Callable, Dict, Mapping

from uta.testgen.project_summary_artifacts import ensure_stage_introspect_file
from uta.testgen.turn_accounting import last_session_locator, session_refs


@dataclass(frozen=True)
class JavaDelegatedQualityPorts:
    run_gate: Callable[..., Dict[str, Any]]
    failure_matches_batch: Callable[..., bool]
    annotate_out_of_scope_failure: Callable[..., Dict[str, Any]]
    gate_feedback: Callable[..., str]
    prompt_feedback: Callable[..., str]
    expected_test_paths: Callable[..., list[str]]
    failure_stage: Callable[[Dict[str, Any]], str]


logger = logging.getLogger(__name__)


def verify_delegated_quality_gate(
    state: Mapping[str, Any], *, ports: JavaDelegatedQualityPorts
) -> Dict[str, Any]:
    """Run the authoritative target-scoped Maven gate without a model turn."""
    if str(state.get("quality_gate_backend") or "") != "maven_enforcer":
        return {
            "phase_outcome": "skipped",
            "evidence": {"reason": "delegated_quality_gate_not_configured"},
        }
    batch = list(state.get("batch") or [])
    if not batch:
        return {
            "phase_outcome": "failed",
            "evidence": {
                "failure_reason": "delegated_quality_gate_requires_stable_batch"
            },
        }

    repo_path = str(state["repo_path"])
    started = time.perf_counter()
    gate_result = dict(
        ports.run_gate(
            dict(state), repo_path, batch=batch, target_scoped=True
        )
    )
    elapsed = time.perf_counter() - started
    attempts = int(
        (state.get("attempts_by_phase") or {}).get("delegated_quality_gate", 0)
    )
    maximum = int(
        (state.get("max_attempts_by_phase") or {}).get(
            "delegated_quality_gate", 6
        )
    )
    evidence: Dict[str, Any] = {
        "quality_gate_result": gate_result,
        "gate_seconds": elapsed,
        "attempt": attempts,
        "max_attempts": maximum,
        "failure_stage": ports.failure_stage(gate_result),
        "output": ports.gate_feedback(gate_result),
    }
    if gate_result.get("passed"):
        authoritative = _unresolved_authoritative_failure(
            state, repo_path, batch, ports=ports, attempts=attempts
        )
        if authoritative is None:
            return {"phase_outcome": "passed", "evidence": evidence}
        # The target-scoped gate is narrower than the one that failed. On the
        # first attempt it can pass while the authoritative full gate is still
        # failing for this very batch, and accepting that would close the task
        # on evidence that never covered the failure. Repair from the full
        # gate's evidence instead.
        logger.warning(
            "Target-scoped precheck passed but the authoritative full gate failed "
            "for the current batch; repairing from the full-gate evidence before "
            "accepting a scoped pass"
        )
        gate_result = dict(authoritative)
        evidence.update(
            {
                "quality_gate_result": gate_result,
                "failure_stage": ports.failure_stage(gate_result),
                "output": ports.gate_feedback(gate_result),
                "scoped_pass_overridden": True,
            }
        )

    if not ports.failure_matches_batch(
        dict(state), repo_path, gate_result, batch
    ):
        annotated = ports.annotate_out_of_scope_failure(
            gate_result,
            state=dict(state),
            repo_path=repo_path,
            batch=batch,
        )
        evidence.update(
            {
                "quality_gate_result": annotated,
                "failure_reason": "delegated_gate_failure_outside_batch",
                "output": ports.gate_feedback(annotated),
            }
        )
        return {"phase_outcome": "failed", "evidence": evidence}

    # The neutral cycle owns stopping policy, including delegated CI repairs.
    # Do not reintroduce a separate fixed retry loop here.
    kind = "mutation" if evidence["failure_stage"] == "mutation_fix" else "coverage"
    detail = _enforcement_evidence_detail(gate_result)
    metric = detail.get(kind) or (detail.get("pitMutation") if kind == "mutation" else {}) or {}
    rate = metric.get("rate")
    # Maven reports a scoped aggregate, not independent class measurements.
    # Key it by the whole stable batch rather than assigning it to each class.
    evidence.update(
        repair_kind=kind,
        repair_phase="delegated_quality_gate",
        scores_by_target={"|".join(sorted(batch)): rate} if rate is not None else {},
    )

    evidence["prompt_feedback"] = ports.prompt_feedback(
        repo_path,
        str(evidence["failure_stage"]),
        attempts + 1,
        str(evidence["output"]),
    )
    evidence["allowed_test_paths"] = ports.expected_test_paths(
        dict(state), batch
    )
    if kind == "mutation":
        artifact = write_delegated_survivors(repo_path, gate_result, batch)
        evidence["survivor_artifact"] = str(artifact) if artifact else None
    return {"phase_outcome": "repair", "evidence": evidence}


def _user_repair_context_section(state: Mapping[str, Any], max_chars: int = 4000) -> str:
    """The requester's own words about what the change should do.

    An RDC-triggered repair has them, and the gate prompt was leaving them
    out -- so the agent saw the failing numbers and the diff but not the
    intent, and optimised for the gate rather than for the behaviour. Bounded
    and tail-trimmed: if it has to be cut, the end is where the specifics
    usually are.
    """
    context = _delegated_gate_context(state)
    user = context.get("user") if isinstance(context.get("user"), dict) else {}
    value = str(user.get("context") or "").strip()
    if not value:
        return ""
    if len(value) > max_chars:
        value = value[-max_chars:]
    return f"### USER-SUPPLIED REPAIR CONTEXT\n{value}\n\n"


def _unresolved_authoritative_failure(
    state: Mapping[str, Any],
    repo_path: str,
    batch: list,
    *,
    ports: "JavaDelegatedQualityPorts",
    attempts: int,
) -> Optional[Dict[str, Any]]:
    """The precheck's full-gate failure, when a scoped pass must not clear it.

    Only on the first attempt: after a repair turn has run, the scoped gate is
    reporting on work that actually happened and is the better evidence. And
    only when the failure belongs to this batch -- an unrelated module's
    failure is not this task's to answer.
    """
    if attempts != 0:
        return None
    # `phase_results[phase]` is the phase's whole projection, and the precheck
    # returns `delegated_quality_gate` at its top level -- not under
    # `evidence`, which most phases use. Reading the wrong one made this
    # return None on every run, which is the shape a scoped pass would take
    # anyway, so nothing looked wrong.
    precheck = dict((state.get("phase_results") or {}).get("precheck_existing_tests") or {})
    initial = precheck.get("delegated_quality_gate") or _phase_evidence(
        state, "precheck_existing_tests"
    ).get("delegated_quality_gate")
    if not isinstance(initial, Mapping) or initial.get("passed"):
        return None
    if not ports.failure_matches_batch(dict(state), repo_path, dict(initial), batch):
        return None
    return dict(initial)


def render_delegated_quality_gate_prompt(state: Mapping[str, Any]) -> str:
    """Render the current scoped gate failure; agent-core owns the turn."""
    evidence = _phase_evidence(state, "delegated_quality_gate_verify")
    if not evidence or not evidence.get("quality_gate_result"):
        raise ValueError("delegated quality repair requires verifier evidence")
    batch = list(state.get("batch") or [])
    allowed_paths = list(evidence.get("allowed_test_paths") or [])
    allowed_lines = (
        "\n".join(f"- `{path}`" for path in allowed_paths)
        or "- target-specific test files only"
    )
    previous_session = (list(state.get("session_ids") or []) or [None])[-1]
    prompt = (
        "The delegated quality gate is still failing after the generated unit tests.\n\n"
        "For this task, the authoritative coverage/mutation checker is the configured "
        "Maven test-enforcement command.\n\n"
        f"Target batch: {', '.join(batch)}\n"
        f"Previous generation session: `{previous_session or ''}`\n\n"
        "### EDIT SCOPE\n"
        "Stay inside the selected target scope. You may edit only:\n"
        f"{allowed_lines}\n"
        "- `pom.xml` or module `pom.xml` files when Maven/Surefire/PIT wiring blocks test execution\n"
        "- test resources under `src/test/resources`\n"
        "Do not edit unrelated existing tests, production code, generated build output, or unrelated modules.\n\n"
        "Within the first 8 tool calls, edit one of the target test files or stop and report that the repair is impractical. "
        "Runtime logs, cache artifacts, and broad dependency exploration do not count as progress.\n\n"
        f"{_user_repair_context_section(state)}"
        "### TEST-ENFORCEMENT FEEDBACK\n"
        f"{evidence.get('prompt_feedback') or evidence.get('output') or ''}\n\n"
        + _survivor_instructions(evidence)
        +
        "Fix or improve the generated unit tests so the diff coverage and diff mutation gates pass. "
        "Prefer focused assertions for the changed behavior. If the gate reports unrelated baseline "
        "test failures, report them instead of fixing unrelated tests."
    )
    introspect = ensure_stage_introspect_file(
        str(state["repo_path"]), str(evidence.get("failure_stage") or "coverage_fix")
    )
    return (
        prompt
        + "\n\n### STAGE INTROSPECT\n"
        + f"- Prior lessons for this stage: `{introspect}`\n"
        + "- Read this file before broad exploration and apply only relevant lessons."
    )


def _survivor_instructions(evidence: Mapping[str, Any]) -> str:
    if evidence.get("failure_stage") != "mutation_fix":
        return ""
    artifact = evidence.get("survivor_artifact")
    return (
        "### AUTHORITATIVE SURVIVOR PLAN\n"
        + (f"Read `{artifact}` before editing. It contains all target survivor groups, "
           "source lines, representative mutations, original XML paths and the reproduction command.\n"
           if artifact else
           "The current invocation's survivor artifact is unavailable. Report this evidence gap; "
           "do not infer equivalence from aggregate scores or use stale module reports.\n")
        + "Work across ALL remaining actionable groups in rank order, not just one convenient example. "
        "Keep suspected equivalent mutants separate: they do not justify abandoning other groups. "
        "For each group report attempted tests, observable behavior asserted, or a specific source-backed blocker; "
        "label untouched groups unattempted, never equivalent. Run the authoritative gate after the edits. "
        "Do not change thresholds, operators or exclusions to clear the gate.\n\n"
    )


def interpret_delegated_quality_gate(
    state: Mapping[str, Any], turn: Mapping[str, Any]
) -> Dict[str, Any]:
    # The delegated gate repairs the workspace exactly like the four `fix_*`
    # phases do, so it invalidates the verification before it in exactly the
    # same way. It was the one repair path left out when the others were
    # fixed, and beta task 44 died on it with `fail_unsafe` -- the same
    # failure, one phase over.
    attempts = advance_verification_attempts(state.get("attempts_by_phase") or {})
    attempts["delegated_quality_gate"] = int(
        attempts.get("delegated_quality_gate", 0)
    ) + 1
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
    "JavaDelegatedQualityPorts",
    "interpret_delegated_quality_gate",
    "render_delegated_quality_gate_prompt",
    "verify_delegated_quality_gate",
]

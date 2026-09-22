"""The equivalent-mutant review, as the generation cycle runs it.

A CI repair whose mutation score stopped improving gets one review turn before
it gives up. This module is the cycle's side of that turn, and it is neutral:
a language backend only answers `scoring_survivors(state)`. Everything else --
whether to ask, what to ask, and what the answer means -- is decided here and
in `uta.enforcement.equivalent_mutants`.

The review never passes the unit. It records a decision on the unit's result;
only the fix session's fresh gate rerun may use that decision, and only if the
gate reproduces exactly the mutants that were reviewed.
"""

from __future__ import annotations

import hashlib
import logging
import re
import stat
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from uta.enforcement.enforcement import git_output
from uta.enforcement.equivalent_mutants import (
    REVIEW_EVIDENCE_KEY,
    REVIEW_PHASE,
    ReviewDecision,
    ScoringSurvivors,
    assess_eligibility,
    parse_verdicts,
)
from uta.shared.config import settings

logger = logging.getLogger(__name__)

#: The phase outcome that routes a stalled mutation measurement to the review.
REVIEW_OUTCOME = "review_equivalence"
#: The measurement phases that can stall on mutation, in the order searched.
TRIGGER_PHASES = ("measure_mutation", "delegated_quality_gate_verify")
#: The only stop that earns a review (`uta.testgen.repair_progress`).
NO_PROGRESS_REASON = "mutation_repair_no_progress"
#: A verdict file for at most 30 mutants is a few kilobytes; anything near this
#: is not an answer to the prompt.
MAX_VERDICTS_BYTES = 1024 * 1024

_SURVIVORS_KEY = "equivalence_survivors"
_VERDICTS_PATH_KEY = "equivalence_verdicts_path"
_TREE_KEY = "equivalence_tree"


def prepare_review(
    phase: str, state: Mapping[str, Any], projection: Dict[str, Any], backend: Any
) -> Dict[str, Any]:
    """Turn a CI repair's no-progress stop into one review turn, if it can run.

    A CI repair that stalled on mutation may be stalled on mutants no test can
    kill, so before giving up it is asked once whether they are equivalent.
    Eligibility is judged here, before routing, because the topology sends a
    prompt straight to its turn: an ineligible unit stays `failed` and never
    reaches the model.
    """
    evidence = dict(projection.get("evidence") or {})
    if (
        projection.get("phase_outcome") != "failed"
        or evidence.get("failure_reason") != NO_PROGRESS_REASON
        or state.get("quality_mode") != "ci_incremental"
        or REVIEW_PHASE in (state.get("phase_results") or {})
    ):
        return projection
    measured = {**dict(state), "phase_results": {**dict(state.get("phase_results") or {}), phase: projection}}
    reader = getattr(backend, "scoring_survivors", None)
    survivors: Optional[ScoringSurvivors] = reader(measured) if callable(reader) else None
    repo = Path(str(state["repo_path"]))
    unit = str(state.get("unit_id") or "")
    reason = assess_eligibility(survivors, cap=int(settings.ci_equivalent_mutant_review_max)).reason
    # Without HEAD the edit check would compare two empty answers and pass.
    tree = None if reason else tracked_tree_fingerprint(repo)
    if not reason and tree is None:
        reason = "workspace_unverifiable"
    if reason:
        logger.info("ci_equivalence_ineligible unit=%s reason=%s", unit, reason)
        evidence[REVIEW_EVIDENCE_KEY] = ReviewDecision("ineligible", reason, survivors).to_dict()
        return {**projection, "evidence": evidence}
    evidence.update({
        _SURVIVORS_KEY: survivors.to_dict(),
        _VERDICTS_PATH_KEY: f".uta_cache/equivalence/{_safe(unit)}/verdicts.json",
        _TREE_KEY: tree,
    })
    return {
        **projection,
        "phase_outcome": REVIEW_OUTCOME,
        "evidence": evidence,
        "equivalence_review_timeout_seconds": int(settings.ci_equivalent_mutant_review_timeout_seconds),
    }


def render_prompt(state: Mapping[str, Any]) -> str:
    request = _review_request(state)
    if request is None:
        raise ValueError("equivalent-mutant review requested without prepared survivors")
    survivors, verdicts_path, _tree = request
    # Removed at the prompt, not earlier, so a retried turn can never be
    # credited with verdicts an interrupted turn left behind.
    stale = Path(str(state["repo_path"])) / verdicts_path
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.unlink(missing_ok=True)
    return render_review_prompt(survivors, verdicts_path=verdicts_path)


def render_review_prompt(survivors: ScoringSurvivors, *, verdicts_path: str) -> str:
    """The review prompt. It lives with the cycle, which owns prompts; the rule does not."""
    from dataclasses import asdict

    from uta.testgen.prompts.loader import render_prompt

    return render_prompt(
        "equivalent_mutant_review",
        mutants=[asdict(mutant) for mutant in survivors.mutants],
        verdicts_path=verdicts_path,
        mutation_rate=survivors.mutation_rate,
        mutation_gate=survivors.mutation_gate,
    )


def interpret(state: Mapping[str, Any], turn: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Read the review's verdicts into a saved decision; the unit still fails."""
    unit = str(state.get("unit_id") or "")
    request = _review_request(state)
    if request is None:
        return _decided(ReviewDecision("rejected", "turn_failed", None))
    survivors, verdicts_path, tree = request
    repo = Path(str(state["repo_path"]))
    status = str((turn or {}).get("status") or state.get("turn_status") or "")
    session_ref = _session_ref(state)
    model_id = str(((turn or {}).get("usage") or {}).get("main_model") or (turn or {}).get("model_id") or "")

    outcome, reason, verdicts, digest = "rejected", "", (), ""
    if not tree or tracked_tree_fingerprint(repo) != tree:
        reason = "review_modified_workspace"
    elif status and status != "completed":
        reason = "turn_failed"
    else:
        text = _read_verdicts(repo, verdicts_path)
        parsed = parse_verdicts(text, expected_keys=[mutant.key for mutant in survivors.mutants])
        outcome, reason, verdicts = parsed.outcome, parsed.reason, parsed.verdicts
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else ""
    decision = ReviewDecision(outcome, reason, survivors, verdicts, digest, session_ref, model_id)
    if decision.outcome == "all_equivalent":
        logger.info("ci_equivalence_review_all_equivalent unit=%s mutants=%d", unit, len(survivors.mutants))
    else:
        logger.info("ci_equivalence_review_rejected unit=%s reason=%s", unit, decision.reason)
    return _decided(decision)


def tracked_tree_fingerprint(repo: Path) -> Optional[str]:
    """Identity of every tracked file's content, or None when git cannot say.

    Untracked build output is ignored; the source fingerprints and the fresh
    gate rerun are what bind a grant to the code.
    """
    head = git_output(repo, "rev-parse", "HEAD")
    if not head:
        return None
    diff = git_output(repo, "diff", "HEAD", "--binary")
    return hashlib.sha256(f"{head}\n{diff}".encode("utf-8")).hexdigest()



def _read_verdicts(repo: Path, verdicts_path: str) -> Optional[str]:
    """The model's verdict file, or None if it is missing or not trustworthy."""
    path = repo / verdicts_path
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_VERDICTS_BYTES:
        return None
    if not path.resolve().is_relative_to(repo.resolve()):
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def review_payload_for_results(state: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The saved review a unit's `complete_generation` should persist, if any."""
    review = _phase_evidence(state, REVIEW_PHASE).get(REVIEW_EVIDENCE_KEY)
    return dict(review) if isinstance(review, Mapping) else None


def _phase_evidence(state: Mapping[str, Any], phase: str) -> Mapping[str, Any]:
    result = (state.get("phase_results") or {}).get(phase)
    evidence = result.get("evidence") if isinstance(result, Mapping) else None
    return evidence if isinstance(evidence, Mapping) else {}


def _review_request(state: Mapping[str, Any]):
    for phase in TRIGGER_PHASES:
        evidence = _phase_evidence(state, phase)
        if not isinstance(evidence.get(_SURVIVORS_KEY), Mapping):
            continue
        return (
            ScoringSurvivors.from_dict(evidence[_SURVIVORS_KEY]),
            str(evidence.get(_VERDICTS_PATH_KEY) or ""),
            str(evidence.get(_TREE_KEY) or ""),
        )
    return None


def _decided(decision: ReviewDecision) -> Dict[str, Any]:
    return {
        "phase_outcome": "failed",
        "evidence": {
            REVIEW_EVIDENCE_KEY: decision.to_dict(),
            "failure_reason": NO_PROGRESS_REASON,
        },
    }


def _session_ref(state: Mapping[str, Any]) -> Dict[str, str]:
    refs = state.get("turn_session_refs") or []
    if refs and isinstance(refs[-1], Mapping):
        return {str(key): str(value) for key, value in refs[-1].items()}
    session = state.get("turn_session_id")
    return {"session_id": str(session)} if session else {}


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.") or "unit"


__all__ = [
    "REVIEW_OUTCOME",
    "REVIEW_PHASE",
    "TRIGGER_PHASES",
    "interpret",
    "prepare_review",
    "render_prompt",
    "render_review_prompt",
    "review_payload_for_results",
    "tracked_tree_fingerprint",
]

"""A CI repair that would give up on mutation first asks one review turn.

The review is a neutral cycle phase: the language only supplies survivors, the
cycle decides when to ask, and the saved decision never passes the unit -- only
the fix session's fresh rerun may do that.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from uta.enforcement.equivalent_mutants import (
    REVIEW_EVIDENCE_KEY,
    REVIEW_PHASE,
    MutantIdentity,
    ScoringSurvivors,
)
from uta.testgen.graph.cycle import (
    generation_operation,
    interpret_phase,
    render_phase_prompt,
)
from uta.testgen.equivalence_review import prepare_review, review_payload_for_results
from uta.testgen.graph.cycle import build_cycle_workflow
from uta.testgen.prompts import PromptArtifactScope
from uta.testgen.repair_progress import SCORES_EVIDENCE_KEY, apply_repair_progress

from tests.test_generation_cycle_build import ScriptedBackend, ScriptedRunner, context_for, initial_state

TRIGGER = "measure_mutation"


def _survivors(*keys):
    return ScoringSurvivors(
        language="python",
        mutants=tuple(MutantIdentity(key, "pkg/text.py", 3, "or_to_and", "or -> and") for key in keys),
        unreviewed_scoring_failures=0,
        source_fingerprints={"pkg/text.py": "sha"},
        mutation_rate=90.0,
        mutation_gate=95.0,
    )


# --- the policy trigger ------------------------------------------------------


def _flat_mutation_state(**updates):
    state = {
        "quality_mode": "ci_incremental",
        "attempts_by_phase": {"fix_mutation": 2},
        "max_attempts_by_phase": {"fix_mutation": 6},
        "best_scores_by_phase": {"fix_mutation": {"t": 90.0}},
        "no_progress_by_phase": {"fix_mutation": 1},
        "phase_results": {},
    }
    state.update(updates)
    return state


def _flat_measurement():
    return {"phase_outcome": "repair", "evidence": {SCORES_EVIDENCE_KEY: {"t": 90.0}}}


def test_the_stopping_policy_itself_knows_nothing_of_reviews():
    result = apply_repair_progress(TRIGGER, _flat_mutation_state(), _flat_measurement())

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == "mutation_repair_no_progress"


def _stalled():
    return {"phase_outcome": "failed", "evidence": {"failure_reason": "mutation_repair_no_progress"}}


def test_review_is_only_asked_for_once_only_in_ci_repair_and_only_on_no_progress(tmp_path):
    _init_repo(tmp_path)
    backend = _Backend(_survivors("m1"))
    base = {"repo_path": str(tmp_path), "unit_id": "u1", "quality_mode": "ci_incremental", "phase_results": {}}

    assert prepare_review(TRIGGER, base, _stalled(), backend)["phase_outcome"] == "review_equivalence"
    assert prepare_review(TRIGGER, {**base, "quality_mode": "class_batch"}, _stalled(), backend)["phase_outcome"] == "failed"
    reviewed = {**base, "phase_results": {REVIEW_PHASE: {"phase_outcome": "failed"}}}
    assert prepare_review(TRIGGER, reviewed, _stalled(), backend)["phase_outcome"] == "failed"
    exhausted = {"phase_outcome": "failed", "evidence": {"failure_reason": "mutation_repair_attempts_exhausted"}}
    assert prepare_review(TRIGGER, base, exhausted, backend) == exhausted
    coverage = {"phase_outcome": "failed", "evidence": {"failure_reason": "coverage_repair_no_progress"}}
    assert prepare_review("measure_coverage", base, coverage, backend) == coverage


def test_review_payload_for_results_reads_the_review_phase():
    review = {"outcome": "all_equivalent"}
    state = {"phase_results": {REVIEW_PHASE: {"evidence": {REVIEW_EVIDENCE_KEY: review}}}}
    assert review_payload_for_results(state) == review
    assert review_payload_for_results({"phase_results": {}}) is None


# --- eligibility before routing ---------------------------------------------


class _Backend:
    def __init__(self, survivors):
        self.survivors = survivors
        self.seen_state = None

    def run_phase(self, phase, state):
        return _stalled()

    def scoring_survivors(self, state):
        self.seen_state = state
        return self.survivors


def _operation(tmp_path, backend):
    state = {"repo_path": str(tmp_path), "unit_id": "u1", "quality_mode": "ci_incremental", "phase_results": {}}
    return generation_operation(state, {"phase": TRIGGER}, {"backend": backend, "ledger": object()})


def test_eligible_survivors_route_to_review_with_what_the_turn_needs(tmp_path):
    _init_repo(tmp_path)
    backend = _Backend(_survivors("m1", "m2"))

    result = _operation(tmp_path, backend)

    evidence = result["evidence"]
    assert result["phase_outcome"] == "review_equivalence"
    assert evidence["equivalence_survivors"]["mutants"][0]["key"] == "m1"
    assert evidence["equivalence_verdicts_path"] == ".uta_cache/equivalence/u1/verdicts.json"
    assert "equivalence_tree" in evidence
    assert result["equivalence_review_timeout_seconds"] > 0
    # The language sees its own stalled measurement, not the state from before it.
    measured = backend.seen_state["phase_results"][TRIGGER]
    assert measured["evidence"]["failure_reason"] == "mutation_repair_no_progress"


def test_unproven_or_unsupported_survivors_fail_as_before(tmp_path):
    result = _operation(tmp_path, _Backend(None))
    assert result["phase_outcome"] == "failed"
    assert result["evidence"][REVIEW_EVIDENCE_KEY]["outcome"] == "ineligible"
    assert result["evidence"][REVIEW_EVIDENCE_KEY]["reason"] == "survivors_unproven"

    class Legacy:
        def run_phase(self, phase, state):
            return _stalled()

    assert _operation(tmp_path, Legacy())["phase_outcome"] == "failed"


# --- the neutral prompt and interpretation -----------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/text.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")


def _review_state(tmp_path, backend):
    _init_repo(tmp_path)
    operation = _operation(tmp_path, backend)
    return {
        "repo_path": str(tmp_path),
        "unit_id": "u1",
        "phase_results": {TRIGGER: operation},
    }


def _write_verdicts(tmp_path, *items):
    path = tmp_path / ".uta_cache/equivalence/u1/verdicts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"verdicts": list(items)}))


EQUIVALENT = {
    "key": "m1", "verdict": "equivalent",
    "divergence_region": "exactly one side empty",
    "why_indistinguishable": "both branches return 0.0 for every input in that region",
    "source_lines": [3],
}


class _Refusing:
    def render_prompt(self, phase, state):
        raise AssertionError("the review prompt is not a language's to render")

    def interpret(self, phase, state, turn):
        raise AssertionError("the review verdict is not a language's to interpret")


def test_review_prompt_is_rendered_by_the_cycle(tmp_path):
    state = _review_state(tmp_path, _Backend(_survivors("m1")))

    prompt = render_phase_prompt(REVIEW_PHASE, state, _Refusing())

    assert "m1" in prompt
    assert ".uta_cache/equivalence/u1/verdicts.json" in prompt


def test_review_prompt_includes_authoritative_mutation_diff(tmp_path):
    exact_diff = "--- before\n+++ after\n-    enabled = False\n+    enabled = None"
    survivors = ScoringSurvivors(
        language="python",
        mutants=(
            MutantIdentity(
                "m1",
                "pkg/text.py",
                3,
                "constant_replacement",
                "survived",
                mutation_diff=exact_diff,
            ),
        ),
        unreviewed_scoring_failures=0,
        source_fingerprints={"pkg/text.py": "sha"},
        mutation_rate=90.0,
        mutation_gate=95.0,
    )
    state = _review_state(tmp_path, _Backend(survivors))

    prompt = render_phase_prompt(REVIEW_PHASE, state, _Refusing())

    assert exact_diff in prompt
    assert "authoritative mutation diff" in prompt.lower()


def test_review_prompt_uses_supported_public_behavior_as_observability_boundary(tmp_path):
    state = _review_state(tmp_path, _Backend(_survivors("m1")))

    prompt = render_phase_prompt(REVIEW_PHASE, state, _Refusing())

    assert "supported public behavior" in prompt
    assert "private or protected implementation method" in prompt
    assert "monkey-patching" in prompt
    assert "instrumentation" in prompt
    assert "documented extension point" in prompt


def test_all_equivalent_review_is_saved_but_never_passes_the_unit(tmp_path):
    state = _review_state(tmp_path, _Backend(_survivors("m1")))
    _write_verdicts(tmp_path, EQUIVALENT)
    (tmp_path / "target").mkdir()
    (tmp_path / "target/build.log").write_text("untracked output is not an edit")

    result = interpret_phase(REVIEW_PHASE, state, {"status": "completed"}, _Refusing())

    review = result["evidence"][REVIEW_EVIDENCE_KEY]
    assert result["phase_outcome"] == "failed"
    assert review["outcome"] == "all_equivalent"
    assert review["verdicts"][0]["key"] == "m1"
    assert review["verdicts_sha256"]
    assert review["survivors"]["mutants"][0]["key"] == "m1"


def test_rejections_are_saved_with_their_reason(tmp_path):
    state = _review_state(tmp_path, _Backend(_survivors("m1")))

    missing = interpret_phase(REVIEW_PHASE, state, {"status": "completed"}, _Refusing())
    assert missing["evidence"][REVIEW_EVIDENCE_KEY]["reason"] == "verdicts_invalid"

    _write_verdicts(tmp_path, {**EQUIVALENT, "verdict": "killable"})
    killable = interpret_phase(REVIEW_PHASE, state, {"status": "completed"}, _Refusing())
    assert killable["evidence"][REVIEW_EVIDENCE_KEY]["reason"] == "verdict_not_equivalent"

    _write_verdicts(tmp_path, EQUIVALENT)
    (tmp_path / "pkg/text.py").write_text("x = 2\n")
    edited = interpret_phase(REVIEW_PHASE, state, {"status": "completed"}, _Refusing())
    assert edited["phase_outcome"] == "failed"
    assert edited["evidence"][REVIEW_EVIDENCE_KEY]["reason"] == "review_modified_workspace"


# --- the whole cycle ---------------------------------------------------------


class _ReviewingBackend(ScriptedBackend):
    def run_phase(self, phase, state):
        self.ran.append(phase)
        if phase == TRIGGER:
            return _stalled()
        return {"phase_outcome": self._outcome(phase)}

    def scoring_survivors(self, state):
        return _survivors("m1")


class _VerdictWritingRunner(ScriptedRunner):
    def __init__(self, repo):
        super().__init__()
        self.repo = repo

    def run_turn(self, **kwargs):
        path = self.repo / ".uta_cache/equivalence/Target/verdicts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"verdicts": [EQUIVALENT]}))
        return super().run_turn(**kwargs)


def test_the_cycle_routes_a_stalled_mutation_through_one_review_to_completion(tmp_path):
    _init_repo(tmp_path)
    backend = _ReviewingBackend()
    runner = _VerdictWritingRunner(tmp_path)

    state = _run_ci_cycle(tmp_path, context_for(backend, runner=runner))

    assert REVIEW_PHASE not in backend.prompted
    assert REVIEW_PHASE not in backend.interpreted
    assert runner.calls >= 1
    assert backend.ran[-1] == "complete_generation"
    review = state["phase_results"][REVIEW_PHASE]["evidence"][REVIEW_EVIDENCE_KEY]
    assert review["outcome"] == "all_equivalent"


def test_a_workspace_git_cannot_describe_is_not_reviewed(tmp_path):
    """Without HEAD the tracked-tree check would compare two empty answers."""
    result = _operation(tmp_path, _Backend(_survivors("m1")))

    assert result["phase_outcome"] == "failed"
    assert result["evidence"][REVIEW_EVIDENCE_KEY]["reason"] == "workspace_unverifiable"


def test_rendering_the_prompt_removes_a_previous_turns_verdicts(tmp_path):
    state = _review_state(tmp_path, _Backend(_survivors("m1")))
    _write_verdicts(tmp_path, EQUIVALENT)

    render_phase_prompt(REVIEW_PHASE, state, _Refusing())

    assert not (tmp_path / ".uta_cache/equivalence/u1/verdicts.json").exists()


def test_verdicts_that_are_a_symlink_or_oversized_are_rejected(tmp_path):
    state = _review_state(tmp_path, _Backend(_survivors("m1")))
    outside = tmp_path.parent / f"{tmp_path.name}-outside.json"
    outside.write_text(json.dumps({"verdicts": [EQUIVALENT]}))
    link = tmp_path / ".uta_cache/equivalence/u1/verdicts.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    linked = interpret_phase(REVIEW_PHASE, state, {"status": "completed"}, _Refusing())
    assert linked["evidence"][REVIEW_EVIDENCE_KEY]["reason"] == "verdicts_invalid"

    link.unlink()
    link.write_text(json.dumps({"verdicts": [EQUIVALENT], "padding": "x" * (2 * 1024 * 1024)}))
    oversized = interpret_phase(REVIEW_PHASE, state, {"status": "completed"}, _Refusing())
    assert oversized["evidence"][REVIEW_EVIDENCE_KEY]["reason"] == "verdicts_invalid"


def _run_ci_cycle(tmp_path, context):
    """`run_cycle`, for a CI repair unit: only those are reviewed."""
    root = tmp_path / "prompt-state"
    root.mkdir(mode=0o700, exist_ok=True)
    scope = PromptArtifactScope(root=root, run_id="run-1", task_id=1, managed=True)
    graph = build_cycle_workflow(context={**context, "prompt_artifact_scope": scope})
    return graph.with_config(recursion_limit=200).invoke(
        {**initial_state(tmp_path), "quality_mode": "ci_incremental"}
    )

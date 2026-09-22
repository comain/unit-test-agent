"""What the generation cycle carries between its nodes.

Deliberately small, and deliberately all identifiers and evidence. Everything
here is checkpointed by LangGraph after every node, so two rules apply:

* **It must serialize.** A live object on state — a backend, a session, an open
  file — fails to checkpoint, and the failure surfaces inside the graph
  machinery rather than where someone put it there.
* **It must be worth restoring.** State is what a resumed run comes back to.
  A value that cannot be trusted after a crash does not belong here; it
  belongs in the ledger, which can prove it.

Live objects travel in `context` instead, which is passed at build time and
never persisted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class CycleState(TypedDict, total=False):
    """One target's journey through the cycle."""

    # -- identity: what this run is about ------------------------------------
    repo_path: str
    task_db_path: str
    task_id: str
    unit_id: str
    language: str
    workflow_run_id: str
    source_commit: str
    batch: List[str]
    module: Optional[str]
    coverage_gate: int
    mutation_gate: int
    timeout_seconds: int
    planning_timeout_seconds: int
    compile_fix_timeout_seconds: int
    repair_timeout_seconds: int
    quality_mode: str
    quality_gate_backend: str
    quality_gate_command: str
    rdc_context: Dict[str, Any]
    results: Dict[str, Any]
    session_ids: List[str]
    session_refs: List[Dict[str, str]]
    target_context_paths: Dict[str, Dict[str, str]]
    method_efforts_by_class: Dict[str, Any]
    mutation_roi_enabled: bool
    mutation_roi_skip_expensive: bool

    # -- the phase in flight -------------------------------------------------
    #: The label an operator sees, and what a resumed run comes back to.
    current_phase: str
    #: `run`, `reuse_result`, `adopt_result`, … — the ledger's classification
    #: of an interrupted operation.
    reconciliation: str
    operation_id: str
    operation_step: str
    operation_input_fingerprint: str
    logical_attempt: int
    execution_ordinal: int
    prerequisite_operation_ids: List[str]
    #: Whether a restored envelope was actually usable.
    rehydration: str
    #: The phase's verdict: passed, repair, retry, exhausted, skipped, failed.
    phase_outcome: str

    # -- the turn ------------------------------------------------------------
    #: Written by `generation_prompt`, read by the shared `agent_turn`.
    prompt_file: str
    #: Safe identity-only metadata paired atomically with ``prompt_file``.
    prompt_inputs_file: str
    #: Manifest-last completion marker for the prompt bundle.
    prompt_manifest_file: str
    #: The normalized, JSON-safe `AgentTurnResult` as a mapping. Never the
    #: provider's own result object, which would not survive a checkpoint.
    turn_result: Dict[str, Any]
    turn_status: str
    turn_text: str
    turn_session_id: Optional[str]
    turn_session_refs: List[Dict[str, str]]
    turn_usage: Dict[str, Any]
    turn_diagnostics: Dict[str, Any]
    turn_retrospective: Dict[str, Any]
    turn_patch_count: int
    turn_recovered: bool
    turn_elapsed_seconds: float
    turn_raw_log_path: Optional[str]
    turn_history: List[Dict[str, Any]]

    # -- accumulated evidence ------------------------------------------------
    #: Per-phase results, keyed by phase label, for the report and for
    #: reconciling a resumed run against what already happened.
    phase_results: Dict[str, Any]
    #: How many repair attempts each phase has spent. This is what bounds a
    #: repair loop -- the graph's shape never does.
    attempts: Dict[str, int]
    attempts_by_phase: Dict[str, int]
    max_attempts_by_phase: Dict[str, int]
    #: Best score seen so far per phase, per target. This is what makes the
    #: coverage and mutation repair loops heuristic rather than fixed-length:
    #: a round that improves on the best earns another, and one that does not
    #: ends the loop even with attempts left.
    best_scores_by_phase: Dict[str, Dict[str, float]]
    no_progress_by_phase: Dict[str, int]
    #: Bound on the equivalent-mutant review turn, read by its `agent_turn`.
    equivalence_review_timeout_seconds: int
    repair_feedback: str
    effort_strategy: str
    model_id: str | None
    #: Paths the cycle has written, so a guard can tell an expected edit from
    #: one nobody asked for.
    changed_paths: List[str]
    context_dir: str
    project_prompt_paths: Dict[str, Any]
    complexity_by_class: Dict[str, Any]
    strict_coverage_classes: List[Dict[str, Any]]
    roi_enabled: bool
    ci_diff_coverage_gate: int
    ci_diff_mutation_gate: int
    plan_index_query_command: str
    generation_index_query_command: str
    spec_context: str
    stop_after_stage: Optional[str]
    phase_timings: Dict[str, float]
    session_token_usage: Dict[str, Any]
    phase_token_usage: Dict[str, Any]
    session_retrospect: Dict[str, Any]
    session_diagnostics: Dict[str, Any]
    session_patch_count: int
    target: Dict[str, Any]
    generated_test_path: str
    #: Each phase's declared output paths as they were *before* it ran, from
    #: the ledger's reconciliation decision. This is how a phase tells whether
    #: it produced anything: sampling after the turn cannot distinguish the
    #: agent's own new file from one that was always there.
    #:
    #: Declared here because LangGraph drops any key this schema does not
    #: name -- a node can return it and it silently never arrives.
    output_fingerprints_before: Dict[str, str]
    allow_existing_non_uta: bool
    existing_test_path: str
    existing_test_reasons: List[str]
    context_payload: Dict[str, Any]
    index_query_command: str
    side_effect_hints: List[str]
    companion_files: List[str]
    changed_lines: Dict[str, Any]
    base_ref: str

    # -- outcome -------------------------------------------------------------
    error: Optional[str]
    terminal_reason: Optional[str]
    stopped_early: bool


__all__ = ["CycleState"]

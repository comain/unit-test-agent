from typing import Any, Dict, List, Optional, TypedDict

class AgentState(TypedDict):
    # Inputs
    repo_path: str
    language: str
    language_decision: Dict[str, Any]
    module: Optional[str]
    module_filter: Optional[str]
    days: int
    max_files: int
    select_all_files: bool
    explicit_class_fqns: List[str]
    explicit_targets: List[Any]
    coverage_gate: int
    mutation_gate: int
    quality_mode: str
    quality_gate_backend: str
    quality_gate_command: str
    rdc_context: Dict[str, Any]
    classes_per_agent_run: int  # Number of classes per OpenCode generation session
    branch_name: str
    started_at: float
    stop_after_stage: Optional[str]
    resume: bool
    preserve_branch: bool

    # intermediate data
    candidates: List[str]  # Compatibility list of target IDs
    target_candidates: List[Dict[str, Any]]
    current_target: Optional[Dict[str, Any]]
    current_target_batch: List[Dict[str, Any]]
    # Java compatibility aliases. Neutral workflow code must consume the
    # normalized target fields above rather than these class-oriented fields.
    current_class: Optional[str]
    current_batch: List[str]
    
    # Code context
    graph: Optional[Any]
    flows: List[Any]
    
    # OpenCode session
    session_id: Optional[str]
    session_ids: List[str]
    session_refs: List[Dict[str, str]]
    
    # Results
    results: Dict[str, Dict[str, Any]] # fqn -> {status, coverage, mutation_score}
    phase_timings: Dict[str, float]
    phase_token_usage: Dict[str, Any]
    session_retrospect: Dict[str, Any]
    session_diagnostics: Dict[str, Any]
    session_patch_count: int
    session_token_usage: Dict[str, Any]
    current_stage: str
    deterministic_change_paths: List[str]
    # Paths already dirty in the workspace when the run started (residue from a
    # reused/preserved CI workspace). Used to distinguish test files the repair
    # actually authored this run from pre-existing leftovers at commit time.
    run_initial_dirty_paths: List[str]
    generation_cycle: Dict[str, Any]

    # Pipeline control
    error: Optional[str]
    finished: bool
    stopped_early: bool

    # Production task tracking (optional; None/False disables DB updates)
    production: bool
    task_id: Optional[int]
    task_db_path: Optional[str]
    run_log_path: Optional[str]
    backend_context: Dict[str, Any]

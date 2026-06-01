from typing import List, Dict, Optional, TypedDict, Any
from uta.language.java.parse.models import CodeGraph, ProcessFlow

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
    ci_context: Dict[str, Any]
    classes_per_agent_run: int  # Number of classes per OpenCode generation session
    branch_name: str
    started_at: float
    stop_after_stage: Optional[str]
    resume: bool
    preserve_branch: bool

    # intermediate data
    candidates: List[str] # List of class FQNs
    target_candidates: List[Dict[str, Any]]
    current_class: Optional[str]
    current_batch: List[str]  # Current batch of class FQNs to process together
    current_target: Optional[Dict[str, Any]]
    current_target_batch: List[Dict[str, Any]]
    
    # Code context
    graph: Optional[CodeGraph]
    flows: List[ProcessFlow]
    
    # OpenCode session
    session_id: Optional[str]
    session_ids: List[str]
    
    # Results
    results: Dict[str, Dict[str, Any]] # fqn -> {status, coverage, mutation_score}
    phase_timings: Dict[str, float]
    phase_token_usage: Dict[str, Any]
    session_retrospect: Dict[str, Any]
    session_token_usage: Dict[str, Any]
    current_stage: str
    deterministic_change_paths: List[str]
    
    # Pipeline control
    error: Optional[str]
    finished: bool
    stopped_early: bool

    # Production task tracking (optional; None/False disables DB updates)
    production: bool
    task_id: Optional[int]
    task_db_path: Optional[str]
    run_log_path: Optional[str]

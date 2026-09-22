"""Compatibility exports for the shared Python mutation planner."""

from __future__ import annotations

from uta_py_enforce.mutation_candidates import (
    PYTHON_OPERATOR_POLICY_VERSION,
    PYTHON_SUPPRESSION_POLICY_VERSION,
    PythonMutationOpportunityPlan,
    build_mutmut3_candidate_plan_from_meta,
    collect_python_mutation_opportunities,
    low_value_side_effect_lines,
    select_one_opportunity_per_line,
)

__all__ = [
    "PYTHON_OPERATOR_POLICY_VERSION",
    "PYTHON_SUPPRESSION_POLICY_VERSION",
    "PythonMutationOpportunityPlan",
    "build_mutmut3_candidate_plan_from_meta",
    "collect_python_mutation_opportunities",
    "low_value_side_effect_lines",
    "select_one_opportunity_per_line",
]

"""Stable import surface for Python verification.

This module used to be the whole verifier. It is now the facade over the
modules it was split into: ``orchestration`` sequences a run, ``runtime_setup``
and ``coverage_execution`` and ``mutation_phase`` are its phases, and
``models``, ``runtime_config``, ``process``, ``pytest_execution``,
``mutation_policy``, ``mutation_scoping``, ``mutation_execution`` and
``results`` hold the responsibilities underneath them.

The names below are re-exported because callers and tests across the tree
import them from here. ``__all__`` records that as a contract rather than an
accident, and lets the linter tell a re-export from a dead import. New code
should import from the owning module; this facade exists so the split did not
have to churn every caller at once.
"""

from __future__ import annotations

from uta_py_enforce.mutation_workspace import mutation_support_copy_paths as _mutmut_support_copy_paths
from uta_py_enforce.mutmut_adapter import adapter_command as _shared_mutmut_adapter_command

from uta.language.python.verification.candidate_planning import (
    _filter_generation_policy_to_lines,
    _mutation_units_from_generation_policy,
)
from uta.language.python.verification.coverage_execution import _coverage_include_patterns
from uta.language.python.verification.evidence import (
    CoverageSummary,
    MutationSummary,
    parse_coverage_xml,
    parse_mutmut_summary,
    parse_mutmut_survivors,
)
from uta.language.python.verification.models import (
    CommandEvidence,
    PythonRuntimeConfig,
    PythonVerificationResult,
    RunCommand,
)
from uta.language.python.verification.mutation_execution import (
    _aggregate_mutation_summaries,
    _annotate_mutmut_survivor_diffs,
    _batch_execution_failure,
    _finalize_batched_outcome,
)
from uta.language.python.verification.mutation_scoping import (
    _empty_mutation_summary,
    _filter_changed_lines_for_mutation,
    _reconcile_no_test_association,
)
from uta.language.python.verification.mutmut_runtime import (
    _clean_generated_mutants,
    _cleanup_mutation_state,
    _mutmut_config_overlay,
    _mutmut_generate_metadata_command,
    _mutmut_runner_command,
    _write_mutmut_import_compat,
)
from uta.language.python.verification.orchestration import verify_python_target
from uta.language.python.verification.process import (
    _run_command,
    _runner_with_env,
    _subprocess_run,
)
from uta.language.python.verification.pytest_execution import (
    _prepare_pytest_execution_context,
)
from uta.language.python.verification.results import (
    precheck_python_large_change,
    precheck_python_target_runtime_incompatibility,
)
from uta.language.python.verification.runtime_config import resolve_python_runtime_config
from uta.language.python.verification.runtime_setup import UTA_OWNED_MUTMUT_VERSION


__all__ = [
    "CommandEvidence",
    "CoverageSummary",
    "MutationSummary",
    "PythonRuntimeConfig",
    "PythonVerificationResult",
    "RunCommand",
    "UTA_OWNED_MUTMUT_VERSION",
    "_aggregate_mutation_summaries",
    "_annotate_mutmut_survivor_diffs",
    "_batch_execution_failure",
    "_clean_generated_mutants",
    "_cleanup_mutation_state",
    "_coverage_include_patterns",
    "_empty_mutation_summary",
    "_filter_changed_lines_for_mutation",
    "_filter_generation_policy_to_lines",
    "_finalize_batched_outcome",
    "_mutation_units_from_generation_policy",
    "_mutmut_config_overlay",
    "_mutmut_generate_metadata_command",
    "_mutmut_runner_command",
    "_mutmut_support_copy_paths",
    "_prepare_pytest_execution_context",
    "_reconcile_no_test_association",
    "_run_command",
    "_runner_with_env",
    "_shared_mutmut_adapter_command",
    "_subprocess_run",
    "_write_mutmut_import_compat",
    "parse_coverage_xml",
    "parse_mutmut_summary",
    "parse_mutmut_survivors",
    "precheck_python_large_change",
    "precheck_python_target_runtime_incompatibility",
    "resolve_python_runtime_config",
    "verify_python_target",
]

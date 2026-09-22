"""Project-level documentation for agent prompts.

Compatibility facade delegating to modular implementations under `uta.testgen.project_summary`.
"""

from __future__ import annotations

import logging
import subprocess

from agent_core.harness.lifecycle import bootstrap_harness_workspace

from uta.testgen.project_summary.bootstrap import (
    _extract_init_text,
    _harvest_opencode_init_artifacts,
    _has_authoritative_repo_summary,
    _is_meaningful_init_output,
    _is_uta_generated_summary,
    _read_text,
    _write_opencode_init_output,
    maybe_run_opencode_init_slash,
    maybe_run_project_bootstrap,
    maybe_run_project_init_command,
)
from uta.testgen.project_summary.constants import (
    COMPILE_FACTS_FILENAME,
    CONTEXT_SUMMARY_FILENAME,
    OPENCODE_INIT_MERGE_HEADER,
    OPENCODE_INIT_OUTPUT_FILENAME,
    REPO_SUMMARY_FILENAME,
    SESSION_RETROSPECT_FILENAME,
    STAGE_INTROSPECT_FILENAME,
    TEST_GUIDANCE_FILENAME,
    UTA_GENERATED_MARKER,
)
from uta.testgen.project_summary.guidance import (
    _build_test_generation_guidance_markdown,
    _discover_nearby_api_repos,
    _extract_pom_coords,
    _infer_test_patterns,
    _list_modules,
    _sample_test_files,
)
from uta.testgen.project_summary.introspect import (
    _safe_stage_name,
    append_stage_introspect,
    ensure_stage_introspect_file,
    merge_compile_fix_facts,
    stage_introspect_path,
    write_session_retrospect,
)
from uta.testgen.project_summary.summaries import (
    _build_context_summary_markdown,
    _build_repo_summary_markdown,
    _graph_stats,
    prompt_template_paths,
    sync_project_summaries,
)

logger = logging.getLogger("uta")

__all__ = [
    "COMPILE_FACTS_FILENAME",
    "CONTEXT_SUMMARY_FILENAME",
    "OPENCODE_INIT_MERGE_HEADER",
    "OPENCODE_INIT_OUTPUT_FILENAME",
    "REPO_SUMMARY_FILENAME",
    "SESSION_RETROSPECT_FILENAME",
    "STAGE_INTROSPECT_FILENAME",
    "TEST_GUIDANCE_FILENAME",
    "UTA_GENERATED_MARKER",
    "_build_context_summary_markdown",
    "_build_repo_summary_markdown",
    "_build_test_generation_guidance_markdown",
    "_discover_nearby_api_repos",
    "_extract_init_text",
    "_extract_pom_coords",
    "_graph_stats",
    "_harvest_opencode_init_artifacts",
    "_has_authoritative_repo_summary",
    "_infer_test_patterns",
    "_is_meaningful_init_output",
    "_is_uta_generated_summary",
    "_list_modules",
    "_read_text",
    "_safe_stage_name",
    "_sample_test_files",
    "_write_opencode_init_output",
    "append_stage_introspect",
    "bootstrap_harness_workspace",
    "ensure_stage_introspect_file",
    "logger",
    "maybe_run_opencode_init_slash",
    "maybe_run_project_bootstrap",
    "maybe_run_project_init_command",
    "merge_compile_fix_facts",
    "prompt_template_paths",
    "stage_introspect_path",
    "subprocess",
    "sync_project_summaries",
    "write_session_retrospect",
]

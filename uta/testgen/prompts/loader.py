"""Prompt loading and stable/volatile splitting for prompt analysis.

Templates may contain a `{# CACHE_BOUNDARY #}` Jinja comment marker.
Everything before the marker is the *stable prefix* (cacheable across calls);
everything after is the *volatile tail* (per-call, must not be cached).

`render_prompt_split` returns the two regions independently so callers can
measure and analyze stable-vs-volatile prompt composition. Current OpenCode
runtime behavior sends the concatenated prompt as one payload.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Dict, Literal, Protocol, Tuple

from agent_core.prompts import PromptLibrary

CACHE_BOUNDARY_MARKER = "{# CACHE_BOUNDARY #}"

_PROMPTS_DIR = Path(__file__).parent
_PROMPT_LIBRARY = PromptLibrary(_PROMPTS_DIR, cache_source_text=True)
# This sentinel is deliberately not valid template text.  Rendering the whole
# source as one section preserves the historical ``Template(raw).render``
# newline behavior, which differs by one LF from independently rendered halves.
_NO_BOUNDARY_SENTINEL = "\0"
_PRODUCTION_EDIT_PATTERNS = [
    re.compile(r"\bsrc/main/java\b", re.IGNORECASE),
    re.compile(r"\bproduction\s+code\b", re.IGNORECASE),
    re.compile(r"\bmodify\s+prod(?:uction)?\b", re.IGNORECASE),
    re.compile(r"\bedit\s+prod(?:uction)?\b", re.IGNORECASE),
    re.compile(r"修改.*生产代码"),
    re.compile(r"修改.*业务代码"),
]


class _RenderView(Protocol):
    """The sole compatibility surface retained for the deprecated loaders."""

    def render(self, **kwargs: Any) -> str: ...


class _PromptRenderView:
    __slots__ = ("_name", "_section")

    def __init__(self, name: str, section: Literal["full", "stable", "volatile"]):
        self._name = name
        self._section = section

    def render(self, **kwargs: Any) -> str:
        values = _with_prompt_defaults(kwargs)
        if self._section == "full":
            return _render_full(self._name, values)
        stable, volatile = _render_sections(self._name, values)
        return stable if self._section == "stable" else volatile


def load_prompt(name: str) -> _RenderView:
    """Return the deprecated render-only view for a full prompt."""
    return _PromptRenderView(_template_name(name), "full")


def load_prompt_split(name: str) -> Tuple[_RenderView, _RenderView]:
    """Return deprecated render-only stable and volatile views.

    If the prompt contains no CACHE_BOUNDARY marker, the entire prompt is
    treated as volatile (empty stable prefix).
    """
    template_name = _template_name(name)
    return (
        _PromptRenderView(template_name, "stable"),
        _PromptRenderView(template_name, "volatile"),
    )


def render_prompt(name: str, **kwargs) -> str:
    kwargs = _with_prompt_defaults(kwargs)
    return _render_full(_template_name(name), kwargs)


def render_prompt_split(name: str, **kwargs) -> Tuple[str, str]:
    """Render the stable and volatile regions independently.

    The same kwargs are passed to both halves; a kwarg only used in one half
    is silently ignored by the other (Jinja default behavior).
    """
    kwargs = _with_prompt_defaults(kwargs)
    return _render_sections(_template_name(name), kwargs)


def _render_full(name: str, values: Dict[str, Any]) -> str:
    rendered = _PROMPT_LIBRARY.render_sections(
        name,
        boundary=_NO_BOUNDARY_SENTINEL,
        keep_trailing_newline=False,
        values=values,
    )
    return rendered.text


def _render_sections(name: str, values: Dict[str, Any]) -> Tuple[str, str]:
    rendered = _PROMPT_LIBRARY.render_sections(
        name,
        boundary=CACHE_BOUNDARY_MARKER,
        keep_trailing_newline=False,
        values=values,
    )
    return rendered.stable, rendered.volatile


def _with_prompt_defaults(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(kwargs)
    for key, value in _OPTIONAL_PROMPT_DEFAULTS.items():
        data.setdefault(key, value.copy() if isinstance(value, (dict, list)) else value)
    rdc_context_abs = data.get("rdc_context_abs") or os.environ.get("UTA_RDC_CONTEXT_PATH") or ""
    if rdc_context_abs:
        _validate_rdc_context_for_unit_test_repair(str(rdc_context_abs))
        data["rdc_context_abs"] = str(rdc_context_abs)
    else:
        data.setdefault("rdc_context_abs", "")
    return data


_OPTIONAL_PROMPT_DEFAULTS: Dict[str, Any] = {
    "quality_mode": "class_batch",
    "ci_diff_coverage_gate": 95,
    "ci_diff_mutation_gate": 100,
    "spec_context": "",
    "stage_introspect_abs": "",
    "mockito_api_guidance": "",
    "repo_summary_abs": "",
    "repo_summary_exists": False,
    "context_summary_abs": "",
    "test_guidance_abs": "",
    "compile_facts_abs": "",
    "compile_facts_exists": False,
    "wave_one_only": False,
    "symbol": "",
    "existing_test_path": "",
    "existing_test_reasons": [],
    "companion_files": [],
    "side_effect_hints": [],
    "changed_line_hints": [],
    "scored_methods": [],
    "prior_hints": [],
    "generated_test_path": "",
    "test_file_path": "",
    "context_abs": "",
    "target_context_abs": "",
    "coverage_diagnostics": "",
    "coverage_report": "",
    "mutation_diagnostics": "",
    "mutation_report": "",
    "mutation_repair_context_abs": "",
    "mutation_repair_roi_guided_full": False,
    "mutation_repair_split": False,
    "mutation_repair_group_abs": "",
    "mutation_repair_group": "",
}


def _template_name(name: str) -> str:
    return name if name.endswith(".txt") else f"{name}.txt"


def _validate_rdc_context_for_unit_test_repair(path: str) -> None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for pattern in _PRODUCTION_EDIT_PATTERNS:
        if pattern.search(text):
            raise ValueError("RDC context requests production-code edits, which are unsupported for unit-test repair")

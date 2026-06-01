"""Prompt loading and stable/volatile splitting for prompt analysis.

Templates may contain a `{# CACHE_BOUNDARY #}` Jinja comment marker.
Everything before the marker is the *stable prefix* (cacheable across calls);
everything after is the *volatile tail* (per-call, must not be cached).

`render_prompt_split` returns the two regions independently so callers can
measure and analyze stable-vs-volatile prompt composition. Current OpenCode
runtime behavior sends the concatenated prompt as one payload.
"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import re
from typing import Any, Dict, Tuple

from jinja2 import Template

CACHE_BOUNDARY_MARKER = "{# CACHE_BOUNDARY #}"

_PROMPTS_DIR = Path(__file__).parent
_PRODUCTION_EDIT_PATTERNS = [
    re.compile(r"\bsrc/main/java\b", re.IGNORECASE),
    re.compile(r"\bproduction\s+code\b", re.IGNORECASE),
    re.compile(r"\bmodify\s+prod(?:uction)?\b", re.IGNORECASE),
    re.compile(r"\bedit\s+prod(?:uction)?\b", re.IGNORECASE),
    re.compile(r"修改.*生产代码"),
    re.compile(r"修改.*业务代码"),
]


def _prompt_path(name: str) -> Path:
    if not name.endswith(".txt"):
        name = f"{name}.txt"
    return _PROMPTS_DIR / name


@lru_cache(maxsize=None)
def _read_prompt(name: str) -> str:
    return _prompt_path(name).read_text(encoding="utf-8")


def load_prompt(name: str) -> Template:
    """Return a Jinja Template for the named prompt (single-region)."""
    return Template(_read_prompt(name))


def load_prompt_split(name: str) -> Tuple[Template, Template]:
    """Return (stable_prefix_template, volatile_tail_template).

    If the prompt contains no CACHE_BOUNDARY marker, the entire prompt is
    treated as volatile (empty stable prefix).
    """
    raw = _read_prompt(name)
    if CACHE_BOUNDARY_MARKER not in raw:
        return Template(""), Template(raw)
    stable_str, _, volatile_str = raw.partition(CACHE_BOUNDARY_MARKER)
    return Template(stable_str), Template(volatile_str)


def render_prompt(name: str, **kwargs) -> str:
    kwargs = _with_prompt_defaults(kwargs)
    return load_prompt(name).render(**kwargs)


def render_prompt_split(name: str, **kwargs) -> Tuple[str, str]:
    """Render the stable and volatile regions independently.

    The same kwargs are passed to both halves; a kwarg only used in one half
    is silently ignored by the other (Jinja default behavior).
    """
    kwargs = _with_prompt_defaults(kwargs)
    stable_t, volatile_t = load_prompt_split(name)
    return stable_t.render(**kwargs), volatile_t.render(**kwargs)


def _with_prompt_defaults(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(kwargs)
    data.setdefault("quality_mode", "class_batch")
    data.setdefault("ci_diff_coverage_gate", 95)
    data.setdefault("ci_diff_mutation_gate", 100)
    ci_context_abs = data.get("ci_context_abs") or os.environ.get("UTA_CI_CONTEXT_PATH") or ""
    if ci_context_abs:
        _validate_ci_context_for_unit_test_repair(str(ci_context_abs))
        data["ci_context_abs"] = str(ci_context_abs)
    else:
        data.setdefault("ci_context_abs", "")
    return data


def _validate_ci_context_for_unit_test_repair(path: str) -> None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for pattern in _PRODUCTION_EDIT_PATTERNS:
        if pattern.search(text):
            raise ValueError("CI context requests production-code edits, which are unsupported for unit-test repair")

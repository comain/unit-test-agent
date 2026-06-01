"""Language-neutral Wave-1 / Wave-2 callable prioritization.

Wave assignment is a workflow concern: planning should focus first on
high-value callables regardless of whether the backend target is Java, Python,
or a later language. Language providers may expose different markdown sections,
but this module owns the normalized prioritization contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

_WAVE1_VERBS = frozenset(
    {
        "create",
        "process",
        "handle",
        "execute",
        "validate",
        "calculate",
        "compute",
        "save",
        "update",
        "delete",
        "build",
        "init",
        "initialize",
        "load",
        "run",
        "send",
        "apply",
        "check",
        "verify",
        "resolve",
        "transform",
        "parse",
        "convert",
        "fetch",
        "query",
        "filter",
        "merge",
        "assign",
        "allocate",
        "dispatch",
    }
)
_ACCESSOR_RE = re.compile(r"^(?:get|set|is|has)[A-Z]")
_METHOD_SECTION_RE = re.compile(r"## Public Methods\n(.*?)(?=\n##|\Z)", re.DOTALL)
_SYMBOL_SECTION_RE = re.compile(r"## Symbols\n(.*?)(?=\n##|\Z)", re.DOTALL)
_BACKTICK_SYMBOL_RE = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class MethodWave:
    name: str
    wave: int
    reason: str


def assign_waves(callable_names: Iterable[str]) -> List[MethodWave]:
    """Assign deterministic planning waves for normalized callable names."""
    seen: Dict[str, MethodWave] = {}
    for raw_name in callable_names:
        name = _normalize_callable_name(raw_name)
        if not name or name in seen:
            continue
        wave, reason = _callable_name_wave(name)
        seen[name] = MethodWave(name=name, wave=wave, reason=reason)
    return sorted(seen.values(), key=lambda item: (item.wave, item.name))


def assign_waves_from_context(context_md: str) -> List[MethodWave]:
    """Extract Java/Python callable names from context markdown and assign waves."""
    names: List[str] = []
    method_section = _METHOD_SECTION_RE.search(context_md)
    if method_section:
        names.extend(_extract_java_method_names(method_section.group(1)))
    symbol_section = _SYMBOL_SECTION_RE.search(context_md)
    if symbol_section:
        names.extend(_extract_symbol_names(symbol_section.group(1)))
    return assign_waves(names)


def format_wave_table(waves: List[MethodWave]) -> str:
    """Format wave assignments as a Markdown table for prompt injection."""
    if not waves:
        return "(no public callables found)"
    lines = ["| Method | Wave | Reason |", "|--------|------|--------|"]
    for item in waves:
        lines.append(f"| `{item.name}` | {item.wave} | {item.reason} |")
    return "\n".join(lines)


def _callable_name_wave(method_name: str) -> Tuple[int, str]:
    if _ACCESSOR_RE.match(method_name):
        return 2, "accessor"
    lower = method_name.lower()
    for verb in _WAVE1_VERBS:
        if lower.startswith(verb) or verb in lower:
            return 1, f"business-verb:{verb}"
    return 2, "no-wave1-signal"


def _extract_java_method_names(section: str) -> List[str]:
    names: List[str] = []
    for match in _BACKTICK_SYMBOL_RE.finditer(section):
        raw = match.group(1).strip()
        if "(" not in raw:
            continue
        names.append(raw.split("(", 1)[0].strip())
    return names


def _extract_symbol_names(section: str) -> List[str]:
    names: List[str] = []
    for line in section.splitlines():
        if "(function)" not in line and "(method)" not in line:
            continue
        match = _BACKTICK_SYMBOL_RE.search(line)
        if match:
            names.append(match.group(1))
    return names


def _normalize_callable_name(raw_name: str) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return ""
    name = name.split("(", 1)[0].strip()
    name = name.rsplit(".", 1)[-1].strip()
    if "::" in name:
        name = name.rsplit("::", 1)[-1].strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return ""
    return name

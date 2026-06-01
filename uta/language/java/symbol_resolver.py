"""Resolve unresolved Java symbols to candidate source files.

Fills the gap surfaced in token_opt_phase2 strategies C (symbol write-back),
K-subset (replace LLM greps with Python), and L1 (record missing-symbol
inefficiencies).

The resolver performs two passes over ``repo_path``:
  1. Index every ``*.java`` file's ``package`` and top-level ``public class/interface/enum``.
     The index is cached per (repo_path, mtime-summary) and reused across calls.
  2. Look up a short type name; return candidate FQNs ranked by rough locality:
     - same package as the test target (if provided)
     - same Maven module as the target
     - any other match

This keeps resolution deterministic and never calls the LLM. It is intentionally
cheap — a richer tree-sitter-backed pass can replace it behind the same API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w\.]+)\s*;", re.MULTILINE)
_TOP_DECL_RE = re.compile(
    r"^\s*(?:public\s+|abstract\s+|final\s+|sealed\s+|non-sealed\s+)*"
    r"(?:class|interface|enum|record)\s+(\w+)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SymbolCandidate:
    """One candidate resolution for an unresolved short name."""

    short_name: str
    fqn: str
    path: str
    # Lower is better — 0 = same package, 1 = same module, 2 = elsewhere.
    locality: int

    @property
    def import_line(self) -> str:
        return f"import {self.fqn};"


def resolve_symbol(
    short_name: str,
    repo_path: str,
    *,
    target_package: Optional[str] = None,
    target_module: Optional[str] = None,
    limit: int = 5,
) -> List[SymbolCandidate]:
    """Return up to ``limit`` candidates for ``short_name`` ranked by locality."""
    if not short_name:
        return []

    index = _get_symbol_index(repo_path)
    matches = index.get(short_name, ())
    if not matches:
        return []

    scored: List[SymbolCandidate] = []
    for fqn, path in matches:
        package = fqn.rsplit(".", 1)[0] if "." in fqn else ""
        locality = 2
        if target_package and package == target_package:
            locality = 0
        elif target_module and (f"/{target_module}/" in path or path.startswith(target_module + "/")):
            locality = 1
        scored.append(
            SymbolCandidate(
                short_name=short_name,
                fqn=fqn,
                path=path,
                locality=locality,
            )
        )

    scored.sort(key=lambda c: (c.locality, c.fqn))
    return scored[:limit]


def resolve_symbols(
    short_names: Iterable[str],
    repo_path: str,
    *,
    target_package: Optional[str] = None,
    target_module: Optional[str] = None,
    limit_per_symbol: int = 3,
) -> Dict[str, List[SymbolCandidate]]:
    """Bulk-resolve a set of short names."""
    result: Dict[str, List[SymbolCandidate]] = {}
    for name in short_names:
        name = (name or "").strip()
        if not name or name in result:
            continue
        result[name] = resolve_symbol(
            name,
            repo_path,
            target_package=target_package,
            target_module=target_module,
            limit=limit_per_symbol,
        )
    return result


def invalidate_cache(repo_path: Optional[str] = None) -> None:
    """Drop the memoized symbol index.

    Pass ``repo_path`` to invalidate a single repo; pass ``None`` for all.
    """
    if repo_path is None:
        _get_symbol_index.cache_clear()
    else:
        # lru_cache has no per-key eviction; the simplest safe option is to clear.
        _get_symbol_index.cache_clear()


@lru_cache(maxsize=16)
def _get_symbol_index(repo_path: str) -> Dict[str, Tuple[Tuple[str, str], ...]]:
    """Index every Java class/interface/enum under ``repo_path``.

    Returns ``{short_name: ((fqn, relpath), ...)}``.
    """
    root = Path(repo_path)
    if not root.exists():
        return {}

    skipped_parts = {"target", "build", ".git", ".idea", ".uta_cache", "node_modules"}
    scratch_re = re.compile(r"^test_.*\.java$")

    index: Dict[str, List[Tuple[str, str]]] = {}
    for java_file in root.rglob("*.java"):
        parts = set(java_file.relative_to(root).parts)
        if parts & skipped_parts:
            continue
        if scratch_re.match(java_file.name):
            continue

        try:
            text = java_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        pkg_match = _PACKAGE_RE.search(text)
        package = pkg_match.group(1) if pkg_match else ""
        for decl in _TOP_DECL_RE.finditer(text):
            short_name = decl.group(1)
            fqn = f"{package}.{short_name}" if package else short_name
            rel = str(java_file.relative_to(root))
            index.setdefault(short_name, []).append((fqn, rel))

    return {k: tuple(v) for k, v in index.items()}


def format_candidates_markdown(
    resolutions: Dict[str, Sequence[SymbolCandidate]],
) -> str:
    """Render resolutions as a markdown table ready to append to compile_context.md."""
    if not resolutions:
        return ""
    lines = ["| Short name | Candidate FQN | Path | Locality |", "|---|---|---|---|"]
    for short_name, candidates in resolutions.items():
        if not candidates:
            lines.append(f"| `{short_name}` | _no candidates_ |  |  |")
            continue
        for cand in candidates:
            locality = {0: "same-pkg", 1: "same-module", 2: "other"}.get(cand.locality, str(cand.locality))
            lines.append(f"| `{short_name}` | `{cand.fqn}` | `{cand.path}` | {locality} |")
    return "\n".join(lines) + "\n"

"""Language-neutral git-diff helpers for changed files and changed lines.

Both backends and the CI/enforcement paths need "what changed since base_ref"
and "which lines changed" — the git invocation and unified-diff parsing are
identical across languages; only the production-file filter differs, and that
already lives on each ``LanguageAdapter.is_production_source_path``. Centralizing
the mechanics here lets any backend (and the e2e harness) reuse them instead of
re-implementing or reaching into a backend's private functions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Sequence

from uta.enforcement.enforcement import git_output

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_paths(repo: Path, base_ref: str) -> List[str]:
    """All repo-relative paths changed between ``base_ref`` and HEAD."""
    output = git_output(Path(repo), "diff", "--name-only", f"{base_ref}...HEAD")
    if not output:
        return []
    return [path.strip() for path in output.splitlines() if path.strip()]


def changed_production_files(adapter, repo: Path, base_ref: str) -> List[str]:
    """Changed paths filtered to one language's production sources."""
    return [path for path in changed_paths(repo, base_ref) if adapter.is_production_source_path(path)]


def changed_lines_by_file(repo: Path, base_ref: str, paths: Sequence[str]) -> Dict[str, List[int]]:
    """Map each path to the line numbers added/changed vs ``base_ref``."""
    changed: Dict[str, List[int]] = {}
    for path in paths:
        normalized = str(path or "").strip().replace("\\", "/")
        if not normalized:
            continue
        output = git_output(Path(repo), "diff", "--unified=0", f"{base_ref}...HEAD", "--", normalized)
        changed[normalized] = sorted(parse_added_diff_lines(output))
    return changed


def parse_added_diff_lines(diff_text: str) -> set[int]:
    """Line numbers of added (`+`) lines from a ``--unified=0`` diff."""
    lines: set[int] = set()
    current_line = None
    for raw_line in str(diff_text or "").splitlines():
        hunk = _HUNK_RE.match(raw_line)
        if hunk:
            current_line = int(hunk.group(1))
            continue
        if current_line is None:
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            lines.add(current_line)
            current_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            continue
        else:
            current_line += 1
    return lines

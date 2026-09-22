"""Target normalization, git changed lines derivation, and non-executable short-circuiting."""

from __future__ import annotations

import io
from pathlib import Path
import token
import tokenize
from typing import Any, Sequence

from uta_enforce_core.contracts import EnforcementTarget
from uta_enforce_core.diff import changed_lines_by_file, changed_paths, is_production_python_path
from uta_enforce_core.targets import target_source_path


def resolve_enforcement_targets(
    repo: Path,
    targets: Sequence[EnforcementTarget | str],
    *,
    base_ref: str = "origin/master",
) -> list[EnforcementTarget]:
    """Resolve and expand requested targets, falling back to git diff changed production files if empty."""
    repo = Path(repo).resolve()
    if targets:
        result: list[EnforcementTarget] = []
        for item in targets:
            if isinstance(item, EnforcementTarget):
                result.append(item)
            else:
                source_path = target_source_path(str(item))
                result.append(
                    EnforcementTarget(
                        language="python",
                        target_id=f"pyfile:{source_path}",
                        source_path=source_path,
                        display_name=source_path,
                    )
                )
        return result

    # Discover from git diff
    changed_files = [path for path in changed_paths(repo, base_ref) if is_production_python_path(path)]
    return [
        EnforcementTarget(
            language="python",
            target_id=f"pyfile:{path}",
            source_path=path,
            display_name=path,
        )
        for path in changed_files
    ]


def changed_lines_for_targets(
    repo: Path,
    base_ref: str,
    targets: Sequence[EnforcementTarget],
) -> dict[str, list[int]]:
    """Derive added/modified line numbers by file for the specified targets."""
    paths = [t.source_path for t in targets if t.source_path]
    return changed_lines_by_file(repo, base_ref, paths)


def has_only_non_executable_changed_lines(
    repo: Path,
    source_path: str,
    changed_lines: Sequence[int],
) -> bool:
    """Check if all changed lines in source_path are comments, docstrings, or blank lines."""
    target_lines = {int(line) for line in changed_lines}
    if not source_path or not target_lines:
        return False
    file_path = repo / source_path
    if not file_path.is_file():
        return False
    try:
        source = file_path.read_text(encoding="utf-8")
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        executable_lines = {
            line
            for item in tokens
            if item.type not in {
                token.COMMENT,
                token.ENCODING,
                token.ENDMARKER,
                token.INDENT,
                token.DEDENT,
                token.NEWLINE,
                tokenize.NL,
            }
            for line in range(item.start[0], item.end[0] + 1)
        }
    except (OSError, UnicodeDecodeError, tokenize.TokenError):
        return False
    return target_lines.isdisjoint(executable_lines)


def non_executable_target_evidence(
    source_path: str,
    changed_lines: Sequence[int],
    *,
    coverage_gate: float = 0.0,
    mutation_gate: float = 0.0,
) -> dict[str, Any]:
    """Generate normalized evidence for a target with only non-executable changes."""
    scoped = {source_path: sorted(int(l) for l in changed_lines)}
    return {
        "status": "passed",
        "reasonCode": "no_executable_changed_lines",
        "testsPass": True,
        "coverage": {
            "covered": 0,
            "total": 0,
            "rate": 100.0,
            "gate": coverage_gate,
            "passed": True,
            "scope": "changed_lines",
            "changedLines": scoped,
            "noExecutableChangedLines": True,
        },
        "mutation": {
            "runtimeLane": "not_run",
            "generated": 0,
            "killed": 0,
            "survived": 0,
            "noCoverage": 0,
            "rate": 100.0,
            "gate": mutation_gate,
            "passed": True,
            "scope": "changed_lines",
            "changedLines": scoped,
        },
        "artifacts": {},
        "message": "Python enforcement passed; changed target lines are comments or whitespace",
    }

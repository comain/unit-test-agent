"""Changed-line scoping and interpretation of a raw mutation summary.

UTA scores mutation on the changed lines of a target, not on the whole file,
so the numbers mutmut reports have to be narrowed and then read. Both halves
live here: the source-level mask and the changed-line filters that decide what
mutmut may touch, and the summary shaping that turns an execution outcome into
a scored result -- zero-mutant completions, no-test-association metadata
reconciliation, and the "nothing mutatable" pass. These are pure functions
over a summary and a source file; the commands that produce them are elsewhere.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Optional

from uta.language.python.mutation_candidates import low_value_side_effect_lines
from uta.language.python.verification.evidence import MutationSummary
from uta.language.python.verification.mutmut_runtime import (
    _append_no_mutate_pragma,
    _changed_line_payload,
    _maskable_pragma_lines,
    _normalize_changed_lines,
    _normalize_relpath,
)


@dataclass(frozen=True)
class _ScopedMutationCounts:
    generated: int = 0
    killed: int = 0
    survived: int = 0
    no_tests: int = 0
    timeout: int = 0
    suspicious: int = 0
    skipped: int = 0
    no_coverage: int = 0


def _mutation_backend_reason(output: str) -> str:
    normalized = output.lower()
    if "whatthepatch" in normalized or "mutmut[patch]" in normalized:
        return "missing_mutmut_patch_dependency"
    return "mutation_backend_failed"


def _zero_mutants_means_no_candidates(
    source_file: Path,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    output: str,
) -> bool:
    if not _looks_like_zero_mutant_completion(output):
        return False
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    target_lines = None
    if normalized_changed_lines is not None:
        target_lines = normalized_changed_lines.get(_normalize_relpath(source_path), set())
        if not target_lines:
            target_lines = normalized_changed_lines.get(_normalize_relpath(source_file.name), set())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if target_lines is None:
            return False
        start = int(getattr(node, "lineno", 0) or 0)
        end = int(getattr(node, "end_lineno", start) or start)
        if any(start <= line <= end for line in target_lines):
            return False
    return True


def _mutation_with_no_test_association_count(mutation: MutationSummary, count: int) -> MutationSummary:
    """Use mutmut metadata to distinguish real no-test mutants from zero candidates."""
    generated = max(int(mutation.generated), int(count))
    no_tests = max(int(mutation.no_tests), int(count))
    detected = int(mutation.killed)
    denominator = detected + int(mutation.survived) + int(mutation.timeout) + int(mutation.suspicious)
    rate = 100.0 if denominator == 0 else round((detected / denominator) * 100.0, 4)
    return replace(
        mutation,
        generated=generated,
        no_tests=no_tests,
        rate=rate,
        passed=rate >= mutation.gate,
    )


def _reconcile_no_test_association(
    mutation: MutationSummary,
    *,
    output: str,
    metadata_count: Optional[int],
) -> MutationSummary:
    """Count generated no-test mutants consistently in batch and single-pass execution."""

    if (
        mutation.generated <= 0
        and metadata_count is not None
        and metadata_count > 0
        and _looks_like_mutmut_no_test_association(output)
    ):
        return _mutation_with_no_test_association_count(mutation, metadata_count)
    return mutation


def _mark_no_mutatable_candidates(
    mutation: MutationSummary,
    *,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
) -> MutationSummary:
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    if normalized_changed_lines is None:
        return replace(mutation, generated=0, killed=0, no_tests=0, rate=100.0, passed=True)
    normalized_source = _normalize_relpath(source_path)
    return replace(
        mutation,
        generated=0,
        killed=0,
        no_tests=0,
        rate=100.0,
        passed=True,
        scope="changed_lines",
        changed_lines=_changed_line_payload(normalized_changed_lines, {normalized_source}),
        changed_line_mutants_generated=0,
        changed_line_mutants_killed=0,
    )


def _empty_mutation_summary(*, gate: float, runtime_lane: str) -> MutationSummary:
    return MutationSummary(
        runtime_lane=runtime_lane,
        generated=0,
        killed=0,
        survived=0,
        no_coverage=0,
        rate=100.0,
        gate=gate,
        passed=True,
    )


def _mutmut_meta_mutant_count(repo: Path, source_path: str) -> Optional[int]:
    meta_path = repo / "mutants" / f"{_normalize_relpath(source_path)}.meta"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    exit_code_by_key = meta.get("exit_code_by_key")
    if not isinstance(exit_code_by_key, dict):
        return None
    return len(exit_code_by_key)


def _looks_like_zero_mutant_completion(output: str) -> bool:
    text = str(output or "")
    if re.search(r"\b0\s+generated\b", text, flags=re.IGNORECASE):
        return True
    return bool(re.search(r"\b0\s*/\s*0\b", text))


def _looks_like_mutmut_no_test_association(output: str) -> bool:
    normalized = str(output or "").lower()
    return (
        "could not find any test case for any mutant" in normalized
        or "unable to force test failures" in normalized
    )


def _scope_mutation_to_changed_lines(
    mutation: MutationSummary,
    *,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    mutation_masked: bool = False,
    scoped_counts: Optional[_ScopedMutationCounts] = None,
    sampling: Optional[Mapping[str, Any]] = None,
) -> MutationSummary:
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    if normalized_changed_lines is None:
        return mutation
    normalized_source = _normalize_relpath(source_path)
    target_lines = normalized_changed_lines.get(normalized_source, set())
    diff_survivors = [
        survivor
        for survivor in mutation.survivors
        if _normalize_relpath(str(survivor.get("file") or "")) == normalized_source
        and int(survivor.get("line") or 0) in target_lines
    ]
    if scoped_counts and scoped_counts.generated > 0:
        survived = scoped_counts.survived
        no_tests = scoped_counts.no_tests
        timeout = scoped_counts.timeout
        suspicious = scoped_counts.suspicious
        skipped = scoped_counts.skipped
        no_coverage = scoped_counts.no_coverage
        changed_line_generated = scoped_counts.generated
        changed_line_killed = scoped_counts.killed
    else:
        survived = len(diff_survivors)
        no_tests = mutation.no_tests
        timeout = mutation.timeout
        suspicious = mutation.suspicious
        skipped = mutation.skipped
        no_coverage = mutation.no_coverage
        changed_line_generated = mutation.generated if mutation_masked else 0
        changed_line_scored_failed = survived + timeout + suspicious
        changed_line_unscored = no_tests + no_coverage + skipped
        changed_line_killed = max(changed_line_generated - changed_line_scored_failed - changed_line_unscored, 0)
    changed_line_scored_failed = survived + timeout + suspicious
    has_mutation_evidence = changed_line_generated > 0 or not target_lines
    denominator = changed_line_killed + changed_line_scored_failed
    rate = 100.0 if denominator == 0 and has_mutation_evidence else round((changed_line_killed / denominator) * 100.0, 4)
    return replace(
        mutation,
        generated=mutation.generated,
        killed=changed_line_killed if mutation_masked else mutation.killed,
        survived=survived,
        no_coverage=no_coverage,
        no_tests=no_tests,
        timeout=timeout,
        suspicious=suspicious,
        skipped=skipped,
        rate=rate,
        passed=has_mutation_evidence and rate >= mutation.gate,
        scope="changed_lines",
        changed_lines=_changed_line_payload(normalized_changed_lines, {normalized_source}),
        diff_survivors=diff_survivors,
        changed_line_mutants_generated=changed_line_generated,
        changed_line_mutants_killed=changed_line_killed,
        changed_line_mutants_scored=denominator,
        sampling=dict(sampling or {}),
    )


def _filter_changed_lines_for_mutation(
    source_file: Path,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    source_path: str,
) -> Optional[Mapping[str, Iterable[int]]]:
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    if normalized_changed_lines is None:
        return changed_lines
    normalized_source = _normalize_relpath(source_path)
    target_lines = set(normalized_changed_lines.get(normalized_source, set()))
    if not target_lines:
        return normalized_changed_lines
    ignored = _low_value_side_effect_statement_lines(source_file)
    if not ignored:
        return normalized_changed_lines
    filtered = {
        path: set(lines)
        for path, lines in normalized_changed_lines.items()
    }
    filtered[normalized_source] = {line for line in target_lines if line not in ignored}
    return filtered


def _mutation_changed_lines_empty_for_source(
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    source_path: str,
) -> bool:
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    if normalized_changed_lines is None:
        return False
    normalized_source = _normalize_relpath(source_path)
    return normalized_source in normalized_changed_lines and not normalized_changed_lines.get(normalized_source, set())


def _mutation_no_scored_lines_summary(
    *,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    gate: float,
    runtime_lane: str,
    sampling: Optional[Mapping[str, Any]] = None,
) -> MutationSummary:
    normalized_changed_lines = _normalize_changed_lines(changed_lines) or {}
    normalized_source = _normalize_relpath(source_path)
    return MutationSummary(
        runtime_lane=runtime_lane,
        generated=0,
        killed=0,
        survived=0,
        no_coverage=0,
        rate=100.0,
        gate=float(gate),
        passed=True,
        scope="changed_lines",
        changed_lines=_changed_line_payload(normalized_changed_lines, {normalized_source}),
        changed_line_mutants_generated=0,
        changed_line_mutants_killed=0,
        changed_line_mutants_scored=0,
        sampling=dict(sampling or {}),
    )


def _low_value_side_effect_statement_lines(source_file: Path) -> set[int]:
    return set(low_value_side_effect_lines(source_file))


@dataclass(frozen=True)
class _MutationMask:
    path: Path
    original_text: Optional[str] = None
    scoped: bool = False

    @property
    def masked(self) -> bool:
        return self.original_text is not None

    def restore(self) -> None:
        if self.original_text is not None:
            self.path.write_text(self.original_text, encoding="utf-8")


def _apply_changed_line_mutation_mask(
    source_file: Path,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    source_path: str,
) -> _MutationMask:
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    if normalized_changed_lines is None:
        return _MutationMask(source_file)
    target_lines = normalized_changed_lines.get(_normalize_relpath(source_path), set())
    if not target_lines or not source_file.exists():
        return _MutationMask(source_file, scoped=bool(target_lines))

    original = source_file.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    maskable_lines = _maskable_pragma_lines(original)
    masked_lines = [
        _append_no_mutate_pragma(line)
        if index in maskable_lines and index not in target_lines and not _is_pragma_safe_skip(line)
        else line
        for index, line in enumerate(lines, start=1)
    ]
    masked = "".join(masked_lines)
    if masked == original:
        return _MutationMask(source_file, scoped=True)
    source_file.write_text(masked, encoding="utf-8")
    return _MutationMask(source_file, original, scoped=True)


def _is_pragma_safe_skip(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("#")
        or stripped.endswith("\\")
        or "pragma: no mutate" in stripped
    )

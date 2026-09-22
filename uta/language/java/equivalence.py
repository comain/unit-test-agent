"""Java's answer to "which mutants did this gate count against its score?"

The UTA gate prints only counts: `diff mutation score R% (detected/total
detected)`. The equivalent-mutant review needs identities, so they are rebuilt
from the gate's own PIT reports -- the directory its command names, never the
newest report on disk.

The plugin's exact counting rule (which lines, which statuses) is not visible
from here, so it is not assumed. Every plausible reading -- changed lines or
the whole filter-diff report, PIT's detected set or killed alone, with or
without non-viable mutants in the total -- is tried, and only a reading that
reproduces *both* printed numbers is believed. If none does, or two readings
disagree about which mutants failed, the survivors are unproven and the review
never runs.
"""

from __future__ import annotations

import hashlib
import re
from itertools import product
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from uta.enforcement.diff import changed_lines_by_file
from uta.enforcement.enforcement import git_output
from uta.enforcement.equivalent_mutants import MutantIdentity, ScoringSurvivors
from uta.shared.config import settings

#: PIT's own `DetectionStatus.isDetected()` set.
_PIT_DETECTED = frozenset({"KILLED", "TIMED_OUT", "MEMORY_ERROR", "RUN_ERROR", "NON_VIABLE"})
_KILLED_ONLY = frozenset({"KILLED"})


def java_scoring_survivors(
    gate_result: Mapping[str, Any],
    repo_path: Path | str,
    *,
    base_ref: Optional[str] = None,
) -> Optional[ScoringSurvivors]:
    from uta.language.java.ci_evidence import output_evidence_detail
    from uta.language.java.generation.mutation_context import pit_compat_reports
    from uta.language.java.maven.pitest import parse_pitest_mutations

    repo = Path(repo_path).resolve()
    result = dict(gate_result or {})
    output = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    mutation = output_evidence_detail(output).get("mutation") or {}
    if mutation.get("source") != "diff":
        return None
    detected, total = int(mutation.get("killed") or 0), int(mutation.get("generated") or 0)

    reports = pit_compat_reports(str(repo), result)
    if not reports:
        return None
    try:
        rows = [row for report in reports for row in parse_pitest_mutations(str(report))]
    except Exception:
        return None
    sources = _resolve_sources(repo, rows)
    if sources is None:
        return None

    ref = base_ref or _base_ref(result)
    if not ref or ref.startswith("-"):
        return None
    changed = {
        path: set(lines)
        for path, lines in changed_lines_by_file(repo, ref, sorted(set(sources.values()))).items()
    }
    in_diff = [row for row in rows if row["line"] in changed.get(sources[_source_key(row)], ())]

    readings = []
    for scoped, detected_set, drop_non_viable in product(
        (in_diff, rows), (_PIT_DETECTED, _KILLED_ONLY), (False, True)
    ):
        counted = [row for row in scoped if not (drop_non_viable and row["status"] == "NON_VIABLE")]
        hits = sum(1 for row in counted if row["status"] in detected_set)
        if (hits, len(counted)) == (detected, total):
            readings.append(_failures(counted, detected_set, sources))
    if not readings or any(reading != readings[0] for reading in readings[1:]):
        return None

    reviewable, unreviewed = readings[0]
    return ScoringSurvivors(
        language="java",
        mutants=tuple(sorted(reviewable, key=lambda mutant: (mutant.source_path, mutant.line, mutant.key))),
        unreviewed_scoring_failures=unreviewed,
        source_fingerprints={
            path: hashlib.sha256((repo / path).read_bytes()).hexdigest()
            for path in sorted({mutant.source_path for mutant in reviewable})
        },
        mutation_rate=float(mutation.get("rate") or 0.0),
        mutation_gate=float(mutation.get("gate") or settings.ci_diff_mutation_gate),
    )


def java_gate_failure_flags(gate_result: Mapping[str, Any]) -> Optional[Dict[str, bool]]:
    """Did this UTA gate fail on mutation alone? None when its output cannot say.

    A passing gate line reads `diff line coverage 100.00% passed ...`; a failing
    one continues differently (`... 87.50% is below required 95.00%`), so any
    word other than `passed` is a failure. A red suite stops PIT before any
    mutation is scored.
    """
    from uta.language.java.ci_evidence import pit_baseline_failure_detail

    result = dict(gate_result or {})
    output = f"{result.get('summary') or ''}\n{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    coverage = re.findall(r"diff line coverage\s+[0-9.]+%\s+(\w+)", output, flags=re.IGNORECASE)
    mutation = re.findall(r"diff mutation score\s+[0-9.]+%\s+(\w+)", output, flags=re.IGNORECASE)
    if not coverage or not mutation:
        return None
    tests_passed = pit_baseline_failure_detail(output) is None
    coverage_passed = all(word.lower() == "passed" for word in coverage)
    mutation_failed = any(word.lower() != "passed" for word in mutation)
    return {
        "tests_passed": tests_passed,
        "coverage_passed": coverage_passed,
        "mutation_only_failure": bool(
            tests_passed and coverage_passed and mutation_failed and not result.get("passed")
        ),
    }


def _failures(
    counted: List[Dict[str, Any]], detected_set: frozenset, sources: Mapping[Tuple[str, str], str]
) -> Tuple[frozenset, int]:
    reviewable = []
    unreviewed = 0
    for row in counted:
        if row["status"] in detected_set:
            continue
        if row["status"] == "SURVIVED":
            reviewable.append(_identity(row, sources[_source_key(row)]))
        else:
            unreviewed += 1
    return frozenset(reviewable), unreviewed


def _identity(row: Mapping[str, Any], source_path: str) -> MutantIdentity:
    fields = (
        row["source_file"], row["mutated_class"], row["method"], row["method_description"],
        str(row["line"]), row["mutator"], row["indexes"], row["blocks"],
    )
    return MutantIdentity(
        key=hashlib.sha1("|".join(fields).encode("utf-8")).hexdigest(),
        source_path=source_path,
        line=int(row["line"]),
        operator=str(row["mutator"]).rsplit(".", 1)[-1],
        description=f"{row['method']}: {row['description']}",
    )


def _source_key(row: Mapping[str, Any]) -> Tuple[str, str]:
    return str(row["mutated_class"]), str(row["source_file"])


def _resolve_sources(repo: Path, rows: List[Dict[str, Any]]) -> Optional[Dict[Tuple[str, str], str]]:
    """Map each (class, sourceFile) to exactly one tracked production file."""
    tracked = [
        line.strip()
        for line in git_output(repo, "ls-files", "--", "src/main/java/*.java", "*/src/main/java/*.java").splitlines()
        if line.strip()
    ]
    resolved: Dict[Tuple[str, str], str] = {}
    for key in {_source_key(row) for row in rows}:
        mutated_class, source_file = key
        package = mutated_class.split("$", 1)[0].rpartition(".")[0]
        relative = "/".join(filter(None, (package.replace(".", "/"), source_file)))
        suffix = f"src/main/java/{relative}"
        matches = [path for path in tracked if path == suffix or path.endswith("/" + suffix)]
        if not source_file or len(matches) != 1:
            return None
        resolved[key] = matches[0]
    return resolved


def _base_ref(result: Mapping[str, Any]) -> str:
    evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
    diff = evidence.get("diff") if isinstance(evidence.get("diff"), Mapping) else {}
    return str(evidence.get("baseRef") or diff.get("baseRef") or "origin/master")


__all__ = ["java_gate_failure_flags", "java_scoring_survivors"]

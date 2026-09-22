"""Python's answer to "which mutants did this gate count against its score?"

mutmut names every mutant (`pkg.mod.x_fn__mutmut_3`), so identities come
straight from the survivor list the verifier already kept. Two shapes carry it:
the CI gate's aggregated `evidence.mutation` and the generation cycle's
`MutationSummary.as_dict()`; both expose the same survivor lists and counts.

What cannot be proven is refused. A sampled run reviews one sample and
rechecks another, so it is never eligible. A survivor without an id, file or
line cannot be matched on recheck. And the list must account for exactly the
`survived` count the score was computed from, or some survivor went missing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from uta.enforcement.equivalent_mutants import MutantIdentity, ScoringSurvivors


def python_scoring_survivors(
    mutation: Optional[Mapping[str, Any]],
    repo_path: Path | str,
) -> Optional[ScoringSurvivors]:
    if not isinstance(mutation, Mapping) or not mutation:
        return None
    sampling = mutation.get("sampling")
    if mutation.get("sampled") or (isinstance(sampling, Mapping) and sampling.get("enabled")):
        return None

    candidate_locations = _candidate_locations(mutation)
    scoped = str(mutation.get("scope") or "") == "changed_lines"
    listed = mutation.get("diff_survivors") if scoped else mutation.get("survivors")
    if scoped and not listed:
        all_survivors = mutation.get("survivors")
        plan = mutation.get("candidate_plan") or mutation.get("candidatePlan")
        adapter_filtered = (
            isinstance(plan, Mapping)
            and plan.get("adapterFilteredGenerationApplied") is True
        )
        survivor_keys = {
            str(item.get("id") or "").strip()
            for item in all_survivors if isinstance(item, Mapping)
        } if isinstance(all_survivors, list) else set()
        if adapter_filtered and survivor_keys and survivor_keys <= candidate_locations.keys():
            listed = all_survivors
    if not isinstance(listed, list):
        return None
    if len(listed) != int(mutation.get("survived") or 0):
        return None

    repo = Path(repo_path).resolve()
    mutants = []
    for survivor in listed:
        if not isinstance(survivor, Mapping):
            return None
        mutant_id = str(survivor.get("id") or "").strip()
        source_path = str(survivor.get("file") or "").strip().replace("\\", "/")
        line = survivor.get("line")
        if mutant_id in candidate_locations:
            source_path, line = candidate_locations[mutant_id]
        if not mutant_id or not source_path or not isinstance(line, int) or line <= 0:
            return None
        mutants.append(
            MutantIdentity(
                key=mutant_id,
                source_path=source_path,
                line=line,
                operator=_operator(mutant_id),
                description=str(survivor.get("description") or ""),
                mutation_diff=str(
                    survivor.get("mutmut_show_output")
                    or survivor.get("mutmutShowOutput")
                    or ""
                ),
            )
        )
    if len({mutant.key for mutant in mutants}) != len(mutants):
        return None

    fingerprints = {}
    for path in sorted({mutant.source_path for mutant in mutants}):
        source = repo / path
        if not source.is_file():
            return None
        fingerprints[path] = hashlib.sha256(source.read_bytes()).hexdigest()

    return ScoringSurvivors(
        language="python",
        mutants=tuple(sorted(mutants, key=lambda mutant: (mutant.source_path, mutant.line, mutant.key))),
        # Excluded from the score (no tests, no coverage, skipped) is not a
        # failure; a timeout or a suspicious result is, and no argument about
        # equivalence can excuse it.
        unreviewed_scoring_failures=int(mutation.get("timeout") or 0) + int(mutation.get("suspicious") or 0),
        source_fingerprints=fingerprints,
        mutation_rate=float(mutation.get("rate") or 0.0),
        mutation_gate=float(mutation.get("gate") or 0.0),
    )


def _candidate_locations(mutation: Mapping[str, Any]) -> Dict[str, tuple[str, int]]:
    """Map exact tool keys to source locations from UTA's candidate plan."""
    plan = mutation.get("candidate_plan") or mutation.get("candidatePlan")
    selected = plan.get("activeSelected") if isinstance(plan, Mapping) else None
    locations: Dict[str, tuple[str, int]] = {}
    for candidate in selected if isinstance(selected, list) else ():
        if not isinstance(candidate, Mapping):
            continue
        key = str(candidate.get("toolCandidateKey") or "").strip()
        opportunity = candidate.get("opportunity")
        if not key or not isinstance(opportunity, Mapping):
            continue
        source_path = str(opportunity.get("sourcePath") or "").strip().replace("\\", "/")
        line = opportunity.get("line")
        if source_path and isinstance(line, int) and line > 0:
            locations[key] = (source_path, line)
    return locations


def python_gate_failure_flags(gate_result: Mapping[str, Any]) -> Optional[Dict[str, bool]]:
    """Did this Python gate fail on mutation alone? None when evidence is missing.

    Tests are judged per target: a target whose suite fails never produces
    coverage, so the aggregate coverage can still read passed while a test is red.
    """
    evidence = (gate_result or {}).get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    coverage, mutation = evidence.get("coverage"), evidence.get("mutation")
    targets = evidence.get("targetResults")
    if not isinstance(coverage, Mapping) or not isinstance(mutation, Mapping) or not isinstance(targets, list) or not targets:
        return None
    tests_passed = all(isinstance(target, Mapping) and target.get("testsPass") is True for target in targets)
    coverage_passed = coverage.get("passed") is True
    return {
        "tests_passed": tests_passed,
        "coverage_passed": coverage_passed,
        "mutation_only_failure": bool(
            tests_passed and coverage_passed and mutation.get("passed") is False
            and not (gate_result or {}).get("passed")
        ),
    }


def _operator(mutant_id: str) -> str:
    name = mutant_id.rsplit(".", 1)[-1]
    return name.split("__mutmut_", 1)[0] if "__mutmut_" in name else "mutmut"


__all__ = ["python_gate_failure_flags", "python_scoring_survivors"]

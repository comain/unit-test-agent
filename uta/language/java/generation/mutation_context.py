"""Mutation-repair context policy: what survives into the fix prompt.

Mutation output is large and mostly low-value. These are the ROI rules that
decide how many families and examples a repair turn is allowed to see, and how
many enhancement attempts it gets.
"""

from typing import Any, Dict, List
import json
from pathlib import Path
import shlex

from uta.shared.config import settings as uta_settings


def write_delegated_survivors(repo_path: str, result: Dict[str, Any], batch: List[str]):
    """Snapshot all survivor families from this gate's isolated PIT invocation.

    Never search for the newest module report: another target or an agent's
    manual Maven run can have overwritten it. The command names the owner.
    """
    from uta.language.java.maven.pitest import summarize_surviving_mutants

    invocation = _pit_compat_invocation(repo_path, result)
    reports = sorted(invocation.rglob("mutations.xml")) if invocation else []
    if not reports:
        return None
    command = result.get("command") or []
    targets = {
        target: [family for report in reports for family in summarize_surviving_mutants(
            str(report), target, max_families=100000, max_examples_per_family=3
        )]
        for target in sorted(batch)
    }
    artifact = invocation / "repair-survivors.json"
    artifact.write_text(json.dumps({
        "reports": [str(report) for report in reports],
        "reproduceCommand": command,
        "targets": targets,
    }, indent=2), encoding="utf-8")
    return artifact


def pit_compat_reports(repo_path: str, result: Dict[str, Any]) -> List[Path]:
    """The `mutations.xml` files written by exactly this gate's PIT invocation.

    The command names its own evidence directory; anything else -- the newest
    module report, another target's run -- may belong to a different gate.
    """
    invocation = _pit_compat_invocation(repo_path, result)
    return sorted(invocation.rglob("mutations.xml")) if invocation else []


def _pit_compat_invocation(repo_path: str, result: Dict[str, Any]):
    command = result.get("command") or []
    args = shlex.split(command) if isinstance(command, str) else command
    prefix = "-Duta.pit.compat.evidence="
    evidence = next((arg[len(prefix):] for arg in args if str(arg).startswith(prefix)), None)
    if not evidence:
        return None
    root = Path(repo_path).resolve()
    invocation = Path(evidence)
    if not invocation.is_absolute():
        invocation = root / invocation
    invocation = invocation.resolve().parent
    if not invocation.is_relative_to(root / ".uta_cache" / "pit-compat"):
        return None
    return invocation


def _should_run_mutation(test_ok: bool, mutation_gate_score: int) -> bool:
    return bool(test_ok and mutation_gate_score > 0)



def _mutation_enhancement_attempts() -> int:
    return max(1, int(uta_settings.mutation_enhancement_attempts or 1))


def _filter_mutation_families_by_roi(
    families: List[Dict[str, Any]],
    *,
    max_families: int = 15,
    skip_expensive: bool = True,
) -> List[Dict[str, Any]]:
    """Retain only the top-ROI mutation families for the fix prompt (strategy F).

    Drops:
    - Families marked ``likely_equivalent`` (unobservable mutations).
    - ``effort_band=expensive`` families when ``skip_expensive=True``.
    Keeps at most ``max_families`` entries in ROI rank order.
    """
    filtered = [
        fam for fam in families
        if not fam.get("likely_equivalent")
        and not (skip_expensive and fam.get("effort_band") == "expensive")
    ]
    return filtered[:max_families]



def _flatten_mutation_family_examples(families: List[Dict[str, Any]], max_examples: int = 12) -> List[Dict[str, Any]]:
    examples: List[Dict[str, Any]] = []
    for family in families:
        for example in family.get("examples", []):
            examples.append(example)
            if len(examples) >= max_examples:
                return examples
    return examples













__all__ = [
    "_filter_mutation_families_by_roi",
    "_flatten_mutation_family_examples",
    "_mutation_enhancement_attempts",
    "_should_run_mutation",
]

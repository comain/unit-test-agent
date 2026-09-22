"""Neutral evidence envelope helpers for lightweight enforcement."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .targets import target_payload


SCHEMA_VERSION = 1
BACKEND = "python_enforcer"
EVIDENCE_PREFIX = "UTA_PYTHON_ENFORCEMENT_EVIDENCE="


def finalize(evidence: dict[str, Any]) -> dict[str, Any]:
    clone = dict(evidence)
    clone["evidenceId"] = ""
    digest = hashlib.sha256(json.dumps(clone, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
    evidence["evidenceId"] = "uta-python-enforcement-" + digest
    return evidence


def format_evidence_markers(evidence: Mapping[str, Any]) -> str:
    lines = [f"[test-enforcer] python evidence {evidence.get('status')} {evidence.get('reasonCode')}"]
    coverage = evidence.get("coverage") or {}
    mutation = evidence.get("mutation") or {}
    if coverage:
        lines.append(
            f"[test-enforcer] python diff line coverage {float(coverage.get('rate') or 0):.2f}% "
            f"{'passed' if coverage.get('passed') else 'failed'} "
            f"({int(coverage.get('covered') or 0)}/{int(coverage.get('total') or 0)})"
        )
    if mutation:
        if mutation.get("reasonCode") == "mutation_backend_failed":
            lines.append(
                "[test-enforcer] python diff mutation unavailable: mutation backend failed; "
                f"{int(mutation.get('notChecked') or 0)} selected mutants were not executed"
            )
        elif mutation.get("reasonCode") == "mutation_no_tests":
            lines.append(
                "[test-enforcer] python diff mutation unavailable: no tests associated with "
                f"{int(mutation.get('noTests') or 0)} selected mutants"
            )
        else:
            lines.append(
                f"[test-enforcer] python diff mutation score {float(mutation.get('rate') or 0):.2f}% "
                f"{'passed' if mutation.get('passed') else 'failed'} "
                f"({int(mutation.get('survived') or 0)} diff survivors, "
                f"{int(mutation.get('changedLineMutantsGenerated') or 0)} diff mutants)"
            )
    lines.append(EVIDENCE_PREFIX + json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Aggregation
#
# One implementation, in the contract, because both lanes need the same answer
# and the binding's own roll-up is lossier -- it drops `candidatePlan`, which
# the spec calls compatible-or-nothing. Aggregating here from per-target
# evidence means the local CLI, the UTA proxy and the binding cannot disagree
# about what a set of target results adds up to.
# --------------------------------------------------------------------------


def aggregate_coverage(target_results: Sequence[Mapping[str, Any]], gate: float) -> dict[str, Any] | None:
    summaries = [item.get("coverage") for item in target_results if isinstance(item.get("coverage"), dict)]
    if not summaries:
        return None
    covered = sum(int(item.get("covered") or 0) for item in summaries)
    total = sum(int(item.get("total") or 0) for item in summaries)
    passed = all(item.get("passed") is True for item in summaries)
    return {"covered": covered, "total": total, "rate": 100.0 if total == 0 else round((covered / total) * 100.0, 4), "gate": gate, "passed": passed, "modules": len(summaries), "scope": "changed_lines", "no_executable_changed_lines": all(item.get("no_executable_changed_lines") is True for item in summaries)}


def aggregate_mutation(target_results: Sequence[Mapping[str, Any]], gate: float) -> dict[str, Any] | None:
    summaries = [item.get("mutation") for item in target_results if isinstance(item.get("mutation"), dict)]
    if not summaries:
        return None
    generated = sum(int(item.get("generated") or 0) for item in summaries)
    killed = sum(int(item.get("killed") or 0) for item in summaries)
    survived = sum(int(item.get("survived") or 0) for item in summaries)
    no_tests = sum(int(item.get("noTests") or item.get("no_tests") or 0) for item in summaries)
    timeout_count = sum(int(item.get("timeout") or 0) for item in summaries)
    suspicious = sum(int(item.get("suspicious") or 0) for item in summaries)
    not_checked = sum(int(item.get("notChecked") or 0) for item in summaries)
    scored = sum(int(item.get("changedLineMutantsScored") or 0) for item in summaries)
    denominator = killed + survived + timeout_count + suspicious
    backend_failed = any(item.get("reasonCode") == "mutation_backend_failed" for item in summaries)
    no_tests_failed = any(item.get("reasonCode") == "mutation_no_tests" for item in summaries)
    rate = (
        0.0
        if backend_failed or no_tests_failed
        else 100.0
        if denominator == 0
        else round((killed / denominator) * 100.0, 4)
    )
    plans = [item.get("candidatePlan") for item in summaries if isinstance(item.get("candidatePlan"), dict)]
    return {
        "generated": generated,
        "killed": killed,
        "survived": survived,
        "noTests": no_tests,
        "timeout": timeout_count,
        "suspicious": suspicious,
        "notChecked": not_checked,
        "changedLineMutantsGenerated": generated,
        "changedLineMutantsKilled": killed,
        "changedLineMutantsScored": scored,
        "rate": rate,
        "gate": gate,
        "passed": all(item.get("passed") is True for item in summaries) and rate >= gate,
        "reasonCode": (
            "mutation_backend_failed"
            if backend_failed
            else "mutation_no_tests"
            if no_tests_failed
            else None
        ),
        "modules": len(summaries),
        "scope": "changed_lines",
        "candidatePlan": aggregate_candidate_plan(plans),
    }


def aggregate_candidate_plan(plans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    active = [item for plan in plans for item in (plan.get("activeSelected") or [])]
    report_full = [item for plan in plans for item in (plan.get("reportFullSelected") or [])]
    exact = [item for plan in plans for item in (plan.get("exactToolCandidateKeys") or [])]
    eligible = [item for plan in plans for item in (plan.get("eligibleMutationOpportunities") or [])]
    suppressed = [item for plan in plans for item in (plan.get("suppressed") or [])]
    mechanisms = sorted(
        {str(plan.get("filterMechanism")) for plan in plans if plan.get("filterMechanism")}
    )
    suppression_by_reason: dict[str, int] = {}
    for item in suppressed:
        if not isinstance(item, Mapping):
            continue
        reason = str(item.get("reasonCode") or "")
        if reason:
            suppression_by_reason[reason] = suppression_by_reason.get(reason, 0) + 1
    generation_policies = [
        plan.get("generationPolicy")
        for plan in plans
        if isinstance(plan.get("generationPolicy"), Mapping)
    ]
    count_fields = (
        "changedLineCount",
        "eligibleOpportunities",
        "selectedOpportunitiesBeforeCap",
        "selectedOpportunities",
        "omittedByOnePerLine",
        "omittedByHardCap",
        "suppressedOpportunities",
    )
    generation_policy = {
        field: sum(int(policy.get(field) or 0) for policy in generation_policies)
        for field in count_fields
    }
    generation_policy["truncated"] = any(bool(policy.get("truncated")) for policy in generation_policies)
    payload = {
        "activeSelected": active,
        "reportFullSelected": report_full,
        "exactToolCandidateKeys": exact,
        "eligibleMutationOpportunities": eligible,
        "suppressed": suppressed,
        "suppressionByReason": suppression_by_reason,
        "filterMechanisms": mechanisms,
        "adapterFilteredGenerationApplied": bool(plans)
        and all(bool(plan.get("adapterFilteredGenerationApplied")) for plan in plans),
        "generationPolicy": generation_policy,
        "samplingLayer": {"enabled": False},
        "sampled": False,
    }
    if len(mechanisms) == 1:
        payload["filterMechanism"] = mechanisms[0]
    return payload


def target_result(source_path: str, status: str, reason: str, tests_pass: bool, message: str, coverage: Any, mutation: Any, commands: Sequence[Mapping[str, Any]], selected: Sequence[str], candidates: Sequence[str], candidate_results: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "target": target_payload(source_path),
        "status": status,
        "reasonCode": reason,
        "testsPass": tests_pass,
        "message": message,
        "coverage": coverage,
        "mutation": mutation,
        "commands": list(commands),
        "selectedTestPaths": list(selected),
        "candidateTestPaths": list(candidates),
        "candidateResults": list(candidate_results or []),
        "configuredTestPaths": list(candidates),
        "artifacts": {},
        "setup": {"tool": "lightweight_tool"},
    }

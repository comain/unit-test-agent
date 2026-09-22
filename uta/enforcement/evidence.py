"""Normalising enforcement evidence into one shape.

Coverage and mutation results arrive in two forms -- a structured payload when
the run produced one, and scraped Maven output when it did not -- and every
reader wants the same dictionary out of them.

This lived as thirteen private methods on the report renderer, which meant the
test-generation lane reached across into `CiReportRenderer._evidence_detail`,
a private classmethod in the delivery layer, wrapped in a bare `except` that
swallowed whatever went wrong. It normalises evidence rather than rendering
anything, so it belongs beside the evidence it reads.

None of these are report-specific. The renderer is now one caller of them.
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

from uta.enforcement.mutation_candidates import candidate_plan_counts
from uta.enforcement.test_quality import aggregate_test_quality


class _NoRawOutputEvidence:
    """What a language without an output parser contributes: nothing.

    Fail quiet rather than loud. An unregistered language should add no
    output-derived evidence to a report, not raise inside one.
    """

    @staticmethod
    def baseline_failure_detail(output: str):
        return None

    @staticmethod
    def should_merge(enforcement, output: str) -> bool:
        return False

    @staticmethod
    def output_detail(output: str) -> Dict[str, Any]:
        return {}

    @staticmethod
    def dependency_warnings(output: str) -> list[str]:
        return []


def raw_output_parser_for(language: str | None):
    """The registered reader of one language's raw enforcement output.

    Defaults to Java when the payload does not name a language, because that
    reproduces exactly what this module did before: it always ran the Maven/PIT
    parser, whatever produced the output. That default is a wart, not a design
    -- it goes when Python grows its own parser and the caller can stop
    guessing.
    """
    from uta.shared.backends import UnknownBackendError, make_backend

    try:
        return make_backend(str(language or "java"), "output_evidence")
    except UnknownBackendError:
        return _NoRawOutputEvidence()


def evidence_detail(enforcement: Dict[str, Any] | None) -> Dict[str, Any]:
    if not enforcement:
        return {"coverage": None, "mutation": None, "pitMutation": None}
    if enforcement.get("status") == "missing_evidence":
        return {
            "coverage": _zero_coverage_detail(),
            "mutation": _zero_mutation_detail(),
            "pitMutation": None,
        }
    structured = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else None
    output = f"{enforcement.get('stdout') or ''}\n{enforcement.get('stderr') or ''}"
    parser = raw_output_parser_for(enforcement.get("language"))
    warning_reader = getattr(parser, "dependency_warnings", None)
    dependency_warnings = warning_reader(output) if warning_reader else []
    mutation_blocked = parser.baseline_failure_detail(output)
    if structured:
        target_results = structured.get("targetResults") if isinstance(structured.get("targetResults"), list) else []
        mutation_summary = structured.get("mutation")
        report_mutation_gate = (
            isinstance(mutation_summary, dict)
            and mutation_summary.get("gateScope") == "report"
        )
        detail = {
            "coverage": _structured_coverage_detail(structured.get("coverage"), target_results=target_results),
            "coverageFailures": _structured_coverage_failures(target_results),
            "mutation": _structured_mutation_detail(mutation_summary, target_results=target_results),
            "mutationFailures": {"count": 0, "targets": []}
            if report_mutation_gate
            else _structured_mutation_failures(target_results),
            "mutationSkipped": _structured_mutation_skipped(target_results),
            "skippedTargets": _structured_skipped_targets(target_results),
            "mutationBudget": dict(structured.get("mutationBudget") or {})
            if isinstance(structured.get("mutationBudget"), dict)
            else {},
            "testQuality": _structured_test_quality_detail(structured.get("testQuality"), target_results),
            "pitMutation": None,
            "dependencyWarnings": dependency_warnings,
        }
        if mutation_blocked:
            detail["mutation"] = None
            detail["mutationFailures"] = {"count": 0, "targets": []}
            detail["mutationSkipped"] = {"count": 0, "targets": []}
            detail["mutationBlocked"] = mutation_blocked
        if parser.should_merge(enforcement, output):
            output_detail = parser.output_detail(output)
            for key in ("coverage", "mutation", "pitMutation"):
                if not detail.get(key) and output_detail.get(key):
                    detail[key] = output_detail[key]
            if output_detail.get("mutationBlocked"):
                detail["mutation"] = None
                detail["pitMutation"] = None
                detail["mutationBlocked"] = output_detail["mutationBlocked"]
        return detail
    detail = parser.output_detail(output)
    if dependency_warnings:
        detail["dependencyWarnings"] = dependency_warnings
    return detail


def _zero_coverage_detail() -> Dict[str, Any]:
    return {
        "rate": 0.0,
        "formattedRate": "0.00%",
        "covered": 0,
        "total": 0,
        "modules": 0,
    }


def _zero_mutation_detail() -> Dict[str, Any]:
    return {
        "rate": 0.0,
        "formattedRate": "0.00%",
        "killed": 0,
        "generated": 0,
        "modules": 0,
        "source": "missing_evidence",
    }


def _structured_coverage_detail(
    summary: Any,
    *,
    target_results: Sequence[Any] = (),
) -> Dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    rate = float(summary.get("rate") or 0.0)
    modules = int(summary.get("modules") or 0)
    if modules <= 0:
        modules = sum(1 for item in target_results if isinstance(item, dict) and isinstance(item.get("coverage"), dict)) or 1
    return {
        "rate": rate,
        "formattedRate": f"{rate:.2f}%",
        "covered": int(summary.get("covered") or 0),
        "total": int(summary.get("total") or 0),
        "modules": modules,
        "scope": str(summary.get("scope") or ""),
        "gate": float(summary.get("gate") or 0.0),
        "passed": summary.get("passed"),
        "source": "python",
    }


def _structured_coverage_failures(target_results: Sequence[Any]) -> list[Dict[str, Any]]:
    failures: list[Dict[str, Any]] = []
    for item in target_results:
        if not isinstance(item, dict):
            continue
        coverage = item.get("coverage")
        if not isinstance(coverage, dict) or coverage.get("passed") is True:
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        rate = float(coverage.get("rate") or 0.0)
        failures.append(
            {
                "target": target.get("display_name") or target.get("source_path") or target.get("target_id") or "unknown",
                "formattedRate": f"{rate:.2f}%",
                "covered": int(coverage.get("covered") or 0),
                "total": int(coverage.get("total") or 0),
                "gate": float(coverage.get("gate") or 0.0),
                "scope": str(coverage.get("scope") or ""),
                "message": item.get("message") or "",
            }
        )
    return failures


def _structured_mutation_detail(summary: Any,
    *,
    target_results: Sequence[Any] = (),
) -> Dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    scope = str(summary.get("scope") or "")
    # Under CI sampling the gate is the report aggregate, so the per-target
    # rates are diagnostic. Letting the worst of them force `passed` False here
    # would reinstate the per-target gate the enforcement lane just removed.
    report_gate = summary.get("gateScope") == "report"
    failed_rates = [] if report_gate else _structured_mutation_failed_rates(target_results)
    modules = int(summary.get("modules") or 0)
    if modules <= 0:
        modules = sum(1 for item in target_results if isinstance(item, dict) and isinstance(item.get("mutation"), dict)) or 1
    if scope == "changed_lines":
        killed = int(summary.get("changedLineMutantsKilled") or summary.get("killed") or 0)
        generated = int(summary.get("changedLineMutantsGenerated") or summary.get("generated") or 0)
        scored = _scored_changed_line_mutants(summary)
        rate = round((killed / scored) * 100.0, 4) if scored else float(summary.get("rate") or 0.0)
    else:
        killed = int(summary.get("killed") or 0)
        generated = int(summary.get("generated") or 0)
        scored = max(generated - int(summary.get("noTests") or summary.get("no_tests") or 0), 0)
        rate = float(summary.get("rate") or 0.0)
    worst_target_rate = min(failed_rates) if failed_rates else None
    incomplete_targets = [
        item for item in target_results
        if isinstance(item, dict)
        and str(item.get("reasonCode") or "") in {
            "mutation_backend_failed", "mutation_resource_exhausted",
        }
    ]
    unexecuted = 0
    for item in incomplete_targets:
        metric = item.get("mutation") or {}
        unexecuted += max(
            int(metric.get("changedLineMutantsGenerated") or metric.get("generated") or 0)
            - _scored_changed_line_mutants(metric)
            - int(metric.get("noTests") or 0), 0,
        )
    incomplete = bool(incomplete_targets)
    return {
        "rate": rate,
        "formattedRate": f"Incomplete ({unexecuted} unexecuted)" if incomplete else f"{rate:.2f}%",
        "incomplete": incomplete,
        "unexecuted": unexecuted,
        "killed": killed,
        "generated": generated,
        "scored": scored,
        "survived": int(summary.get("survived") or 0),
        "noTests": int(summary.get("noTests") or summary.get("no_tests") or 0),
        "timeout": int(summary.get("timeout") or 0),
        "suspicious": int(summary.get("suspicious") or 0),
        "modules": modules,
        "scope": scope,
        "gate": float(summary.get("gate") or 0.0),
        "passed": False if incomplete else (summary.get("passed") if report_gate else (False if failed_rates else summary.get("passed"))),
        "gateScope": str(summary.get("gateScope") or "target"),
        "source": "python",
        "worstTargetRate": worst_target_rate,
        "formattedWorstTargetRate": f"{worst_target_rate:.2f}%" if worst_target_rate is not None else None,
        "sampled": bool(summary.get("sampled") or (isinstance(summary.get("sampling"), dict) and summary.get("sampling", {}).get("enabled"))),
        "sampling": summary.get("sampling") if isinstance(summary.get("sampling"), dict) else {},
        "candidatePlan": _candidate_plan_detail(summary.get("candidatePlan")),
    }


def _structured_mutation_failures(target_results: Sequence[Any]) -> Dict[str, Any]:
    failures: list[Dict[str, Any]] = []
    for item in target_results:
        if not isinstance(item, dict):
            continue
        mutation = item.get("mutation")
        if not isinstance(mutation, dict) or mutation.get("passed") is True:
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        rate = _mutation_display_rate(mutation)
        failures.append(
            {
                "target": target.get("display_name") or target.get("source_path") or target.get("target_id") or "unknown",
                "formattedRate": f"{rate:.2f}%",
                "killed": int(mutation.get("changedLineMutantsKilled") or mutation.get("killed") or 0),
                "generated": int(mutation.get("changedLineMutantsGenerated") or mutation.get("generated") or 0),
                "scored": _scored_changed_line_mutants(mutation)
                if str(mutation.get("scope") or "") == "changed_lines"
                else max(
                    int(mutation.get("generated") or 0)
                    - int(mutation.get("no_tests") or mutation.get("noTests") or 0),
                    0,
                ),
                "noTests": int(mutation.get("no_tests") or mutation.get("noTests") or 0),
                "timeout": int(mutation.get("timeout") or 0),
                "suspicious": int(mutation.get("suspicious") or 0),
                "gate": float(mutation.get("gate") or 0.0),
                "scope": str(mutation.get("scope") or ""),
                "message": item.get("message") or "",
                "sampled": bool(isinstance(mutation.get("sampling"), dict) and mutation.get("sampling", {}).get("enabled")),
                "sampling": mutation.get("sampling") if isinstance(mutation.get("sampling"), dict) else {},
                "candidatePlan": _candidate_plan_detail(mutation.get("candidatePlan")),
            }
        )
    return {"count": len(failures), "targets": failures}


def _structured_mutation_skipped(target_results: Sequence[Any]) -> Dict[str, Any]:
    skipped: list[Dict[str, Any]] = []
    for item in target_results:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("mutation"), dict):
            continue
        coverage = item.get("coverage")
        if not isinstance(coverage, dict) or coverage.get("passed") is True:
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        skipped.append(
            {
                "target": target.get("display_name") or target.get("source_path") or target.get("target_id") or "unknown",
                "reason": item.get("reasonCode") or "coverage_gate_failed",
                "message": item.get("message") or "",
            }
        )
    return {"count": len(skipped), "targets": skipped}


# Reason codes for a target that passed *without being verified*. They are the
# report's advisory warnings, and they are also the set repair must not act on:
# a skipped target has no failing gate to fix, so opening an LLM repair for it
# spends model turns on work the gate will skip again next run.
ADVISORY_SKIP_REASONS = {
    "python_large_change_skipped": "diff too large to verify",
    "python_runtime_incompatible_skipped": "incompatible with the verification runtime",
}


def _structured_skipped_targets(target_results: Sequence[Any]) -> Dict[str, Any]:
    """Targets that passed without being verified, as advisory report warnings.

    These do not fail the gate -- that is the point of the skip -- so the report
    has to say out loud which files carry no coverage or mutation evidence.
    """
    skipped: list[Dict[str, Any]] = []
    for item in target_results:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reasonCode") or "")
        if reason not in ADVISORY_SKIP_REASONS:
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        skipped.append(
            {
                "target": target.get("display_name") or target.get("source_path") or target.get("target_id") or "unknown",
                "reason": reason,
                "reasonLabel": ADVISORY_SKIP_REASONS[reason],
                "message": item.get("message") or "",
            }
        )
    return {"count": len(skipped), "targets": skipped}


def _structured_mutation_failed_rates(target_results: Sequence[Any]) -> list[float]:
    rates: list[float] = []
    for item in target_results:
        if not isinstance(item, dict):
            continue
        mutation = item.get("mutation")
        if isinstance(mutation, dict) and mutation.get("passed") is not True:
            rates.append(_mutation_display_rate(mutation))
    return rates


def _structured_test_quality_detail(summary: Any, target_results: Sequence[Any]) -> Dict[str, Any] | None:
    warnings: list[Dict[str, Any]] = []
    top_rule_ids: list[Dict[str, Any]] = []
    scanner_failure_count = 0
    if isinstance(summary, dict):
        warnings.extend(item for item in (summary.get("warnings") or []) if isinstance(item, dict))
        top_rule_ids.extend(item for item in (summary.get("topRuleIds") or []) if isinstance(item, dict))
        scanner_failure_count += int(summary.get("scannerFailureCount") or 0)
    for item in target_results:
        if not isinstance(item, dict):
            continue
        quality = item.get("testQuality") if isinstance(item.get("testQuality"), dict) else {}
        warnings.extend(warning for warning in (quality.get("warnings") or []) if isinstance(warning, dict))
        scanner_failure_count += int(quality.get("scannerFailureCount") or 0)
    if not warnings and not top_rule_ids and not scanner_failure_count:
        return None
    aggregate = aggregate_test_quality(warnings)
    if top_rule_ids:
        aggregate["topRuleIds"] = top_rule_ids
    aggregate["scannerFailureCount"] = max(int(aggregate.get("scannerFailureCount") or 0), scanner_failure_count)
    if isinstance(summary, dict) and int(summary.get("warningCount") or 0) > int(aggregate.get("warningCount") or 0):
        aggregate["warningCount"] = int(summary.get("warningCount") or 0)
    return aggregate


def _candidate_plan_detail(candidate_plan: Any) -> Dict[str, Any]:
    if not isinstance(candidate_plan, dict) or not candidate_plan:
        return {}
    sampling_layer = candidate_plan.get("samplingLayer") if isinstance(candidate_plan.get("samplingLayer"), dict) else {}
    counts = candidate_plan_counts(candidate_plan)
    mechanisms = candidate_plan.get("filterMechanisms")
    filter_mechanism = (
        ", ".join(str(item) for item in mechanisms)
        if isinstance(mechanisms, list)
        else str(candidate_plan.get("filterMechanism") or "")
    )
    return {
        "filterMechanism": filter_mechanism,
        "changedLines": counts["changedLines"],
        "coveredChangedLines": counts["coveredChangedLines"],
        "eligibleOpportunities": counts["eligibleOpportunities"],
        "suppressedOpportunities": counts["suppressedOpportunities"],
        "omittedByOnePerLine": counts["omittedByOnePerLine"],
        "reportFullCandidates": counts["reportFullCandidates"],
        "activeCandidates": counts["activeCandidates"],
        "exactToolCandidateKeys": counts["exactToolCandidateKeys"],
        "preGenerationSelectedOpportunities": int(candidate_plan.get("preGenerationSelectedOpportunities") or 0),
        "preGenerationMaterializationRatio": float(candidate_plan.get("preGenerationMaterializationRatio") or 0.0),
        "preGenerationMaterializationWarning": bool(candidate_plan.get("preGenerationMaterializationWarning")),
        "selectedOpportunities": int(candidate_plan.get("selectedOpportunities") or 0),
        "materializedCandidates": int(candidate_plan.get("materializedCandidates") or counts["selectedCandidates"]),
        "materializedCandidateRatio": float(candidate_plan.get("materializedCandidateRatio") or 0.0),
        "materializedCandidateWarning": bool(candidate_plan.get("materializedCandidateWarning")),
        "runMutants": counts["runMutants"],
        "scoredMutants": counts["scoredMutants"],
        "killed": counts["killed"],
        "survived": counts["survived"],
        "noTests": counts["noTests"],
        "timeout": counts["timeout"],
        "suspicious": counts["suspicious"],
        "comparable": candidate_plan.get("comparable"),
        "changedFingerprints": list(candidate_plan.get("changedFingerprints") or []),
        "sampled": bool(sampling_layer.get("enabled")),
        "samplingLayer": sampling_layer,
    }


def _mutation_display_rate(summary: Dict[str, Any]) -> float:
    if str(summary.get("scope") or "") == "changed_lines":
        killed = int(summary.get("changedLineMutantsKilled") or summary.get("killed") or 0)
        scored = _scored_changed_line_mutants(summary)
        if scored:
            return round((killed / scored) * 100.0, 4)
    return float(summary.get("rate") or 0.0)


def _scored_changed_line_mutants(summary: Dict[str, Any]) -> int:
    explicit = summary.get("changedLineMutantsScored")
    if explicit is not None:
        return int(explicit or 0)
    generated = int(summary.get("changedLineMutantsGenerated") or summary.get("generated") or 0)
    no_tests = int(summary.get("noTests") or summary.get("no_tests") or 0)
    no_coverage = int(summary.get("noCoverage") or summary.get("no_coverage") or 0)
    skipped = int(summary.get("skipped") or 0)
    return max(generated - no_tests - no_coverage - skipped, 0)

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Dict, Literal, Mapping, Optional, Sequence, Tuple


MUTMUT3_METADATA_SELECTED_EXECUTION = "mutmut3_metadata_selected_execution"
MUTMUT3_OPERATOR_FILTERED_GENERATION = MUTMUT3_METADATA_SELECTED_EXECUTION
MUTMUT15_LEGACY_CHANGED_LINE_SCOPE = "mutmut15_legacy_changed_line_scope"
MUTATION_NOT_SUPPORTED_FOR_RUNTIME = "mutation_not_supported_for_runtime"
MUTATION_CANDIDATE_PLANNER_VERSION = "candidate-plan-v1"

FilterMechanism = Literal[
    "mutmut3_metadata_selected_execution",
    "mutmut15_legacy_changed_line_scope",
    "mutation_not_supported_for_runtime",
]


@dataclass(frozen=True)
class MutationVerificationContext:
    """Immutable verifier inputs used to build and compare mutation plans."""

    repo_url: str
    base_ref: str
    base_commit: str
    head_commit: str
    target_id: str
    source_path: str
    selected_test_paths: Tuple[str, ...]
    runtime_fingerprint: str
    dependency_fingerprint: str
    mutation_tool_version: str
    selected_test_policy_version: str
    operator_policy_version: str
    suppression_policy_version: str
    mutation_tool_api_fingerprint: str
    candidate_plan_config_fingerprint: str
    policy_mode: Literal["report_full"] = "report_full"
    enable_ci_sampling: bool = False

    def as_fingerprint_dict(self) -> Dict[str, Any]:
        """Return the stable context payload used by candidate-plan ids."""

        return {
            "repoUrl": self.repo_url,
            "baseRef": self.base_ref,
            "baseCommit": self.base_commit,
            "headCommit": self.head_commit,
            "targetId": self.target_id,
            "sourcePath": self.source_path,
            "selectedTestPaths": tuple(self.selected_test_paths),
            "runtimeFingerprint": self.runtime_fingerprint,
            "dependencyFingerprint": self.dependency_fingerprint,
            "mutationToolVersion": self.mutation_tool_version,
            "selectedTestPolicyVersion": self.selected_test_policy_version,
            "operatorPolicyVersion": self.operator_policy_version,
            "suppressionPolicyVersion": self.suppression_policy_version,
            "mutationToolApiFingerprint": self.mutation_tool_api_fingerprint,
            "candidatePlanConfigFingerprint": self.candidate_plan_config_fingerprint,
            "policyMode": self.policy_mode,
            "enableCiSampling": bool(self.enable_ci_sampling),
        }


@dataclass(frozen=True)
class MutationOpportunity:
    """Pre-generation mutation opportunity; no native mutmut key is required."""

    language: str
    source_path: str
    line: int
    line_span: Tuple[int, int]
    symbol: str
    opportunity_id: str
    operator_name: str
    family_hint: str
    diff_hunk: str
    operator_priority: int
    roi_score: float
    selection_rank: Tuple[Any, ...]
    covered: bool
    executable: bool
    selection_reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "language": self.language,
            "sourcePath": self.source_path,
            "line": int(self.line),
            "lineSpan": list(self.line_span),
            "symbol": self.symbol,
            "opportunityId": self.opportunity_id,
            "operatorName": self.operator_name,
            "familyHint": self.family_hint,
            "diffHunk": self.diff_hunk,
            "operatorPriority": int(self.operator_priority),
            "roiScore": float(self.roi_score),
            "selectionRank": list(self.selection_rank),
            "covered": bool(self.covered),
            "executable": bool(self.executable),
            "selectionReason": self.selection_reason,
        }


@dataclass(frozen=True)
class MutationCandidate:
    """Generated mutation candidate with an exact language-tool candidate key."""

    opportunity: MutationOpportunity
    candidate_id: str
    tool_candidate_key: str
    score: float

    def __post_init__(self) -> None:
        if not str(self.tool_candidate_key or "").strip():
            raise ValueError("tool_candidate_key is required for generated mutation candidates")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "candidateId": self.candidate_id,
            "toolCandidateKey": self.tool_candidate_key,
            "score": float(self.score),
            "opportunity": self.opportunity.as_dict(),
        }


@dataclass(frozen=True)
class SuppressedMutationOpportunity:
    """Suppressed pre-generation opportunity and its engine reason code."""

    opportunity: MutationOpportunity
    reason_code: str
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "opportunity": self.opportunity.as_dict(),
            "reasonCode": self.reason_code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MutationSamplingLayer:
    """Compatibility evidence for legacy sampling fields; disabled for cap-based planning."""

    enabled: bool
    strategy: str = ""
    threshold: int = 0
    limit: int = 0
    selected_candidate_ids: Tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def disabled(cls) -> "MutationSamplingLayer":
        return cls(enabled=False)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "strategy": self.strategy,
            "threshold": int(self.threshold),
            "limit": int(self.limit),
            "selectedCandidateIds": list(self.selected_candidate_ids),
        }


@dataclass(frozen=True)
class MutationCandidatePlan:
    """Language-neutral mutation candidate plan shared by CI and repair."""

    language: str
    target_id: str
    source_path: str
    policy_mode: Literal["report_full"]
    candidate_plan_id: str
    changed_lines: Tuple[int, ...]
    executable_changed_lines: Tuple[int, ...]
    covered_changed_lines: Tuple[int, ...]
    eligible_opportunities: Tuple[MutationOpportunity, ...]
    suppressed: Tuple[SuppressedMutationOpportunity, ...]
    report_full_selected: Tuple[MutationCandidate, ...]
    active_selected: Tuple[MutationCandidate, ...]
    omitted_by_one_per_line: Tuple[MutationOpportunity, ...]
    suppression_by_reason: Mapping[str, int]
    sampling_layer: MutationSamplingLayer
    filter_mechanism: FilterMechanism
    generation_policy_artifact: Optional[str]
    generation_policy_fingerprint: str
    exact_tool_candidate_keys: Tuple[str, ...]
    planner_version: str
    arid_rule_version: str
    test_selection_policy_version: str
    suppression_policy_version: str
    operator_policy_version: str
    mutation_tool_api_fingerprint: str
    mutation_tool_config_fingerprint: str
    effective_sampling_config: Mapping[str, Any]
    runtime_fingerprint: str
    dependency_fingerprint: str
    repo_url: str = ""
    base_ref: str = ""
    base_commit: str = ""
    head_commit: str = ""
    selected_test_paths: Tuple[str, ...] = field(default_factory=tuple)
    candidate_plan_artifact_path: Optional[str] = None
    selected_key_execution_applied: bool = False
    adapter_filtered_generation_applied: bool = False
    run_mutants: int = 0
    scored_mutants: int = 0
    killed: int = 0
    survived: int = 0
    no_tests: int = 0
    timeout: int = 0
    suspicious: int = 0

    def counts(self) -> Dict[str, int]:
        """Return canonical count fields for candidate-plan evidence."""

        return {
            "changedLines": len(self.changed_lines),
            "coveredChangedLines": len(self.covered_changed_lines),
            "eligibleOpportunities": len(self.eligible_opportunities),
            "eligibleMutationCandidates": len(self.report_full_selected),
            "suppressedCandidates": len(self.suppressed),
            "suppressedOpportunities": len(self.suppressed),
            "omittedByOnePerLine": len(self.omitted_by_one_per_line),
            "reportFullCandidates": len(self.report_full_selected),
            "reportFullSelectedCandidates": len(self.report_full_selected),
            "activeCandidates": len(self.active_selected),
            "selectedCandidates": len(self.active_selected),
            "exactToolCandidateKeys": len(self.exact_tool_candidate_keys),
            "exactToolCandidateKeyCount": len(self.exact_tool_candidate_keys),
            "runMutants": int(self.run_mutants),
            "scoredMutants": int(self.scored_mutants),
            "killed": int(self.killed),
            "survived": int(self.survived),
            "noTests": int(self.no_tests),
            "timeout": int(self.timeout),
            "suspicious": int(self.suspicious),
        }

    def as_dict(self) -> Dict[str, Any]:
        opportunity_ids = tuple(item.opportunity_id for item in self.eligible_opportunities)
        report_full_candidate_ids = tuple(item.candidate_id for item in self.report_full_selected)
        active_candidate_ids = tuple(item.candidate_id for item in self.active_selected)
        active_tool_candidate_keys = tuple(item.tool_candidate_key for item in self.active_selected)
        payload = {
            "language": self.language,
            "targetId": self.target_id,
            "sourcePath": self.source_path,
            "policyMode": self.policy_mode,
            "candidatePlanId": self.candidate_plan_id,
            "changedLines": list(self.changed_lines),
            "executableChangedLines": list(self.executable_changed_lines),
            "coveredChangedLines": list(self.covered_changed_lines),
            "eligibleMutationOpportunities": [item.as_dict() for item in self.eligible_opportunities],
            "suppressed": [item.as_dict() for item in self.suppressed],
            "reportFullSelected": [item.as_dict() for item in self.report_full_selected],
            "activeSelected": [item.as_dict() for item in self.active_selected],
            "omittedByOnePerLine": [item.as_dict() for item in self.omitted_by_one_per_line],
            "suppressionByReason": dict(self.suppression_by_reason),
            "samplingLayer": self.sampling_layer.as_dict(),
            "filterMechanism": self.filter_mechanism,
            "generationPolicyArtifact": self.generation_policy_artifact,
            "candidatePlanArtifactPath": self.candidate_plan_artifact_path,
            "generationPolicyFingerprint": self.generation_policy_fingerprint,
            "exactToolCandidateKeys": list(self.exact_tool_candidate_keys),
            "selectedKeyExecutionApplied": bool(self.selected_key_execution_applied),
            "adapterFilteredGenerationApplied": bool(self.adapter_filtered_generation_applied),
            "opportunityIds": list(opportunity_ids),
            "reportFullCandidateIds": list(report_full_candidate_ids),
            "activeCandidateIds": list(active_candidate_ids),
            "activeToolCandidateKeys": list(active_tool_candidate_keys),
            "plannerVersion": self.planner_version,
            "aridRuleVersion": self.arid_rule_version,
            "testSelectionPolicyVersion": self.test_selection_policy_version,
            "suppressionPolicyVersion": self.suppression_policy_version,
            "operatorPolicyVersion": self.operator_policy_version,
            "mutationToolApiFingerprint": self.mutation_tool_api_fingerprint,
            "mutationToolConfigFingerprint": self.mutation_tool_config_fingerprint,
            "effectiveSamplingConfig": dict(self.effective_sampling_config),
            "runtimeFingerprint": self.runtime_fingerprint,
            "dependencyFingerprint": self.dependency_fingerprint,
            "repoUrl": self.repo_url,
            "baseRef": self.base_ref,
            "baseCommit": self.base_commit,
            "headCommit": self.head_commit,
            "selectedTestPaths": list(self.selected_test_paths),
        }
        counts = self.counts()
        payload.update(
            {
                "eligibleMutationCandidates": counts["eligibleMutationCandidates"],
                "reportFullSelectedCandidates": counts["reportFullSelectedCandidates"],
                "selectedCandidates": counts["selectedCandidates"],
                "suppressedCandidates": counts["suppressedCandidates"],
                "exactToolCandidateKeyCount": counts["exactToolCandidateKeyCount"],
                "runMutants": counts["runMutants"],
                "scoredMutants": counts["scoredMutants"],
                "killed": counts["killed"],
                "survived": counts["survived"],
                "noTests": counts["noTests"],
                "timeout": counts["timeout"],
                "suspicious": counts["suspicious"],
            }
        )
        return payload


def make_opportunity_id(
    *,
    language: str,
    source_path: str,
    line: int,
    symbol: str,
    operator_name: str,
    source_line: str,
    diff_hunk: str,
) -> str:
    return _stable_hash(
        language,
        source_path,
        int(line),
        symbol,
        operator_name,
        _normalize_text(source_line),
        _normalize_text(diff_hunk),
    )


def make_candidate_id(
    *,
    opportunity_id: str,
    tool_candidate_key: str,
    mutation_tool_api_fingerprint: str,
) -> str:
    if not str(tool_candidate_key or "").strip():
        raise ValueError("tool_candidate_key is required for candidate ids")
    return _stable_hash(opportunity_id, tool_candidate_key, mutation_tool_api_fingerprint)


def make_candidate_plan_id(
    *,
    context: MutationVerificationContext,
    selected_candidate_ids: Sequence[str],
    filter_mechanism: str,
) -> str:
    return _stable_hash(
        context.as_fingerprint_dict(),
        tuple(selected_candidate_ids),
        filter_mechanism,
        MUTATION_CANDIDATE_PLANNER_VERSION,
    )


def config_fingerprint(values: Mapping[str, Any]) -> str:
    """Return a stable fingerprint for config values that affect selection."""

    return _stable_hash(dict(sorted((str(key), value) for key, value in values.items())))


def candidate_plan_allows_zero_scored_mutation(candidate_plan: Mapping[str, Any]) -> bool:
    """Return true only when candidate-plan evidence proves there is nothing exact to score."""

    if not isinstance(candidate_plan, Mapping) or not candidate_plan:
        return False
    active = candidate_plan.get("activeSelected") or []
    report_full = candidate_plan.get("reportFullSelected") or []
    exact_keys = candidate_plan.get("exactToolCandidateKeys") or []
    eligible = candidate_plan.get("eligibleMutationOpportunities") or []
    suppressed = candidate_plan.get("suppressed") or []
    if not isinstance(active, list) or not isinstance(report_full, list) or not isinstance(exact_keys, list):
        return False
    if active or report_full or exact_keys:
        return False
    if isinstance(eligible, list) and eligible:
        return False
    if isinstance(suppressed, list) and suppressed:
        return all(isinstance(item, Mapping) and str(item.get("reasonCode") or "").strip() for item in suppressed)
    return True


def candidate_plan_requires_mutation_execution(candidate_plan: Mapping[str, Any]) -> bool:
    """Return true when deterministic planning selected work that must produce evidence."""

    if not isinstance(candidate_plan, Mapping) or not candidate_plan:
        return False
    for key in (
        "eligibleMutationOpportunities",
        "activeSelected",
        "reportFullSelected",
        "exactToolCandidateKeys",
    ):
        value = candidate_plan.get(key)
        if isinstance(value, list) and value:
            return True
    return False


def candidate_plan_counts(candidate_plan: Mapping[str, Any]) -> Dict[str, int]:
    """Return normalized candidate-plan counts for serialized evidence payloads."""

    if not isinstance(candidate_plan, Mapping):
        return {}

    def count_any(*names: str) -> int:
        for name in names:
            value = candidate_plan.get(name)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    return {
        "changedLines": count_any("changedLines"),
        "coveredChangedLines": count_any("coveredChangedLines"),
        "eligibleOpportunities": count_any("eligibleOpportunities", "eligibleMutationOpportunities"),
        "eligibleMutationCandidates": count_any("eligibleMutationCandidates", "reportFullSelectedCandidates", "reportFullSelected"),
        "suppressedCandidates": count_any("suppressedCandidates", "suppressed"),
        "suppressedOpportunities": count_any("suppressedOpportunities", "suppressed"),
        "omittedByOnePerLine": count_any("omittedByOnePerLineCount", "omittedByOnePerLine"),
        "reportFullCandidates": count_any("reportFullCandidates", "reportFullSelectedCandidates", "reportFullSelected"),
        "reportFullSelectedCandidates": count_any("reportFullSelectedCandidates", "reportFullCandidates", "reportFullSelected"),
        "activeCandidates": count_any("activeCandidates", "selectedCandidates", "activeSelected"),
        "selectedCandidates": count_any("selectedCandidates", "activeCandidates", "activeSelected"),
        "exactToolCandidateKeys": count_any("exactToolCandidateKeys", "exactToolCandidateKeyCount"),
        "exactToolCandidateKeyCount": count_any("exactToolCandidateKeyCount", "exactToolCandidateKeys"),
        "runMutants": count_any("runMutants"),
        "scoredMutants": count_any("scoredMutants"),
        "killed": count_any("killed"),
        "survived": count_any("survived"),
        "noTests": count_any("noTests"),
        "timeout": count_any("timeout"),
        "suspicious": count_any("suspicious"),
        "notChecked": count_any("notChecked"),
    }


def candidate_plan_with_execution_evidence(
    candidate_plan: Mapping[str, Any],
    *,
    run_mutants: int,
    scored_mutants: int,
    killed: int,
    survived: int,
    no_tests: int,
    timeout: int,
    suspicious: int,
    not_checked: int = 0,
) -> Dict[str, Any]:
    """Attach language-neutral execution outcomes to a candidate plan."""

    plan = dict(candidate_plan)
    plan.update(
        {
            "runMutants": int(run_mutants),
            "scoredMutants": int(scored_mutants),
            "killed": int(killed),
            "survived": int(survived),
            "noTests": int(no_tests),
            "timeout": int(timeout),
            "suspicious": int(suspicious),
            "notChecked": int(not_checked),
        }
    )
    return plan


def all_selected_mutants_unassociated(
    *, generated: int, scored: int, no_tests: int
) -> bool:
    """Return whether mutation execution associated no selected mutant with tests.

    A zero scored denominator is not a successful mutation gate when candidates
    were generated but every outcome was ``no tests``. Mixed ``no tests``
    outcomes remain excluded from the score as usual.
    """

    return int(generated) > 0 and int(scored) == 0 and int(no_tests) > 0


def compare_candidate_plan_payloads(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> Dict[str, Any]:
    """Compare two persisted candidate-plan payloads for repair reruns."""

    fingerprint_fields = (
        "runtimeFingerprint",
        "dependencyFingerprint",
        "mutationToolApiFingerprint",
        "mutationToolConfigFingerprint",
        "testSelectionPolicyVersion",
        "operatorPolicyVersion",
        "suppressionPolicyVersion",
        "repoUrl",
        "baseRef",
        "baseCommit",
        "headCommit",
        "selectedTestPaths",
        "changedLines",
    )
    changed = [
        field_name
        for field_name in fingerprint_fields
        if previous.get(field_name) != current.get(field_name)
    ]
    previous_keys = tuple(previous.get("exactToolCandidateKeys") or ())
    current_keys = tuple(current.get("exactToolCandidateKeys") or ())
    # Candidate plan IDs may differ across equivalent execution strategies
    # (for example single-plan versus batched aggregation). The deterministic
    # contract is the selected tool candidate sequence under unchanged
    # fingerprints; the opaque plan id is diagnostic evidence, not identity.
    same_plan = previous_keys == current_keys
    comparable = not changed
    return {
        "comparable": comparable,
        "changedFingerprints": changed,
        "samePlan": bool(same_plan),
        "determinismBug": bool(comparable and not same_plan),
    }


def _stable_hash(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in str(value or "").strip().splitlines())

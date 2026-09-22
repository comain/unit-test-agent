from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence

from uta.enforcement.mutation_candidates import (
    MUTMUT15_LEGACY_CHANGED_LINE_SCOPE,
    MUTMUT3_METADATA_SELECTED_EXECUTION,
    MutationCandidatePlan,
    MutationSamplingLayer,
    MutationVerificationContext,
    candidate_plan_counts,
    candidate_plan_with_execution_evidence,
    config_fingerprint,
    make_candidate_plan_id,
)
from uta_enforce_core.mutation_batching import (
    filter_generation_policy_to_lines as _filter_generation_policy_to_lines,
    mutation_units_from_generation_policy as _mutation_units_from_generation_policy,
)
from uta.language.python.mutation_candidates import build_mutmut3_candidate_plan_from_meta
from uta.language.python.verification.evidence import CoverageSummary, MutationSummary
from uta.language.python.verification.mutmut_runtime import (
    _mutmut_major_version,
    _normalize_changed_lines,
    _normalize_relpath,
)
from uta.shared.config import settings
from uta.shared.targets import TargetRef


class _PythonRuntimeConfig(Protocol):
    artifact_dir: str
    timeout_seconds: int
    python2_mutmut_bin: Optional[str]
    cache_key: str
    dependency_fingerprints: Mapping[str, str]


def _legacy_mutmut15_candidate_plan(
    *,
    repo: Path,
    target: TargetRef,
    source_path: str,
    test_paths: Sequence[str],
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    mutmut_version: str,
    config: _PythonRuntimeConfig,
    base_ref: str = "",
    base_commit: str = "",
    head_commit: str = "",
    repo_url: str = "",
) -> Dict[str, Any]:
    """Build honest evidence for the Python2 mutmut 1.5 legacy verifier lane."""

    normalized_changed = _normalize_changed_lines(changed_lines) or {}
    normalized_source = _normalize_relpath(source_path)
    target_lines = tuple(sorted(int(line) for line in normalized_changed.get(normalized_source, set())))
    config_fp = config_fingerprint(
        {
            "filterMechanism": MUTMUT15_LEGACY_CHANGED_LINE_SCOPE,
            "artifactDir": config.artifact_dir,
            "timeoutSeconds": config.timeout_seconds,
            "python2MutmutBin": config.python2_mutmut_bin or "mutmut",
        }
    )
    context = MutationVerificationContext(
        repo_url=repo_url or _git_remote_url(repo),
        base_ref=base_ref,
        base_commit=base_commit,
        head_commit=head_commit or _git_head(repo),
        target_id=target.target_id,
        source_path=normalized_source,
        selected_test_paths=tuple(str(path) for path in test_paths),
        runtime_fingerprint="python2",
        dependency_fingerprint=config.cache_key or config_fingerprint(config.dependency_fingerprints),
        mutation_tool_version=str(mutmut_version or "").strip(),
        selected_test_policy_version="strict-python-v1",
        operator_policy_version="mutmut15-legacy",
        suppression_policy_version="mutmut15-legacy",
        mutation_tool_api_fingerprint="mutmut15-no-exact-key",
        candidate_plan_config_fingerprint=config_fp,
        policy_mode="report_full",
        enable_ci_sampling=False,
    )
    plan_id = make_candidate_plan_id(
        context=context,
        selected_candidate_ids=(),
        filter_mechanism=MUTMUT15_LEGACY_CHANGED_LINE_SCOPE,
    )
    plan = MutationCandidatePlan(
        language="python",
        target_id=target.target_id,
        source_path=normalized_source,
        policy_mode="report_full",
        candidate_plan_id=plan_id,
        changed_lines=target_lines,
        executable_changed_lines=target_lines,
        covered_changed_lines=target_lines,
        eligible_opportunities=(),
        suppressed=(),
        report_full_selected=(),
        active_selected=(),
        omitted_by_one_per_line=(),
        suppression_by_reason={},
        sampling_layer=MutationSamplingLayer.disabled(),
        filter_mechanism=MUTMUT15_LEGACY_CHANGED_LINE_SCOPE,
        generation_policy_artifact=None,
        generation_policy_fingerprint="mutmut15-legacy",
        exact_tool_candidate_keys=(),
        planner_version="candidate-plan-v1",
        arid_rule_version="mutmut15-legacy",
        test_selection_policy_version="strict-python-v1",
        suppression_policy_version="mutmut15-legacy",
        operator_policy_version="mutmut15-legacy",
        mutation_tool_api_fingerprint="mutmut15-no-exact-key",
        mutation_tool_config_fingerprint=config_fp,
        effective_sampling_config={"enabled": False},
        runtime_fingerprint="python2",
        dependency_fingerprint=context.dependency_fingerprint,
        repo_url=context.repo_url,
        base_ref=context.base_ref,
        base_commit=context.base_commit,
        head_commit=context.head_commit,
        selected_test_paths=context.selected_test_paths,
    )
    return plan.as_dict()


def _modern_mutmut3_candidate_plan(
    *,
    repo: Path,
    target: TargetRef,
    source_path: str,
    test_paths: Sequence[str],
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    coverage: CoverageSummary,
    mutmut_version: str,
    config: _PythonRuntimeConfig,
    enable_ci_sampling: bool,
    base_ref: str = "",
    base_commit: str = "",
    head_commit: str = "",
    repo_url: str = "",
    generation_policy: Optional[Mapping[str, Any]] = None,
    plan_builder: Callable[..., MutationCandidatePlan] = build_mutmut3_candidate_plan_from_meta,
) -> Dict[str, Any]:
    normalized_changed = _normalize_changed_lines(changed_lines)
    if normalized_changed is None:
        return {}
    normalized_source = _normalize_relpath(source_path)
    config_fp = config_fingerprint(
        {
            "candidatePlanEnabled": True,
            "maxChildren": int(getattr(settings, "python_mutation_max_children", 0) or 0),
            "adapterGenerationTimeoutSeconds": int(
                getattr(settings, "python_mutation_adapter_generation_timeout_seconds", 0) or 0
            ),
            "selectedExecutionTimeoutSeconds": int(
                getattr(settings, "python_mutation_selected_execution_timeout_seconds", 0) or 0
            ),
            "perMutantTimeoutSeconds": int(getattr(settings, "python_mutation_per_mutant_timeout_seconds", 0) or 0),
            "generationMaxSourceBytes": int(getattr(settings, "python_mutation_generation_max_source_bytes", 0) or 0),
            "generationMaxChangedLines": int(getattr(settings, "python_mutation_generation_max_changed_lines", 0) or 0),
            "generationMaxOpportunities": int(getattr(settings, "python_mutation_generation_max_opportunities", 0) or 0),
            "generationMaxSelected": int(getattr(settings, "python_mutation_generation_max_selected", 0) or 0),
            "generationCiMaxChangedLines": int(
                getattr(settings, "python_mutation_generation_ci_max_changed_lines", 0) or 0
            ),
            "generationCiMaxOpportunities": int(
                getattr(settings, "python_mutation_generation_ci_max_opportunities", 0) or 0
            ),
            "generationCiMaxSelected": int(getattr(settings, "python_mutation_generation_ci_max_selected", 0) or 0),
        }
    )
    try:
        plan = plan_builder(
            repo,
            source_path=normalized_source,
            target_id=target.target_id,
            changed_lines=normalized_changed,
            covered_lines=normalized_changed.get(normalized_source, set()) if coverage.passed else set(),
            selected_test_paths=tuple(test_paths),
            mutmut_version=mutmut_version,
            runtime_fingerprint="python3",
            dependency_fingerprint=config.cache_key or config_fingerprint(config.dependency_fingerprints),
            config_fingerprint_value=config_fp,
            mutmut_internal_api_fingerprint=f"mutmut3-meta-v{_mutmut_major_version(mutmut_version)}",
            repo_url=repo_url or _git_remote_url(repo),
            base_ref=base_ref,
            base_commit=base_commit,
            head_commit=head_commit or _git_head(repo),
            generation_policy=generation_policy,
        )
        return _candidate_plan_with_generation_policy(plan.as_dict(), generation_policy)
    except Exception as exc:
        return _candidate_plan_with_generation_policy(
            {
                "language": "python",
                "targetId": target.target_id,
                "sourcePath": normalized_source,
                "policyMode": "report_full",
                "filterMechanism": MUTMUT3_METADATA_SELECTED_EXECUTION,
                "changedLines": tuple(sorted(normalized_changed.get(normalized_source, set()))),
                "exactToolCandidateKeys": [],
                "activeSelected": [],
                "reportFullSelected": [],
                "eligibleMutationOpportunities": [
                    {
                        "sourcePath": normalized_source,
                        "selectionReason": "candidate plan construction failed",
                    }
                ],
                "planningError": str(exc),
            },
            generation_policy,
        )


def _candidate_plan_with_generation_policy(
    candidate_plan: Dict[str, Any],
    generation_policy: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    if not generation_policy:
        return candidate_plan
    policy_summary = {
        "capProfile": str(generation_policy.get("capProfile") or "full"),
        "truncated": bool(generation_policy.get("truncated")),
        "truncationReasons": list(generation_policy.get("truncationReasons") or ()),
        "caps": dict(generation_policy.get("caps") or {}),
        "changedLineCount": int(generation_policy.get("changedLineCount") or 0),
        "eligibleOpportunities": int(generation_policy.get("eligibleOpportunities") or 0),
        "selectedOpportunitiesBeforeCap": int(generation_policy.get("selectedOpportunitiesBeforeCap") or 0),
        "hardCapSelectedOpportunities": int(generation_policy.get("hardCapSelectedOpportunities") or 0),
        "selectedOpportunities": int(generation_policy.get("selectedOpportunities") or 0),
        "omittedByCap": int(generation_policy.get("omittedByCap") or 0),
        "omittedByHardCap": int(generation_policy.get("omittedByHardCap") or 0),
        "omittedByRepresentativeSelection": int(generation_policy.get("omittedByRepresentativeSelection") or 0),
    }
    if isinstance(generation_policy.get("hardCaps"), Mapping):
        policy_summary["hardCaps"] = dict(generation_policy.get("hardCaps") or {})
    if isinstance(generation_policy.get("representativeSelection"), Mapping):
        policy_summary["representativeSelection"] = dict(generation_policy.get("representativeSelection") or {})
    path = str(generation_policy.get("path") or "").strip()
    if path:
        candidate_plan["generationPolicyArtifact"] = path
    candidate_plan["generationPolicy"] = policy_summary
    candidate_plan["effectiveCapConfig"] = {
        "profile": policy_summary["capProfile"],
        **policy_summary["caps"],
    }
    return candidate_plan


def _candidate_plan_generated_tool_keys(candidate_plan: Mapping[str, Any]) -> List[str]:
    if not isinstance(candidate_plan, Mapping) or not candidate_plan:
        return []
    exact_keys = candidate_plan.get("exactToolCandidateKeys")
    if isinstance(exact_keys, list):
        return [str(key) for key in exact_keys if str(key or "").strip()]
    selected = candidate_plan.get("activeSelected")
    if not isinstance(selected, list):
        return []
    keys: List[str] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        key = str(item.get("toolCandidateKey") or "").strip()
        if key:
            keys.append(key)
    return keys


def _candidate_plan_with_execution_evidence(
    candidate_plan: Mapping[str, Any],
    mutation: MutationSummary,
) -> Dict[str, Any]:
    plan = dict(candidate_plan)
    generated = int(mutation.generated or 0)
    no_tests = int(mutation.no_tests or 0)
    no_coverage = int(mutation.no_coverage or 0)
    skipped = int(mutation.skipped or 0)
    scored = max(generated - no_tests - no_coverage - skipped, 0)
    active_selected = plan.get("activeSelected") if isinstance(plan.get("activeSelected"), list) else []
    report_full_selected = plan.get("reportFullSelected") if isinstance(plan.get("reportFullSelected"), list) else []
    eligible = plan.get("eligibleMutationOpportunities") if isinstance(plan.get("eligibleMutationOpportunities"), list) else []
    counts = candidate_plan_counts(plan)
    generation_policy = plan.get("generationPolicy") if isinstance(plan.get("generationPolicy"), Mapping) else {}
    pre_generation_selected_opportunities = int(
        generation_policy.get("selectedOpportunities")
        or len(active_selected)
        or counts["selectedCandidates"]
        or 0
    )
    materialized_candidates = counts["selectedCandidates"]
    selected_opportunities = materialized_candidates
    if pre_generation_selected_opportunities > 0:
        pre_generation_materialized_ratio = materialized_candidates / pre_generation_selected_opportunities
    else:
        pre_generation_materialized_ratio = 1.0
    if selected_opportunities > 0:
        materialized_ratio = materialized_candidates / selected_opportunities
    else:
        materialized_ratio = 1.0
    plan.update(
        {
            "selectedKeyExecutionApplied": False,
            "adapterFilteredGenerationApplied": bool(counts["exactToolCandidateKeys"] or active_selected),
            "preGenerationSelectedOpportunities": pre_generation_selected_opportunities,
            "preGenerationMaterializationRatio": round(pre_generation_materialized_ratio, 4),
            "preGenerationMaterializationWarning": (
                pre_generation_selected_opportunities >= 100 and pre_generation_materialized_ratio < 0.5
            ),
            "selectedOpportunities": selected_opportunities,
            "materializedCandidates": materialized_candidates,
            "materializedCandidateRatio": round(materialized_ratio, 4),
            "materializedCandidateWarning": selected_opportunities >= 100 and materialized_ratio < 0.9,
            "eligibleMutationCandidates": counts["eligibleMutationCandidates"],
            "reportFullSelectedCandidates": counts["reportFullSelectedCandidates"],
            "selectedCandidates": counts["selectedCandidates"],
            "suppressedCandidates": counts["suppressedCandidates"],
            "exactToolCandidateKeyCount": counts["exactToolCandidateKeyCount"],
            "opportunityIds": [
                str(item.get("opportunityId") or "")
                for item in eligible
                if isinstance(item, Mapping) and str(item.get("opportunityId") or "").strip()
            ],
            "reportFullCandidateIds": [
                str(item.get("candidateId") or "")
                for item in report_full_selected
                if isinstance(item, Mapping) and str(item.get("candidateId") or "").strip()
            ],
            "activeCandidateIds": [
                str(item.get("candidateId") or "")
                for item in active_selected
                if isinstance(item, Mapping) and str(item.get("candidateId") or "").strip()
            ],
            "activeToolCandidateKeys": [
                str(item.get("toolCandidateKey") or "")
                for item in active_selected
                if isinstance(item, Mapping) and str(item.get("toolCandidateKey") or "").strip()
            ],
            "omittedByOnePerLineCount": counts["omittedByOnePerLine"],
        }
    )
    return candidate_plan_with_execution_evidence(
        plan,
        run_mutants=generated,
        scored_mutants=scored,
        killed=int(mutation.killed or 0),
        survived=int(mutation.survived or 0),
        no_tests=no_tests,
        timeout=int(mutation.timeout or 0),
        suspicious=int(mutation.suspicious or 0),
    )


def _persist_candidate_plan_artifact(
    repo: Path,
    mutation_dir: Path,
    candidate_plan: Mapping[str, Any],
) -> Dict[str, Any]:
    plan = dict(candidate_plan)
    plan_id = str(plan.get("candidatePlanId") or "unknown").strip() or "unknown"
    source = str(plan.get("sourcePath") or "target").replace("\\", "/")
    safe_source = re.sub(r"[^A-Za-z0-9_.-]+", "_", source).strip("_") or "target"
    artifact_path = mutation_dir / f"{safe_source}.{plan_id[:16]}.candidate-plan.json"
    artifact_rel = artifact_path.relative_to(repo).as_posix()
    plan["candidatePlanArtifactPath"] = artifact_rel
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")
    return plan


def _candidate_plan_has_eligible_opportunities(candidate_plan: Mapping[str, Any]) -> bool:
    eligible = candidate_plan.get("eligibleMutationOpportunities")
    return isinstance(eligible, list) and bool(eligible)


def _modern_candidate_plan_enabled(changed_lines: Optional[Mapping[str, Iterable[int]]]) -> bool:
    return _normalize_changed_lines(changed_lines) is not None


def _generated_module_bytes(repo: Path, source_path: str) -> int:
    path = Path(repo) / "mutants" / _normalize_relpath(source_path)
    try:
        return int(path.stat().st_size) if path.exists() else 0
    except OSError:
        return 0


def _git_head(repo: Path) -> str:
    return _git_text(repo, "rev-parse", "HEAD")


def _git_remote_url(repo: Path) -> str:
    return _git_text(repo, "config", "--get", "remote.origin.url")


def _git_text(repo: Path, *args: str) -> str:
    from uta.shared.git import git

    try:
        result = git(timeout=5).run(
            repo,
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            capture_output=False,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return str(result.stdout or "").strip()

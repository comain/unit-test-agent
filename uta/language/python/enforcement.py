from __future__ import annotations

import hashlib
import io
import token
import tokenize
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from uta.enforcement.diff import changed_lines_by_file, changed_paths
from uta.enforcement.enforcement import (
    ValidationVerdict,
    evidence_marker_header,
    evidence_marker_payload,
    finalize_evidence,
    git_output as _git_output,
    validate_evidence_envelope,
)
from uta.shared.languages import RawTargetSelection, default_registry
from uta.enforcement.mutation_candidates import candidate_plan_allows_zero_scored_mutation, candidate_plan_counts
from uta.shared.targets import TargetRef
from uta.enforcement.test_quality import summarize_test_quality_payloads
from uta.language.python.context_builder import PythonContextBuilder
from uta.language.python.test_selection import (
    discover_strict_python_test_candidates,
    is_strict_python_test_candidate_path,
)
from uta.shared.config import settings
from uta.language.python.ci_sampling import CiCapSamplingPolicy
from uta.language.python.mutation_candidates import collect_python_mutation_opportunities
from uta.language.python.test_quality import scan_python_test_quality_evidence
from uta.language.python.verification.runner import (
    CoverageSummary,
    MutationSummary,
    PythonRuntimeConfig,
    PythonVerificationResult,
    precheck_python_large_change,
    precheck_python_target_runtime_incompatibility,
    resolve_python_runtime_config,
)


PYTHON_ENFORCEMENT_SCHEMA_VERSION = 1
PYTHON_ENFORCEMENT_BACKEND = "python_enforcer"
PYTHON_ENFORCEMENT_CORE_VERSION = "1.0.0"
UTA_VERSION = "local"
INTERNAL_ENFORCEMENT_PROFILE_ENV = "UTA_INTERNAL_PYTHON_ENFORCEMENT_PROFILE"
CI_REPORT_ENFORCEMENT_PROFILE = "ci_report_v1"


def ci_mutation_sampling_requested(environment: Mapping[str, str]) -> bool:
    """Return whether the trusted CI adapter requested the CI mutation profile."""
    return environment.get(INTERNAL_ENFORCEMENT_PROFILE_ENV) == CI_REPORT_ENFORCEMENT_PROFILE


class PythonEnforcementStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    missing_evidence = "missing_evidence"
    command_error = "command_error"


def run_python_enforcement(
    *,
    repo_path: Path,
    target_values: Sequence[str],
    test_paths: Sequence[str],
    base_ref: str = "origin/master",
    coverage_gate: float = 80.0,
    mutation_gate: Optional[float] = 70.0,
    run_mutation: bool = True,
    syntax_version: str = "python3",
    runtime_overrides: Optional[Mapping[str, Any]] = None,
    dev_skills_launcher_version: Optional[str] = None,
    enable_ci_mutation_sampling: bool = False,
    enforcement_binding: Optional[Any] = None,
) -> Dict[str, Any]:
    """Verify the changed Python targets and return one evidence envelope.

    One lane. The request goes to an enforcement binding, which reports what
    it ran, and this module projects that into the envelope every consumer
    reads -- the repository facts, the aggregates and the per-target payloads
    are the product's to supply, not the binding's to know.

    `enforcement_binding` is the injection seam: it replaces what verifies,
    which is the only thing a caller has any business replacing.
    """
    return _run_canonical_python_enforcement(
        repo_path=repo_path,
        target_values=target_values,
        test_paths=test_paths,
        base_ref=base_ref,
        coverage_gate=coverage_gate,
        mutation_gate=mutation_gate,
        run_mutation=run_mutation,
        syntax_version=syntax_version,
        runtime_overrides=runtime_overrides,
        dev_skills_launcher_version=dev_skills_launcher_version,
        enable_ci_mutation_sampling=enable_ci_mutation_sampling,
        enforcement_binding=enforcement_binding,
    )



def _preflight(
    envelope: Mapping[str, Any],
    *,
    base_ref: str,
    base_commit: str,
    head_commit: str,
    targets: Sequence[Any],
    test_paths: Sequence[str],
) -> Optional[Dict[str, Any]]:
    """Refuse a run that cannot produce a meaningful verdict. Shared by both lanes.

    These four conditions are the reason this is not inlined in one lane. An
    unresolvable base ref makes `git diff` return nothing, so every file looks
    unchanged and an enforcement gate *passes* -- on a repository it never
    managed to compare. In CI an unfetched or misspelled base ref is the
    realistic failure, not an exotic one, and a gate that certifies a change it
    could not diff fails silently and in the dangerous direction.

    Returns the finished refusal payload, or None to proceed.
    """
    if not base_commit:
        return _finalize({
            **envelope,
            "status": PythonEnforcementStatus.command_error.value,
            "passed": False,
            "reasonCode": "missing_base_ref",
            "summary": f"Base ref is not available: {base_ref}",
        })
    if not head_commit:
        return _finalize({
            **envelope,
            "status": PythonEnforcementStatus.command_error.value,
            "passed": False,
            "reasonCode": "missing_head_commit",
            "summary": "Unable to resolve HEAD",
        })
    if not targets:
        return _finalize({
            **envelope,
            "status": PythonEnforcementStatus.passed.value,
            "passed": True,
            "reasonCode": "no_changed_python_targets",
            "summary": "Python enforcement passed; no changed production Python files",
        })
    if not test_paths:
        return _finalize({
            **envelope,
            "status": PythonEnforcementStatus.missing_evidence.value,
            "passed": False,
            "reasonCode": "missing_test_paths",
            "summary": "Python enforcement requires at least one --test-path",
        })
    return None


def _evidence_envelope(
    *,
    repo: Path,
    base_ref: str,
    base_commit: str,
    head_commit: str,
    changed_files: Sequence[str],
    changed_lines: Mapping[str, Any],
    targets: Sequence[Any],
    dev_skills_launcher_version: Optional[str] = None,
) -> Dict[str, Any]:
    """The evidence envelope, defined once for every lane that emits it.

    Both the legacy and the canonical lane return this shape, because both feed
    the same consumers -- the report UI, the task database, the RDC gate, and
    `validate_python_enforcement_evidence`. A lane that invents its own payload
    does not replace legacy, it breaks whoever reads it.

    Everything here is derived from the repository and the request, not from
    running enforcement, which is why one definition can serve both.
    """
    envelope: Dict[str, Any] = {
        "schemaVersion": PYTHON_ENFORCEMENT_SCHEMA_VERSION,
        "evidenceId": "",
        "language": "python",
        "backend": PYTHON_ENFORCEMENT_BACKEND,
        "repo": str(repo),
        "baseRef": base_ref,
        "baseCommit": base_commit,
        "headRef": "HEAD",
        "headCommit": head_commit,
        "changedProductionFiles": list(changed_files),
        "changedLines": dict(changed_lines),
        "targets": [target.as_selection() for target in targets],
        "coverage": None,
        "mutation": None,
        "commands": [],
        "artifacts": {},
        "setup": {},
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "utaVersion": UTA_VERSION,
        "enforcementCoreVersion": PYTHON_ENFORCEMENT_CORE_VERSION,
    }
    if dev_skills_launcher_version:
        envelope["devSkillsLauncherVersion"] = dev_skills_launcher_version
    return envelope


def _run_canonical_python_enforcement(
    *,
    repo_path: Path,
    target_values: Sequence[str],
    test_paths: Sequence[str],
    base_ref: str = "origin/master",
    coverage_gate: float = 80.0,
    mutation_gate: Optional[float] = 70.0,
    run_mutation: bool = True,
    syntax_version: str = "python3",
    runtime_overrides: Optional[Mapping[str, Any]] = None,
    dev_skills_launcher_version: Optional[str] = None,
    enable_ci_mutation_sampling: bool = False,
    enforcement_binding: Optional[Any] = None,
) -> Dict[str, Any]:
    from uta.enforcement.bindings.python_proxy import UtaPythonEnforcementProxy
    from uta_enforce_core.commands import SafeProcessRunner
    from uta_enforce_core.contracts import (
        EnforcementInvocationContext,
        EnforcementRequest,
        EnforcementTarget,
        QualityGates,
        RuntimeSelection,
    )

    repo = Path(repo_path).expanduser().resolve()
    targets = _target_refs(repo, target_values, base_ref=base_ref)

    # The same four refusals the legacy lane makes, made before the binding is
    # asked to do anything. A lane that replaces legacy has to refuse what
    # legacy refuses, or it certifies work legacy would have blocked.
    base_commit = _git_output(repo, "rev-parse", "--verify", base_ref)
    head_commit = _git_output(repo, "rev-parse", "HEAD")
    changed_files = _changed_production_python_files(repo, base_ref)
    changed_lines = _changed_lines_by_file(
        repo,
        base_ref,
        sorted(set(changed_files + [t.source_path for t in targets if t.source_path])),
    )
    envelope = _evidence_envelope(
            repo=repo,
            base_ref=base_ref,
            base_commit=base_commit,
            head_commit=head_commit,
            changed_files=changed_files,
            changed_lines=changed_lines,
            targets=targets,
            dev_skills_launcher_version=dev_skills_launcher_version,
    )
    refusal = _preflight(
        envelope,
        base_ref=base_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        targets=targets,
        test_paths=test_paths,
    )
    if refusal is not None:
        return refusal

    # A target whose changed lines are all comments or blank is not work the
    # binding should be asked to do: legacy refuses it before test collection,
    # and a lane that replaces legacy has to refuse what legacy refuses. Left
    # in, canonical ran a full pytest and mutmut pass over a comment-only diff
    # and reported it as verification rather than as a skip.
    skipped = [
        target
        for target in targets
        if _has_only_non_executable_changed_lines(repo, target, changed_lines)
    ]
    executable = [target for target in targets if target not in skipped]

    # A target the configured interpreter cannot even compile is skipped, not
    # failed. Enforcing it would report "no test file" for a file no test on
    # this lane could ever cover, and that failure opens an LLM repair that
    # cannot succeed. Legacy ran this before reporting missing test evidence;
    # deleting legacy left the check without a caller, and a py2-only target
    # would have started failing builds.
    runtime_skips: Dict[str, Any] = {}
    config = resolve_python_runtime_config(repo, overrides=runtime_overrides)
    for target in list(executable):
        # Checked before the py_compile probe: a wholesale-rewritten file is
        # skipped on the cheap line count rather than after spawning an
        # interpreter for it.
        skip = precheck_python_large_change(
            target,
            changed_lines=changed_lines,
            coverage_gate=float(coverage_gate),
            mutation_gate=float(mutation_gate or 0.0),
            syntax_version=syntax_version,
            config=config,
        ) or precheck_python_target_runtime_incompatibility(
            repo,
            target,
            syntax_version=syntax_version,
            coverage_gate=float(coverage_gate),
            mutation_gate=float(mutation_gate or 0.0),
            config=config,
            changed_lines=changed_lines,
        )
        if skip is not None:
            executable.remove(target)
            runtime_skips[target.target_id] = (target, skip)

    if not executable:
        return _skipped_only_evidence(
            runtime_skips=runtime_skips,
            envelope=envelope,
            skipped=skipped,
            repo=repo,
            changed_lines=changed_lines,
            coverage_gate=float(coverage_gate),
            mutation_gate=float(mutation_gate or 0.0),
            runtime_overrides=runtime_overrides,
        )

    request = EnforcementRequest(
        repo_path=repo,
        language="python",
        targets=tuple(
            EnforcementTarget(
                language="python",
                target_id=t.target_id,
                source_path=t.source_path or "",
                test_paths=tuple(test_paths),
                metadata={"symbol": t.symbol} if getattr(t, "symbol", None) else {},
            )
            for t in executable
        ),
        quality_gates=QualityGates(
            diff_coverage_min=float(coverage_gate) / 100.0,
            diff_mutation_min=(
                float(mutation_gate or 0.0) / 100.0 if run_mutation else None
            ),
        ),
        runtime=RuntimeSelection(
            python_executable=config.python_bin,
            dependency_overlay_enabled=config.dependency_overlay_enabled,
            syntax_version=syntax_version,
            timeout_seconds=int((runtime_overrides or {}).get("timeout_seconds") or 7200),
        ),
        base_ref=base_ref,
        test_paths=tuple(test_paths),
    )

    # The policy rides on the context rather than on the proxy's factory. The
    # factory only fires for the default proxy, so wiring it there meant any
    # injected binding silently lost CI cost control -- the caller who most
    # needs to see the policy would be the one who never got it.
    proxy = enforcement_binding or UtaPythonEnforcementProxy()
    # The CI cap is a report-wide budget, not a per-target one. `select_mutants`
    # fires once per target, so a policy that re-applied `max_selected` each
    # time let an N-target report run N x the cap -- the exact cost blowup the
    # profile exists to prevent. The budget is therefore divided across targets
    # here, before the binding runs, and each target's share travels with it.
    mutation_selection, mutation_selection_seed = _ci_report_mutation_selection(
        repo=repo,
        targets=executable,
        changed_lines=changed_lines,
        enabled=enable_ci_mutation_sampling,
        base_commit=base_commit,
    )
    mutation_allocations = {path: len(lines) for path, lines in mutation_selection.items()}
    context = EnforcementInvocationContext(
        run_command=SafeProcessRunner(),
        sampling_policy=(
            CiCapSamplingPolicy.from_settings(allocations=mutation_allocations)
            if enable_ci_mutation_sampling
            else None
        ),
    )
    result = proxy.enforce(request, context)

    evidence = _project_binding_evidence(
        result,
        repo=repo,
        base_ref=base_ref,
        targets=executable,
        skipped=skipped,
        runtime_skips=runtime_skips,
        test_paths=test_paths,
        coverage_gate=float(coverage_gate),
        mutation_gate=float(mutation_gate or 0.0),
        runtime_overrides=runtime_overrides,
        dev_skills_launcher_version=dev_skills_launcher_version,
        ci_mutation_sampling=enable_ci_mutation_sampling,
    )
    if enable_ci_mutation_sampling:
        aggregate_plan = evidence.get("mutation") if isinstance(evidence.get("mutation"), Mapping) else {}
        candidate_plan = (aggregate_plan or {}).get("candidatePlan")
        if not isinstance(candidate_plan, Mapping):
            candidate_plan = {}
        evidence["mutationBudget"] = {
            "scope": "report",
            "configuredCandidates": int(settings.python_mutation_generation_ci_max_selected or 0),
            "allocatedCandidates": sum(mutation_allocations.values()),
            "selectedCandidates": int(candidate_plan.get("activeCandidates") or 0),
            "runMutants": int(candidate_plan.get("runMutants") or 0),
            "strategy": "deterministic_target_symbol_operator_hash_v1",
            "seed": mutation_selection_seed,
            "targetAllocations": dict(sorted(mutation_allocations.items())),
        }
    return evidence


def _ci_report_mutation_selection(
    *,
    repo: Path,
    targets: Sequence[TargetRef],
    changed_lines: Mapping[str, Sequence[int]],
    enabled: bool,
    base_commit: str,
) -> "tuple[Dict[str, tuple], str]":
    """Select one deterministic representative mutation sample for the report.

    Round-robin over (target, symbol, operator) groups so the budget spreads
    across files and operators instead of being consumed by whichever target
    happens to sort first. Ordering is a digest of the commit pair, so the
    sample is stable for a given diff -- a CI failure reproduces -- without
    being alphabetical.
    """
    if not enabled:
        return {}, ""
    budget = max(0, int(settings.python_mutation_generation_ci_max_selected or 0))
    selection: Dict[str, List[int]] = {
        str(target.source_path): [] for target in targets if target.source_path
    }
    groups: Dict[tuple, List[Any]] = {}
    opportunity_ids: List[str] = []
    for source_path in sorted(selection):
        lines = sorted({int(line) for line in changed_lines.get(source_path, ()) if int(line) > 0})
        if not lines:
            continue
        try:
            opportunities = collect_python_mutation_opportunities(
                repo / source_path,
                source_path=source_path,
                changed_lines=lines,
            ).selected
        except (OSError, SyntaxError, UnicodeDecodeError):
            opportunities = ()
        for opportunity in opportunities:
            opportunity_ids.append(str(opportunity.opportunity_id))
            groups.setdefault(
                (source_path, str(opportunity.symbol), str(opportunity.operator_name)),
                [],
            ).append(opportunity)
    # Seeding on the head commit reshuffled the sample on every push, so a CI
    # failure stopped reproducing across an amended or rebased commit that did
    # not touch the diff. Addressing the seed by the opportunity set instead
    # keeps the same diff sampling the same mutants.
    seed = hashlib.sha256(
        (
            "deterministic_target_symbol_operator_hash_v1|"
            + str(base_commit)
            + "|"
            + "|".join(sorted(opportunity_ids))
        ).encode("utf-8")
    ).hexdigest()
    for key, items in groups.items():
        groups[key] = sorted(
            items,
            key=lambda item: hashlib.sha256(
                f"{seed}|{item.opportunity_id}".encode("utf-8")
            ).hexdigest(),
        )
    group_order = sorted(
        groups,
        key=lambda key: hashlib.sha256(f"{seed}|{'|'.join(key)}".encode("utf-8")).hexdigest(),
    )
    while budget > 0 and group_order:
        next_groups: List[tuple] = []
        for key in group_order:
            if budget <= 0:
                break
            items = groups[key]
            if items:
                opportunity = items.pop(0)
                selection[key[0]].append(int(opportunity.line))
                budget -= 1
            if items:
                next_groups.append(key)
        group_order = next_groups
    return {path: tuple(sorted(set(lines))) for path, lines in sorted(selection.items())}, seed


def _runtime_skip_payloads(
    runtime_skips: Mapping[str, Any],
    *,
    repo: Path,
    test_paths: Sequence[str],
) -> List[Dict[str, Any]]:
    """Targets the interpreter cannot compile, reported as skips."""
    payloads = []
    for target, result in runtime_skips.values():
        payload = _target_result_evidence(target, result)
        payload["configuredTestPaths"] = [str(path) for path in test_paths]
        payload["selectedTestPaths"] = []
        payload["candidateTestPaths"] = []
        payload["candidateResults"] = []
        payload["testQuality"] = scan_python_test_quality_evidence(repo, [])
        payloads.append(payload)
    return payloads


def _skipped_target_payload(
    target: Any,
    *,
    repo: Path,
    changed_lines: Mapping[str, Sequence[int]],
    coverage_gate: float,
    mutation_gate: float,
    runtime_overrides: Optional[Mapping[str, Any]],
    test_paths: Sequence[str],
) -> Dict[str, Any]:
    """One comment-only target, in the shape a verified target has.

    Built from the same `_non_executable_changed_lines_result` the legacy lane
    uses, so the two lanes agree on `no_executable_changed_lines` rather than
    agreeing only that the run passed.
    """
    config = resolve_python_runtime_config(repo, overrides=runtime_overrides)
    result = _non_executable_changed_lines_result(
        target=target,
        changed_lines=changed_lines,
        coverage_gate=coverage_gate,
        mutation_gate=mutation_gate,
        config=config,
    )
    payload = _target_result_evidence(target, result)
    payload["configuredTestPaths"] = [str(path) for path in test_paths]
    payload["selectedTestPaths"] = []
    payload["candidateTestPaths"] = []
    payload["candidateResults"] = []
    payload["testQuality"] = scan_python_test_quality_evidence(repo, [])
    return payload


def _skipped_only_evidence(
    *,
    runtime_skips: Mapping[str, Any] = {},
    envelope: Dict[str, Any],
    skipped: Sequence[Any],
    repo: Path,
    changed_lines: Mapping[str, Sequence[int]],
    coverage_gate: float,
    mutation_gate: float,
    runtime_overrides: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Every target was comment-only, so the binding is never invoked."""
    target_results = [
        _skipped_target_payload(
            target,
            repo=repo,
            changed_lines=changed_lines,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            runtime_overrides=runtime_overrides,
            test_paths=(),
        )
        for target in skipped
    ]
    target_results += _runtime_skip_payloads(runtime_skips, repo=repo, test_paths=())
    # Two ways to skip everything, and they are not the same finding. Naming
    # the comment-only reason for a run that skipped on interpreter
    # incompatibility would send a reader looking for comments.
    if skipped and not runtime_skips:
        reason = "no_executable_changed_lines"
        summary = "No executable changed Python lines to verify"
    elif runtime_skips and not skipped:
        reason = "passed"
        skip_reasons = {result.reason_code for _, result in runtime_skips.values()}
        if skip_reasons == {"python_large_change_skipped"}:
            summary = "All changed Python targets were skipped as too large to verify"
        elif skip_reasons == {"python_runtime_incompatible_skipped"}:
            summary = "All changed Python targets were skipped as runtime-incompatible"
        else:
            summary = "All changed Python targets were skipped"
    else:
        reason = "passed"
        summary = "All changed Python targets were skipped"
    envelope.update(
        {
            "status": PythonEnforcementStatus.passed.value,
            "passed": True,
            "reasonCode": reason,
            "summary": summary,
            "targetResults": target_results,
            "coverage": _aggregate_coverage(target_results, coverage_gate),
            "mutation": _aggregate_mutation(target_results, mutation_gate),
            "commands": [],
            "artifacts": _collect_artifacts(target_results),
            "setup": _setup_evidence(repo, {}, runtime_overrides),
        }
    )
    return _finalize(envelope)


def _project_binding_evidence(
    result: Any,
    *,
    repo: Path,
    base_ref: str,
    targets: Sequence[Any],
    test_paths: Sequence[str],
    skipped: Sequence[Any] = (),
    runtime_skips: Mapping[str, Any] = {},
    coverage_gate: float,
    mutation_gate: float,
    runtime_overrides: Optional[Mapping[str, Any]] = None,
    dev_skills_launcher_version: Optional[str] = None,
    ci_mutation_sampling: bool = False,
) -> Dict[str, Any]:
    """Put the binding's execution evidence inside UTA's envelope.

    The binding reports what it *ran* -- per-target coverage, mutation and the
    commands it issued. It does not know the repository facts, the versions or
    the aggregate shape UTA's consumers read, and it should not: those are the
    product's. This projects one into the other.

    Aggregation deliberately reuses `_aggregate_coverage` and
    `_aggregate_mutation`, the same functions the legacy lane uses, rather than
    the binding's own roll-up. Two aggregations that agree today drift
    tomorrow, and the candidate plan the spec calls compatible-or-nothing only
    survives one of them.
    """
    evidence = dict(getattr(result, "evidence", {}) or {})
    base_commit = _git_output(repo, "rev-parse", "--verify", base_ref)
    head_commit = _git_output(repo, "rev-parse", "HEAD")
    changed_files = _changed_production_python_files(repo, base_ref)
    target_source_paths = [target.source_path for target in targets if target.source_path]
    changed_lines = _changed_lines_by_file(
        repo, base_ref, sorted(set(changed_files + target_source_paths))
    )

    envelope = _evidence_envelope(
        repo=repo,
        base_ref=base_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        changed_files=changed_files,
        changed_lines=changed_lines,
        targets=targets,
        dev_skills_launcher_version=dev_skills_launcher_version,
    )

    setup = _setup_evidence(repo, evidence, runtime_overrides)

    target_results = [
        _skipped_target_payload(
            target,
            repo=repo,
            changed_lines=changed_lines,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            runtime_overrides=runtime_overrides,
            test_paths=test_paths,
        )
        for target in skipped
    ]
    target_results += _runtime_skip_payloads(
        runtime_skips, repo=repo, test_paths=test_paths
    )
    prefix = len(target_results)
    target_results += [dict(item) for item in (evidence.get("targets") or [])]
    # The binding appends one evidence entry per requested target, in request
    # order, on every path it takes -- so position identifies the target. It
    # does not carry the target back, and it should not: `as_selection()` is
    # UTA's shape, built from UTA's TargetRef.
    for payload, target in zip(target_results[prefix:], targets):
        payload.setdefault("configuredTestPaths", [str(path) for path in test_paths])
        selected = payload.pop("selectedTest", None)
        payload.setdefault("selectedTestPaths", [selected] if selected else [])
        payload.setdefault("candidateTestPaths", list(payload["selectedTestPaths"]))
        payload.setdefault("target", target.as_selection())
        payload.setdefault("setup", _target_setup_evidence(setup, payload))
        payload.setdefault(
            "candidateResults", _binding_candidate_results(payload)
        )
        payload.setdefault(
            "testQuality",
            scan_python_test_quality_evidence(repo, payload["selectedTestPaths"]),
        )

    if ci_mutation_sampling:
        _reclassify_sampled_mutation_misses(target_results)

    status = str(evidence.get("status") or "")
    passed = status == PythonEnforcementStatus.passed.value
    if ci_mutation_sampling and not passed and not _has_unsampled_failure(target_results):
        status = PythonEnforcementStatus.passed.value
        passed = True
    reason_code, summary = _diagnosis(evidence, target_results, passed=passed)

    coverage = _aggregate_coverage(target_results, coverage_gate)
    mutation = _aggregate_mutation(target_results, mutation_gate)
    if ci_mutation_sampling and mutation is not None:
        # One report-wide statistical gate. A sampled target sees a handful of
        # mutants, so its own rate is noise -- gating on it fails builds for
        # sampling luck. The aggregate over the whole report is the number the
        # profile can actually stand behind.
        mutation["gateScope"] = "report"
        mutation["passed"] = float(mutation.get("rate") or 0.0) >= float(mutation_gate)
        if not mutation["passed"] and passed:
            status = PythonEnforcementStatus.failed.value
            passed = False
            reason_code = "mutation_gate_failed"
            summary = (
                f"Python aggregate mutation score {float(mutation.get('rate') or 0.0):.2f}% "
                f"is below gate {float(mutation_gate):.2f}%"
            )

    envelope.update(
        {
            "status": status,
            "passed": passed,
            "reasonCode": reason_code,
            "summary": summary,
            "targetResults": target_results,
            "coverage": coverage,
            "mutation": mutation,
            "commands": [_legacy_command(item) for item in (evidence.get("commands") or [])],
            "artifacts": _collect_artifacts(target_results),
            "setup": setup,
        }
    )
    return _finalize(envelope)


def _reclassify_sampled_mutation_misses(target_results: List[Dict[str, Any]]) -> None:
    """A sampled target's mutation score is diagnostic, not a gate.

    Under the CI profile a target is scored on its share of a report-wide
    mutant budget -- often a single mutant. Failing the target on that is
    failing it on which mutant the sampler happened to pick. The score stays
    visible in the evidence; only its gating power is removed.
    """
    for payload in target_results:
        if str(payload.get("reasonCode") or "") not in {"mutation_gate_failed", "mutation_failed"}:
            continue
        if not payload.get("testsPass"):
            continue
        coverage = payload.get("coverage")
        if not isinstance(coverage, Mapping) or not coverage.get("passed"):
            continue
        if payload.get("mutation") is None:
            continue
        payload["status"] = "passed"
        payload["reasonCode"] = "sampled_mutation_diagnostic"
        payload["message"] = (
            "Sampled target mutation rate is diagnostic; the CI mutation gate "
            "is enforced on the report aggregate."
        )


def _has_unsampled_failure(target_results: Sequence[Mapping[str, Any]]) -> bool:
    """Whether anything other than a sampled mutation miss is still failing."""
    return any(
        str(payload.get("status") or "") != "passed"
        and str(payload.get("reasonCode") or "") not in {"", "passed", "sampled_mutation_diagnostic"}
        for payload in target_results
    )


#: The binding only ever reports these two at the top level. Anything more
#: specific has to come from the target that actually failed.
_GENERIC_REASONS = {"", "passed", "quality_gate_failed", "failed"}

#: The binding says "no targets"; every UTA consumer, including UTA's own
#: validator, knows this condition by the longer name.
_REASON_ALIASES = {"no_targets": "no_changed_python_targets"}


def _diagnosis(
    evidence: Mapping[str, Any],
    target_results: Sequence[Mapping[str, Any]],
    *,
    passed: bool,
) -> "tuple[str, str]":
    """The reason code and summary, taken from the target that failed.

    The binding reports `quality_gate_failed` for everything, which tells a
    reader nothing when the real cause was a test that would not import or a
    file that would not compile. Legacy walks the targets and keeps the first
    non-passing one's reason and message; this does the same, from the same
    data, which the binding already puts in `evidence["targets"]`.
    """
    top = _REASON_ALIASES.get(str(evidence.get("reasonCode") or ""), str(evidence.get("reasonCode") or ""))
    if passed:
        return (top or "passed"), "Python enforcement passed"

    for target in target_results:
        # A skip is not a failure. Skipped targets pass with a non-`passed`
        # reason code (`no_executable_changed_lines`), so matching on the
        # reason alone let one supply the whole report's diagnosis -- naming a
        # skip as the cause and carrying its "enforcement passed" message into
        # a failed report, while the targets that actually failed went unnamed.
        if str(target.get("status") or "") == "passed":
            continue
        target_reason = str(target.get("reasonCode") or "")
        if target_reason and target_reason != "passed":
            message = str(target.get("message") or "")
            return target_reason, message or f"Python enforcement failed: {target_reason}"

    reason = top if top not in _GENERIC_REASONS else "quality_gate_failed"
    return reason, f"Python enforcement failed: {reason}"


def _legacy_command(command: Mapping[str, Any]) -> Dict[str, Any]:
    """Rename the binding's camelCase back to the frozen field names.

    `exitCode` where a consumer reads `exit_code` does not raise -- it reads as
    a missing value, so every command silently looks successful.
    """
    item = dict(command)
    if "exitCode" in item:
        item["exit_code"] = item.pop("exitCode")
    if "elapsedSeconds" in item:
        item["elapsed_seconds"] = item.pop("elapsedSeconds")
    item.setdefault("exit_code", None)
    item.setdefault("name", "")
    return item


#: The two overlay outcomes the binding reports, named exactly as the legacy
#: lane names them. Anything else means no overlay was needed at all.
_OVERLAY_SETUP_STATUS = {
    "dependency_overlay_install": "executed",
    "dependency_overlay_cached": "cached",
    "dependency_overlay_compat_install": "compat",
    "dependency_overlay_compat_cached": "compat",
}


def _target_setup_evidence(
    setup: Mapping[str, Any], payload: Mapping[str, Any]
) -> Dict[str, Any]:
    """The per-target setup block, in the shape consumers already read.

    Three of the four fields are the run's resolved runtime config, identical
    for every target -- so they come from the envelope's own `setup` rather
    than being resolved a second time. `setupStatus` is per target, because
    only some targets have a nested dependency overlay, and the binding
    reports the outcome under the same two command names the legacy lane uses.
    """
    status = "skipped"
    for command in payload.get("commands") or ():
        mapped = _OVERLAY_SETUP_STATUS.get(str((command or {}).get("name") or ""))
        if mapped is not None:
            status = mapped
    return {
        "setupStatus": status,
        "environmentProfile": setup.get("environmentProfile"),
        "dependencyFingerprints": dict(setup.get("dependencyFingerprints") or {}),
        "cacheKey": setup.get("cacheKey"),
    }


def _binding_candidate_results(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The one candidate the binding verified, in the legacy candidate shape.

    The legacy lane walks every discovered candidate test until one passes and
    records an entry per attempt. The binding selects candidates the same way
    but verifies only the first, so there is exactly one attempt to report --
    never zero, which is what `ci_evidence` reads to explain a `test_failed`
    target and what an empty list would silently withhold.
    """
    selected = list(payload.get("selectedTestPaths") or [])
    if not selected:
        return []
    return [
        {
            "testPaths": selected,
            "status": payload.get("status"),
            "reasonCode": payload.get("reasonCode"),
            "testsPass": payload.get("testsPass"),
            "message": payload.get("message"),
            "coverage": payload.get("coverage"),
            "mutation": payload.get("mutation"),
        }
    ]


def _collect_artifacts(target_results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    artifacts: Dict[str, Any] = {}
    for item in target_results:
        for key, value in (item.get("artifacts") or {}).items():
            artifacts.setdefault(key, []).append(value)
    return artifacts


def _setup_evidence(
    repo: Path,
    evidence: Mapping[str, Any],
    runtime_overrides: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """The runtime facts, resolved the way the legacy lane resolves them.

    `environmentProfile` comes from the resolved config, not from the binding.
    The binding reports the interpreter it chose ("python3"); the envelope field
    names the *profile* UTA resolved ("default"), and that is the value already
    frozen in evidence consumers.
    """
    config = resolve_python_runtime_config(repo, overrides=runtime_overrides)
    return {
        "environmentProfile": config.environment_profile,
        "dependencyFingerprints": dict(config.dependency_fingerprints),
        "cacheKey": config.cache_key,
        "configSources": dict(config.config_sources),
    }


def validate_python_enforcement_evidence(
    evidence: Mapping[str, Any],
    *,
    expected_head: Optional[str] = None,
) -> ValidationVerdict:
    verdict = validate_evidence_envelope(
        evidence,
        language="python",
        backend=PYTHON_ENFORCEMENT_BACKEND,
        schema_version=PYTHON_ENFORCEMENT_SCHEMA_VERSION,
        expected_head=expected_head,
    )
    if verdict is not None:
        return verdict
    coverage = evidence.get("coverage") or {}
    mutation = evidence.get("mutation") or {}
    if evidence.get("reasonCode") != "no_changed_python_targets":
        if coverage.get("passed") is not True:
            return ValidationVerdict(False, "coverage_gate_failed", "Python evidence did not include passing coverage")
        if coverage.get("no_executable_changed_lines") is True:
            return ValidationVerdict(True, "passed", str(evidence.get("summary") or "Python enforcement passed"))
        if mutation.get("passed") is not True:
            return ValidationVerdict(False, "mutation_gate_failed", "Python evidence did not include passing mutation")
        if (
            mutation.get("scope") == "changed_lines"
            and _has_changed_line_payload(evidence, mutation)
            and int(mutation.get("changedLineMutantsGenerated") or 0) <= 0
            and not candidate_plan_allows_zero_scored_mutation(mutation.get("candidatePlan") or {})
            # Aggregation does not preserve a single candidate plan. Accept
            # zero mutants only when every target independently proves that
            # its deterministic candidate plan had nothing eligible to score.
            and not _target_plans_allow_zero_scored_mutation(evidence)
        ):
            return ValidationVerdict(
                False,
                "missing_mutation_evidence",
                "Python changed-line mutation evidence did not include generated changed-line mutants",
            )
    return ValidationVerdict(True, "passed", str(evidence.get("summary") or "Python enforcement passed"))


def _target_plans_allow_zero_scored_mutation(evidence: Mapping[str, Any]) -> bool:
    target_results = evidence.get("targetResults") or []
    if not isinstance(target_results, list) or not target_results:
        return False
    for target_result in target_results:
        if not isinstance(target_result, Mapping):
            return False
        mutation = target_result.get("mutation")
        if not isinstance(mutation, Mapping):
            return False
        if int(mutation.get("changedLineMutantsGenerated") or 0) > 0:
            return False
        if not candidate_plan_allows_zero_scored_mutation(mutation.get("candidatePlan") or {}):
            return False
    return True


def format_evidence_markers(evidence: Mapping[str, Any]) -> str:
    lines = [
        evidence_marker_header("python", evidence),
    ]
    coverage = evidence.get("coverage") or {}
    mutation = evidence.get("mutation") or {}
    if coverage:
        state = "passed" if coverage.get("passed") else "failed"
        lines.append(
            "[test-enforcer] python diff line coverage "
            f"{float(coverage.get('rate') or 0.0):.2f}% {state} "
            f"({int(coverage.get('covered') or 0)}/{int(coverage.get('total') or 0)})"
        )
    if mutation:
        state = "passed" if mutation.get("passed") else "failed"
        if mutation.get("scope") == "changed_lines":
            lines.append(
                "[test-enforcer] python diff mutation score "
                f"{float(mutation.get('rate') or 0.0):.2f}% {state} "
                f"({int(mutation.get('survived') or 0)} diff survivors, "
                f"{int(mutation.get('changedLineMutantsGenerated') or 0)} diff mutants, "
                f"{int(mutation.get('generated') or 0)} file mutants)"
            )
        else:
            lines.append(
                "[test-enforcer] python diff mutation score "
                f"{float(mutation.get('rate') or 0.0):.2f}% {state} "
                f"({int(mutation.get('killed') or 0)}/{int(mutation.get('generated') or 0)} detected)"
            )
        candidate_plan = mutation.get("candidatePlan") if isinstance(mutation.get("candidatePlan"), dict) else {}
        if candidate_plan:
            lines.append(
                "[test-enforcer] python mutation candidate plan "
                f"{candidate_plan.get('filterMechanism') or 'unknown'} "
                f"eligible={len(candidate_plan.get('eligibleMutationOpportunities') or [])} "
                f"selected={len(candidate_plan.get('activeSelected') or [])}/"
                f"{len(candidate_plan.get('reportFullSelected') or [])} "
                f"scored={int(candidate_plan.get('scoredMutants') or 0)} "
                f"killed={int(candidate_plan.get('killed') or 0)} "
                f"survived={int(candidate_plan.get('survived') or 0)}"
            )
    quality_line = _test_quality_marker_line(evidence)
    if quality_line:
        lines.append(quality_line)
    lines.append(evidence_marker_payload("python", evidence))
    return "\n".join(lines) + "\n"


def _test_quality_marker_line(evidence: Mapping[str, Any]) -> str:
    summary = summarize_test_quality_payloads([evidence.get("testQuality")] + [
        item.get("testQuality")
        for item in (evidence.get("targetResults") or [])
        if isinstance(item, Mapping)
    ])
    warning_count = int(summary.get("warningCount") or 0)
    if not warning_count:
        return ""
    top_rules = ",".join(
        f"{item['ruleId']}x{int(item.get('count') or 0)}"
        for item in (summary.get("topRuleIds") or [])[:3]
        if isinstance(item, Mapping) and item.get("ruleId")
    )
    top_part = f" top={top_rules}" if top_rules else ""
    return (
        f"[test-enforcer] python test quality {warning_count} advisory warning(s)"
        f"{top_part} (coverage/mutation gates unaffected)"
    )


def _target_refs(repo: Path, target_values: Sequence[str], *, base_ref: str) -> List[TargetRef]:
    adapter = default_registry().adapter_for("python")
    values = list(target_values or []) or _changed_production_python_files(repo, base_ref)
    return [adapter.normalize_target(RawTargetSelection(target=value)) for value in values]


def _changed_production_python_files(repo: Path, base_ref: str) -> List[str]:
    # Keep the Python-specific production filter local (its semantics feed the CI
    # gate); only the git mechanics are shared via the engine helper.
    return [path for path in changed_paths(repo, base_ref) if _is_production_python_path(path)]


def _changed_lines_by_file(repo: Path, base_ref: str, paths: Sequence[str]) -> Dict[str, List[int]]:
    return changed_lines_by_file(repo, base_ref, paths)


def _has_only_non_executable_changed_lines(
    repo: Path,
    target: TargetRef,
    changed_lines: Mapping[str, Sequence[int]],
) -> bool:
    source_path = str(target.source_path or "").replace("\\", "/")
    target_lines = {int(line) for line in changed_lines.get(source_path, ())}
    if not source_path:
        return False
    # No added lines at all -- a deletion-only or pure-rename change. There is
    # nothing here to verify, and saying so is the vacuous case of this very
    # predicate. Reporting it as "not comment-only" sent the target through full
    # verification, where coverage fell back to the whole file and invented a
    # changed-line obligation the diff never created.
    if not target_lines:
        return True
    try:
        source = (repo / source_path).read_text(encoding="utf-8")
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


def _non_executable_changed_lines_result(
    *,
    target: TargetRef,
    changed_lines: Mapping[str, Sequence[int]],
    coverage_gate: float,
    mutation_gate: float,
    config: PythonRuntimeConfig,
) -> PythonVerificationResult:
    source_path = str(target.source_path or "").replace("\\", "/")
    scoped_lines = {source_path: sorted(int(line) for line in changed_lines.get(source_path, ()))}
    return PythonVerificationResult(
        status="passed",
        reason_code="no_executable_changed_lines",
        tests_pass=True,
        coverage=CoverageSummary(
            covered=0,
            total=0,
            rate=100.0,
            gate=coverage_gate,
            passed=True,
            xml_path="",
            scope="changed_lines",
            changed_lines=scoped_lines,
            no_executable_changed_lines=True,
        ),
        mutation=MutationSummary(
            runtime_lane="not_run",
            generated=0,
            killed=0,
            survived=0,
            no_coverage=0,
            rate=100.0,
            gate=mutation_gate,
            passed=True,
            scope="changed_lines",
            changed_lines=scoped_lines,
        ),
        message="Python enforcement passed; changed target lines are comments or whitespace",
        environment_profile=config.environment_profile,
        dependency_fingerprints=dict(config.dependency_fingerprints),
        cache_key=config.cache_key,
    )


def _is_production_python_path(path: str) -> bool:
    from uta_enforce_core.diff import is_production_python_path

    return is_production_python_path(path)


def _target_result_evidence(target: TargetRef, result: PythonVerificationResult) -> Dict[str, Any]:
    mutation_artifacts = result.mutation.artifacts if result.mutation else {}
    return {
        "target": target.as_selection(),
        "status": result.status,
        "reasonCode": result.reason_code,
        "testsPass": result.tests_pass,
        "message": result.message,
        "coverage": result.coverage.as_dict() if result.coverage else None,
        "mutation": result.mutation.as_dict() if result.mutation else None,
        "commands": [command.as_dict() for command in result.commands],
        "artifacts": dict(mutation_artifacts),
        "setup": {
            "setupStatus": result.setup_status,
            "environmentProfile": result.environment_profile,
            "dependencyFingerprints": dict(result.dependency_fingerprints),
            "cacheKey": result.cache_key,
        },
    }


def _target_evidence_test_paths(repo: Path, target: TargetRef, *, configured_test_paths: Sequence[str]) -> List[str]:
    context_payload: Dict[str, Any] = {}
    try:
        context_payload = PythonContextBuilder(repo).build_target_context(target)
    except Exception:
        context_payload = {}
    candidates = discover_strict_python_test_candidates(repo, target, context_payload=context_payload)
    selected = [
        str(candidate.path).replace("\\", "/")
        for candidate in candidates
        if candidate.path and (repo / candidate.path).exists()
    ]
    if selected:
        return list(dict.fromkeys(selected))
    explicit_files = [
        str(path).replace("\\", "/")
        for path in configured_test_paths
        if str(path).strip().endswith(".py")
        and (repo / str(path)).exists()
        and is_strict_python_test_candidate_path(str(path), target, context_payload=context_payload)
    ]
    if explicit_files:
        return list(dict.fromkeys(explicit_files))
    return []


def _aggregate_coverage(target_evidence: Sequence[Mapping[str, Any]], gate: float) -> Optional[Dict[str, Any]]:
    summaries = [item.get("coverage") for item in target_evidence if item.get("coverage")]
    if not summaries:
        return None
    covered = sum(int(summary.get("covered") or 0) for summary in summaries)
    total = sum(int(summary.get("total") or 0) for summary in summaries)
    scope = _aggregate_scope(summaries)
    changed_lines = _merge_changed_lines(summaries)
    all_passed = all(summary.get("passed") is True for summary in summaries)
    if scope == "changed_lines" and changed_lines and total == 0 and all_passed:
        rate = 100.0
    else:
        rate = 100.0 if total == 0 else round((covered / total) * 100.0, 4)
    no_executable_changed_lines = all(summary.get("no_executable_changed_lines") is True for summary in summaries)
    payload: Dict[str, Any] = {
        "covered": covered,
        "total": total,
        "rate": rate,
        "gate": gate,
        "passed": all_passed and rate >= gate,
        "no_executable_changed_lines": no_executable_changed_lines,
        "modules": len(summaries),
    }
    if scope:
        payload["scope"] = scope
    if changed_lines:
        payload["changed_lines"] = changed_lines
    return payload


def _aggregate_mutation(target_evidence: Sequence[Mapping[str, Any]], gate: float) -> Optional[Dict[str, Any]]:
    summaries = [item.get("mutation") for item in target_evidence if item.get("mutation")]
    if not summaries:
        return None
    generated = sum(int(summary.get("generated") or 0) for summary in summaries)
    killed = sum(int(summary.get("killed") or 0) for summary in summaries)
    survived = sum(int(summary.get("survived") or 0) for summary in summaries)
    no_coverage = sum(int(summary.get("no_coverage") or 0) for summary in summaries)
    no_tests = sum(int(summary.get("no_tests") or 0) for summary in summaries)
    timeout = sum(int(summary.get("timeout") or 0) for summary in summaries)
    suspicious = sum(int(summary.get("suspicious") or 0) for summary in summaries)
    changed_line_generated = sum(int(summary.get("changedLineMutantsGenerated") or 0) for summary in summaries)
    changed_line_killed = sum(int(summary.get("changedLineMutantsKilled") or 0) for summary in summaries)
    changed_line_scored = sum(
        _scored_changed_line_mutants(summary)
        for summary in summaries
    )
    scope = _aggregate_scope(summaries)
    changed_lines = _merge_changed_lines(summaries)
    diff_survivors = [
        survivor
        for summary in summaries
        for survivor in (summary.get("diff_survivors") or [])
        if isinstance(survivor, dict)
    ]
    all_passed = all(summary.get("passed") is True for summary in summaries)
    failed_rates = [float(summary.get("rate") or 0.0) for summary in summaries if summary.get("passed") is not True]
    if scope == "changed_lines":
        if changed_line_scored > 0:
            rate = round((changed_line_killed / changed_line_scored) * 100.0, 4)
        elif changed_line_generated > 0:
            rate = 100.0
        elif changed_lines:
            # A target with no materializable candidates is a valid pass when
            # its verifier explicitly accepted that outcome. Do not contradict
            # the target decision merely because changed source lines exist.
            rate = 100.0 if all_passed else 0.0
        else:
            rate = 100.0
        passed = all_passed and rate >= gate
    else:
        denominator = killed + survived
        rate = 100.0 if denominator == 0 else round((killed / denominator) * 100.0, 4)
        passed = all_passed and rate >= gate
    payload: Dict[str, Any] = {
        "generated": generated,
        "killed": killed,
        "survived": survived,
        "noCoverage": no_coverage,
        "noTests": no_tests,
        "timeout": timeout,
        "suspicious": suspicious,
        "changedLineMutantsGenerated": changed_line_generated,
        "changedLineMutantsKilled": changed_line_killed,
        "changedLineMutantsScored": changed_line_scored,
        "rate": rate,
        "gate": gate,
        "passed": passed,
        "modules": len(summaries),
    }
    if failed_rates:
        payload["worstTargetRate"] = min(failed_rates)
    if scope:
        payload["scope"] = scope
    if changed_lines:
        payload["changed_lines"] = changed_lines
    if diff_survivors:
        payload["diff_survivors"] = diff_survivors
    sampled_summaries = [
        summary.get("sampling")
        for summary in summaries
        if isinstance(summary.get("sampling"), dict) and summary.get("sampling", {}).get("enabled")
    ]
    if sampled_summaries:
        payload["sampled"] = True
        payload["sampledTargets"] = len(sampled_summaries)
        payload["sampling"] = {
            "strategy": "deterministic_changed_line_hash",
            "targets": sampled_summaries,
            "totalChangedLines": sum(int(item.get("totalChangedLines") or 0) for item in sampled_summaries),
            "sampledChangedLines": sum(int(item.get("sampledChangedLines") or 0) for item in sampled_summaries),
        }
    candidate_plan = _aggregate_candidate_plan(summaries)
    if candidate_plan:
        payload["candidatePlan"] = candidate_plan
    return payload


def _scored_changed_line_mutants(summary: Mapping[str, Any]) -> int:
    explicit = summary.get("changedLineMutantsScored")
    if explicit is not None:
        return int(explicit or 0)
    generated = int(summary.get("changedLineMutantsGenerated") or 0)
    no_tests = int(summary.get("no_tests") or summary.get("noTests") or 0)
    no_coverage = int(summary.get("no_coverage") or summary.get("noCoverage") or 0)
    skipped = int(summary.get("skipped") or 0)
    return max(generated - no_tests - no_coverage - skipped, 0)


def _aggregate_candidate_plan(summaries: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    plans = [
        summary.get("candidatePlan")
        for summary in summaries
        if isinstance(summary.get("candidatePlan"), dict) and summary.get("candidatePlan")
    ]
    if not plans:
        return {}
    sampling_layers = [
        plan.get("samplingLayer")
        for plan in plans
        if isinstance(plan.get("samplingLayer"), dict) and plan.get("samplingLayer", {}).get("enabled")
    ]
    plan_counts = [candidate_plan_counts(plan) for plan in plans]
    return {
        "targetPlans": len(plans),
        "filterMechanisms": sorted({str(plan.get("filterMechanism") or "") for plan in plans if plan.get("filterMechanism")}),
        "changedLines": sum(counts["changedLines"] for counts in plan_counts),
        "coveredChangedLines": sum(counts["coveredChangedLines"] for counts in plan_counts),
        "eligibleOpportunities": sum(counts["eligibleOpportunities"] for counts in plan_counts),
        "eligibleMutationCandidates": sum(counts["eligibleMutationCandidates"] for counts in plan_counts),
        "suppressedCandidates": sum(counts["suppressedCandidates"] for counts in plan_counts),
        "suppressedOpportunities": sum(counts["suppressedOpportunities"] for counts in plan_counts),
        "omittedByOnePerLine": sum(counts["omittedByOnePerLine"] for counts in plan_counts),
        "reportFullCandidates": sum(counts["reportFullCandidates"] for counts in plan_counts),
        "activeCandidates": sum(counts["activeCandidates"] for counts in plan_counts),
        "exactToolCandidateKeys": sum(counts["exactToolCandidateKeys"] for counts in plan_counts),
        "runMutants": sum(counts["runMutants"] for counts in plan_counts),
        "scoredMutants": sum(counts["scoredMutants"] for counts in plan_counts),
        "killed": sum(counts["killed"] for counts in plan_counts),
        "survived": sum(counts["survived"] for counts in plan_counts),
        "noTests": sum(counts["noTests"] for counts in plan_counts),
        "timeout": sum(counts["timeout"] for counts in plan_counts),
        "suspicious": sum(counts["suspicious"] for counts in plan_counts),
        "sampled": bool(sampling_layers),
        "sampledTargets": len(sampling_layers),
    }


def _aggregate_scope(summaries: Sequence[Mapping[str, Any]]) -> Optional[str]:
    scopes = {str(summary.get("scope") or "") for summary in summaries}
    scopes.discard("")
    return next(iter(scopes)) if len(scopes) == 1 else None


def _merge_changed_lines(summaries: Sequence[Mapping[str, Any]]) -> Dict[str, List[int]]:
    merged: Dict[str, set[int]] = {}
    for summary in summaries:
        changed_lines = summary.get("changed_lines") or {}
        if not isinstance(changed_lines, Mapping):
            continue
        for path, lines in changed_lines.items():
            normalized = str(path or "").replace("\\", "/")
            if not normalized:
                continue
            merged.setdefault(normalized, set()).update(int(line) for line in lines or [] if int(line) > 0)
    return {path: sorted(lines) for path, lines in sorted(merged.items())}


def _has_changed_line_payload(evidence: Mapping[str, Any], mutation: Mapping[str, Any]) -> bool:
    for payload in (evidence.get("changedLines"), mutation.get("changed_lines")):
        if not isinstance(payload, Mapping):
            continue
        for lines in payload.values():
            if lines:
                return True
    return False


def _finalize(evidence: Dict[str, Any]) -> Dict[str, Any]:
    return finalize_evidence(evidence, evidence_id_prefix="uta-python-enforcement")

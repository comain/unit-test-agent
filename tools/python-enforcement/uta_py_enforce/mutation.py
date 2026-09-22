"""Mutation verification for lightweight Python enforcement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
import os
import shutil
from typing import Any, Mapping, Sequence

from uta_py_enforce.config import (
    GENERATION_HARD_CAP,
    KEEP_MUTANTS_ENV,
    env_without_ci_sampling,
    generation_bytes_per_line_factor,
    generation_max_batches,
    generation_max_generated_bytes,
    generation_strategy,
)
from uta_py_enforce.mutation_candidates import build_mutmut3_candidate_plan_from_meta
from uta_py_enforce.mutation_policy import build_generation_policy, suppression_counts
from uta_py_enforce.mutmut_adapter import adapter_command
from uta_py_enforce.pytest_env import mutation_pytest_env, pytest_process_command
from uta_py_enforce.module_resolver import resolve_python_module
from uta_py_enforce.mutmut_import_compat import write_mutmut_import_compat
from uta_py_enforce.mutation_workspace import (
    focused_pytest_runner,
    mutation_support_copy_paths,
    mutmut_pytest_add_cli_args,
)
from uta_py_enforce.runtime import run_command
from uta_enforce_core.mutation_batching import (
    aggregate_batch_candidate_plans,
    filter_generation_policy_to_lines,
    mutation_units_from_generation_policy,
    partition_units_into_batches,
)
from uta_enforce_core.mutation_candidates import (
    all_selected_mutants_unassociated,
    candidate_plan_with_execution_evidence,
)


MUTMUT_STATUS_BY_EXIT_CODE = {
    1: "killed",
    3: "killed",
    -24: "timeout",
    24: "timeout",
    152: "timeout",
    0: "survived",
    5: "no tests",
    2: "interrupted",
    33: "no tests",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    255: "timeout",
    -11: "segfault",
    None: "not checked",
}


def _generation_artifacts(
    target_changed_lines: Sequence[int], *, batch_count: int | None = None
) -> dict[str, str]:
    """Facts about *how* mutants were generated, which only this layer knows."""

    artifacts = {"mutation_generation_strategy": generation_strategy()}
    if target_changed_lines:
        artifacts["mutation_scope"] = "changed_lines"
    if batch_count is not None:
        artifacts["mutation_batch_count"] = str(batch_count)
    return artifacts


@dataclass(frozen=True)
class _GenerationPass:
    """One generate-and-score cycle over a slice of the generation policy.

    `hard_cap` produces exactly one pass over the whole policy; `batch`
    produces one per partition batch. Everything downstream reads passes, so
    the two strategies differ only in how this list is built.
    """

    index: int
    policy: Mapping[str, Any]
    path: Path
    suffix: str


def _generation_passes(
    source_file: Path,
    generation_policy: Mapping[str, Any],
    policy_path: Path,
) -> tuple[list[_GenerationPass], Any]:
    """Split the policy into the passes this strategy runs.

    Batching exists because mutmut copies the whole enclosing function once per
    mutant, so a single generated module grows with (mutants x function size)
    and leaves CPython's linear parse regime. Partitioning by function keeps
    each generated module under a byte budget; partitioning *by function* in
    particular keeps every unit's mutmut keys inside one batch, which is what
    stops the per-generation key collisions.
    """
    if generation_strategy() != "batch":
        return [_GenerationPass(0, generation_policy, policy_path, "")], None

    try:
        source_text = source_file.read_text(encoding="utf-8")
    except OSError:
        source_text = ""
    units = mutation_units_from_generation_policy(
        source_text, generation_policy.get("selected") or []
    )
    factor = generation_bytes_per_line_factor()
    if factor > 1.0:
        units = [
            replace(unit, unit_bytes=int(unit.unit_bytes * factor)) for unit in units
        ]
    partition = partition_units_into_batches(
        units,
        budget=generation_max_generated_bytes(),
        max_batches=generation_max_batches(),
    )
    if len(partition.batches) <= 1:
        # One batch is a single pass. Writing a second policy file and naming
        # the commands `_batch_0` would only make identical work look different.
        return [_GenerationPass(0, generation_policy, policy_path, "")], partition

    passes = []
    for batch in partition.batches:
        policy = filter_generation_policy_to_lines(generation_policy, batch.lines)
        path = policy_path.with_suffix(f".batch{batch.index}.json")
        path.write_text(json.dumps(policy, indent=2, sort_keys=True), encoding="utf-8")
        passes.append(_GenerationPass(batch.index, policy, path, f"_batch_{batch.index}"))
    return passes, partition


def _run_generation_passes(
    passes: Sequence[_GenerationPass],
    partition: Any,
    *,
    repo: Path,
    source_path: str,
    test_path: str,
    changed_lines: Sequence[int],
    mutants_dir: Path,
    timeout: int,
    commands: list[dict[str, Any]],
    python_bin: str,
    syntax_version: str,
    execution_env: Mapping[str, str] | None = None,
) -> tuple[dict[str, int], list[str], dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Generate and score every pass, then merge them into one result.

    Counts and keys accumulate across passes because each batch regenerates
    `mutants/` from scratch -- the directory is read and cleared per pass, so
    what survives here is the union, not the last batch's leftovers.

    Failure is reported closed: the *first* metadata failure is what the caller
    sees, so a batch that could not generate is never averaged away by later
    batches that could.
    """
    totals: dict[str, int] = {}
    keys: list[str] = []
    plans: list[dict[str, Any]] = []
    batches_meta: list[dict[str, Any]] = []
    first_failed_metadata: dict[str, Any] | None = None
    last_metadata: dict[str, Any] | None = None
    mutmut_run: dict[str, Any] = {}

    for item in passes:
        if len(passes) > 1:
            shutil.rmtree(mutants_dir, ignore_errors=True)
        metadata_run = run_command(
            f"mutmut_generate_metadata{item.suffix}",
            adapter_command(
                python_bin, max_children=1, policy_path=item.path, mode="metadata"
            ),
            repo,
            timeout,
            commands,
            mutation_pytest_env(mutants_dir, [test_path], env_without_ci_sampling(execution_env)),
        )
        last_metadata = metadata_run
        if int(metadata_run.get("exitCode") or 0) == 0:
            mutmut_run = run_command(
                f"mutmut_run_selected{item.suffix}",
                adapter_command(
                    python_bin, max_children=1, policy_path=item.path, mode="run"
                ),
                repo,
                timeout,
                commands,
                mutation_pytest_env(mutants_dir, [test_path], env_without_ci_sampling(execution_env)),
            )
        else:
            if first_failed_metadata is None:
                first_failed_metadata = metadata_run
            mutmut_run = metadata_run

        pass_counts, pass_keys = read_mutmut_meta(mutants_dir)
        for status, count in pass_counts.items():
            totals[status] = totals.get(status, 0) + int(count)
        keys.extend(pass_keys)
        plans.append(
            _candidate_plan(
                repo,
                source_path=source_path,
                test_path=test_path,
                changed_lines=changed_lines,
                keys=pass_keys,
                generation_policy=item.policy,
                python_bin=python_bin,
                syntax_version=syntax_version,
            )
        )
        if len(passes) > 1:
            batches_meta.append(
                {
                    "index": item.index,
                    "allowedLineCount": len(item.policy.get("selectedLines") or ()),
                    "batchGeneratedBytes": _generated_module_bytes(repo, source_path),
                }
            )

    if len(passes) > 1:
        candidate_plan = aggregate_batch_candidate_plans(
            plans,
            partition_signature=partition.signature,
            max_generated_bytes=generation_max_generated_bytes(),
            batches_meta=batches_meta,
            omitted_by_generated_bytes_cap=partition.omitted_by_generated_bytes_cap,
            omitted_by_max_batches=partition.omitted_by_max_batches,
        )
        warnings = _partition_warnings(partition)
        if candidate_plan and warnings:
            candidate_plan["mutationBatchWarnings"] = warnings
    else:
        candidate_plan = plans[0] if plans else {}

    return (
        totals,
        sorted(keys),
        candidate_plan,
        first_failed_metadata or last_metadata,
        mutmut_run,
    )


def _partition_warnings(partition: Any) -> list[str]:
    """Say out loud where mutation coverage was reduced to fit the budget.

    A trimmed line is a mutation that will never be scored. Reporting the count
    and the lines is the difference between a smaller run and a quietly weaker
    gate.
    """
    warnings: list[str] = []
    for lines, reason in (
        (partition.omitted_by_generated_bytes_cap, "exceeded the generated-bytes budget"),
        (partition.omitted_by_max_batches, "trimmed to fit the max-batch guard"),
    ):
        if lines:
            warnings.append(
                "Mutation coverage reduced: %d line(s) %s (lines %s)."
                % (len(lines), reason, ",".join(str(line) for line in lines))
            )
    return warnings


def _generated_module_bytes(repo: Path, source_path: str) -> int:
    path = repo / "mutants" / source_path
    try:
        return int(path.stat().st_size) if path.exists() else 0
    except OSError:
        return 0


def _apply_sampling_policy(
    policy: dict[str, Any], sampling_policy: Any, *, source_path: str
) -> dict[str, Any]:
    """Let the caller narrow the selection before anything is generated.

    `capabilities()` has advertised `accepts_sampling_policy` since this
    binding existed, and nothing ever read `context.sampling_policy` -- so a
    caller that supplied one got full generation and a policy file claiming
    `capProfile: "full"`. For CI that is not a wrong verdict, it is an
    unbounded bill, which is why it went unnoticed.

    Narrowing happens here, before generation, because a mutant that is never
    generated costs nothing; filtering after the fact would save only the
    scoring.

    `capProfile` records which profile was *in force*, not whether it happened
    to bite -- the same meaning the legacy lane gives it. A small diff under CI
    caps is still a capped run, and an auditor asking "was this bounded?"
    needs the answer to be about the configuration rather than about the size
    of that particular change.
    """
    if sampling_policy is None:
        return policy
    selected = list(policy.get("selected") or ())
    sampled = list(
        sampling_policy.select_mutants(
            selected, target_context={"sourcePath": source_path}
        )
    )
    if len(sampled) >= len(selected):
        return {**policy, "capProfile": "ci"}
    kept_lines = {int(item["line"]) for item in sampled if "line" in item}
    narrowed = filter_generation_policy_to_lines(policy, kept_lines)
    narrowed["capProfile"] = "ci"
    narrowed["sampledFrom"] = len(selected)
    return narrowed


#: mutmut's wording when the selected tests cover none of the mutated lines.
_UNCOVERED_MARKERS = (
    "tests do not cover any code that we mutated",
    "did not cover any code that we mutated",
)


def _mutants_uncovered_by_tests(command: Mapping[str, Any] | None) -> bool:
    if not command:
        return False
    blob = f"{command.get('stdout') or ''}\n{command.get('stderr') or ''}".lower()
    return any(marker in blob for marker in _UNCOVERED_MARKERS)


def run_mutation(
    repo: Path,
    source_path: str,
    test_path: str,
    changed_lines: Mapping[str, Sequence[int]],
    gate: float,
    mutmut_bin: str,
    timeout: int,
    commands: list[dict[str, Any]],
    *,
    python_bin: str,
    syntax_version: str,
    sampling_policy: Any = None,
    execution_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    target_changed_lines = list(changed_lines.get(source_path, []))
    generation_policy = None
    policy_path = None
    if syntax_version != "python2":
        generation_policy = build_generation_policy(
            repo / source_path,
            source_path=source_path,
            changed_lines=target_changed_lines,
            covered_lines=target_changed_lines,
            hard_cap=GENERATION_HARD_CAP,
        )
        policy_path = repo / ".uta_cache" / "python-enforcement" / "mutation" / (
            source_path.replace("/", "_") + ".generation-policy.json"
        )
        generation_policy = _apply_sampling_policy(
            generation_policy, sampling_policy, source_path=source_path
        )
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(json.dumps(generation_policy, indent=2, sort_keys=True), encoding="utf-8")
        if not generation_policy.get("selectedLines"):
            return _no_candidate_mutation(
                source_path,
                target_changed_lines,
                gate,
                generation_policy,
                artifacts={
                    **({"mutmut_generation_policy": str(policy_path)} if policy_path.exists() else {}),
                    **_generation_artifacts(target_changed_lines),
                },
            )

    setup_cfg = repo / "setup.cfg"
    pyproject = repo / "pyproject.toml"
    before_setup = setup_cfg.read_text(encoding="utf-8") if setup_cfg.exists() else None
    before_pyproject = pyproject.read_text(encoding="utf-8") if pyproject.exists() else None
    mutants_dir = repo / "mutants"
    shutil.rmtree(mutants_dir, ignore_errors=True)
    collection_probe = None
    clean_test_probe = None
    metadata_run = None
    candidate_plan: dict[str, Any] = {}
    execution_env = _with_import_compat(repo, source_path, execution_env)
    try:
        write_mutmut_setup(
            repo,
            source_path,
            test_path,
            python_bin=python_bin,
            syntax_version=syntax_version,
        )
        if syntax_version == "python2":
            mutmut_run = run_command(
                "mutmut_run",
                [mutmut_bin, "run", "--max-children", "1"],
                repo,
                timeout,
                commands,
                mutation_pytest_env(mutants_dir, [test_path], env_without_ci_sampling(execution_env)),
            )
            batch_count = 1
            counts, keys = read_mutmut_meta(mutants_dir)
            candidate_plan = _candidate_plan(
                repo,
                source_path=source_path,
                test_path=test_path,
                changed_lines=target_changed_lines,
                keys=keys,
                generation_policy=generation_policy,
                python_bin=python_bin,
                syntax_version=syntax_version,
            )
        else:
            passes, partition = _generation_passes(
                repo / source_path, generation_policy, policy_path
            )
            counts, keys, candidate_plan, metadata_run, mutmut_run = _run_generation_passes(
                passes,
                partition,
                repo=repo,
                source_path=source_path,
                test_path=test_path,
                changed_lines=target_changed_lines,
                mutants_dir=mutants_dir,
                timeout=timeout,
                commands=commands,
                python_bin=python_bin,
                syntax_version=syntax_version,
                # Without this the generation passes run mutmut against
                # `os.environ`, so a repository whose tests import a dependency
                # that lives only in the enforcement overlay -- Django, here --
                # fails to load `mutants/conftest.py`. pytest exits 4, and
                # mutmut raises on that before its own output catcher can dump
                # the ImportError, so the evidence showed only
                # "mutation backend failed" with an empty stdout.
                execution_env=execution_env,
            )
            batch_count = len(passes)
        if counts.get("not checked"):
            clean_test_env = mutation_pytest_env(
                mutants_dir,
                [test_path],
                env_without_ci_sampling(execution_env),
            )
            # The probe runs the unmutated trampoline. Mutmut's generated
            # modules require this selector even when no mutant is active.
            clean_test_env["MUTANT_UNDER_TEST"] = ""
            clean_test_probe = run_command(
                "mutmut_clean_test_probe",
                pytest_process_command(python_bin, ["-x", "--assert=plain", test_path]),
                mutants_dir,
                min(timeout, 120),
                commands,
                clean_test_env,
            )
        elif _all_mutants_suspicious(counts) and not _has_collection_error(mutmut_run):
            collection_probe = run_command(
                "mutmut_collection_probe",
                pytest_process_command(python_bin, ["--collect-only", "-q", test_path]),
                mutants_dir,
                min(timeout, 120),
                commands,
                mutation_pytest_env(mutants_dir, [test_path], env_without_ci_sampling(execution_env)),
            )
    finally:
        _restore_config_file(setup_cfg, before_setup)
        _restore_config_file(pyproject, before_pyproject)
        # `mutants/` is the only record of what mutmut actually executed, and it
        # is normally removed here. Diagnosing a run that scores mutants but
        # kills none requires seeing that tree, so an operator can keep it.
        # Off by default: the tree is a full copy of the repository.
        if not os.environ.get(KEEP_MUTANTS_ENV):
            shutil.rmtree(mutants_dir, ignore_errors=True)
    killed = counts["killed"] + counts["segfault"]
    survived = counts["survived"]
    timeout_count = counts["timeout"]
    suspicious = counts["suspicious"]
    not_checked = counts["not checked"]
    # mutmut says this in its own words when the selected tests exercise none
    # of the mutated lines. That is a fact about the target's tests, not a
    # fault in the tool: the run completed and reported a result. Treating it
    # as a backend failure aborted a fifty-six module run over one constants
    # file and hid a 96.99% aggregate that would have passed.
    uncovered_mutants = 0
    if not_checked and _mutants_uncovered_by_tests(mutmut_run):
        # Not killed and not a fault, so they count against the score rather
        # than vacating it. Scoring them as "no coverage" alone would leave an
        # empty denominator, which reads as 100% -- the same vacuous pass that
        # an empty changed-line set produces.
        uncovered_mutants = not_checked
        survived_uncovered = not_checked
        not_checked = 0
    else:
        survived_uncovered = 0
    generated = sum(counts.values())
    scored = max(generated - counts["no tests"] - counts["skipped"] - not_checked, 0)
    survived += survived_uncovered
    denominator = killed + survived + timeout_count + suspicious
    rate = 100.0 if denominator == 0 else round((killed / denominator) * 100.0, 4)
    collection_failed = _all_mutants_suspicious(counts) and (
        _has_collection_error(mutmut_run) or _has_collection_error(collection_probe)
    )
    metadata_failed = bool(metadata_run and int(metadata_run.get("exitCode") or 0) != 0)
    backend_failed = bool(
        metadata_failed
        or not_checked > 0
        or (generated == 0 and int(mutmut_run.get("exitCode") or 0) != 0)
    )
    failure_stage = (
        "metadata_generation"
        if metadata_failed
        else "mutation_execution"
        if not_checked > 0
        else "mutation_backend"
        if backend_failed
        else None
    )
    failure_detail = _command_failure_detail(clean_test_probe or mutmut_run or metadata_run) if backend_failed else ""
    candidate_plan = candidate_plan_with_execution_evidence(
        candidate_plan,
        run_mutants=generated,
        scored_mutants=scored,
        killed=killed,
        survived=survived,
        no_tests=counts["no tests"],
        timeout=timeout_count,
        suspicious=suspicious,
        not_checked=not_checked,
    )
    plan_failed = bool(
        syntax_version != "python2"
        and generated > 0
        and len(candidate_plan.get("exactToolCandidateKeys") or ()) != generated
    )
    zero_candidate_pass = generated == 0 and not backend_failed
    no_tests_unassociated = (
        all_selected_mutants_unassociated(
            generated=generated,
            scored=scored,
            no_tests=counts["no tests"],
        )
        and not backend_failed
        and not collection_failed
    )
    artifacts: dict[str, str] = {}
    if policy_path and policy_path.exists():
        artifacts["mutmut_generation_policy"] = str(policy_path)
    compat_dir = str((execution_env or {}).get("UTA_MUTMUT_IMPORT_COMPAT_DIR") or "")
    if compat_dir:
        artifacts["import_compat"] = compat_dir
    artifacts.update(_generation_artifacts(target_changed_lines, batch_count=batch_count))
    return {
        "runtime_lane": "lightweight",
        "generated": generated,
        "killed": killed,
        "survived": survived,
        "noCoverage": 0,
        "noTests": counts["no tests"],
        "timeout": timeout_count,
        "suspicious": suspicious,
        "notChecked": not_checked,
        # Named separately so a reader can tell "the tests never reached this
        # mutant" from "the tests ran and missed it".
        "uncoveredMutants": uncovered_mutants,
        "skipped": counts["skipped"],
        "changedLineMutantsGenerated": generated,
        "changedLineMutantsKilled": killed,
        "changedLineMutantsScored": scored,
        "rate": 0.0 if backend_failed or no_tests_unassociated else rate,
        "gate": gate,
        "passed": (
            (rate >= gate or zero_candidate_pass)
            and not backend_failed
            and not collection_failed
            and not plan_failed
            and not no_tests_unassociated
        ),
        "reasonCode": (
            "mutation_test_collection_failed"
            if collection_failed
            else "mutation_backend_failed"
            if backend_failed
            else "mutation_candidate_plan_failed"
            if plan_failed
            else "mutation_no_tests"
            if no_tests_unassociated
            else None
        ),
        "failureStage": failure_stage,
        "failureDetail": failure_detail,
        "scope": "changed_lines",
        "changed_lines": {source_path: list(changed_lines.get(source_path, []))},
        "candidatePlan": candidate_plan,
        "artifacts": artifacts,
    }


def _candidate_plan(
    repo: Path,
    *,
    source_path: str,
    test_path: str,
    changed_lines: Sequence[int],
    keys: Sequence[str],
    generation_policy: Mapping[str, Any] | None,
    python_bin: str,
    syntax_version: str,
) -> dict[str, Any]:
    if syntax_version == "python2":
        return {
            "filterMechanism": "mutmut15_legacy_changed_line_scope",
            "changedLines": list(changed_lines),
            "eligibleMutationOpportunities": [{"toolCandidateKey": key} for key in keys],
            "reportFullSelected": [{"toolCandidateKey": key} for key in keys],
            "activeSelected": [{"toolCandidateKey": key} for key in keys],
            "exactToolCandidateKeys": [],
            "samplingLayer": {"enabled": False},
        }
    policy_bytes = json.dumps(generation_policy or {}, sort_keys=True).encode("utf-8")
    plan = build_mutmut3_candidate_plan_from_meta(
        repo,
        source_path=source_path,
        target_id=f"pyfile:{source_path}",
        changed_lines={source_path: changed_lines},
        covered_lines=changed_lines,
        selected_test_paths=(test_path,),
        mutmut_version="mutmut3",
        runtime_fingerprint=f"{syntax_version}:{python_bin}",
        dependency_fingerprint="lightweight",
        config_fingerprint_value=hashlib.sha256(policy_bytes).hexdigest(),
        mutmut_internal_api_fingerprint="mutmut3-shared-adapter-v1",
        generation_policy=generation_policy,
    )
    payload = plan.as_dict()
    payload["generationPolicy"] = {
        key: generation_policy.get(key)
        for key in (
            "capProfile",
            "caps",
            "changedLineCount",
            "eligibleOpportunities",
            "selectedOpportunitiesBeforeCap",
            "hardCapSelectedOpportunities",
            "selectedOpportunities",
            "omittedByCap",
            "omittedByHardCap",
            "omittedByRepresentativeSelection",
            "suppressedOpportunities",
            "truncated",
            "truncationReasons",
        )
        if generation_policy is not None and key in generation_policy
    }
    return payload


def _no_candidate_mutation(
    source_path: str,
    changed_lines: Sequence[int],
    gate: float,
    generation_policy: Mapping[str, Any],
    *,
    artifacts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "runtime_lane": "lightweight",
        "generated": 0,
        "killed": 0,
        "survived": 0,
        "noCoverage": 0,
        "noTests": 0,
        "timeout": 0,
        "suspicious": 0,
        "notChecked": 0,
        "skipped": 0,
        "changedLineMutantsGenerated": 0,
        "changedLineMutantsKilled": 0,
        "changedLineMutantsScored": 0,
        "rate": 100.0,
        "gate": gate,
        "passed": True,
        "reasonCode": "no_mutatable_candidates",
        "scope": "changed_lines",
        "changed_lines": {source_path: list(changed_lines)},
        "candidatePlan": {
            "filterMechanism": "mutmut3_metadata_selected_execution",
            "changedLines": list(changed_lines),
            "eligibleMutationOpportunities": list(generation_policy.get("selectedBeforeCap") or ()),
            "reportFullSelected": [],
            "activeSelected": [],
            "exactToolCandidateKeys": [],
            "suppressed": list(generation_policy.get("suppressed") or ()),
            "suppressionByReason": suppression_counts(generation_policy),
            "samplingLayer": {"enabled": False},
            "adapterFilteredGenerationApplied": True,
            "generationPolicy": dict(generation_policy),
        },
        "artifacts": dict(artifacts or {}),
    }


def _with_import_compat(
    repo: Path, source_path: str, execution_env: Mapping[str, str] | None
) -> dict[str, str]:
    """Install the sitecustomize shim mutmut pytest children inherit."""
    mutation_dir = repo / ".uta_cache" / "python-enforcement" / "mutation"
    canonical = resolve_python_module(repo, source_path).module_name
    compat_dir = write_mutmut_import_compat(
        mutation_dir,
        repo=repo,
        source_path=source_path,
        canonical_module=canonical,
    )
    env = dict(execution_env or {})
    env["UTA_MUTMUT_IMPORT_COMPAT_DIR"] = str(compat_dir)
    env["UTA_MUTMUT_TARGET_REL"] = source_path.replace("\\", "/").lstrip("/")
    env["UTA_MUTMUT_CANONICAL_MODULE"] = canonical
    env["UTA_MUTMUT_REPO_ROOT"] = str(repo.resolve())
    existing = str(env.get("PYTHONPATH") or "")
    env["PYTHONPATH"] = os.pathsep.join(item for item in (str(compat_dir), existing) if item)
    return env


def empty_mutation(changed_lines: Sequence[int], gate: float, *, passed: bool) -> dict[str, Any]:
    return {
        "runtime_lane": "lightweight",
        "generated": 0,
        "killed": 0,
        "survived": 0,
        "noCoverage": 0,
        "noTests": 0,
        "timeout": 0,
        "suspicious": 0,
        "notChecked": 0,
        "changedLineMutantsGenerated": 0,
        "changedLineMutantsKilled": 0,
        "changedLineMutantsScored": 0,
        "rate": 100.0 if passed else 0.0,
        "gate": gate,
        "passed": passed,
        "scope": "changed_lines",
        "changed_lines": {},
        "candidatePlan": {
            "activeSelected": [],
            "reportFullSelected": [],
            "exactToolCandidateKeys": [],
            "eligibleMutationOpportunities": [],
            "suppressed": [{"reasonCode": "mutation_disabled"}],
            "samplingLayer": {"enabled": False},
        },
        "artifacts": {},
    }


def write_mutmut_setup(
    repo: Path,
    source_path: str,
    test_path: str,
    *,
    python_bin: str,
    syntax_version: str = "python3",
) -> None:
    tests_dir = (
        str(Path(test_path).parent or Path(".")).replace("\\", "/") or "."
        if syntax_version == "python2"
        else test_path
    )
    support_paths = mutation_support_copy_paths(repo, source_path, (test_path,))
    pytest_args = mutmut_pytest_add_cli_args(repo, (test_path,))
    pyproject = repo / "pyproject.toml"
    if syntax_version != "python2" and pyproject.exists():
        lines = [
            "[tool.mutmut]",
            f"paths_to_mutate = {json.dumps([source_path])}",
            f"tests_dir = {json.dumps([tests_dir])}",
        ]
        if pytest_args:
            lines.append(f"pytest_add_cli_args = {json.dumps(pytest_args)}")
        if support_paths:
            lines.append(f"also_copy = {json.dumps(support_paths)}")
        original = pyproject.read_text(encoding="utf-8")
        pyproject.write_text(
            _replace_toml_section(original, "[tool.mutmut]", "\n".join(lines)),
            encoding="utf-8",
        )
        return
    lines = [
        "[mutmut]",
        f"paths_to_mutate={source_path}",
        f"tests_dir={tests_dir}",
    ]
    if pytest_args:
        lines.append("pytest_add_cli_args=" + "\n  ".join(pytest_args))
    if support_paths:
        lines.append("also_copy=\n  " + "\n  ".join(support_paths))
    lines.append(f"runner={focused_pytest_runner(python_bin, (test_path,))}")
    (repo / "setup.cfg").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _replace_toml_section(text: str, header: str, body: str) -> str:
    lines = str(text or "").splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return "\n".join([text.rstrip(), body]).lstrip("\n") + "\n"
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].strip().startswith("[") and lines[index].strip().endswith("]")
        ),
        len(lines),
    )
    return "\n".join([*lines[:start], *body.splitlines(), *lines[end:]]).rstrip() + "\n"


def _restore_config_file(path: Path, original: str | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(original, encoding="utf-8")


def _all_mutants_suspicious(counts: Mapping[str, int]) -> bool:
    generated = sum(int(value or 0) for value in counts.values())
    return generated > 0 and int(counts.get("suspicious") or 0) == generated


def _has_collection_error(command: Mapping[str, Any] | None) -> bool:
    if not command:
        return False
    output = "\n".join(str(command.get(name) or "") for name in ("stdout", "stderr")).lower()
    markers = ("error collecting", "errors during collection", "error during collection")
    return any(marker in output for marker in markers)


def read_mutmut_meta(mutants_dir: Path) -> tuple[dict[str, int], list[str]]:
    counts = {status: 0 for status in ("killed", "survived", "no tests", "skipped", "suspicious", "timeout", "segfault", "interrupted", "not checked")}
    keys: list[str] = []
    for meta_path in mutants_dir.rglob("*.meta"):
        # Mutmut 3 writes `<source.py>.meta`. rglob("*.meta") also matches
        # copied TensorFlow checkpoints such as gaia's ONet-16.meta.
        if not meta_path.name.endswith(".py.meta"):
            continue
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, exit_code in (payload.get("exit_code_by_key") or {}).items():
            status = MUTMUT_STATUS_BY_EXIT_CODE.get(exit_code, "suspicious")
            counts[status] = counts.get(status, 0) + 1
            keys.append(str(key))
    return counts, sorted(keys)


def _command_failure_detail(command: Mapping[str, Any] | None) -> str:
    if not command:
        return "Mutation backend stopped before selected mutants were executed"
    output = "\n".join(str(command.get(name) or "") for name in ("stdout", "stderr")).strip()
    if output:
        return output[-4000:]
    return f"Mutation backend exited with code {int(command.get('exitCode') or 0)} before selected mutants were executed"

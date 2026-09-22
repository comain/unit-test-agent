# ADR-001: Shared Python Mutation Candidate Plan

## Status

Proposed

## Date

2026-06-22

## Revision History

- 2026-06-22: Proposed shared Python mutation candidate plan for CI report and repair-session parity.
- 2026-06-22: Accepted a rollout based on adapter-filtered mutmut metadata generation plus unkeyed execution over the generated mutant set for the decisive score.
- 2026-06-22: Recorded mutmut 2 findings but kept Python2 compatibility on the current `mutmut==1.5.0` lane.
- 2026-06-22: Collapsed the public design to a single `MutationCandidatePlan`; Python adapter-local generation policy is not a separate engine-level plan.
- 2026-06-22: Split pre-generation opportunities from generated candidates inside the plan and added explicit verifier context/config requirements.

## Context

Python `mutmut` verification is too slow for large diffs because mutmut can run many mutants, and each mutant can pay Python process/setup overhead. UTA also has two invocation paths that must agree: CI report enforcement and repair-session verification. Prior production issues showed that when CI and repair select different tests or mutation evidence, users see contradictory pass/fail results.

The spec requires:

1. CI report and repair-session verification share candidate planning, arid suppression, selected-test semantics, exact mutmut operator selection, and evidence contract.
2. Repair/nightly/full verification use the full selected candidate plan.
3. CI report enforcement may use a smaller deterministic cap profile inside the adapter generation policy when the mutation set is large.
4. Mutation reruns are deterministic for the same immutable inputs.

## Decision

Use one engine-owned `MutationCandidatePlan` contract for Python mutation verification. The plan's canonical mode is `report_full`:

1. ask the Python adapter to build a candidate plan from changed lines, coverage, symbol ranges, arid suppression, and deterministic operator ranking;
2. inside the adapter, choose one operator opportunity per eligible changed line using a versioned deterministic operator-priority policy and stable tie-breakers;
3. apply the active deterministic cap profile before generation, then materialize only adapter-selected mutmut metadata without broad mutant generation or broad mutant test execution;
4. attach generated exact mutmut keys to planned line/operator/diff metadata and fail closed if generated metadata does not match the selected opportunities;
5. return one `MutationCandidatePlan` containing pre-generation opportunities, generated candidate ids, exact keys, generation-policy fingerprint, cap profile, and evidence counts;
6. execute the decisive mutation score by running mutmut over the adapter-generated candidate set, not by relying on broad metadata-generation output or a later selected-key filter;
7. allow CI report enforcement to apply a smaller deterministic cap before metadata generation;
8. keep repair-session and nightly/manual full verification on the larger full cap by default.

## Alternatives Considered

### Separate CI-Bounded And Repair-Full Planners

Pros:

- Easy to bolt onto current CI path.
- Lets CI optimize aggressively.

Cons:

- Reintroduces CI/repair drift.
- Makes rerun determinism harder to prove.
- Forces report code to explain two policy sources.

Rejected because the main correctness requirement is that CI and repair share the same eligibility and filtering contract.

### Stock Mutmut CLI Exact-Key Filtering Only

Pros:

- Mutmut accepts exact mutant names in the public CLI.
- It is simpler to prototype.

Cons:

- Stock mutmut 3 creates mutation schemata before applying exact-name execution.
- A broad unkeyed `mutmut run` can still be expensive for large diffs.
- It creates a misleading denominator risk if UTA reports broad output as a narrow selected set.

Rejected as the only mechanism. The accepted rollout applies UTA selection inside metadata generation, then runs mutmut over the generated candidate set. Evidence currently reports `mutmut3_metadata_selected_execution` for compatibility, but the mechanism is adapter-filtered generation plus execution over generated mutants.

### Mutmut Version-Specific Filtering

Pros:

- Mutmut 2.x explicitly supports `mutmut_config.py`; its `pre_mutation(context)` hook can set `context.skip=True`. It also exposes patch-file scoping and mutation-type enable/disable flags.
- These controls could support a future Python2 migration.

Cons:

- Current UTA Python2 verification requires `mutmut==1.5.0`, so adopting mutmut2 is a migration, not a documentation-only compatibility path.
- Mutmut 1.5.0, 2.x, and 3.x expose different mechanisms and different mutant id semantics.
- Multiple primary filtering implementations increase drift and test matrix size.

Rejected as the Python3 primary path and deferred for Python2. Python2 remains on the current `mutmut==1.5.0` legacy lane with explicit evidence wording.

### Pragma-Only Line Masking

Pros:

- Simple extension of the existing Python verifier.
- Reduces mutants outside changed lines.

Cons:

- Cannot choose one operator when mutmut generates multiple mutants on the same changed line.
- Still runs too many Python processes for large changed lines with many operator candidates.
- Makes the displayed denominator ambiguous because line selection and operator selection are different concepts.

Rejected for the Python3 primary path. It may remain only as part of an explicit legacy compatibility mechanism, and evidence must not describe that as mutmut 3 metadata-selected execution.

### Broad Post-Run Report Filtering Only

Pros:

- Minimal code change.

Cons:

- Does not reduce mutmut runtime.
- Still pays process/setup cost for mutants that should never be considered.
- Does not prove the selected keys can be run as a stable mutmut set.
- Cannot satisfy the large-diff performance requirement.

Rejected because the decisive gate must fail closed when metadata cannot map generated mutants to changed lines; however, execution should run the generated set rather than use exact keys as a second filter.

## Consequences

1. `uta/engine/mutation_candidates.py` becomes the owner of candidate-plan contracts, opportunity/candidate ids, fingerprints, and deterministic helper policy.
2. `uta/engine/mutation_suppression.py` owns neutral suppression reason codes; Python owns AST bindings for those categories.
3. Python verification remains the owner of AST extraction, coverage inputs, mutmut 3 metadata-selected execution, exact mutmut-key mapping, and mutmut execution.
4. CI report and repair-session verification must call the same planner.
5. Evidence and reports can compare reruns using `candidatePlanId`.
6. The design avoids a DB migration by adding fields to existing structured evidence JSON.
7. Candidate planning has one public contract, but the first rollout maps exact keys from mutmut metadata because exact mutmut keys are not available before mutmut generation. This keeps the API concise while avoiding the incorrect assumption that broad mutmut output can be reported as a selected denominator.
8. Suppressed and omitted records are `MutationOpportunity` records and do not require `tool_candidate_key`; generated/scored records are `MutationCandidate` records and do require exact keys.
9. The implementation owns mutmut 3 metadata mapping and adapter-filtered generation. It must fail closed on incompatible mutmut metadata rather than silently falling back to broad stock output.
10. Persisted CI report evidence is a comparison anchor for repair sessions. It is not the source of selection; reruns must recompute the plan from current fingerprinted inputs and policy fingerprints.
11. If recomputation differs from persisted evidence with no fingerprint change, UTA fails the target as a determinism bug. If recomputation differs because a fingerprinted input changed, such as branch head, runtime, dependency, or policy config, repair continues and marks the evidence `comparable=false`.

# Python Mutation Scalability Implementation Plan

Status: approved
Jira: N/A

## 1. Goal

Implement the approved Python mutation scalability design from:

- `docs/spec-python-mutation-scalability.md`
- `docs/design-python-mutation-scalability.md`
- `docs/decisions/ADR-001-python-mutation-candidate-plan.md`

The implementation must keep CI report verification and repair-session verification aligned through one language-agnostic mutation candidate contract. Python3 gets the new deterministic candidate-plan path. Python2 remains on the existing `mutmut==1.5.0` legacy changed-line scope and must not be documented or reported as using mutmut2 behavior.

## 2. Constraints

1. Core workflow, report, progress, and cost code must stay language-agnostic.
2. Java behavior must not regress, especially PIT mutation-family repair context.
3. Python CI report and repair session must share candidate planning and strict test-selection inputs.
4. CI report may use a smaller deterministic cap profile for large candidate sets; repair must run the larger full cap profile.
5. Deterministic reruns must either match the prior candidate fingerprint or explicitly mark evidence as non-comparable when the branch/runtime changed.
6. No DB migration is expected for this change.
7. Current worktree baseline was cleaned before implementation started.

## 3. Phase 1 - Engine Contracts And Config

### 3.1 Add Engine Mutation Contracts

Files:

- `uta/engine/mutation_candidates.py`
- `uta/engine/mutation_suppression.py`
- focused tests under `tests/`

Work:

1. Add `MutationVerificationContext`, `MutationOpportunity`, `MutationCandidate`, `SuppressedMutationOpportunity`, and `MutationCandidatePlan`.
2. Add deterministic id helpers for opportunity id, generated candidate id, and plan id.
3. Add neutral suppression reason codes in the engine, with no Python AST details in the engine layer.
4. Add filter mechanisms including `mutmut3_operator_adapter` and `mutmut15_legacy_changed_line_scope`.

Acceptance:

- Suppressed and omitted records can be represented without a mutmut candidate key.
- Generated/scored candidates require an exact tool candidate key.
- Unit tests prove deterministic id stability and fingerprint sensitivity.

### 3.2 Add Config And Rollback Knobs

Files:

- `uta/config.py`
- `tests/test_config_defaults.py`

Work:

1. Add feature flag `python_mutation_candidate_plan_enabled`.
2. Add deterministic CI cap controls and legacy env aliases.
3. Add mutmut execution controls: max children, adapter generation timeout, selected execution timeout.
4. Include config values that affect selection/execution in candidate-plan evidence.

Acceptance:

- Defaults are explicit and tested.
- Env overrides work.
- Disabling the feature flag cleanly returns Python3 to the old changed-line masking/post-filter path.

## 4. Phase 2 - Python Candidate Planning And Verifier Integration

### 4.1 Add Python Opportunity Extraction

Files:

- `uta/language/python/mutation_candidates.py`
- Python parser/test-selection helpers as needed
- focused tests under `tests/`

Work:

1. Map changed production lines to static mutation opportunities.
2. Apply engine-owned suppression categories through Python AST bindings.
3. Rank opportunities by covered line, operator priority, symbol, path, line, and opportunity id.
4. Apply one-representative-per-line selection before mutmut generation.

Acceptance:

- Logging/metrics/import-wiring/config-glue opportunities can be suppressed with engine reason codes.
- Opportunity ordering is deterministic.
- One-line-many-operator cases select one stable representative opportunity.

### 4.2 Add Python3 Mutmut Adapter Path

Files:

- `uta/language/python/mutation_candidates.py`
- `uta/language/python/verification/runner.py`
- focused tests with monkeypatched mutmut internals

Work:

1. Implement the mutmut3 adapter behind `python_mutation_candidate_plan_enabled`.
2. Use mutmut internals to generate only the selected opportunities where supported.
3. Record exact mutmut candidate keys only after generation.
4. Fail closed or fall back according to the feature flag when mutmut internals are incompatible.

Acceptance:

- Generated candidates map back to selected opportunities.
- Evidence records mutmut internal API fingerprint.
- The adapter does not affect Python2/mutmut1.5.

### 4.3 Preserve Python2 Legacy Path

Files:

- `uta/language/python/verification/runner.py`
- Python2 verification tests

Work:

1. Keep Python2 pinned to `mutmut==1.5.0`.
2. Report `filterMechanism=mutmut15_legacy_changed_line_scope`.
3. Ensure Python2 evidence does not mention mutmut2 config or mutmut3 exact-key behavior.

Acceptance:

- Python2 verification still runs with mutmut1.5.
- Python2 report evidence clearly shows legacy precision.
- No Python2 docs or code path claims mutmut2 support.

## 5. Phase 3 - Shared CI And Repair Semantics

### 5.1 Wire Verifier Context

Files:

- `uta/language/python/verification/runner.py`
- Python enforcement/batch integration files
- focused tests under `tests/`

Work:

1. Build `MutationVerificationContext` from repo URL, base/head, runtime, dependency fingerprint, strict tests, operator policy, suppression policy, mutmut version, mutmut API fingerprint, and config fingerprint.
2. Pass the context into Python verification from both CI report and repair.
3. Store candidate-plan evidence in the existing evidence contract.

Acceptance:

- CI and repair produce comparable candidate-plan ids for unchanged inputs.
- Changed fingerprints produce `comparable=false` rather than a false determinism failure.
- No DB migration is required.

### 5.2 Add CI Cap Profile

Files:

- Python enforcement/report paths
- focused tests under `tests/`

Work:

1. For large candidate sets, apply deterministic CI-only caps before mutmut metadata generation.
2. Keep repair full-plan execution.
3. Report selected-before-cap, generated, omitted-by-cap, suppressed, killed, survived, timeout, and not-run counts.

Acceptance:

- Caps are deterministic for the same plan id and config.
- Report and repair share the same base candidate plan.
- Repair never uses the smaller CI cap profile as its full mutation gate.

### 5.3 Align Repair Rerun Logic

Files:

- `uta/language/python/batch.py`
- shared workflow helpers if applicable
- focused tests under `tests/`

Work:

1. Repair recomputes candidate planning from the same context contract.
2. If the recomputed candidate plan differs without a changed fingerprint, fail as a UTA determinism bug.
3. If fingerprints changed, continue and mark evidence non-comparable.
4. Keep mutation-repair planner behavior: first round receives the full ROI map; fallback round can use a smaller cleanup set.

Acceptance:

- A prior passing strict test and candidate plan remains passing on rerun.
- Drift is visible in evidence instead of silently selecting a different mutation set.
- Java mutation-family context remains unchanged.

## 6. Phase 4 - Reports, Dev Skills, And Verification

### 6.1 Report Rendering

Files:

- CI report renderer/templates
- recent jobs/report helpers if needed
- focused report tests

Work:

1. Show mutation funnel counts and filter mechanism.
2. Show Python2 legacy `mutmut==1.5.0` mode explicitly.
3. Show deterministic cap-trimming counts when a cap is active.
4. Show candidate-plan comparability status for repair sessions.

Acceptance:

- Users can tell why mutation count is smaller than covered changed lines.
- Users can tell whether CI cap-trimmed a large candidate set.
- Python2 reports do not imply mutmut2/3 behavior.

### 6.2 Sync Dev-Skills Guidance If Needed

Files:

- `/path/to/dev-skills/scripts/uta_dev_gate.py`
- `/path/to/dev-skills/references/test-enforce-usage.md`
- related tests in the dev-skills repo

Work:

1. Sync evidence parsing only if the local Python enforcement script consumes the changed evidence.
2. Keep Java guidance unchanged unless Java evidence text changes.
3. Document Python2 mutmut1.5 legacy behavior where relevant.

Acceptance:

- Dev-skills local enforcement agrees with UTA report evidence semantics.
- Existing Java dev gate behavior is not regressed.

### 6.3 Verification Matrix

Required local verification:

1. Focused unit tests for engine contracts, config, Python candidate planning, Python verifier, report rendering, and repair comparison.
2. Existing Java regression tests for PIT mutation-family repair context.
3. Python3 real-repo verification on a large changed-line target.
4. Python2 real-repo verification with `mutmut==1.5.0`.
5. UTA self-test or local integration test when feasible.

Required staged verification before deployment:

1. CI report path on node2 for a Python3 repo.
2. Repair-session path on node2 for the same Python3 repo.
3. Java CI report regression on a recent known-good Java task.
4. Java repair regression on a recent known-good Java task.

Acceptance:

- Python3 large mutation target finishes within configured timeout or reports deterministic cap trimming.
- Python2 remains functional with mutmut1.5.
- Java PIT behavior is unchanged.
- CI report and repair agree on candidate plan when fingerprints match.

## 7. Build Checkpoints

Checkpoint 1:

- Complete Phase 1.
- Run focused engine/config tests.

Checkpoint 2:

- Complete Phase 2.
- Run focused Python verifier tests, including Python2 legacy test.

Checkpoint 3:

- Complete Phase 3.
- Run CI/repair parity tests and Java mutation-family regression tests.

Checkpoint 4:

- Complete Phase 4 docs/report/dev-skills sync.
- Run full focused suite and real-repo verification.

## 8. Phase 5 - Spec/Design Compliance Closure

Review after the initial implementation found that the core selected-key path exists, but several acceptance-critical parts of the spec/design remain incomplete. This phase closes those gaps before the workstream can be treated as implemented.

### 8.1 Complete Immutable Context Propagation

Files:

- `uta/language/python/enforcement.py`
- `uta/language/python/verification/runner.py`
- `uta/language/python/batch.py`
- focused tests under `tests/`

Work:

1. Pass base ref, base commit, head commit, repo URL, selected tests, runtime/dependency fingerprints, policy versions, and config fingerprints into Python verification.
2. Build `MutationVerificationContext` from those inputs for both CI enforcement and repair verification.
3. Ensure Python2 legacy evidence records the same immutable context while still reporting `mutmut15_legacy_changed_line_scope`.

Acceptance:

- `candidatePlanId` changes when base/head/test/runtime/dependency/policy/config inputs change.
- Candidate plan evidence includes non-empty base/head context when enforcement provides it.
- Existing callers remain compatible through additive/defaulted arguments.

### 8.2 Persist Complete Candidate Plan Evidence And Artifacts

Files:

- `uta/engine/mutation_candidates.py`
- `uta/language/python/verification/runner.py`
- focused tests under `tests/`

Work:

1. Add canonical summary fields required by the spec: adapter-filtered generation flag, candidate artifact path, opportunity/candidate id arrays, generated exact tool keys, run/scored/killed/survived/no-test/timeout/suspicious counts, and comparable metadata.
2. Persist the target-level candidate plan JSON under `.uta_cache/python/mutation/`.
3. Attach candidate-plan execution counts after the decisive mutation run.

Acceptance:

- Raw candidate plan artifact exists and matches the persisted evidence block.
- Target evidence persists enough fields for repair parity even if `.uta_cache` is deleted.
- Missing exact-key metadata still fails closed.

### 8.3 Enforce Zero-Candidate Semantics Consistently

Files:

- `uta/engine/mutation_candidates.py`
- `uta/language/python/enforcement.py`
- `/path/to/dev-skills/scripts/uta_dev_gate.py`
- UTA and dev-skills tests

Work:

1. Add one shared predicate for "zero generated/scored mutation may pass" based on candidate-plan proof.
2. Use it in UTA Python evidence validation.
3. Sync dev-skills Python enforcement validation and tests.

Acceptance:

- Zero generated/scored mutation passes only when candidatePlan proves no eligible exact candidates or all opportunities were reason-suppressed.
- CandidatePlan with eligible opportunities but no exact keys still fails.
- Legacy evidence without candidatePlan keeps existing validation behavior.

### 8.4 Align Repair With Persisted CI Candidate Plan Anchor

Files:

- `uta/language/python/ci.py`
- `uta/language/python/batch.py`
- repair context/task metadata tests

Work:

1. Carry the original per-target CI `candidatePlan` into repair context/task metadata.
2. Compare the first recomputed repair candidate plan against the persisted CI anchor.
3. If fingerprints changed, continue with `comparable=false`; if no fingerprint changed and selected ids/keys differ, fail as a UTA determinism bug.

Acceptance:

- Repair does not silently switch candidate plans relative to the CI report.
- Changed branch/runtime/dependency/test/config fingerprints are visible as non-comparable, not false failures.
- Later repair attempts continue to compare against the previous attempt as an additional guard.

### 8.5 Expand Report And Console Observability

Files:

- `uta/language/python/enforcement.py`
- `uta/api_trigger/reporting.py`
- `uta/api_trigger/templates/report.html`
- report rendering tests

Work:

1. Add candidate-plan console marker lines for local dev users.
2. Roll up target-level candidate-plan counts into aggregate mutation evidence.
3. Render the report funnel: changed lines -> covered changed lines -> eligible opportunities -> selected/run candidates -> scored mutants -> killed/survived/no-test/timeout/suspicious.
4. Show non-comparable rerun information when available.

Acceptance:

- CI reports explain why selected mutation counts are smaller than covered changed lines.
- Local console output has enough candidate-plan summary without requiring users to inspect raw JSON.
- Aggregate denominators derive from target-level candidate-plan evidence.

### 8.6 Final Compliance Verification

Required verification:

1. Focused UTA tests for immutable context, artifact/evidence fields, aggregate/report funnel, repair anchor comparison, and zero-candidate validation.
2. Focused dev-skills tests for candidatePlan pass/fail, zero-no-eligible pass, inconsistent candidatePlan failure, and legacy compatibility.
3. Existing Java mutation-family repair regression tests.
4. Existing Python verifier/enforcement tests.

Acceptance:

- Every missing item from the review maps to a passing test.
- No Java regression in mutation-family repair context.
- Dev-skills and UTA agree on Python candidatePlan semantics.

## 9. Requirement Coverage Addendum

| Source | Requirement / design decision | Covered by task(s) | Notes |
| --- | --- | --- | --- |
| spec 5.4 | Immutable inputs must build and compare candidatePlanId. | 8.1, 8.4 | Includes base/head, selected tests, runtime/deps, policy/config fingerprints. |
| spec 5.4 | Zero generated/scored may pass only with candidate-plan proof. | 8.3 | Applies to UTA and dev-skills. |
| spec 5.5 | CandidatePlan block includes artifact path, selected ids/keys, counts, and fingerprints. | 8.2 | Additive evidence fields only. |
| spec 5.5 | Raw candidate plan JSON under `.uta_cache/python/mutation/`. | 8.2 | Debug aid; persisted report evidence remains source of truth. |
| spec 5.5 | CLI/report show candidate funnel and non-comparable reruns. | 8.5 | Report and console observability. |
| spec 5.5 | Repair rebuilds report_full plan and compares to persisted CI evidence anchor. | 8.4 | Persisted evidence is comparison anchor, not selection source. |
| spec 5.6 | Dev-skills local enforcement agrees with UTA semantics. | 8.3, 8.6 | Includes zero-candidate and legacy compatibility. |
| design 5.3/5.4 | Aggregate evidence rolls up target-level candidatePlan counts. | 8.5 | Avoid recalculating denominators from raw mutmut metadata alone. |
| design 7.1 | Verifier adds plan artifacts and evidence; enforcement adds console markers. | 8.2, 8.5 | Closes implementation review gaps. |
| design 8.1 | Reports expose phase/performance evidence where available. | 8.2, 8.5 | At minimum preserve and render available command timing/counts. |

## 10. Phase 6 - Post-Rollout Legacy Subsystem Removal

This phase is deferred until after the Python3 candidate-plan path is the default
(`python_mutation_candidate_plan_enabled` defaults on) and mutmut<3 support is
dropped for Python3. It removes the transitional Python3 fallback machinery while
keeping the Python2/mutmut1.5 lane intact.

### 10.1 Decommission Metadata-Counting / Show-Budget Subsystem

Context: the modern-path gate in `verify_python_target` requires the feature flag
on, `lane != "mutmut-legacy-py2"`, and mutmut major version >= 3. The `else`
(legacy) branch therefore serves three constituencies: (1) Python2/mutmut1.5,
(2) Python3 with the flag off, (3) Python3 with mutmut<3. The metadata-counting and
`mutmut show` budget machinery reads `mutants/<source>.meta`, which is a mutmut 3.x
artifact. mutmut 1.5.0 never writes it, so Python2 falls back to changed-line
masking and never reaches this subsystem. It is the Python3 transitional fallback
only and becomes dead once Phase 6's preconditions hold.

Files:

- `uta/language/python/verification/runner.py`
- focused Python verifier tests, including a Python2 mutmut1.5 fixture test

Removal candidates (Python3-fallback only, NOT used by Python2):

- `_mutmut_meta_counts_for_changed_lines`
- `_mutmut_exact_counts_for_changed_lines`
- the `mutmut show` budget/prefix-counting helpers (`_MutmutShowBudget`,
  `_non_killed_candidate_keys`, `_counts_for_mutant_prefixes`,
  `_changed_mutant_prefixes`, `_write_mutmut_show_scope_note`, and the related
  `mutmut show` scope-note artifact path) where they are reachable only from the
  metadata-count path.

Must be preserved (Python2/mutmut1.5 lane, permanent until a separate py2 spec):

- `_mutmut_run_command`, `parse_mutmut_summary`
- `_apply_changed_line_mutation_mask`, `_scope_mutation_to_changed_lines`
- `_legacy_mutmut15_candidate_plan`, `_mutmut_config_overlay`, patch-file writing,
  survivor parsing/show used by the legacy run

Work:

1. Confirm preconditions: candidate-plan flag defaults on for Python3 and mutmut<3
   is no longer supported for Python3.
2. Add/confirm a Python2 mutmut1.5 fixture test that asserts the masking-based
   changed-line counts before removal, so removal cannot silently change Python2
   scoring.
3. Remove the metadata-counting / show-budget subsystem and any code that only the
   Python3 fallback reached.
4. Simplify the modern-path gate now that the Python3 fallback branch is gone.

Acceptance:

- Python2/mutmut1.5 verification still runs and reports the same changed-line
  mutation counts as before removal.
- Python3 verification uses only the candidate-plan adapter path.
- No reference to `mutants/*.meta` counting or `mutmut show` budget remains for the
  Python3 path.
- Java behavior is unchanged.

## 11. Current Status

Phase 5 is implemented in the current worktree. Remaining rollout work is
deployment/staged verification, not additional local implementation scope for this
compliance closure.

Phase 6 cleanup has started. The first local cleanup slice made the Python3
candidate-plan path the default, rejects Python3 `mutmut<3` instead of falling
back, and removes the Python3 metadata-counting / `mutmut show` scope-budget
fallback. Python2/mutmut1.5 keeps its legacy lane until a separate future spec
retires it.

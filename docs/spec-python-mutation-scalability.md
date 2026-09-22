# Spec: Practical Python Mutation Scalability

## Changelog

- 2026-06-22: Initial Phase 1 spec for practical, diff-based Python mutation gating inspired by Google's mutation-testing-at-scale approach.
- 2026-06-22: Clarified CI report and repair-session verification must share the same candidate planning logic, with CI using a smaller deterministic cap profile before mutmut emits metadata.
- 2026-06-22: Added deterministic rerun contract: same immutable inputs must produce the same selected tests, mutation candidate set, denominator, and verdict.
- 2026-06-22: Added mutmut v2/v3 source-basis findings. Python3/mutmut 3 records line-mapped metadata for adapter-generated mutants and runs that generated set for the decisive gate. Python2 stays on the current `mutmut==1.5.0` lane and must not claim mutmut2 or mutmut3 semantics.
- 2026-06-22: Replaced selected-key execution with adapter-filtered generation. UTA now materializes only the mutants it intends to run, then runs mutmut over that generated set.
- 2026-06-22: Collapsed public planning into one `MutationCandidatePlan`; adapter-local generation policy is an implementation detail, not a separate engine-level plan.
- 2026-06-22: Addressed accepted design-review findings: split pre-generation opportunities from generated candidates, made verifier determinism inputs explicit, kept Python2 on mutmut 1.5.0, added engine suppression categories, and named rollback/performance config knobs.
- 2026-06-22: Renamed engine evidence from mutmut-specific keys to language-neutral `mutationTool*` keys without compatibility aliases because this feature has not shipped yet.

## 1. Assumptions

1. This is non-Jira internal UTA tool work; no Jira or release approval release artifacts are required for this spec phase.
2. The immediate pain is Python `mutmut` runtime in CI enforcement and repair verification, while Java must not regress.
3. The existing coverage gate remains strict over all executable changed lines; mutation optimization must not weaken coverage enforcement.
4. CI report enforcement and repair/nightly/full verification use the same candidate planning logic. The CI report path may use a smaller deterministic cap profile when the selected mutation set is too large; repair/full uses a larger cap profile.
5. The design must preserve the UTA architecture invariant: language-specific mutation tools stay behind language adapters; candidate planning, evidence shape, reporting, and policy are language-agnostic.
6. There are two invocation paths for Python mutation verification: CI report enforcement and repair-session verification. They must share the same candidate planning, arid suppression, mutmut operator selection, selected-test semantics, and evidence contract. CI report enforcement may use the smaller CI cap profile, but it must not fork a separate verifier implementation.
7. Mutation checking must be deterministic. A target that passes mutation for the same repo, base ref, head commit, target source path, changed lines, selected tests, runtime config, and policy mode must not fail on rerun because of different candidate selection, different test selection, stale mutmut cache, random sampling, or nondeterministic report aggregation.

## 2. Source Basis

Google's "Practical Mutation Testing at Scale" paper gives the relevant operating model:

1. Run mutation testing incrementally on changed code, not the whole codebase.
2. Filter irrelevant mutants before generation, including arid-node suppression.
3. Bound cognitive and compute cost by limiting mutants per line/review.
4. Use historical/operator performance to select higher-value mutants.

Mutmut's current implementation and public docs show relevant Python mechanisms:

1. Mutmut 3.x generates mutants from LibCST operators before execution. Its visitor already has source positions and enclosing-symbol metadata, so UTA can apply a deterministic operator policy before appending a mutation candidate.
2. Mutmut 3.x generated metadata records exact mutant keys under `mutants/<source>.meta`; those keys are useful evidence, but stock CLI key filtering alone is not the scalability boundary because stock `mutmut run <key...>` still performs generation before exact-name execution.
3. Mutmut can skip code with `# pragma: no mutate`, but pragmas are not the primary Python3 candidate filter because they cannot limit same-line operator count. UTA should avoid temporary source edits in the primary path.
4. Mutmut 3.x configuration remains useful for source paths, test selection, and copy/import compatibility.
5. Mutmut 2.x explicitly supports `mutmut_config.py`, including `pre_mutation(context)` where UTA can set `context.skip=True`, plus patch-file scoping and mutation-type enable/disable flags. This is useful source context, but this design does not migrate the existing Python2 lane to mutmut2.
6. Current UTA Python2 verification is tied to `mutmut==1.5.0`. Python2 compatibility in this design means preserving that lower-precision legacy lane and reporting it honestly, not claiming mutmut2 or Python3/mutmut3 exact-key filtering.

## 3. Objective

Make Python mutation gating practical for large diffs by reducing generated/executed mutants before mutmut execution, using deterministic and explainable candidate planning:

1. Mutate only relevant production changed lines.
2. Suppress arid/low-value mutation candidates before execution.
3. Apply one exact mutmut operator candidate per eligible changed line, with deterministic hard caps when the selected mutation set is too large.
4. Report exactly what was considered, skipped, selected, cap-trimmed, run, killed, and survived.

The user is UTA CI/plugin users and UTA maintainers. Success means large Python diffs no longer spend tens of minutes running thousands of low-value mutants without explaining why, while the report remains honest about the scope of the mutation gate.

## 4. Current Scope Discovery

| Area | Path | Current behavior | Decision |
| --- | --- | --- | --- |
| Python verification | `uta/language/python/verification/runner.py` | Runs coverage first, filters low-value side-effect changed lines, applies deterministic generation caps, runs adapter-generated mutants, then aggregates changed-line mutation counts. | In scope. This is the main enforcement runtime path to harden. |
| Python enforcement aggregation | `uta/language/python/enforcement.py` | Aggregates target evidence, changed-line mutation counts, no-test/no-coverage/skipped counts, and candidate-plan metadata. | In scope. Needs richer candidate-plan evidence and clearer semantics. |
| Python repair session verification | `uta/language/python/batch.py`, `uta/language/python/verification/runner.py` | Repair reruns verification after LLM edits and currently depends on the same verifier entrypoint for coverage/mutation checks. | In scope. Must consume the same candidate plan as CI enforcement. Repair uses the larger full cap profile; CI report enforcement uses the smaller CI cap profile for large mutation sets. |
| CI report rendering | `uta/api_trigger/reporting.py`, `uta/api_trigger/templates/report.html` | Shows scored mutation counts and existing mutation summary, but not the full pre-mutation funnel. | In scope. Must show skipped/selected/run counters explicitly. |
| Shared mutation repair planner | `uta/engine/mutation_repair.py`, `uta/language/python/mutation_context.py` | Optimizes post-survivor LLM repair context with ROI grouping. | Adjacent. Do not replace it; feed it better precomputed mutation evidence when available. |
| Shared mutation ROI math | `uta/engine/mutation_roi.py` | Shared kill-per-effort scoring helpers for repair groups. | In scope for selected-mutant ranking if generalized from survivor repair to pre-run candidates. |
| Java PIT path | `uta/language/java/maven/pitest.py`, `uta/graph/nodes.py` | Java already has PIT family summaries and Maven/test-enforcer diff filtering. | Regression scope only. Do not narrow Java PIT context or change Java gate semantics in this spec. |
| Dev-skills local enforcement | `/path/to/dev-skills/scripts/uta_dev_gate.py`, `/path/to/dev-skills/references/test-enforce-usage.md` | Local dev gate and guidance must stay aligned with UTA enforcement behavior. | In scope for implementation if Python enforcement semantics or wording change. |
| Maven/test-enforcer plugin repos | external maven plugin repos | Java-side diff coverage/mutation gate implementation. | Out of scope unless Java report evidence must be aligned later. |
| Provider/model fallback | `uta/opencode/*` | Handles LLM failures and token/model fallback. | Out of scope; mutation runtime reduction must not depend on model behavior. |
| DB schema | `uta/tasks/db.py`, CI record store JSON | Current evidence is JSON payload based. | No DB migration expected. Add fields to structured evidence JSON only. |

## 5. Requirements

### 5.1 Candidate Planning

1. Add a language-agnostic mutation candidate plan contract in the engine layer.
2. CI report enforcement and repair-session verification must both call this shared planner. The shared policy mode is `report_full`:
   - `report_full`: build the full eligible candidate plan, including one exact mutmut key per eligible changed line;
   - repair-session verification uses `report_full` directly;
   - nightly/manual full verification uses the same `report_full` semantics;
   - CI report enforcement may apply one deterministic report-wide CI cap across the aggregate selected mutation set. Selection must be representative across targets, symbols, and operators and must not reset the cap per target.
   - When CI report sampling is enabled, mutation gating is report-aggregate only. Per-target sampled mutation rates are diagnostic and must not independently fail a target, trigger alternate-test retries, or contradict a passing aggregate decision. Coverage, test execution, mutation backend, timeout, and resource failures remain target failures.
   - If a sampled aggregate fails and the user creates a repair session, UTA must refresh the branch and rerun Python enforcement with the non-sampled full-cap profile before creating repair targets. Repair targets must come from that full evidence, never from sampled target diagnostics.
   - The deterministic selection fingerprint must exclude test-only HEAD changes. It changes only when the base, production mutation-opportunity universe, or selection policy changes.
3. Python adapter must produce pre-generation opportunity records and generated candidate records before running mutmut:
   - source path;
   - changed line;
   - symbol/function/class range;
   - coverage status for that line;
   - arid/low-value reason if suppressed;
   - candidate family/operator hint when statically knowable;
   - stable pre-generation `opportunity_id`;
   - exact mutmut key only after mutmut3 metadata maps generated candidates back to changed lines.
4. Candidate planning must happen before mutation execution, not only after reading `mutants/*.meta`.
5. Candidate planning must distinguish:
   - changed production lines;
   - executable changed lines;
   - coverage-eligible changed lines;
   - mutation-eligible changed lines;
   - suppressed arid/low-value lines;
   - selected lines/candidates for CI execution.
6. Python3/mutmut 3 filtering should use one clean implementation path: a UTA-owned mutmut adapter that filters during metadata generation. The rollout reads mutmut metadata for evidence, maps exact keys to eligible changed lines, and runs mutmut over the adapter-generated set rather than passing exact keys as an execution filter. Existing mutmut config overlay may still set source paths, test selection, and copy/import compatibility; `mutmut_config.py` dynamic filtering is legacy-only and is not part of the Python3 design.
7. The verifier must never leave target source files modified after a run, even when mutmut times out.
8. If exact mutmut key enumeration or execution is unavailable in a legacy Python/mutmut path, the verifier must mark the evidence with a different `filterMechanism` and must not report a narrow exact-key-per-line denominator.
9. Python3 candidate planning has one public plan contract:
   - `MutationCandidatePlan` contains pre-generation selected opportunities plus post-generation generated-key evidence attached by the Python adapter;
   - the adapter may build an internal generation policy from changed-line eligibility, coverage, arid suppression, and operator ranking, but this policy is not a separate engine-level plan type;
   - the public plan contains pre-generation opportunities, generated exact mutmut-key candidates, selected candidate ids, generation-policy fingerprint, and scoring denominator.
10. The public plan must not require exact mutmut keys for suppressed or omitted opportunities. Generated/scored candidates require `tool_candidate_key`; suppressed and omitted opportunities use `opportunity_id` and an optional key only when the adapter can prove one exists.

### 5.2 Arid-Node Suppression

1. Suppress low-value/arid mutation candidates before mutmut execution where possible.
2. Initial Python arid categories should include:
   - logging, tracing, metrics, audit-only calls;
   - import-only/module wiring boilerplate;
   - pure constant/config declarations without behavior;
   - generated or framework registration glue when no behavior assertion is meaningful;
   - code that is not executable changed production code.
3. Suppression must be conservative: if UTA cannot prove a candidate is low-value, keep it eligible.
4. Every suppressed candidate must carry a machine-readable reason and a report-visible summary.
5. Suppression rules should be implemented as reusable engine categories with Python AST bindings, not as scattered Python-only conditionals:
   - engine owns neutral categories and reason codes such as `low_value_logging`, `metrics_only`, `import_wiring`, `pure_config_constant`, and `generated_framework_glue`;
   - Python adapter owns AST matching for those categories;
   - every suppression carries `opportunity_id`, `reason_code`, and `reason`.

### 5.3 Mutation Selection And CI Cap Policy

1. For every invocation path, build the same deterministic selected mutation set from eligible changed-line opportunities, apply the active cap profile before generation, then ask the adapter to emit only that set.
2. Default `report_full` policy:
   - prefer one exact mutmut mutant key per eligible changed line;
   - rank candidates by a versioned deterministic operator policy, then stable tie-breakers;
   - keep selection stable across reruns for the same diff/base/head;
   - expose the selected set in evidence.
3. Candidate planning must include mutant-per-line logic before mutmut execution:
   - group static mutation opportunities by source path and changed line;
   - choose one representative opportunity per eligible changed line for `report_full`;
   - map selected opportunities to exact mutmut mutant keys after metadata-selected execution;
   - keep non-selected same-line candidates in evidence as omitted-by-one-per-line, not as hidden failures.
4. The only CI report difference is cap profile: if the selected mutation set is larger than the configured CI limit, CI report enforcement generates and runs the deterministic capped subset.
5. Repair-session verification and nightly/manual full verification use the larger full cap profile by default.
6. The mutation score denominator in CI reports must be the scored candidate count actually selected by the active layer:
   - full selected/scored count for `report_full`;
   - cap-trimmed selected/scored count for capped runs.
7. If a cap trims candidates, the report must say so clearly and show total eligible candidates, selected-before-cap candidates, generated candidates, omitted-by-cap candidates, and scored candidates.
8. Selection must be deterministic:
   - no random sampling;
   - stable ordering by explicit operator priority, versioned ROI score if enabled, source path, line, symbol, operator name, normalized diff, opportunity id, then hash;
   - generated candidate ids must include the exact mutmut mutant key and stable source/diff metadata;
   - pre-generation opportunity ids must include stable source/diff/operator metadata and must not depend on mutmut-generated numbering;
   - reruns must reconstruct the same candidate plan for the same base/head and policy mode; persisted report evidence is used for comparison, not as the source of selection;
   - hard caps must cut deterministically over the pre-generation selected opportunity set.
9. Initial operator priority policy must be explicit and versioned:
   - priority 100: control-flow and decision predicates, including boolean/comparison changes that affect branch selection;
   - priority 90: return expressions, call arguments, arithmetic operators, comparison operators, and exception/control effects;
   - priority 80: collection, numeric, string, and truthiness literal changes inside executed behavior;
   - priority 60: assignment/value propagation changes that are not pure configuration;
   - priority 20: unknown operator families that are still executable and covered;
   - suppressed: logging, tracing, metrics, import-only glue, pure config/constants, and other arid categories from section 5.2.
10. ROI/history can affect selection only when it comes from a versioned immutable policy snapshot whose fingerprint is included in `candidatePlanId`. Live mutable history must not affect deterministic CI/retry selection.

### 5.4 Deterministic Rerun Contract

1. CI report enforcement, repair-session verification, and manual reruns must produce the same pass/fail result for the same immutable inputs:
   - repo URL;
   - base ref/base commit;
   - head commit;
   - target source path;
   - changed lines;
   - selected strict test paths;
   - Python runtime/dependency fingerprint;
   - mutmut version;
   - candidate policy mode;
   - active cap profile and omitted-by-cap evidence, when a cap trims candidates.
   - UTA planner version;
   - arid-rule version;
   - selected-test discovery policy version;
   - mutmut config overlay fingerprint;
   - effective cap configuration.
   - operator policy version;
   - suppression policy version;
   - mutation-tool API fingerprint for the Python3 adapter.
2. The verifier must clear or namespace mutmut state so stale `mutants/` metadata cannot change the selected/scored mutant set across reruns.
3. The evidence payload must include a deterministic `candidatePlanId` or equivalent fingerprint built from the immutable inputs and selected candidate ids.
4. Repair rerun must not silently switch test files after a target has already passed; strict test selection must be deterministic and report-visible.
5. If the verifier detects changed fingerprinted inputs, such as changed dependency fingerprint, changed Python runtime, changed branch head, or missing base commit, it must continue verification against the current inputs but mark the evidence as `comparable=false` rather than presenting a clean mutation regression.
6. A recomputed candidate plan mismatch with no changed fingerprinted input is a UTA determinism bug and must fail verification with a diagnostic. A recomputed mismatch with changed fingerprinted inputs is allowed to continue as a new verification context and must be reported as non-comparable to the prior evidence.
7. A zero generated/scored mutation result may pass only when the candidate plan proves there were no eligible exact mutmut operator candidates after coverage and arid suppression. Missing mutmut metadata or failed exact-key enumeration is a tool/planner failure.
8. `verify_python_target` must receive the immutable inputs needed to build the plan id, either as a `MutationVerificationContext` object or explicit equivalent fields. CI enforcement constructs those fields from git, base/head commits, runtime/dependency fingerprints, strict selected tests, and policy versions. Repair reconstructs them from persisted CI evidence plus the current workspace and marks changed fingerprints as non-comparable.

### 5.5 Reporting And Evidence

1. Extend Python mutation evidence with a `candidatePlan` block:
   - `candidatePlanId`;
   - `policyMode`;
   - `samplingLayer` compatibility field, disabled for cap-based planning;
   - `changedLines`;
   - `executableChangedLines`;
   - `coveredChangedLines`;
   - `eligibleMutationCandidates`;
   - `eligibleMutationOpportunities`;
   - `reportFullSelectedCandidates`;
   - `suppressedCandidates`;
   - `suppressionByReason`;
   - `selectedCandidates`;
   - `omittedByOnePerLine`;
   - `selectionStrategy`;
   - `selectionLimit`;
   - `capProfile`;
   - `omittedByCap`;
   - `mutationToolVersion`;
   - `runtimeFingerprint`;
   - `dependencyFingerprint`;
   - `filterMechanism`;
   - `adapterFilteredGenerationApplied`;
   - `selectedKeyExecutionApplied` retained only as a compatibility field and false for the Python3 adapter-filtered path;
   - `exactToolCandidateKeyCount`;
   - `candidatePlanArtifactPath`;
   - `generationPolicyFingerprint`;
   - `suppressionPolicyVersion`;
   - `mutationToolApiFingerprint`;
   - `opportunityIds`;
   - `reportFullCandidateIds`;
   - `activeCandidateIds`;
   - `activeToolCandidateKeys`;
   - `plannerVersion`;
   - `aridRuleVersion`;
   - `testSelectionPolicyVersion`;
   - `mutationToolConfigFingerprint`;
   - `effectiveCapConfig`;
   - `runMutants`;
   - `scoredMutants`;
   - `killed`;
   - `survived`;
   - `noTests`;
   - `timeout`;
   - `suspicious`.
2. CI report must show a concise funnel:
   - changed lines -> covered changed lines -> eligible candidates -> selected/run mutants -> scored mutants -> killed/survived.
3. Per-target failure rows must show whether failure came from:
   - coverage;
   - surviving selected mutants;
   - mutation timeout;
   - no selected test association;
   - tool/runtime error.
4. Raw artifacts should include a JSON candidate plan under `.uta_cache/python/mutation/`.
5. The CLI/test-enforcer console output must include enough of the same summary for local dev users.
6. Reports must explicitly state when a rerun is not comparable to a prior passing mutation result because the candidate plan id, selected tests, runtime fingerprint, dependency fingerprint, branch head, policy mode, or cap profile changed.
7. Repair sessions triggered from a CI report must rebuild the target-level `report_full` candidate plan from current fingerprinted inputs. Persisted CI report evidence is a comparison anchor only. If the rebuilt selected-test/operator-key set differs with no fingerprint change, fail as a UTA determinism bug; if it differs because fingerprinted inputs changed, continue repair and mark `comparable=false`.
8. CI report evidence must persist selected candidate ids, selected exact tool keys, and fingerprints directly; `.uta_cache` artifacts are debug aids, not the source of truth for repair parity.

### 5.6 Compatibility

1. Java behavior must not regress.
2. Python2 legacy support must not be broken. Python2 remains on the current `mutmut==1.5.0` lane unless a separate future spec migrates it. It must report that lower-precision filter contract instead of pretending mutmut2 or exact mutmut3 operator filtering was used.
3. Existing structured evidence consumers must remain compatible; new fields must be additive.
4. Existing report URLs and repair-session flow must keep working.
5. Local dev-skills Python enforcement script and usage guidance must be synced if UTA enforcement behavior or evidence wording changes.
6. CI report and repair-session paths must not drift: any future change to eligibility, arid suppression, test selection, or mutmut filtering must be made in the shared planner/verifier layer and covered by tests for both invocation paths.
7. Dev-skills sync acceptance criteria:
   - `uta_dev_gate.py` validates `mutation.candidatePlan` when present;
   - zero scored/generated mutation passes only with no eligible exact candidates or reason-coded suppression;
   - legacy evidence without candidatePlan keeps existing validation;
   - `test-enforce-usage.md` describes deterministic generation caps and exact-key mutation evidence instead of changed-line sampling.
8. Python2/mutmut 1.5.0 remains a legacy compatibility path:
   - keep the existing `mutmut==1.5.0` requirement for Python2;
   - use the current changed-line scope/masking and post-filter evidence available to that lane;
   - do not claim mutmut2 `mutmut_config.py` or mutmut3 exact-key-per-line semantics for Python2 evidence;
   - report a distinct `filterMechanism`, for example `mutmut15_legacy_changed_line_scope`.
9. Mutmut2 support is out of scope for this implementation. If Python2 later moves from mutmut 1.5.0 to mutmut2, that migration needs a separate compatibility proof over real Python2 repos.

### 5.7 Configuration And Rollback

1. Add candidate-plan config names while preserving old sampling env names as aliases to the CI cap profile during rollout:
   - `python_mutation_candidate_plan_enabled` / `UTA_PYTHON_MUTATION_CANDIDATE_PLAN_ENABLED`;
   - `python_mutation_generation_ci_max_changed_lines` / `UTA_PYTHON_MUTATION_GENERATION_CI_MAX_CHANGED_LINES`;
   - `python_mutation_generation_ci_max_selected` / `UTA_PYTHON_MUTATION_GENERATION_CI_MAX_SELECTED`;
   - legacy aliases: `UTA_PYTHON_MUTATION_SAMPLE_LINE_THRESHOLD`, `UTA_PYTHON_MUTATION_SAMPLE_LINE_LIMIT`, `UTA_PYTHON_MUTATION_CANDIDATE_SAMPLE_THRESHOLD`, `UTA_PYTHON_MUTATION_CANDIDATE_SAMPLE_LIMIT`.
2. Add execution and performance controls:
   - `python_mutation_adapter_generation_timeout_seconds` / `UTA_PYTHON_MUTATION_ADAPTER_GENERATION_TIMEOUT_SECONDS`;
   - `python_mutation_selected_execution_timeout_seconds` / `UTA_PYTHON_MUTATION_SELECTED_EXECUTION_TIMEOUT_SECONDS`;
   - `python_mutation_per_mutant_timeout_seconds` / `UTA_PYTHON_MUTATION_PER_MUTANT_TIMEOUT_SECONDS`;
   - `python_mutation_generation_max_source_bytes` / `UTA_PYTHON_MUTATION_GENERATION_MAX_SOURCE_BYTES`;
   - `python_mutation_generation_max_changed_lines` / `UTA_PYTHON_MUTATION_GENERATION_MAX_CHANGED_LINES`;
   - `python_mutation_generation_max_opportunities` / `UTA_PYTHON_MUTATION_GENERATION_MAX_OPPORTUNITIES`;
   - `python_mutation_generation_max_selected` / `UTA_PYTHON_MUTATION_GENERATION_MAX_SELECTED`;
   - `python_mutation_max_children` / `UTA_PYTHON_MUTATION_MAX_CHILDREN`.
3. The candidate-plan feature flag must be able to disable the new Python3 adapter and return to the existing changed-line masking/post-filter path.
4. Config values that affect selection or execution must be included in `candidatePlanId` or `candidatePlan` evidence through an effective config fingerprint.

## 6. Commands

```bash
# Focused unit tests for Python mutation verification/reporting
.venv/bin/python -m pytest -q tests/test_python_verification.py tests/test_python_enforcement.py

# CI report rendering tests, if report assertions are touched
.venv/bin/python -m pytest -q tests/test_api_trigger*.py tests/test_python_ci*.py

# Existing impacted mutation repair tests
.venv/bin/python -m pytest -q tests/test_mutation_repair*.py tests/test_python_batch_generation.py

# Full local regression before commit
.venv/bin/python -m pytest -q
```

## 7. Project Structure

```text
uta/engine/
  mutation_candidates.py      # new shared candidate-plan contract and selection policy
  mutation_suppression.py     # neutral suppression categories and reason-code taxonomy
  mutation_roi.py             # existing shared ROI helpers, reused where applicable

uta/language/python/
  mutation_candidates.py      # Python mutmut exact-key candidate adapter
  verification/runner.py      # Python mutmut execution and adapter wiring
  enforcement.py              # Python evidence aggregation

uta/api_trigger/
  reporting.py                # report evidence shaping
  templates/report.html       # user-visible mutation funnel

tests/
  test_python_verification.py # verifier/candidate-plan unit tests
  test_python_enforcement.py  # evidence aggregation tests
  test_api_trigger*.py        # report rendering tests
```

## 8. Code Style

Use explicit dataclasses for shared contracts and keep language-specific extraction behind adapters:

```python
@dataclass(frozen=True)
class MutationCandidatePlan:
    language: str
    target_id: str
    source_path: str
    filter_mechanism: str
    changed_lines: tuple[int, ...]
    eligible_opportunities: tuple[MutationOpportunity, ...]
    suppressed: tuple[SuppressedMutationOpportunity, ...]
    selected: tuple[MutationCandidate, ...]
    omitted_by_one_per_line: tuple[MutationOpportunity, ...]
    selection_strategy: str
    exact_tool_candidate_keys: tuple[str, ...]
```

Rules:

1. Engine contracts use neutral names: opportunity, candidate, selected, suppressed, scored.
2. Python mutmut details, including exact mutant keys and metadata parsing, stay in `uta/language/python`.
3. Java PIT details stay in `uta/language/java`.
4. Report/evidence code reads normalized evidence and should not branch on mutmut internals.

## 9. Testing Strategy

1. Unit tests for candidate planning:
   - changed production lines are recognized;
   - non-executable/comment/blank lines are excluded;
   - covered-vs-uncovered changed lines are separated;
   - low-value logging/metrics/tracing calls are suppressed with reasons;
   - deterministic selection is stable across reruns.
2. Unit tests for mutmut operator-selection mechanism:
   - Python adapter builds exact mutmut mutant keys for eligible changed lines;
   - one selected exact mutmut key per eligible changed line is generated and executed by the adapter;
   - Python3 adapter generates only the active line-mapped candidates for the decisive score;
   - mutmut config overlay remains limited to source/test/copy/import compatibility, not candidate filtering;
   - legacy Python2/mutmut 1.5 filtering reports `mutmut15_legacy_changed_line_scope` and does not claim mutmut2 or mutmut3 exact-key semantics.
   - exact-key enumeration failure on covered eligible lines fails closed.
3. Enforcement tests:
   - large diff triggers deterministic CI caps over the shared selected opportunity set and reports total eligible, selected-before-cap, cap-trimmed, generated, and scored counts;
   - mutation gate denominator uses selected/scored mutants;
   - no-test/no-coverage mutants are excluded consistently with existing coverage-gate semantics.
   - zero generated/scored candidates pass only when there are no eligible exact candidates or all candidates are reason-suppressed.
   - CI report and repair-session verification consume the same candidate plan for the same target/diff;
   - CI capped mode differs only by deterministic generation caps, not by eligibility, suppression, or generated-mutant execution rules.
   - repeated enforcement over the same immutable inputs produces the same candidate plan id, selected candidate ids, selected test paths, denominator, and pass/fail result.
4. Report tests:
   - CI HTML shows the candidate funnel;
   - CI-capped runs are clearly labeled;
   - local guidance remains Python-specific.
5. Regression tests:
   - Java PIT repair/report tests still pass;
   - Python strict test selection remains deterministic;
   - repair planner still receives survivor context after verification.
6. Real verification:
   - run one Python3 repo with a large diff and verify CI caps control runtime and evidence is clear;
   - run one Java regression task and verify Java mutation behavior is unchanged;
   - run local dev-skills Python enforcement and check usage guidance after syncing any changed script behavior or wording.
   - run dev-skills tests for candidatePlan-aware mutation validation if dev-skills validator logic changes.

## 10. Boundaries

Always:

1. Keep coverage strict over all executable changed lines.
2. Keep mutation candidate policy deterministic and evidence-backed.
3. Preserve language-agnostic engine contracts.
4. Keep report wording honest when CI mutation is cap-trimmed.
5. Restore all transient target-repo config/source changes.
6. Make mutation selection and scoring reproducible across reruns.

Ask first:

1. Changing Python mutation gate thresholds.
2. Changing Java Maven/test-enforcer behavior.
3. Adding a new runtime dependency.
4. Applying the CI cap profile to repair/full verification.
5. DB schema migrations.

Never:

1. Silently pass a mutation gate because mutation was skipped without an explicit reason.
2. Hide cap-trimmed mutation evidence in the report.
3. Count unselected mutants in the displayed denominator.
4. Leave transient mutmut config files in the target repo.
5. Put mutmut-specific policy directly into report/core workflow layers.
6. Let mutmut cache state, filesystem traversal order, or random sampling decide whether a rerun passes.
7. Report a narrow mutation denominator unless the adapter actually generated and executed the selected keys that define that denominator.

## 11. Success Criteria

1. A large Python diff with thousands of possible mutmut candidates produces a CI-capped run with clear evidence of total eligible, selected-before-cap, omitted-by-cap, generated, and scored mutants.
2. A log/metrics-only Python changed line can pass mutation with a report-visible arid suppression reason while coverage remains enforced.
3. The report shows a mutation funnel instead of only a final percentage.
4. The Python verifier can explain that it used the shared exact-mutant candidate filter, whether adapter-filtered generation or a legacy mechanism was used, and whether a CI cap profile was active.
5. Java regression tests and at least one Java E2E verification still pass.
6. Python repair uses the same eligibility/suppression/filtering logic as CI report enforcement and runs full eligible mutation by default.
7. Local dev-skills Python enforcement output stays aligned with CI evidence.
8. Re-running the same target with the same base/head/config produces the same mutation verdict and denominator; if it does not, the report explains which fingerprint changed or flags nondeterminism as a verifier bug.

## 12. Open Questions

1. The initial CI cap profile keeps legacy sampling env aliases for rollout compatibility, but the implementation should use cap-named settings and measure node2 runtime before changing defaults.
2. Should Java report evidence adopt the same candidate-funnel display later, even if Java mutation execution stays in the Maven plugin?

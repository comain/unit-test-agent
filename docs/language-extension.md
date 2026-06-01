# Adding A Language Backend

This guide documents what must be implemented when adding a third language to UTA. The core rule is that shared workflow, task, report, progress, cost, and CI code should consume engine contracts, not language-specific classes or path conventions.

## 1. Package Layout

Create a language package under `uta/language/<language>/`:

```text
uta/language/<language>/
  __init__.py
  adapter.py
  batch.py
  ci.py
  context.py
  context_builder.py
  enforcement.py
  parse/
    __init__.py
    models.py
    parser.py
  project_summary.py
  scoring.py
  validation.py
  verification/
    __init__.py
    runner.py
```

Only keep files that are meaningful for the language. If a feature is not supported, expose that through capabilities and deterministic diagnostics instead of leaving shared code to guess.

## 2. Target And Language Adapter

Implement `LanguageAdapter` in `uta/language/<language>/adapter.py`.

Required behavior:

- `language`: stable lowercase backend id, for example `go`.
- `capabilities()`: declare function targets, branch coverage, mutation, incremental diff enforcement, import safety hints, and generated-test autopush support.
- `detect(repo_path, changed_paths=None)`: return marker evidence for auto-detection.
- `normalize_target(raw)`: convert CLI, manifest, CI, and test inputs into `TargetRef`.
- `generated_test_policy(repo_path, target)`: define allowed generated test roots and autopush policy.
- `prompt_bundle()`: return prompt template names for plan, generation, and repair phases.

Wire the adapter in `uta/engine/languages.py::default_registry()`.

## 3. Parsing

Implement a `ParseProvider` in `uta/language/<language>/parse/__init__.py`.

The provider must return a result compatible with `uta.engine.parse.ParseProjectResult`:

- `language`
- `repo_path`
- `source_files`
- `callables`
- `imports`
- `diagnostics`
- `contains_target(target_id)`
- `target_id_for_source_path(source_path)`
- `is_testable_target(target_id)`
- `target_selections(target_ids)`

Add the provider to `uta/engine/parse.py::make_parse_provider(language)`.

Parser internals belong under `uta/language/<language>/parse/`. Shared workflow code should not import those internals directly.

## 4. Context And Project Summary

Implement `ContextProvider` in `uta/language/<language>/context.py`.

Required behavior:

- `export_project_context(**kwargs)`: create project-level context artifacts.
- `export_target_context(target, **kwargs)`: create prompt-ready target context files or payloads.
- `query_target(target, query=None)`: support CLI/query-index style target lookups.

Add it to `uta/engine/context.py::make_context_provider(language, ...)`.

Implement `ProjectSummaryProvider` in `uta/language/<language>/project_summary.py` and add it to `uta/engine/project_summary.py::make_project_summary_provider(...)`.

## 5. Batch Generation

Implement `BatchGenerator` in `uta/language/<language>/batch.py`.

Inputs should use `BatchGenerationRequest` and `TargetRef`; outputs should use `BatchGenerationResult` so token usage, phase timing, session ids, final state, and errors stay report-compatible.

The language batch implementation owns:

- generated test path construction
- generated file ownership marker
- prompt rendering
- validation and repair loop orchestration
- verification runner invocation
- task result field updates

Do not add language-specific branches to shared reporting or cost accounting. Return normalized result fields instead.

## 6. Verification

Implement `VerificationRunner` in `uta/language/<language>/verification/__init__.py` and the deterministic tool runner in `verification/runner.py`.

The result object must expose:

- `status`
- `reason_code`
- `as_result_fields()`

`as_result_fields()` should preserve the common fields consumed by task DB, reports, progress UI, and cost accounting:

- `status`
- `coverage`
- `tests_pass`
- `mutation_score`
- `surviving_mutants`
- `total_mutants`
- `killed_mutants`
- `no_coverage_mutants`
- `verification_status`
- `verification_reason`
- `verification_message`
- `verification_commands`
- `coverage_summary`
- `mutation_summary`

Add the runner to `uta/engine/verification.py::default_verification_registry()`.

## 7. Enforcement And CI

Implement an enforcement core in `uta/language/<language>/enforcement.py`.

The evidence payload should include:

- `schemaVersion`
- `evidenceId`
- `language`
- `backend`
- `repo`
- `baseRef`
- `headCommit`
- `changedProductionFiles`
- `changedLines` when incremental diff gates are supported
- `targets`
- `coverage`
- `mutation`
- `commands`
- `artifacts`
- `status`
- `passed`
- `reasonCode`
- `summary`
- `generatedAt`
- `utaVersion`
- language-specific runtime/setup metadata when relevant

Validation should reject unknown schema versions, wrong backends, stale commits, and failed gates with stable reason codes.

Implement `CiLanguageHandler` in `uta/language/<language>/ci.py` so CI repair task creation is language-owned. Add it to the CI plugin registry wiring rather than branching in service code.

## 8. Scoring And Plan Validation

Implement `TargetScorer` in `uta/language/<language>/scoring.py` and add it to `uta/engine/scoring.py::default_scorer_registry()`.

Return `TargetScoreResult` with normalized method/callable rows. The rows should be useful for planning and repair prompts without exposing parser-specific objects.

Implement `PlanContextExtractor` in `uta/language/<language>/validation.py` and add it to `uta/engine/validation.py::default_plan_context_registry()`.

Plan validation should consume normalized callable metadata, not language-specific markdown sections.

## 9. Prompts

Add language-specific prompt templates under `uta/prompts/` and return their names from `PromptBundle`.

At minimum:

- plan prompt
- generate prompt
- compile/runtime repair prompt if applicable
- coverage repair prompt if coverage is supported
- mutation repair prompt if mutation is supported

Prompts should refer to target display names, source paths, symbols, and normalized context payloads instead of assuming Java class FQNs.

## 10. CLI, Tasks, And Reports

Shared CLI and task code should keep using:

- `resolve_language(...)`
- `TargetRef`
- `BatchGenerationRequest`
- `VerificationResult.as_result_fields()`
- `TargetLearningKey`
- engine registries/factories

Avoid adding new language-specific task DB columns unless the target facade cannot represent the backend. Prefer storing language, target id, source path, symbol, granularity, and legacy Java class FQN compatibility fields through the existing target model.

If a schema migration is required, document compatibility and backfill rules before changing code.

## 11. Generated Test Safety

Define generated-test roots in `GeneratedTestPolicy`.

The backend must refuse to overwrite non-UTA-owned tests unless explicitly designed otherwise. CI auto-push allowlists should come from the language policy and generated file markers, not hardcoded path checks in shared code.

## 12. Progress And Cost Compatibility

Do not assume progress labels are Java class FQNs. Use `TargetRef.display_name`, `target_id`, `source_path`, and `symbol`.

Cost accounting should continue to aggregate:

- session ids
- session token usage
- phase token usage
- phase timings
- task target count
- verification result fields

A new backend should add language-specific details inside nested summaries, not replace common result keys.

## 13. Tests

Add focused tests for each contract:

- language detection and target normalization
- parse provider and parser fixtures
- context provider and query-target output
- project summary provider
- scoring and plan validation extractor
- verification runner with command fakes
- enforcement evidence schema and stale-head validation
- CI handler repair task creation
- batch generation path and result accounting
- generated-test overwrite protection
- report/progress/cost compatibility for non-Java display names

Add at least one staged E2E fixture or real-repo check before enabling production routing.

## 14. Acceptance Checklist

- The language appears in `default_registry().languages`.
- `make_parse_provider(language)` works.
- `make_context_provider(language, repo)` works, with required parser inputs supplied when needed.
- `make_project_summary_provider(language, repo)` works.
- `default_scorer_registry().scorer_for(language)` works.
- `default_plan_context_registry().extract(..., language=language)` works.
- `default_verification_registry().runner_for(language)` works.
- CI handler matching and repair task creation are covered.
- Generated test policy is enforced.
- README and architecture docs name the new package paths.
- Java and Python regression tests still pass.

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

Only keep files that are meaningful for the language. If a feature is not supported, say so through deterministic diagnostics instead of leaving shared code to guess.

## 2. Target And Language Ports

Implement the narrow ports declared in `uta/shared/languages.py` in
`uta/language/<language>/adapter.py`. There is no single umbrella
`LanguageAdapter` protocol any more: each port is named for the question it
answers, and a consumer depends on the one it needs.

`LanguageDetector` -- does this repository, or this path, belong to me?

- `language`: stable lowercase backend id, for example `go`.
- `detect(repo_path, changed_paths=None)`: return marker evidence for auto-detection.
- `is_production_source_path(path)`: true for repo-relative production source.

`TargetNormalizer` -- what strict target does this loose selection mean?

- `normalize_target(raw)`: convert CLI, manifest, CI, and test inputs into `TargetRef`.

`PromptBundleProvider` -- which prompt templates does this backend use?

- `prompt_bundle()`: return prompt template names for plan, generation, and
  repair phases. Publish the bundle from its own
  `uta/language/<language>/prompt_bundle.py` so phases can reach the data
  without constructing the composing adapter.

`LanguageBackend` is the union of those three. Do not add a fourth
responsibility to it: everything else a backend provides -- context,
project-summary construction, workspace policy, batch generation, and the
generation backend -- is resolved as an independent role.

Wire every role into the sole built-in composition table,
`uta/composition/language_backends.py`. `uta/shared/backends.py` owns only the
language-neutral registration and lookup mechanism. Do not add a second
resolver or a language `if`/`elif` beside this table.

CI request validation uses the registered `adapter` languages as its allowlist.
Registering that role therefore enables the language identifier; a
syntactically valid but unregistered identifier is rejected before task
creation.

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

Register the provider under the `parse` role in
`uta/composition/language_backends.py`; `uta/shared/parse.py` resolves it.

Parser internals belong under `uta/language/<language>/parse/`. Shared workflow code should not import those internals directly.

## 4. Context And Project Summary

Implement `ContextProvider` and a provider factory in
`uta/language/<language>/context.py`.

Required behavior:

- `export_project_context(**kwargs)`: create project-level context artifacts.
- `export_target_context(target, **kwargs)`: create prompt-ready target context files or payloads.
- `query_target(target, query=None)`: support CLI/query-index style target lookups.

The factory exposes `required_inputs` and `create(BackendConstructionRequest)`.
Shared callers put available resources in the request input map; the factory
declares and consumes only what that language needs. For example, Java requires
a parsed graph while Python does not. Missing required inputs must produce a
specific construction error. Register the factory under `context_factory`.

Implement `ProjectSummaryProvider` and the equivalent backend-owned factory in
`uta/language/<language>/project_summary.py`; register it under
`project_summary_factory`. Shared context and summary code must not branch on a
language name to satisfy construction differences.

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

Register the runner through the applicable verification composition registry.

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

Implement `CiLanguageHandler` in `uta/language/<language>/ci.py` so CI repair task creation is language-owned. Add it to the API trigger registry wiring rather than branching in service code.

## 8. Scoring And Plan Validation

Implement `TargetScorer` in `uta/language/<language>/scoring.py`, register the
`scoring` role in the composition table, and expose it through
`uta/testgen/scoring.py::default_scorer_registry()`.

Return `TargetScoreResult` with normalized method/callable rows. The rows should be useful for planning and repair prompts without exposing parser-specific objects.

Implement `PlanContextExtractor` in `uta/language/<language>/validation.py`,
register the `validation` role, and expose it through
`uta/testgen/validation.py::default_plan_context_registry()`.

Plan validation should consume normalized callable metadata, not language-specific markdown sections.

## 9. Prompts

Add language-specific prompt templates under `uta/testgen/prompts/` and return their names from `PromptBundle`.

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

- The adapter language appears in `default_registry().languages`.
- Every supported role is registered in
  `uta/composition/language_backends.py`; no parallel resolver exists.
- `make_parse_provider(language)` works.
- `make_context_provider(language, repo, backend_inputs=...)` works, with the
  backend factory declaring any required parser inputs.
- `make_project_summary_provider(language, repo, backend_inputs=...)` works
  under the same construction contract.
- `make_backend(language, "generation_backend")` works.
- `default_scorer_registry().scorer_for(language)` works.
- `default_plan_context_registry().extract(..., language=language)` works.
- `default_verification_registry().runner_for(language)` works.
- CI handler matching and repair task creation are covered.
- Generated test policy is enforced.
- README and architecture docs name the new package paths.
- Java and Python regression tests still pass.

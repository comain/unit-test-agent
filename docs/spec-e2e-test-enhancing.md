# Spec: E2E Test Enhancing

## Decision Status

- Jira: N/A. User confirmed this is non-Jira repository-quality tool work.
- Design doc: `docs/design-e2e-test-enhancing.md`.
- Plan doc: `docs/plan-e2e-test-enhancing.md`.
- Usage doc: Not created yet. Required if new developer commands, CI jobs, or required environment variables are introduced.
- Source input: user-provided test-suite review noting that the subsystem test layer is strong, but default E2E coverage does not exercise the full parse -> plan -> generate -> verify -> repair loop with realistic agent behavior.
- Implementation status: Partially implemented. Commit `a020fe5a` added the first architecture-invariant guard in `tests/test_engine_layering.py`.

## Implementation Progress

| Area | Status | Evidence | Remaining Work |
| --- | --- | --- | --- |
| Agnostic-layer import boundary | Done for current intended scope | Commit `a020fe5a` adds `tests/test_engine_layering.py`. It AST-scans `uta/engine` for module-load-time `uta.language.*` imports and `uta/tasks` for any `uta.language.*` import. Function-local lazy dispatch factories and `TYPE_CHECKING` imports are allowed in `uta/engine`. | Decide separately whether `uta/graph` should be refactored into language-agnostic engine code later; it is intentionally excluded today because it still carries the Java LangGraph workflow and Java `CodeGraph` type. |
| Cross-language contract tests | Done | `tests/test_cross_language_contracts.py` covers Java/Python target normalization, generated-test policy, verification runner availability, and result evidence shape. | Keep extending this file when new engine contracts are added. |
| Hermetic scripted pipeline E2E | Done | `tests/e2e/test_scripted_pipeline_e2e.py` covers default-suite Java and Python generation, generated-test materialization, failed verification, repair, and terminal task status with scripted agent/verifier doubles. | Keep this hermetic; live OpenCode stays out of default correctness tests. |
| Java verification-runner coverage | Done | `tests/test_java_verification.py` now covers target-scoped command evidence, timeout/command-error classification, missing evidence, and coverage/mutation handoff. | Parser-only XML behavior remains in `tests/test_maven_parsers.py`. |
| Real E2E mode for large/nightly tests | Done | `pyproject.toml` declares `real_e2e`; `tests/e2e/test_real_mode.py` gates on `UTA_E2E_MODE=real|nightly`, fresh-clones/copies real Java/Python repos, and writes `.uta_reports/real-e2e/real-e2e-results.json`. | Node2 scheduling/deployment remains postponed. |

## Decisions

| Topic | Decision | Reason |
| --- | --- | --- |
| Jira | Keep this as non-Jira work under `docs/`. | User confirmed non-Jira. |
| Hermetic scripted E2E languages | Cover both Java and Python. | The refactor is multi-language; a Python-only scripted E2E would not protect Java regression parity. |
| `uta/graph` boundary | Leave `uta/graph` as-is for this spec. | It is intentionally documented as current Java workflow coupling; this E2E enhancement should not block on a graph refactor. |
| Default runtime budget | The added default hermetic E2E work must fit within a 5-minute full-suite budget. | Keeps default developer verification practical. |
| Default suite inclusion | Include the new hermetic E2E in normal `python3 -m pytest`. | It should catch regressions before optional large/nightly checks. |
| Real E2E execution locations | Support local/manual invocation now; postpone node2 deployment and scheduling. | Maintainers need an ad hoc command first. Node2 automation can be added later after the command and report contract are stable. |
| Canonical real Java lane | Use a representative Java repo from the WMS workspace, defaulting to `$HOME/wms/sample-inbound-core` unless overridden. | Existing E2E defaults already assume this WMS repo family and it exercises Maven/PIT/test-enforcer behavior. |
| Canonical real Python lane | Use the UTA repo itself by default and write a clone-only probe target/test, `uta_e2e_probe.py::discounted_total` with `tests/test_uta_e2e_probe.py`; allow override with `UTA_E2E_PY3_REPO`, `UTA_E2E_PY3_TARGET`, and `UTA_E2E_PY3_TEST_PATHS`. | UTA is a real Python project, and the clone-only probe guarantees non-empty changed-line coverage/mutation evidence without mutating the source workspace. |
| Real E2E workspace strategy | Fresh clone canonical repos for each real E2E run. | Avoid stale workspace state and make real E2E results reproducible. |
| Python 2 real E2E scope | Python 2 is not part of the current `real_e2e` lane set. Keep Python 2 covered by existing staged tests and legacy-runtime diagnostics. | Current canonical real lanes are Java from WMS and Python 3 from UTA itself. |

## Assumptions

1. The target repository is `unit-test-agent`.
2. The goal is to improve UTA's own regression net, not to change generated tests in downstream Java/Python repositories.
3. The default developer test path must remain hermetic: no live OpenCode model, no network, no real corporate repo checkout, and no long Maven/mutmut run.
4. Real-repo verification remains valuable as an opt-in staged check, but live-agent smoke is not a UTA correctness signal and is not part of this enhancement.
5. Architecture-invariant tests should protect the language-agnostic core while respecting any explicitly documented transitional coupling.

## Objective

Enhance UTA's E2E and architecture-regression tests so common regressions in the multi-language generation workflow are caught before deployment.

Target users are UTA maintainers who refactor workflow, language adapters, OpenCode integration, API trigger repair, and enforcement logic. Success means the default local test suite catches regressions in:

- language-agnostic layering,
- cross-language contract conformance,
- prompt/output extraction and generated-test materialization,
- task/progress/cost/report behavior across batch and API trigger paths,
- repair rerun behavior after generation or verification failure.

## Requirements

1. Add a default-run hermetic pipeline E2E that exercises parse -> context -> generate -> materialize test file -> verify -> repair/rerun without a live LLM.
2. Use a scripted agent backend that behaves like the real agent contract, not a one-line `{"type": "completed", "result": "ok"}` stub.
3. Preserve existing staged real-repo env lanes such as `UTA_E2E_PY3_REPO` and `UTA_E2E_REPO`; Python 2 remains covered by staged legacy-runtime checks, not by the current `real_e2e` lane set. Live OpenCode checks are ad hoc provider diagnostics and are not part of the E2E enhancement success criteria.
4. Extend architecture-invariant tests so the shared engine, tasks, API trigger workflow, and any language-agnostic graph/workflow code do not import Java/Python implementation modules directly. Status: partially done in `a020fe5a` for `uta/engine` and `uta/tasks`; `uta/graph` is intentionally excluded until its Java workflow coupling is removed or reclassified.
5. Add cross-language contract tests that run Java and Python adapters through the same normalized scenario and compare behavior at the engine-contract level.
6. Improve Java verification-runner coverage so Java runner orchestration is closer to Python runner coverage.
7. Rename, mark, or document current subsystem integration tests so `tests/e2e/` does not imply full end-to-end coverage where it only validates scheduler/budget/measurability subsystems.
8. Default tests must stay deterministic and reasonably fast.
9. Add a real E2E mode that can be selected by large-test/nightly runners. This mode runs against real local or CI-provisioned Java/Python repositories and real Maven/pytest/mutmut/PIT tooling, without requiring a live agent backend.
10. Real E2E mode must produce a machine-readable report with pass/fail/skip counts, skip reasons, repo identity, branch/base ref, language, target count, tool versions, elapsed time, and artifact paths.

## Commands

Current commands to preserve:

```bash
python3 -m pytest
python3 -m pytest tests/e2e/test_phase9_staged_verification.py
UTA_E2E_PY3_REPO=/path/to/repo UTA_E2E_PY3_TARGET=jobs/forecast.py::forecast_for_store python3 -m pytest tests/e2e/test_phase5_python_verification.py
```

New expected focused commands:

```bash
python3 -m pytest tests/test_engine_layering.py tests/test_cross_language_contracts.py
python3 -m pytest tests/e2e/test_scripted_pipeline_e2e.py
python3 -m pytest tests/test_java_verification.py tests/test_python_verification.py
```

New real E2E mode commands:

```bash
# Large local real-repo verification, no live LLM required.
UTA_E2E_MODE=real python3 -m pytest -m real_e2e tests/e2e/

# Manual nightly-style real mode.
UTA_E2E_MODE=nightly python3 -m pytest -m real_e2e tests/e2e/

# Single-lane examples.
UTA_E2E_MODE=real UTA_E2E_REPO=$HOME/wms/sample-inbound-core python3 -m pytest -m real_e2e tests/test_pipeline_e2e.py
UTA_E2E_MODE=real UTA_E2E_PY3_REPO=$HOME/md/store_sku_prediction_model UTA_E2E_PY3_TARGET=jobs/forecast.py UTA_E2E_PY3_TEST_PATHS=tests/test_forecast.py python3 -m pytest -m real_e2e tests/e2e/
```

Full acceptance command:

```bash
python3 -m pytest
```

The full acceptance command must include the new hermetic E2E and invariant tests without requiring live credentials or local corporate repositories, and the added hermetic E2E coverage must keep the full suite within the 5-minute target budget.

## Project Structure

- `tests/test_engine_layering.py`: architecture-invariant tests for language-agnostic layers.
- `tests/test_cross_language_contracts.py`: new shared-contract tests for Java/Python adapter parity.
- `tests/e2e/test_scripted_pipeline_e2e.py`: new default-run hermetic pipeline E2E.
- `tests/e2e_staged_harness.py`: existing staged Python/Java harness; extend only when behavior belongs in staged verification.
- `tests/e2e/test_real_mode.py`: new real E2E mode entrypoint or marker-focused tests for nightly/large-test runners.
- `tests/test_pipeline_e2e.py`: existing real Java pipeline tests; keep opt-in and document as live/real-repo integration.
- `tests/e2e/test_phase9_staged_verification.py`: existing staged verification for Python lanes and API trigger repair.
- `tests/fixtures/`: small Java/Python fixture repos and scripted-agent fixtures.
- `uta/engine/*`: language-agnostic contracts under test.
- `uta/language/java/*` and `uta/language/python/*`: language-specific implementations tested through engine contracts.
- `uta/opencode/*`: real OpenCode process/client adapters; scripted test agent must not require these to contact a live model.

## Code Style

Prefer executable, contract-level tests with explicit evidence fields:

```python
def test_scripted_pipeline_generates_and_repairs_python_fixture(tmp_path):
    repo = copy_fixture_repo(tmp_path, "py3_flat_project")
    agent = ScriptedAgentBackend(
        turns=[
            AgentTurn.completed_with_file(
                path="tests/uta_generated/test_jobs_forecast.py",
                content="from jobs.forecast import forecast_for_store\n\n"
                "def test_forecast_for_store_empty():\n"
                "    assert forecast_for_store([]) == 0\n",
            ),
            AgentTurn.completed_with_file(
                path="tests/uta_generated/test_jobs_forecast.py",
                content="from jobs.forecast import forecast_for_store\n\n"
                "def test_forecast_for_store_empty():\n"
                "    assert forecast_for_store([]) == 0\n\n"
                "def test_forecast_for_store_recent_values():\n"
                "    assert forecast_for_store([2, 4, 8], uplift=1.0) == 4\n",
            ),
        ]
    )

    result = run_fixture_pipeline(repo, language="python", agent_backend=agent)

    assert result.task_status == "COMPLETED"
    assert result.generated_tests == ["tests/uta_generated/test_jobs_forecast.py"]
    assert result.verification_attempts == ["failed", "passed"]
```

Keep assertions on stable contracts: task status, target status, generated file path, evidence JSON, event sequence, and retry/repair decisions. Avoid asserting on incidental prompt wording unless the test is explicitly a prompt-contract test.

## Testing Strategy

### Level 1: Architecture And Contract Guards

- Keep `tests/test_engine_layering.py` as the machine-enforced guard for the current agnostic-layer boundary.
- Current implemented contract:
  - `uta/engine`: no module-load-time `uta.language.*` imports. Function-local lazy dispatch factories such as `default_*_registry()` / `make_*_provider()` and `TYPE_CHECKING` imports are allowed.
  - `uta/tasks`: no `uta.language.*` imports anywhere.
  - `uta/graph`: out of scope today because it is still the Java LangGraph workflow and carries Java `CodeGraph` types.
- Extend the guard only when a package is actually declared language-agnostic; do not scan language-specific packages or transitional Java workflow packages without first moving or reclassifying the code.
- Add a cross-language contract test that exercises:
  - language detection,
  - target normalization,
  - generated-test policy,
  - parse/context provider shape,
  - verification result shape,
  - mutation repair context shape when supported.

### Level 2: Hermetic Scripted Pipeline E2E

- Add one Python fixture run and one Java fixture run.
- Use a scripted agent backend that can emit realistic events:
  - session created,
  - prompt sent,
  - file edit produced,
  - completed turn,
  - optional failed verification and second repair turn.
- Write a real test file into the fixture repo.
- Run the real materialization, workspace guard, task DB, progress event, and verification-result plumbing.
- Use fast deterministic verification:
  - Python can run real `pytest` over a tiny fixture.
  - Java can start with verifier stubs where Maven is too heavy, but must still validate generated path, task state, and Java runner command contract.

### Level 3: Staged Real-Repo Verification

- Keep existing staged real-repo tests opt-in.
- Make skip reasons explicit and reportable.
- Keep Python 2 runtime absence as a diagnostic skip in staged legacy tests, not a silent green.
- Add a single command that writes a staged report summarizing pass/fail/skip counts and reasons.

### Level 4: Real E2E Mode For Large Tests And Nightly

- Add a pytest marker named `real_e2e`.
- Real E2E tests are excluded from default `python3 -m pytest`.
- Real E2E tests run only when `UTA_E2E_MODE` is `real` or `nightly`.
- Real E2E mode must support these lanes:
  - Java regression lane: real repo, real Maven compile/test-enforcer, real PIT output parsing, no live LLM required by default.
  - Python 3 feature lane: real repo, real pytest/coverage/mutmut, line-diff enforcement, no live LLM required by default.
- Python 2 compatibility remains in the staged verification harness for this implementation round; it is intentionally not a current `real_e2e` lane.
- Nightly mode must write one JSON report, for example `.uta_reports/real-e2e/real-e2e-results.json`, with:
  - mode: `real` or `nightly`,
  - generated timestamp,
  - environment summary with secrets redacted,
  - lane records,
  - pass/fail/skip counts,
  - skip reasons,
  - tool versions,
  - elapsed seconds,
  - report/artifact paths.
- A skipped real lane is acceptable only for missing declared prerequisites. A configured lane that runs and fails is a test failure.
- Real E2E mode must not mutate protected branches. It should copy or checkout into temporary workspaces and clean or archive them according to `UTA_E2E_KEEP_ARTIFACTS`.
- Fresh-clone canonical real repos for each real E2E run into a temporary workspace, then clean or archive that workspace according to `UTA_E2E_KEEP_ARTIFACTS`.
- Nightly-style manual runs should publish or retain the JSON report and key logs as artifacts.
- Node2 deployment, scheduling, and daemon/cron integration are postponed. The implementation should keep the real E2E command manually invocable so node2 can run it later without redesign.

## Boundaries

Always:

- Keep default tests hermetic and deterministic.
- Preserve real-repo lanes as opt-in staged verification.
- Keep real E2E mode selectable through explicit marker/env only; default `pytest` must not pick it up.
- Test shared engine contracts rather than duplicating Java/Python-specific expectations.
- Record skip reasons with enough detail to distinguish missing dependency, missing repo, missing runtime, provider failure, and true regression.
- Keep tests small enough that developers run them before commits; the full default suite target is 5 minutes.

Ask first:

- Adding new runtime dependencies.
- Moving large workflow code out of `uta/graph`.
- Requiring real Maven, real mutmut, or live model access in default tests.
- Changing the real E2E marker/env contract after a nightly runner depends on it.
- Changing public CLI flags or API trigger endpoints.
- Adding DB schema migrations only to support tests.

Never:

- Make default `pytest` depend on corporate network, live LLM credentials, local `~/md` or `~/wms` repos, or node2.
- Hide a failing default-run E2E behind an environment-variable skip.
- Treat a loose one-line fake LLM result as full pipeline coverage.
- Add language-specific imports to language-agnostic engine/task/report layers.
- Remove existing staged real-repo checks while adding hermetic tests.

## Scope Discovery

| Candidate | Evidence Found | Decision | Reason |
| --- | --- | --- | --- |
| `tests/test_engine_layering.py` | Implemented by commit `a020fe5a`. Scans `uta/engine` for module-load-time language imports and `uta/tasks` for all language imports. Explicitly excludes `uta/graph` because graph currently carries Java workflow coupling. | In scope, current subtask done | This is now the first completed guardrail. Future work is to broaden only after graph/workflow ownership is clarified. |
| `tests/e2e/test_phase9_staged_verification.py` | Stages 1-5 cover scan/context, enforcement, batch plumbing, generation skip contract, and API trigger repair. Its live OpenCode branch is a provider diagnostic, not a UTA E2E success criterion. | In scope | Good foundation for staged reporting; needs a default hermetic real-loop lane. |
| `tests/e2e_staged_harness.py` | Has fake Python OpenCode client returning a fenced code block and real OpenCode smoke client. | In scope | Replace or supplement the simple fake with a scripted agent backend that emits realistic file edits and repair turns. The real OpenCode smoke path is provider diagnostic only. |
| `tests/test_pipeline_e2e.py` | Real Java Maven repo tests are skipped when repo is unavailable; Maven compile is stubbed by default. | In scope | Keep as opt-in real-repo verification; do not count as default pipeline coverage. |
| `pyproject.toml` pytest markers | Existing markers include `integration`, `e2e`, and `e2e_git_home`; no dedicated `real_e2e` marker exists. | In scope | Add a large/nightly marker so real E2E can be selected without overloading default E2E. |
| `tests/test_language_registry.py` | Registers a fake third language and validates target normalization. | In scope | Extend from registry-only checks into cross-language behavioral contract checks. |
| `tests/test_verify_registry.py`, `tests/test_batch_abstraction.py`, `tests/test_scoring_abstraction.py` | Confirm registries expose Java/Python and base result/request types are shared. | In scope | Use these as contract-test anchors and add deeper behavior checks. |
| `tests/test_python_verification.py` | Broad Python verification coverage. | In scope | Use as reference for strengthening Java runner orchestration tests. |
| `tests/test_java_verification.py` | Thin Java runner coverage compared with Python. | In scope | Add tests for command construction, evidence parsing handoff, timeout/error classification, and target-scoped verification behavior. |
| `uta/engine/*` | Hosts language-agnostic contracts for batch, context, parse, scoring, validation, verification, mutation repair, workspace guard, and LLM session behavior. | In scope | Primary architecture boundary to guard. |
| `uta/api_trigger/*` | CI trigger/report/repair path shares task, report, and rerun behavior with batch results. | In scope | Hermetic E2E must verify API trigger repair path or a representative subset. |
| `uta/opencode/client.py`, `uta/opencode/process.py`, `uta/opencode/stream.py` | Real OpenCode adapters and JSON stream handling. | In scope for adapter tests; out of scope for default live calls | Scripted backend should mimic the normalized contract, while live process calls remain opt-in. |
| WMS Java real repo | Existing E2E defaults and docs reference the WMS repo family. | In scope for real E2E mode | Canonical Java lane defaults to `$HOME/wms/sample-inbound-core`, overrideable by `UTA_E2E_REPO`. |
| UTA Python real repo | UTA itself is a Python project and can host a clone-only probe during real E2E. | In scope for real E2E mode | Canonical Python lane defaults to the current UTA repo and writes `uta_e2e_probe.py::discounted_total` plus `tests/test_uta_e2e_probe.py` into the fresh clone, overrideable by `UTA_E2E_PY3_REPO`, `UTA_E2E_PY3_TARGET`, and `UTA_E2E_PY3_TEST_PATHS`. |
| Downstream real repos under local home directories | Existing E2E env vars point to real Java/Python repos. | Out of scope for default tests | Keep as staged/real E2E verification only; local repo availability is not stable enough for default CI. |
| Node2 real E2E runner | User postponed node2 deployment/scheduling. | Out of scope for this implementation round | Keep the command manually invocable so node2 can run it later, but do not deploy/schedule it now. |
| Nightly/large-test runner environment | Not currently defined in repo tests. | In scope for command/report contract only | Add local/manual mode now; scheduler ownership is postponed. |
| Production endpoints/APIs | No endpoint-affecting product API change. | Out of scope | This spec changes UTA tests, not external business APIs. |

## Success Criteria

1. `python3 -m pytest` runs Java and Python hermetic full-pipeline E2E coverage that creates a test file and reaches a terminal task status through normal workflow plumbing.
2. The hermetic pipeline E2E fails if generated-test extraction/materialization stops working.
3. The hermetic pipeline E2E fails if verification failure no longer triggers the intended repair/rerun path.
4. A layering test fails if the currently guarded language-agnostic packages import `uta.language.java` or `uta.language.python` outside explicitly allowed lazy factories. Status: done for `uta/engine` and `uta/tasks` in `a020fe5a`.
5. Cross-language contract tests fail if Java and Python adapters diverge on normalized target, generated-test policy, verification evidence shape, or repair context contract.
6. Java verification runner coverage includes runner orchestration, not only XML parser helpers.
7. Existing real-repo tests keep their opt-in behavior with clear skip reasons; live OpenCode checks are outside the UTA E2E success criteria.
8. Real E2E mode can be selected with `UTA_E2E_MODE=real python3 -m pytest -m real_e2e ...`.
9. Manual nightly-style mode can be selected with `UTA_E2E_MODE=nightly` and writes a JSON report suitable for later CI/node2 artifact publishing.
10. README or usage documentation explains the difference between default hermetic E2E, staged real-repo E2E, and real nightly E2E.

## Open Questions

Resolved by user:

1. Non-Jira.
2. Hermetic full-pipeline E2E covers both Java and Python.
3. Leave `uta/graph` as-is for now.
4. Default full-suite runtime target is 5 minutes.
5. New hermetic E2E is included in the normal default suite.
6. Real E2E should run locally/manual by command, publishing `.uta_reports/real-e2e/real-e2e-results.json`; node2 deployment is postponed.
7. Canonical real lanes use Java from the WMS workspace and Python from the UTA repo itself.
8. Real E2E should fresh-clone canonical repos for each run.
9. Node2 scheduler/deployment is postponed; keep the command manually invocable.

Still open: none for the current spec scope.

## Review Checklist

- Change scope: covers architecture guards, cross-language contracts, default hermetic pipeline E2E, staged real-repo lanes, real E2E mode, and docs.
- Abstraction/extensibility: protects shared engine contracts and keeps language behavior behind adapters.
- Verification depth: defines unit, contract, hermetic E2E, staged real-repo, and real nightly E2E layers.
- API/schema/data model compatibility: no public API or DB schema change expected; any later schema proposal requires a design update.
- Risks/mitigations: avoids making default tests depend on network, live models, or local real repos; records clear skip reasons for opt-in lanes.
- Simplicity: prefers one scripted agent backend and one shared contract test style instead of parallel Java/Python-only fake workflows.

## Changelog

| Date | Change | Reason |
| --- | --- | --- |
| 2026-06-11 | Added real E2E mode requirements for large/nightly runners. | User requested a selectable real E2E mode beyond default hermetic tests. |
| 2026-06-11 | Refreshed implementation status after commit `a020fe5a`. | The agnostic-layer import boundary guard is now implemented for `uta/engine` and `uta/tasks`; remaining E2E work stays pending. |
| 2026-06-11 | Resolved open questions for non-Jira scope, both-language hermetic E2E, 5-minute budget, default inclusion, node2/local real E2E, and canonical WMS/Python repo lanes. | User supplied decisions for the open spec questions. |
| 2026-06-11 | Closed remaining real E2E operational questions: fresh clone per run; postpone node2 deployment/scheduling while keeping local/manual invocation. | User supplied decisions for the final open questions. |
| 2026-06-11 | Added `docs/design-e2e-test-enhancing.md`. | Required design doc generated from the finalized spec decisions. |
| 2026-06-11 | Removed live-agent smoke from the E2E enhancement scope. | User clarified that live agent smoke tests are not useful for UTA correctness. |
| 2026-06-11 | Added `docs/plan-e2e-test-enhancing.md`. | Broke the design into ordered, verifiable implementation tasks. |
| 2026-06-11 | Refreshed implementation status after Tasks 1-10. | Cross-language contracts, hermetic scripted E2E, Java runner tests, real E2E marker/report, and README usage docs are implemented. |
| 2026-06-11 | Marked implementation verification complete. | Focused tests, full default suite, and manual `real_e2e` report command passed locally. |
| 2026-06-11 | Closed Phase 6 spec-gap verification. | Real Java lane now records JaCoCo/PIT evidence, Python real lane uses strict test discovery with `no_strict_python_test` skip when appropriate, Python 2 real-lane scope is documented as out of current `real_e2e`, and expanded cross-language contracts plus full suite passed. |
| 2026-06-11 | Switched canonical Python real E2E lane to UTA self-check. | UTA itself hosts a clone-only Python probe so the default Python real lane verifies actual coverage/mutmut behavior without relying on an external MD sample repo. |
| 2026-06-11 | Verified Python real E2E with non-empty changed-line evidence. | `UTA_E2E_MODE=real python3 -m pytest -m real_e2e tests/e2e/test_real_mode.py` passed with Python coverage 100.00% (6/6) and mutation 100.00% (13/13); dev dependencies use mutmut 2.x plus `whatthepatch` for patch-based local mutation stability. |

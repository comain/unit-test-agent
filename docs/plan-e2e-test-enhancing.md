# Implementation Plan: E2E Test Enhancing

Spec: `docs/spec-e2e-test-enhancing.md`  
Design: `docs/design-e2e-test-enhancing.md`  
Jira: N/A. User confirmed non-Jira repository-quality work.  
Release approval: N/A.

## Overview

Enhance UTA's regression net with a default hermetic Java/Python generation-loop E2E, cross-language contract tests, stronger Java verification-runner tests, and an opt-in `real_e2e` mode that fresh-clones real Java/Python repos and emits `.uta_reports/real-e2e/real-e2e-results.json`. Live-agent smoke is intentionally out of scope because it mostly validates provider availability, not UTA correctness.

## Architecture Decisions

- Keep `a020fe5a` as the current architecture guard baseline: `uta/engine` is protected from module-load language imports and `uta/tasks` is protected from all language imports.
- Leave `uta/graph` as-is for this work; it remains documented Java workflow coupling.
- Include hermetic Java and Python E2E in default `python3 -m pytest`, with the full default suite target remaining 5 minutes.
- Put real-repo verification behind `real_e2e` plus `UTA_E2E_MODE=real|nightly`.
- Fresh-clone canonical real repos for real E2E runs; never mutate source workspaces.
- Postpone node2 deployment/scheduling; keep the real E2E command manually invocable.

## Dependency Graph

```text
pytest marker and docs contract
    │
    ├── cross-language contract tests
    │       │
    │       └── hermetic Java/Python scripted E2E
    │
    ├── scripted agent test helper
    │       │
    │       └── hermetic Java/Python scripted E2E
    │
    ├── Java verification-runner coverage
    │       │
    │       └── hermetic Java E2E confidence
    │
    └── real E2E harness
            │
            ├── fresh-clone workspace helper
            ├── real lane runner
            └── real E2E JSON report
```

## Task List

### Phase 0: Completed Baseline

#### Task 0: Preserve Existing Agnostic-Layer Guard

**Description:** Treat commit `a020fe5a` as the completed baseline guard. Keep `tests/test_engine_layering.py` active and avoid broadening it to `uta/graph` in this plan.

**Acceptance criteria:**
- [x] `tests/test_engine_layering.py` exists.
- [x] `uta/engine` module-load language imports are rejected.
- [x] `uta/tasks` language imports are rejected.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/test_engine_layering.py`

**Dependencies:** None.

**Files likely touched:** None.

**Estimated scope:** Done.

### Phase 1: Test Contracts And Scaffolding

#### Task 1: Add Real E2E Marker And Mode Contract

**Description:** Add the `real_e2e` pytest marker and a small guard test or fixture behavior that proves real E2E tests skip unless `UTA_E2E_MODE` is `real` or `nightly`.

**Acceptance criteria:**
- [x] `pyproject.toml` documents the `real_e2e` marker.
- [x] Real E2E mode helper accepts only `real` and `nightly`.
- [x] Default `python3 -m pytest` does not run real clone/tool lanes.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/e2e/test_real_mode.py -k mode`
- [x] Marker listed: `python3 -m pytest --markers | grep real_e2e`

**Dependencies:** None.

**Files likely touched:**
- `pyproject.toml`
- `tests/e2e/test_real_mode.py`

**Estimated scope:** S.

#### Task 2: Add Cross-Language Contract Tests

**Description:** Add shared contract tests that run Java and Python adapters through the same engine-facing expectations without requiring identical language behavior.

**Acceptance criteria:**
- [x] Java and Python adapters both normalize explicit targets into `TargetRef`.
- [x] Java and Python generated-test policies expose allowed roots.
- [x] Java and Python verification runners expose compatible result/evidence shapes.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/test_cross_language_contracts.py`

**Dependencies:** Task 0.

**Files likely touched:**
- `tests/test_cross_language_contracts.py`

**Estimated scope:** S.

#### Task 3: Add Scripted Agent Test Helper

**Description:** Add a deterministic test-only scripted agent backend that mimics the production OpenCode client method shape and can emit generation, failed verification, repair, token/cost, and retrospective responses.

**Acceptance criteria:**
- [x] Helper supports `create_session`, `send_message`, `send_message_split`, `poll_completion`, `analyze_session_tokens`, and `analyze_session_retrospect`.
- [x] Helper can return a generated file response and a repair response in deterministic order.
- [x] Helper is test-only and not imported by production code.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/e2e/test_scripted_pipeline_e2e.py -k scripted_agent`

**Dependencies:** None.

**Files likely touched:**
- `tests/e2e/test_scripted_pipeline_e2e.py`
- optionally `tests/scripted_agent.py`

**Estimated scope:** S.

### Checkpoint: Contracts And Scaffolding

- [x] `python3 -m pytest tests/test_engine_layering.py tests/test_cross_language_contracts.py`
- [x] `python3 -m pytest tests/e2e/test_real_mode.py -k mode`
- [x] No production code imports the scripted test helper.

### Phase 2: Default Hermetic Pipeline E2E

#### Task 4: Add Python Hermetic Scripted Pipeline E2E

**Description:** Add a Python fixture-based E2E that runs the normal Python batch generation path using the scripted agent, materializes a generated test, simulates failed verification, then repairs and passes.

**Acceptance criteria:**
- [x] Test copies `tests/fixtures/python_projects/py3_flat_project` into `tmp_path`.
- [x] Test creates a Python target task and produces a generated test file.
- [x] Test records failed then passed verification attempts and terminal target/task success.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/e2e/test_scripted_pipeline_e2e.py -k python`

**Dependencies:** Tasks 2, 3.

**Files likely touched:**
- `tests/e2e/test_scripted_pipeline_e2e.py`
- `tests/fixtures/python_projects/py3_flat_project/*` only if fixture adjustments are necessary

**Estimated scope:** M.

#### Task 5: Add Java Hermetic Scripted Pipeline E2E

**Description:** Add a Java fixture-based E2E that covers Java target task creation, generated-test materialization, deterministic verification failure, repair, and terminal success without requiring real Maven/PIT in the default suite.

**Acceptance criteria:**
- [x] Test uses a tiny Java fixture repo or creates one under `tmp_path`.
- [x] Test materializes a generated Java test file through the Java workflow surface or command-contract path.
- [x] Test verifies failed then passed deterministic verification and terminal target/task success.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/e2e/test_scripted_pipeline_e2e.py -k java`

**Dependencies:** Tasks 2, 3.

**Files likely touched:**
- `tests/e2e/test_scripted_pipeline_e2e.py`
- `tests/fixtures/*.java` only if a fixture is added or adjusted

**Estimated scope:** M.

#### Task 6: Keep Default Suite Runtime And Naming Honest

**Description:** Ensure the new hermetic tests are included in the default suite, remain fast, and existing subsystem E2E naming is documented so `tests/e2e/` does not overstate coverage.

**Acceptance criteria:**
- [x] Hermetic scripted E2E is not marked `real_e2e` and runs by default.
- [x] Default suite target remains 5 minutes or the plan records the measured gap. Measured final run: 1126 passed, 10 skipped in 91.14s.
- [x] README or usage docs distinguish subsystem E2E, hermetic pipeline E2E, and real E2E.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/e2e/test_scripted_pipeline_e2e.py`
- [x] Full suite measured: `time python3 -m pytest`

**Dependencies:** Tasks 4, 5.

**Files likely touched:**
- `README.md`
- `docs/design-e2e-test-enhancing.md` if measured reality changes the design

**Estimated scope:** S.

### Checkpoint: Default Hermetic E2E

- [x] `python3 -m pytest tests/e2e/test_scripted_pipeline_e2e.py`
- [x] `python3 -m pytest tests/test_engine_layering.py tests/test_cross_language_contracts.py`
- [x] `time python3 -m pytest` remains within the 5-minute target or variance is documented.

### Phase 3: Java Verification Runner Coverage

#### Task 7: Add Java Verification Runner Orchestration Tests

**Description:** Strengthen `tests/test_java_verification.py` so it validates runner orchestration, not only Maven XML parser helpers.

**Acceptance criteria:**
- [x] Command construction for target-scoped Maven/test-enforcer execution is tested.
- [x] Timeout, command error, and missing evidence classifications are tested.
- [x] Parsed coverage/mutation evidence handoff is tested.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/test_java_verification.py tests/test_maven_parsers.py`

**Dependencies:** Task 2.

**Files likely touched:**
- `tests/test_java_verification.py`
- Java enforcement/runner files only if tests reveal a real bug

**Estimated scope:** M.

### Checkpoint: Runner Coverage

- [x] `python3 -m pytest tests/test_java_verification.py tests/test_python_verification.py`
- [x] Any Java runner behavior change is covered by a failing-first test.

### Phase 4: Real E2E Mode

#### Task 8: Add Fresh-Clone Workspace Helper And Report Contract

**Description:** Add a real E2E harness helper that resolves Java/Python source repos, fresh-clones into a run workspace, records tool versions, and writes the real E2E JSON report.

**Acceptance criteria:**
- [x] Source path or Git URL can be cloned/copied into a run-specific workspace.
- [x] Source repos are never mutated directly.
- [x] `.uta_reports/real-e2e/real-e2e-results.json` is written with schema version, summary, environment, lanes, tool versions, and artifacts.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/e2e/test_real_mode.py -k 'clone or report'`

**Dependencies:** Task 1.

**Files likely touched:**
- `tests/e2e/test_real_mode.py`
- optionally `tests/e2e_real_harness.py`

**Estimated scope:** M.

#### Task 9: Wire Java And Python Real E2E Lanes

**Description:** Use the real E2E harness to run canonical Java and Python lanes against fresh clones, using existing staged checks where possible.

**Acceptance criteria:**
- [x] Java lane defaults to `$HOME/wms/sample-inbound-core` and can be overridden by `UTA_E2E_REPO`.
- [x] Python lane defaults to the UTA repo itself with a clone-only `uta_e2e_probe.py::discounted_total` target and `tests/test_uta_e2e_probe.py`, and can be overridden by `UTA_E2E_PY3_REPO`, `UTA_E2E_PY3_TARGET`, and `UTA_E2E_PY3_TEST_PATHS`.
- [x] Missing repo/runtime/tool prerequisites produce structured skips; configured lane failures fail the test.

**Verification:**
- [x] Local command works when repos/tools exist: `UTA_E2E_MODE=real python3 -m pytest -m real_e2e tests/e2e/`
- [x] Missing prerequisite behavior is covered: `python3 -m pytest tests/e2e/test_real_mode.py -k skip`

**Dependencies:** Task 8.

**Files likely touched:**
- `tests/e2e/test_real_mode.py`
- `tests/e2e_staged_harness.py` only if a shared helper is needed

**Estimated scope:** M.

#### Task 10: Document Real E2E Manual Usage

**Description:** Document default hermetic E2E, `real_e2e` manual mode, fresh-clone behavior, report path, and skip/failure interpretation. Keep node2 scheduling explicitly postponed.

**Acceptance criteria:**
- [x] README or usage doc includes default and real E2E commands.
- [x] Docs state live-agent smoke is not part of UTA correctness.
- [x] Docs state node2 deployment/scheduling is postponed.

**Verification:**
- [x] Documentation links resolve locally.
- [x] Commands in docs match the implemented marker/env names.

**Dependencies:** Tasks 8, 9.

**Files likely touched:**
- `README.md`
- `docs/design-e2e-test-enhancing.md`
- `docs/spec-e2e-test-enhancing.md` only if scope changes

**Estimated scope:** S.

### Checkpoint: Real E2E Mode

- [x] `python3 -m pytest tests/e2e/test_real_mode.py`
- [x] `UTA_E2E_MODE=real python3 -m pytest -m real_e2e tests/e2e/` runs locally when canonical repos/tools are available.
- [x] `.uta_reports/real-e2e/real-e2e-results.json` is produced and contains structured lane results.

### Phase 5: Final Verification And Review

#### Task 11: Full Local Verification And Regression Review

**Description:** Run focused and full verification, review the resulting diff against the design, and document any skipped real E2E lanes with reasons.

**Acceptance criteria:**
- [x] Focused tests pass.
- [x] Full `python3 -m pytest` passes and is within the 5-minute target or the measured gap is documented.
- [x] Real E2E report is produced from a manual run or skip reasons are documented.

**Verification:**
- [x] `python3 -m pytest tests/test_engine_layering.py tests/test_cross_language_contracts.py`
- [x] `python3 -m pytest tests/e2e/test_scripted_pipeline_e2e.py`
- [x] `python3 -m pytest tests/test_java_verification.py tests/test_python_verification.py`
- [x] `python3 -m pytest`
- [x] `UTA_E2E_MODE=real python3 -m pytest -m real_e2e tests/e2e/`

**Dependencies:** Tasks 1-10.

**Files likely touched:**
- Test and docs files only if verification exposes gaps

**Estimated scope:** S.

### Checkpoint: Complete

- [x] All default-suite acceptance criteria met.
- [x] Real E2E mode is manually invocable and produces a JSON report.
- [x] No node2 scheduling/deployment work is included.
- [x] Docs match implemented commands and behavior.
- [x] Ready for review.

### Phase 6: Spec Gap Closure From Review

Review after Tasks 1-11 found that the original implementation plan was complete, but the broader spec still has uncovered requirements. This phase closes those gaps before the E2E enhancement can be considered fully complete against `docs/spec-e2e-test-enhancing.md`.

#### Task 12: Complete Real E2E Report Contract

**Description:** Extend the real E2E JSON report so it contains every field required by the spec, not only pass/fail/skip and tool versions.

**Acceptance criteria:**
- [x] Top-level report includes `schemaVersion`, `generatedAt`, `summary`, `environment`, `toolVersions`, and `lanes`.
- [x] Each lane includes repo identity, branch, base ref, target count, elapsed seconds, status, skip/failure reason, workspace path, and artifact paths.
- [x] Structured skip records preserve the same schema as pass/fail lane records.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/e2e/test_real_mode.py -k report`
- [x] Manual report inspection confirms `branch`, `baseRef`, `targetCount`, and `elapsedSeconds` are present: `UTA_E2E_MODE=real python3 -m pytest -m real_e2e tests/e2e/test_real_mode.py`

**Dependencies:** Task 8.

**Files likely touched:**
- `tests/e2e_real_harness.py`
- `tests/e2e/test_real_mode.py`
- `docs/spec-e2e-test-enhancing.md`

**Estimated scope:** S.

#### Task 13: Run Real Java Enforcement Tooling In The Real E2E Lane

**Description:** Upgrade the Java real lane from scan/context-only to a real tooling lane that exercises Maven/test-enforcer coverage and mutation evidence without requiring live LLM generation.

**Acceptance criteria:**
- [x] Java real lane still fresh-clones/copies `$HOME/wms/sample-inbound-core` or `UTA_E2E_REPO`.
- [x] Java real lane runs a bounded real Maven/test-enforcer command or existing Java enforcement runner against a selected target/diff.
- [x] JaCoCo/PIT or UTA test-enforcer evidence is captured in the lane report; missing Maven/JDK/test-enforcer prerequisites are structured skips, while configured command failures fail the lane.

**Verification:**
- [x] Tests pass for command construction and skip/fail classification: `python3 -m pytest tests/e2e/test_real_mode.py -k java`
- [x] Manual real lane command runs on local WMS repo: `UTA_E2E_MODE=real UTA_E2E_REPO=$HOME/wms/sample-inbound-core python3 -m pytest -m real_e2e tests/e2e/test_real_mode.py`
- [x] Report contains Java enforcement evidence artifact paths.

**Dependencies:** Task 12.

**Files likely touched:**
- `tests/e2e/test_real_mode.py`
- `tests/e2e_staged_harness.py`
- Java enforcement/test-enforcer helper tests only if a bug is found

**Estimated scope:** M.

#### Task 14: Run Real Python Coverage And Mutation Tooling In The Real E2E Lane

**Description:** Upgrade the Python real lane from scan-only plus optional `UTA_E2E_PY3_TEST_PATHS` enforcement into a real pytest/coverage/mutmut lane using strict target-test selection.

**Acceptance criteria:**
- [x] Python real lane fresh-clones/copies the UTA repo by default, or the repo configured by `UTA_E2E_PY3_REPO`.
- [x] If `UTA_E2E_PY3_TARGET` and `UTA_E2E_PY3_TEST_PATHS` are set, the configured target/test pair is verified and failures fail the lane.
- [x] If no explicit target/test is configured, the lane auto-selects a target with strict existing unit-test evidence; if none exists, it emits a structured `no_strict_python_test` skip rather than `missing_test_paths`.
- [x] The lane runs real pytest, coverage.py, and mutmut through the Python verifier, and writes coverage/mutation evidence into the real E2E report.

**Verification:**
- [x] Tests pass for strict discovery and skip/fail semantics: `python3 -m pytest tests/e2e/test_real_mode.py -k python`
- [x] Manual real lane command runs on the UTA repo itself: `UTA_E2E_MODE=real python3 -m pytest -m real_e2e tests/e2e/test_real_mode.py`
- [x] Report contains Python coverage and mutation evidence artifact paths or a precise `no_strict_python_test` skip reason.

**Dependencies:** Task 12.

**Files likely touched:**
- `tests/e2e/test_real_mode.py`
- `tests/e2e_real_harness.py`
- `uta/language/python/test_selection.py` only if the selector lacks the needed reusable API

**Estimated scope:** M.

#### Task 15: Resolve Python 2 Real E2E Scope

**Description:** Reconcile the older Python 2 real-lane wording with the current decision to select two canonical real lanes: Java from WMS and Python 3 from UTA itself.

**Acceptance criteria:**
- [x] Spec/design/README state that Python 2 remains covered by existing staged tests but is not part of current real E2E mode.
- [x] The final plan/spec no longer contradict themselves about whether Python 2 is required in real E2E.
- [x] Current real E2E scope is documented as Java WMS plus Python 3 UTA self-check.

**Verification:**
- [x] Documentation grep shows no current-scope Python 2 real-lane implementation requirement remains.

**Dependencies:** Task 12.

**Files likely touched:**
- `docs/spec-e2e-test-enhancing.md`
- `docs/design-e2e-test-enhancing.md`
- `README.md`

**Estimated scope:** S-M depending on scope decision.

#### Task 16: Expand Cross-Language Contract Coverage To Match Spec

**Description:** Extend contract tests beyond target/policy/result shape to include language detection, parse/context provider shape, and mutation repair context shape where supported.

**Acceptance criteria:**
- [x] Java and Python language detection are covered in the shared contract test using comparable repo markers or changed paths.
- [x] Java and Python parse/context providers expose normalized engine-facing fields without importing language packages from guarded core layers.
- [x] Java and Python mutation repair context adapters expose comparable survivor/group/artifact/reproduce-command fields where supported.

**Verification:**
- [x] Tests pass: `python3 -m pytest tests/test_cross_language_contracts.py tests/test_engine_layering.py`
- [x] No production code imports `tests/scripted_agent.py` or other test-only support helpers.

**Dependencies:** Task 2.

**Files likely touched:**
- `tests/test_cross_language_contracts.py`
- Engine/language contract files only if tests expose a real contract gap

**Estimated scope:** M.

#### Task 17: Final Spec-Gap Verification

**Description:** Rerun focused and full verification after Tasks 12-16 and update docs with final evidence.

**Acceptance criteria:**
- [x] Real E2E report satisfies the full spec schema.
- [x] Real Java and Python lanes either run real tooling or emit accepted prerequisite skips.
- [x] Cross-language contract coverage matches the spec text.
- [x] Full default suite remains within the 5-minute target.

**Verification:**
- [x] `python3 -m pytest tests/test_engine_layering.py tests/test_cross_language_contracts.py` - 9 passed.
- [x] `python3 -m pytest tests/e2e/test_real_mode.py` - 13 passed, 1 skipped.
- [x] `python3 -m pytest tests/e2e/test_scripted_pipeline_e2e.py` - 4 passed.
- [x] `python3 -m pytest tests/test_java_verification.py tests/test_python_verification.py` - 56 passed.
- [x] `python3 -m pytest tests/e2e/test_real_mode.py tests/test_python_verification.py` - 60 passed, 1 skipped.
- [x] `python3 -m pytest` - 1137 passed, 10 skipped in 85.43s.
- [x] `UTA_E2E_MODE=real python3 -m pytest -m real_e2e tests/e2e/test_real_mode.py` - 1 passed, 13 deselected in 47.69s. Report summary: Java WMS lane passed with JaCoCo/PIT evidence; Python UTA lane passed with changed-line coverage 100.00% (6/6) and changed-line mutation 100.00% (13/13) using the clone-only `uta_e2e_probe.py::discounted_total` target.

**Dependencies:** Tasks 12-16.

**Files likely touched:**
- `docs/plan-e2e-test-enhancing.md`
- `docs/spec-e2e-test-enhancing.md`
- `docs/design-e2e-test-enhancing.md`

**Estimated scope:** S.

### Checkpoint: Spec Gap Closure Complete

- [x] Real E2E report includes branch/base ref, target count, elapsed seconds, and artifacts.
- [x] Real Java lane exercises real Maven/test-enforcer coverage/mutation evidence or emits a valid prerequisite skip.
- [x] Real Python lane exercises real pytest/coverage/mutmut evidence or emits a valid prerequisite skip.
- [x] Python 2 real-lane scope is resolved and documented.
- [x] Cross-language contract coverage matches the spec.
- [x] Full default suite and real-mode command pass.

## Parallelization Opportunities

- Tasks 2 and 7 can run in parallel after Task 1 because they touch different tests.
- Tasks 4 and 5 can run in parallel after Task 3 if the scripted helper contract is stable.
- Task 10 can start after Task 8 with placeholders, but final docs should wait for Task 9 command behavior.
- Task 11 must be sequential after all implementation tasks.
- Tasks 13 and 14 can run in parallel after Task 12 because Java and Python real-lane tooling are independent.
- Task 16 can run in parallel with Tasks 13-15 because it only changes contract tests unless it exposes a contract bug.
- Task 17 must run after Tasks 12-16.

## Risks And Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Hermetic E2E becomes slow or flaky | Default suite loses developer trust | Use tiny fixtures, deterministic verifier stubs, and measure full-suite time. |
| Scripted helper diverges from production client contract | E2E gives false confidence | Keep method names/result shapes aligned with production client interfaces. |
| Java/Python tests drift into bespoke paths | Cross-language regressions remain hidden | Assert engine-facing contracts in Task 2 and reuse helper patterns across both lanes. |
| Real E2E mutates source repos | Local data loss or dirty workspaces | Fresh clone/copy into run-specific workspace only. |
| Real E2E skips too broadly | Large-test reports become meaningless | Allow skips only for missing prerequisites; configured lane failures fail tests. |
| Node2 scope creep | Larger deployment task delays test improvements | Keep node2 scheduling/deployment explicitly out of scope. |
| Real Maven/mutmut lanes are slow | Manual real E2E becomes impractical | Keep real tooling opt-in, bound commands with timeouts, and record elapsed seconds per lane. |
| Canonical repos lack strict unit tests for an auto-selected Python target | Python real lane skips too often | Prefer target/test pair discovery; report `no_strict_python_test` distinctly from missing repo/runtime/tooling. |
| Spec and plan drift again | False confidence from checked boxes | Keep Phase 6 tasks tied directly to spec requirements and rerun a final spec-gap review. |

## Open Questions

None for current scope. User resolved:

- non-Jira,
- both Java and Python hermetic E2E,
- leave `uta/graph` for now,
- 5-minute default-suite target,
- include hermetic E2E in default suite,
- local/manual real E2E with node2 scheduling postponed,
- fresh-clone canonical WMS/UTA repos.

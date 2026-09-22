# Implementation Plan: Standalone UTA Python Enforcement Core

Jira: N/A - non-Jira UTA tool work
Spec: `docs/spec-python-enforcement-standalone-core.md`
Design: `docs/design-python-enforcement-standalone-core.md`
ADR: `docs/decisions/ADR-003-lightweight-python-enforcement-core.md`
Usage docs: UTA README/report guidance and dev-skills `references/test-enforce-usage.md`
Release approval: N/A for non-Jira local tooling; if this is later attached to a Jira, add release approval evidence before `/ship`.

## Overview

Refactor Python test enforcement so UTA CI, repair sessions, full `uta python-enforce`, and dev-skills local enforcement use one shared lightweight implementation. The lightweight local distribution must run without full UTA checkout, preserve Python 3 batch/operator-filter mutation semantics, preserve Python 2 legacy evidence, and keep CI mutation sampling private to the UTA CI adapter.

## Architecture Decisions

1. Shared enforcement logic lives in a lightweight UTA-owned distribution root under `tools/python-enforcement/`.
2. Language-neutral contracts must not become Python-owned. The plan will first harden the design to introduce a neutral shared package, then put Python behavior under a Python package.
3. `uta python-enforce`, API trigger Python enforcement, repair verification, and the lightweight CLI all call the same core.
4. dev-skills remains a launcher and evidence validator only.
5. CI sampling is adapter-owned and private to UTA CI. dev-skills/local/repair/full CLI always run full hard-capped mutation and reject sampled local evidence.

## Dependency Graph

```text
Spec/design decisions
  -> neutral core contract and config contract
    -> lightweight package skeleton and packaging
      -> evidence/schema helpers
      -> diff/target/test-selection helpers
      -> Python verification and mutation adapters
        -> UTA CLI/API/repair adapters
        -> lightweight CLI
          -> dev-skills launcher/docs
          -> report failure guidance
            -> contract, isolated, integration, and real-repo verification
```

## Task List

### Phase 0: Resolve Design-Review Findings

#### Task 1: Confirm Spec And Design Decisions Stay Hardened

**Description:** Resolve the remaining design-review findings before implementation starts. The docs must keep the neutral package boundary, resolved questions, config/evidence/performance contracts, CI adapter sampling path, and stale-doc inventory current as implementation details are finalized.

**Acceptance criteria:**
- [ ] Spec open questions remain marked resolved or explicitly deferred.
- [ ] Design uses a neutral shared package for engine contracts and Python-specific package for Python behavior.
- [ ] Design documents `PythonRuntimeConfig` / `PythonMutationConfig` fields, env aliases, defaults, and fingerprint inputs.
- [ ] Design includes golden evidence/schema requirement and command field names.
- [ ] Design states that UTA CI injects sampling through an in-process adapter, not a public CLI/env flag.
- [ ] Design includes target/candidate/batch timeout budget math and fail-closed limits.
- [ ] Design includes stale-doc checklist and production proof signals.

**Verification:**
- [ ] `git diff --check -- docs/spec-python-enforcement-standalone-core.md docs/design-python-enforcement-standalone-core.md docs/decisions/ADR-003-lightweight-python-enforcement-core.md`
- [ ] Re-run design-review manually or with reviewer agent and confirm no Critical/Important findings remain.

**Dependencies:** None

**Files likely touched:**
- `docs/spec-python-enforcement-standalone-core.md`
- `docs/design-python-enforcement-standalone-core.md`
- `docs/decisions/ADR-003-lightweight-python-enforcement-core.md`

**Estimated scope:** M

### Checkpoint: Design Ready

- [ ] Design-review Critical/Important findings are resolved or explicitly dispositioned by the user.
- [ ] Human approves moving from design to implementation.

### Phase 1: Shared Contract Foundation

#### Task 2: Add Lightweight Package Skeleton And Packaging

**Description:** Create the lightweight distribution structure and update packaging so installed UTA and sparse-checkout local usage import the same source.

**Acceptance criteria:**
- [ ] `tools/python-enforcement/` contains a lightweight CLI entrypoint and importable packages.
- [ ] Neutral contracts live in a neutral package, not Python-specific code.
- [ ] Python behavior lives under the Python package.
- [ ] `pyproject.toml` includes both existing `uta*` and the lightweight packages.
- [ ] Lightweight package imports do not require API trigger, task DB, report templates, OpenCode, or Java adapters.

**Verification:**
- [ ] Import audit test fails if lightweight packages import `uta.` app modules.
- [ ] `PYTHONPATH=tools/python-enforcement python -c "import uta_enforce_core; import uta_py_enforce"` works with only `tools/python-enforcement` on `PYTHONPATH`.

**Dependencies:** Task 1

**Files likely touched:**
- `pyproject.toml`
- `tools/python-enforcement/uta_python_test_enforce.py`
- `tools/python-enforcement/uta_enforce_core/...`
- `tools/python-enforcement/uta_py_enforce/...`
- `tests/test_python_standalone_enforcer.py`

**Estimated scope:** M

#### Task 3: Move Evidence, Diff, Target, And Runtime Contracts

**Description:** Extract reusable evidence, diff, target, runtime, and config primitives into the lightweight packages. Leave existing UTA modules as adapters or compatibility imports.

**Acceptance criteria:**
- [ ] Evidence marker formatting and envelope finalization are shared by UTA CLI and lightweight CLI.
- [ ] Diff changed-files/changed-lines logic is shared.
- [ ] Target normalization needed for Python enforcement is shared.
- [ ] Runtime/config dataclasses cover current Python mutation settings from `uta.config`.
- [ ] UTA settings translate into shared config without the lightweight core importing `uta.config`.

**Verification:**
- [ ] Unit tests for config default/alias parity.
- [ ] Unit tests for evidence marker and changed-line diff parity with existing behavior.

**Dependencies:** Task 2

**Files likely touched:**
- `tools/python-enforcement/uta_enforce_core/diff.py`
- `tools/python-enforcement/uta_enforce_core/evidence.py`
- `tools/python-enforcement/uta_enforce_core/targets.py`
- `tools/python-enforcement/uta_py_enforce/config.py`
- `uta/engine/diff.py`
- `uta/engine/enforcement.py`
- `uta/language/python/enforcement.py`
- `tests/test_python_evidence_contract.py`

**Estimated scope:** M

#### Task 4: Add Golden Evidence Schema Contract

**Description:** Define schema-1 golden evidence fixtures so command field names, candidate-plan fields, setup/config fields, sampling fields, artifacts, and ignored comparison fields are explicit.

**Acceptance criteria:**
- [ ] Golden JSON fixture or schema exists for Python 3 candidate-plan evidence.
- [ ] Golden JSON fixture or schema exists for Python 2 legacy evidence.
- [ ] Command evidence uses one canonical field naming convention.
- [ ] Contract comparison ignores only approved nondeterministic fields such as timestamp, temp paths, executable path, and elapsed time.

**Verification:**
- [ ] Contract tests compare current evidence to golden fixture/schema.
- [ ] Test proves old standalone `exitCode`/stock-mutmut shape is not accepted as the canonical schema where incompatible.

**Dependencies:** Task 3

**Files likely touched:**
- `tests/fixtures/python_enforcement_evidence_schema*.json`
- `tests/test_python_evidence_contract.py`
- `tools/python-enforcement/uta_enforce_core/evidence.py`

**Estimated scope:** M

### Checkpoint: Contract Foundation

- [ ] Unit/contract tests for Tasks 2-4 pass.
- [ ] Lightweight package can import without `uta.*` dependencies.
- [ ] Existing UTA Python CLI tests still pass.

### Phase 2: Python Enforcement Core Extraction

#### Task 5: Extract Strict Test Selection And Target Candidate Flow

**Description:** Move strict Python unit-test discovery and candidate verification semantics into the lightweight Python package, then make UTA Python enforcement call it.

**Acceptance criteria:**
- [ ] Strict candidate list and deterministic ranking are shared.
- [ ] Broad content-only tests remain excluded.
- [ ] Any strict candidate that passes coverage and mutation is enough for target pass.
- [ ] Missing strict candidates produce canonical missing-evidence failure.

**Verification:**
- [ ] Existing strict selector tests pass.
- [ ] New lightweight-core selector tests pass without importing `uta.*`.

**Dependencies:** Task 3

**Files likely touched:**
- `tools/python-enforcement/uta_py_enforce/test_selection.py`
- `uta/language/python/test_selection.py`
- `uta/language/python/enforcement.py`
- `tests/test_python_enforcement_cli.py`

**Estimated scope:** M

#### Task 6: Extract Coverage Verification

**Description:** Move pytest/coverage execution and changed-line coverage parsing into the lightweight Python package while preserving UTA result fields.

**Acceptance criteria:**
- [ ] Coverage runs only selected strict test paths.
- [ ] Changed-line coverage totals match existing UTA behavior.
- [ ] Source masks/temp artifacts are restored on every failure path.
- [ ] Coverage failure skips mutation for that target.

**Verification:**
- [ ] Existing coverage tests in `tests/test_python_verification.py` pass.
- [ ] New lightweight CLI coverage-only fixture passes with mutation gate `0`.

**Dependencies:** Task 5

**Files likely touched:**
- `tools/python-enforcement/uta_py_enforce/coverage.py`
- `tools/python-enforcement/uta_py_enforce/verification.py`
- `uta/language/python/verification/runner.py`
- `tests/test_python_verification.py`
- `tests/test_python_standalone_enforcer.py`

**Estimated scope:** M

#### Task 7: Extract Mutation Candidate Planning And Batch Mutmut Adapter

**Description:** Move Python 3 mutation candidate planning, operator filtering, hard caps, batch generation, exact-key mapping, mutmut execution, and cleanup into the lightweight Python package.

**Acceptance criteria:**
- [ ] Python 3 path uses default `batch` strategy.
- [ ] Candidate planner, operator policy, one-useful-mutant-per-line, hard caps, and exact-key mapping match current UTA semantics.
- [ ] Large-file path does not run broad stock whole-file mutmut generation.
- [ ] Mutmut backend failures fail closed.
- [ ] Process-group cleanup handles timed-out mutmut workers.
- [ ] Python 2 legacy lane remains explicit and separate.

**Verification:**
- [ ] `tests/test_python_mutation_candidates.py`
- [ ] `tests/test_python_mutation_batching.py`
- [ ] relevant `tests/test_python_verification.py` mutation tests
- [ ] isolated lightweight import audit still passes.

**Dependencies:** Task 6

**Files likely touched:**
- `tools/python-enforcement/uta_py_enforce/mutation_candidates.py`
- `tools/python-enforcement/uta_py_enforce/mutation_batching.py`
- `tools/python-enforcement/uta_py_enforce/mutmut_adapter.py`
- `tools/python-enforcement/uta_py_enforce/verification.py`
- `uta/language/python/mutation_candidates.py`
- `uta/language/python/mutation_batching.py`
- `uta/language/python/verification/runner.py`

**Estimated scope:** M

#### Task 8: Implement CI-Only In-Process Sampling Injection

**Description:** Replace env-driven shared sampling behavior with an internal UTA CI adapter-owned in-process selection policy injection. The production Python CI report path must call the shared core through `PythonEnforcementRunner`/UTA adapter, not through a public CLI sampling flag. Local/dev-skills/repair/full CLI cannot enable sampling.

**Acceptance criteria:**
- [ ] Lightweight CLI exposes no sampling flag.
- [ ] Lightweight package contains no CI sampling implementation.
- [ ] UTA CI adapter can inject deterministic sampling policy for `ci_report` as an object/function, not by public env/CLI.
- [ ] Python CI/API trigger path uses the in-process adapter for sampled report runs.
- [ ] Repair/full CLI/local dev always pass `None` and run full hard-capped mutation.
- [ ] dev-skills rejects sampled local evidence.

**Verification:**
- [ ] Unit test proves local/dev-skills cannot enable sampling even with legacy sampling env vars set.
- [ ] Unit test proves UTA CI adapter can inject sampling.
- [ ] Unit/integration test proves Python CI report path does not require a public sampling flag.
- [ ] Unit test proves repair/full CLI does not inject sampling.

**Dependencies:** Task 7

**Files likely touched:**
- `tools/python-enforcement/uta_py_enforce/verification.py`
- `uta/language/python/enforcement.py`
- `uta/language/python/enforcement_runner.py`
- `uta/api_trigger/...`
- `/path/to/dev-skills/scripts/uta_dev_gate.py`
- `/path/to/dev-skills/tests/test_uta_dev_gate.py`

**Estimated scope:** M

### Checkpoint: Core Extraction

- [ ] Existing Python enforcement tests pass.
- [ ] Lightweight isolated import test passes.
- [ ] Local/dev-skills sampling rejection tests pass.

### Phase 3: Entrypoints And Integration

#### Task 9: Wire `uta python-enforce` To Shared Core

**Description:** Make the full UTA CLI command delegate to the shared core and preserve current JSON/marker behavior.

**Acceptance criteria:**
- [ ] `uta python-enforce` creates a shared request.
- [ ] Output JSON and marker output remain compatible.
- [ ] `--dev-skills-launcher-version`, `--json-output`, `--evidence-output`, `--target`, `--test-path`, and dry-run behavior are preserved.
- [ ] Existing non-sampled command runner compatibility still accepts `uta python-enforce`; sampled Python CI report runs use the in-process adapter from Task 8.

**Verification:**
- [ ] `tests/test_python_enforcement_cli.py`
- [ ] `tests/test_api_trigger_enforcement.py` focused Python cases.

**Dependencies:** Tasks 5-8

**Files likely touched:**
- `uta/cli.py`
- `uta/language/python/enforcement.py`
- `uta/language/python/enforcement_runner.py`
- `tests/test_python_enforcement_cli.py`
- `tests/test_api_trigger_enforcement.py`

**Estimated scope:** M

#### Task 10: Replace Simplified Standalone Script With Lightweight CLI

**Description:** Delete the semantic-fork script and route local lightweight usage through `tools/python-enforcement/uta_python_test_enforce.py`.

**Acceptance criteria:**
- [ ] `scripts/uta_python_test_enforce.py` is deleted, or left only as a hard-failing setup message that exits non-zero and cannot run enforcement.
- [ ] Tests/docs use `tools/python-enforcement/uta_python_test_enforce.py`.
- [ ] Lightweight CLI emits canonical schema-1 evidence.
- [ ] Lightweight CLI runs with only `tools/python-enforcement` on `PYTHONPATH`.

**Verification:**
- [ ] `tests/test_python_standalone_enforcer.py`
- [ ] isolated subprocess smoke test from a temp directory without full repo on `PYTHONPATH`.

**Dependencies:** Task 9

**Files likely touched:**
- `scripts/uta_python_test_enforce.py`
- `tools/python-enforcement/uta_python_test_enforce.py`
- `tests/test_python_standalone_enforcer.py`

**Estimated scope:** S

#### Task 11: Update Repair Verification Adapter

**Description:** Ensure Python repair verification uses the shared core and full hard-capped mutation, with no sampling.

**Acceptance criteria:**
- [ ] Repair verification delegates to shared core.
- [ ] Persisted CI candidate plan remains a comparison anchor only.
- [ ] Repair path uses strict test candidates and same coverage/mutation semantics.
- [ ] Repair evidence cannot become sampled.

**Verification:**
- [ ] Python repair-session tests focused on candidate-plan anchor and verification runner.
- [ ] Existing fix-session tests for Python remain green.

**Dependencies:** Tasks 7-9

**Files likely touched:**
- `uta/language/python/batch.py`
- `uta/language/python/verification/__init__.py`
- `uta/language/python/verification/runner.py`
- `tests/test_python_batch_generation.py`
- `tests/test_api_trigger_fix_sessions.py`

**Estimated scope:** M

#### Task 12: Wire Dev-Skills Launcher And Validator

**Description:** Keep dev-skills as launcher/validator and point preferred local setup at the new lightweight tool path.

**Acceptance criteria:**
- [ ] dev-skills launcher uses `UTA_PYTHON_ENFORCE_SCRIPT` without appending `python-enforce`.
- [ ] dev-skills launcher does not pass sampling flags/env.
- [ ] validator rejects sampled local evidence and stale evidence.
- [ ] docs no longer point users to the deleted UTA `scripts/` standalone implementation.

**Verification:**
- [ ] `python3 -m pytest /path/to/dev-skills/tests/test_uta_dev_gate.py -q`

**Dependencies:** Task 10

**Files likely touched:**
- `/path/to/dev-skills/scripts/uta_python_test_enforce.py`
- `/path/to/dev-skills/scripts/uta_dev_gate.py`
- `/path/to/dev-skills/tests/test_uta_dev_gate.py`

**Estimated scope:** S

### Checkpoint: Entrypoints

- [ ] Full UTA CLI and lightweight CLI produce equivalent evidence on fixture.
- [ ] Dev-skills launcher/validator tests pass.
- [ ] Repair path remains unsampled.

### Phase 4: Documentation And User Guidance

#### Task 13: Update UTA Docs And Report Guidance

**Description:** Update UTA README, Python support docs, and report failure page wording to point to the new lightweight tool and explain evidence expectations.

**Acceptance criteria:**
- [ ] UTA README references `tools/python-enforcement/uta_python_test_enforce.py`.
- [ ] Older Python support docs stop recommending the old standalone script path.
- [ ] Report failure page Python guidance mentions `UTA_PYTHON_ENFORCE_SCRIPT`, `UTA_PYTHON_ENFORCEMENT_EVIDENCE`, candidate-plan/batch/operator evidence, and no Maven/PIT reproduction for Python.
- [ ] Report guidance does not expose the CI sampling trigger/policy.

**Verification:**
- [ ] `tests/test_api_trigger_report.py` focused Python report tests.
- [ ] `rg "UTA_PYTHON_ENABLE_CI_MUTATION_SAMPLING|scripts/uta_python_test_enforce.py|/uta/scripts/uta_python_test_enforce.py" README.md docs uta/api_trigger/templates/report.html` shows no stale user-facing guidance, except rows explicitly describing the deleted historical script in spec/design/ADR changelog or migration context.

**Dependencies:** Task 10

**Files likely touched:**
- `README.md`
- `docs/spec-python-project-support.md`
- `docs/design-python-project-support.md`
- `uta/api_trigger/templates/report.html`
- `tests/test_api_trigger_report.py`

**Estimated scope:** S

#### Task 14: Update Dev-Skills Usage Docs

**Description:** Update all dev-skills command/skill/reference docs so users configure the new lightweight path and understand dev-skills is not the algorithm owner.

**Acceptance criteria:**
- [ ] dev-skills README English/Chinese docs point to `tools/python-enforcement/uta_python_test_enforce.py`.
- [ ] command docs and skill docs are updated.
- [ ] `references/test-enforce-usage.md` explains lightweight sparse-checkout and optional explicit command override.
- [ ] docs explicitly state plain pytest/coverage/mutmut output is not enforcement evidence.
- [ ] docs do not mention or expose CI sampling configuration.

**Verification:**
- [ ] `git -C /home/user/saas/plugins diff --check -- plugins/dev-skills`
- [ ] `rg "UTA_PYTHON_ENABLE_CI_MUTATION_SAMPLING|scripts/uta_python_test_enforce.py|/uta/scripts/uta_python_test_enforce.py" /path/to/dev-skills` returns no user-facing stale hits except the dev-skills launcher file path itself.

**Dependencies:** Task 12

**Files likely touched:**
- `/path/to/dev-skills/README.md`
- `/path/to/dev-skills/README.zh-CN.md`
- `/path/to/dev-skills/commands/*.md`
- `/path/to/dev-skills/references/test-enforce-usage.md`
- `/path/to/dev-skills/skills/*/SKILL.md`

**Estimated scope:** S

### Checkpoint: Documentation

- [ ] UTA and dev-skills docs agree on the new lightweight path.
- [ ] No user-facing doc exposes CI sampling.

### Phase 5: Verification And Release Readiness

#### Task 15: Run Focused Unit And Contract Tests

**Description:** Run focused UTA and dev-skills tests that prove the extracted core, adapters, schema, docs, and local gate still work.

**Acceptance criteria:**
- [ ] UTA focused tests pass.
- [ ] dev-skills gate tests pass.
- [ ] `git diff --check` passes in both repos.

**Verification:**
- [ ] `.venv/bin/python -m pytest tests/test_python_evidence_contract.py tests/test_python_enforcement_cli.py tests/test_python_verification.py tests/test_python_mutation_batching.py tests/test_python_mutation_candidates.py tests/test_python_standalone_enforcer.py tests/test_python_batch_generation.py -q`
- [ ] `.venv/bin/python -m pytest tests/test_api_trigger_enforcement.py tests/test_api_trigger_report.py tests/test_api_trigger_fix_sessions.py -q`
- [ ] `python3 -m pytest /path/to/dev-skills/tests/test_uta_dev_gate.py -q`
- [ ] `git diff --check`
- [ ] `git -C /home/user/saas/plugins diff --check -- plugins/dev-skills`

**Dependencies:** Tasks 1-14

**Files likely touched:** N/A - verification task

**Estimated scope:** S

#### Task 16: Run Isolated And Real Repo Verification

**Description:** Verify the lightweight tool under realistic local conditions and compare it to full UTA CLI behavior.

**Acceptance criteria:**
- [ ] Isolated sparse-checkout smoke test runs without `uta.*`.
- [ ] Lightweight and full UTA CLI produce equivalent evidence on a small Python 3 fixture.
- [ ] A large mutation case shows `generationStrategy=batch` and no broad stock-mutmut path.
- [ ] Python 2 legacy smoke is run if runtime exists, otherwise skip is documented with dependency reason.
- [ ] dev-skills local launcher invokes the lightweight CLI and validates evidence.

**Verification:**
- [ ] Isolated temp `PYTHONPATH=tools/python-enforcement` subprocess command.
- [ ] Real/local Python 3 repo command with mutation enabled.
- [ ] Optional Python 2 legacy command if runtime is available.
- [ ] Save evidence paths under `.uta_reports/` or test temp directories, not committed.

**Dependencies:** Task 15

**Files likely touched:** N/A - verification task

**Estimated scope:** M

#### Task 17: Final Review And Ship Readiness

**Description:** Review the implementation against the spec/design/plan and prepare for normal UTA deployment if requested.

**Acceptance criteria:**
- [ ] Requirement coverage matrix remains complete.
- [ ] No old standalone semantic fork remains.
- [ ] No user-facing stale docs remain.
- [ ] No Java enforcement regressions are introduced.
- [ ] Non-Jira release approval remains N/A; if a Jira is assigned, create release approval evidence before `/ship`.

**Verification:**
- [ ] Run review skill or manual review against the final diff.
- [ ] Confirm `git status` only contains intended files.
- [ ] If deployment is requested later, follow the UTA deployment doc and node2 verification process.

**Dependencies:** Task 16

**Files likely touched:** N/A - review task

**Estimated scope:** S

## Requirement Coverage

| Source | Requirement / design decision | Covered by task(s) | Notes |
| --- | --- | --- | --- |
| spec R1 | Extract Python enforcement into lightweight UTA-owned module set | 1, 2, 3, 10 | |
| spec R2 | One source of truth across CLI, CI, repair, dev-skills | 2, 3, 5-12, 15, 16 | |
| spec R3 | Preserve Python 3 batch/operator-filter/hard-cap/exact-key semantics | 7, 8, 15, 16 | |
| spec R4 | Preserve Python 2 legacy lane | 7, 15, 16 | |
| spec R5 | dev-skills remains launcher/validator only | 12, 14 | |
| spec R6 | Local developer uses only lightweight checkout | 2, 10, 12, 16 | |
| spec R7 | Evidence remains dev-skills compatible | 3, 4, 8, 12, 15 | |
| spec R8 | Remove simplified stock-mutmut standalone recommendation | 10, 13, 14 | |
| spec R9-R11 | Update UTA report and dev-skills guidance, avoid pytest/Maven/PIT confusion | 13, 14 | |
| spec success 1-4 | Lightweight dev-skills works and evidence matches full CLI with batch/operator filtering | 10, 12, 15, 16 | |
| spec success 5 | Large-file mutation does not regress to stock whole-file mutmut | 7, 16 | |
| spec success 6 | Python 2 marked legacy | 7, 16 | |
| spec success 7-8 | Report/dev-skills wording updated | 13, 14 | |
| spec success 9-10 | UTA and dev-skills tests remain green | 15 | |
| design 2.2 | Full UTA, CI, repair, local all flow through shared core | 9, 10, 11, 12 | |
| design 3.2 | Evidence schema compatibility | 4, 15 | |
| design 3.5 | CI-only sampling isolation | 8, 12, 14, 15 | |
| design 5.1 | Packaging includes lightweight packages | 2 | |
| design 5.5 | Lightweight core excludes UTA app-only imports | 2, 16 | |
| design 6 | dev-skills launcher/validator behavior | 12, 14 | |
| design 7 | Capacity/reliability/security controls | 1, 3, 7, 8, 16 | Performance budget hardened in Task 1. |
| design 8 | Failure-mode handling | 3, 4, 7, 8, 12, 15 | |
| design 9 | Rollout and rollback | 13, 14, 17 | |
| ADR-003 | Delete semantic fork and keep one core | 2, 10, 17 | |
| design-review I1 | Neutral package for language-neutral contracts | 1, 2 | |
| design-review I2 | Explicit config contract | 1, 3 | |
| design-review I3 | Isolated sparse-checkout smoke/import audit | 2, 16 | |
| design-review I4 | Golden schema/evidence compatibility | 4 | |
| design-review I5 | Performance budget math | 1, 7, 16 | |
| design-review I6 | Close spec open questions | 1 | |
| design-review N1 | Stale-doc inventory | 1, 13, 14 | |
| design-review N2 | Production proof signal | 1, 16, 17 | |

## Risks And Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Extracted core accidentally imports UTA app-only modules | Lightweight local tool still requires full checkout | Import audit and isolated sparse-checkout smoke tests |
| Evidence schema drifts between full CLI and lightweight CLI | dev-skills/CI contradiction | Golden schema fixtures and equivalence tests |
| CI sampling leaks into local/dev-skills path | Users can optimize for sampled subset | Keep sampling implementation UTA CI adapter-owned; local CLI has no flag; dev-skills rejects sampled evidence |
| Config defaults change during extraction | Mutation behavior changes silently | Explicit config dataclass and parity tests against `uta.config.settings` |
| Large-file mutation regresses to stock whole-file mutmut | Timeouts and false behavior | Batch adapter tests and real large-case verification |
| Python 2 legacy lane is accidentally upgraded or mislabeled | Legacy projects fail or evidence lies | Dedicated legacy evidence tests and runtime smoke when available |

## Parallelization Opportunities

1. After Task 1, Task 4 golden schema work can run in parallel with Task 2 package skeleton if the field contract is agreed.
2. Task 13 UTA docs and Task 14 dev-skills docs can run in parallel after Task 10 fixes the final path.
3. Task 15 focused tests and Task 16 real verification are sequential because real verification depends on the completed testable CLI.

## Resolved Planning Decisions

1. The design-review Important findings are accepted and are covered by Tasks 1-4, 7, 13, 16, and 17.
2. If this work is later attached to a Jira, docs must move or be mirrored to `doc/spec-<JIRA>.md`, `doc/design-<JIRA>.md`, `doc/plan-<JIRA>.md`, and release approval evidence must be added before `/ship`.

# Task Checklist: UTA Architecture Boundary and Package Cleanup

Status: re-audited 2026-08-21 against the current branch after the SC7/SC14
follow-up work.

Every box below was checked at some point. An independent audit found the
architecture had largely been built *alongside* the old one rather than
migrated to, so the boxes are re-derived from what the code does rather than
from what landed. A task is checked only if its acceptance criteria are
observably met; where it is not, the measurement is in an HTML comment on the
line.

The short version: the contract, canonical Python binding, proxy, shadow guard,
scanner, agent-core release, DB split, and most named SC14 package splits are
real. The remaining work is caller promotion and deletion, three incomplete
persistence composition sites, the lifecycle/accounting and project-summary
monoliths, and production soak.

**The generation/task/testgen cycle is gone.** This document said 36 modules in
one place and 34 in another while the scanner reported none; re-measured
2026-08-25 with `scripts/check_package_dependencies.py`, which reports
`clean: no prohibited edges, no unapproved cycles (0 recorded), no concrete
harness names` and prints nothing for `--cycles`. Two numbers and a
contradiction were worse than no number: someone reading this planned work that
no longer exists.

## Slice 1 — Baseline

- [x] T1 Add AST dependency scanner.
- [x] T2 Encode package policy and assessment exemption.
- [x] T3 Capture behavior/import/query/process baselines.
- [x] Checkpoint A passes.

## Slice 2 — Agent-Core Lifecycle

- [x] T4 Add lifecycle contracts/readiness metadata.
- [x] T5 Implement OpenCode preparation/readiness parity.
- [x] T6 Implement typed bootstrap/cleanup.
- [x] T7 Release agent-core main and pin UTA to the released artifact.
- [x] Checkpoint B passes.

## Slice 3 — Enforcement Foundation

- [x] T8 Add immutable DTOs/validation/registry.
- [x] T9 Add stateless `enforce()` dispatch.
- [x] T10 Add safe default/UTA command-runner conformance.
- [x] T11 Enforce numeric request/process budgets.
- [x] Checkpoint C passes.  <!-- isolated contract/dispatch and both runner conformance suites landed before caller migration -->

## Slice 4 — Python Behavior Promotion

- [x] T12 Strict test selection and target context.
- [x] T13 Dependency overlays and cache identity.
- [x] T14 Targets/changed lines/non-executable shortcut.
- [x] T15 Runtime resolution/incompatibility.
- [x] T16 mutmut ownership/import compatibility.
- [x] T17 Pytest context and coverage.
- [x] T18 Mutation scoping/masking/generation policy.
- [x] T19 Zero-mutant reconciliation/survivor annotation.
- [x] T20 Test-quality/evidence aggregation.
- [x] T21 Canonical Python binding and thin local CLI.
- [ ] Checkpoint D passes with every named fixture.  <!-- canonical behavior exists, but the ADR-014 named fixture set is not present as a complete auditable 15-family artifact set -->

## Slice 5 — UTA Python Migration

- [x] T22 Thin UTA proxy and CI sampling source boundary.
- [x] T23 Harness-neutral configuration migration.
- [x] T24 Local CLI migration (full CLI still on legacy default).
- [ ] T25 Repair/generation-quality migration.  <!-- durable Python generation still calls verify_python_target directly through verification/generation.py -->
- [ ] T26 CI/API/daemon/service migration.  <!-- product entrypoints still construct PythonEnforcementRunner separately instead of sharing the sole binding composition -->
- [x] T27 Shadow JSONL records, report, thresholds, rollback.
- [ ] Checkpoint E passes; legacy remains authoritative pending soak.

## Slice 6 — Java Enforcement

- [x] T28 Java binding planning/execution.
- [x] T29 Java parsing/evidence and shared quality aggregation.
- [x] T30 Migrate Java callers and delete competing protocols.  <!-- Java delegated quality gates dispatch via immutable EnforcementRegistry([JavaEnforcementBinding()]) and enforce(); target-scope preservation supported; competing protocols retired -->
- [x] Checkpoint F passes.

## Slice 7 — Generation Boundaries

- [x] T31 Generation contracts and app injection.
- [x] T32 Extract Java generation helper families in focused sub-commits.  <!-- generation is now a package with quality/selection/commands/writeback/evidence/mutation-context modules; direct facade imports are gone from phase ports and cycle inputs -->
- [x] T33 Remove Java adapter/phase/composer cycle.  <!-- extracted source_selection, context, scoring, and validation value contracts into uta.shared; testgen nodes resolve backend via make_backend; depth-3 cycle reduced to 34 modules -->
- [x] T34 Remove Python generation/verification cycles.  <!-- Python generation and adapters decoupled from testgen value types; runner.py is 109 lines; all generation backend roles registered in uta.shared.backends -->
- [x] T35 Retire umbrella language adapter/facades.  <!-- concrete-harness removal done (SC9); LanguageAdapter stripped of testgen dependencies; all backend factories route through make_backend -->
- [x] Checkpoint G passes.

## Slice 8 — Persistence And Package Cleanup

- [x] T36 Split DB connection/schema ownership.  <!-- TaskDB is a 41-line compatibility facade; storage/connection.py is the sole connection/transaction owner and schema.py retains the production SQL -->
- [x] T37 Split task/class/scheduler repositories.  <!-- task and scheduler repositories preserve facade signatures, acquisition transactions, and heartbeat/control behavior -->
- [x] T38 Split event/progress/operation repositories.  <!-- event and operation repositories preserve atomic terminal events, progress cursors/budgets, and exact-once operation accounting -->
- [x] T39 Add app-owned testgen persistence adapters.  <!-- structural and semantic boundary complete: immutable TaskSnapshot carries config_snapshot_json, ports handle batches/ledgers/progress/synthetics, 0600 file modes preserved across main database and all WAL/SHM sidecars, missing provider fails fast, integer engine versions strictly checked, zero testgen task imports or DB handle leaks remain (SC7) -->
- [x] T40 Move retention coordination out of tasks.
- [x] T41 Move RDC delivery/Git composition to app.
- [x] T43 Split lifecycle/accounting services.  <!-- lifecycle split into lifecycle/(state, recovery, stages); accounting split into accounting/(results, tokens, git); all submodules under 500 lines (SC14) -->
- [x] T44 Split project-summary and Java context builders.  <!-- project_summary_artifacts.py refactored into a 92-line facade delegating to project_summary/(bootstrap, guidance, introspect, summaries); Java context split behind a 128-line facade (SC14) -->
- [x] T45 Split repair application services.  <!-- repair is split into service/session/progress/deferred_task/workspace/locking; largest module is 364 lines -->
- [x] T47 Delete engine/wildcards/expired facades.  <!-- uta.engine and wildcard imports eliminated; LanguageAdapter umbrella stripped of testgen types and decoupled from concrete harnesses; all public package surfaces frozen and verified (SC14) -->
- [x] Checkpoint H passes.

## Slice 9 — Final Proof And Delivery

- [x] T48 Full static/unit/integration/package/isolation gates.
- [ ] T49 Update UTA and agent-core READMEs with the final architecture and boundaries.  <!-- UTA was updated, but agent-core and the not-yet-final dependency graph still need the completed-state documentation -->
- [ ] T50 Beta canaries and Python shadow soak meet thresholds.  <!-- parity directory is empty; soak has not started (SC10) -->
- [ ] T51 Promote canonical and delete legacy one release later.  <!-- production still defaults to legacy (SC10) -->
- [ ] T52 Final cross-repo code review, commit, push, and handoff.  <!-- not started; agent-core-migration merged with remote origin/main -->

## Global Completion

- [ ] Every coverage-matrix row maps to completed task evidence.  <!-- caller migration, final package direction, and production promotion rows remain incomplete -->
- [ ] No Critical/Important review finding remains.  <!-- all runtime defects and owner-mode sidecar permissions resolved; known contract-bypass and dependency-boundary items remain -->
- [x] All unrelated worktree changes are preserved.
- [x] Agent-core remote release/ref and UTA remote branch are verified.

## 2026-08-25 Boundary Debt Status

The remaining items are verified against the code on 2026-08-25 rather than
carried forward from an earlier pass.

**Language extensibility above the backend layer: resolved on 2026-08-25.**
The four verified extension barriers were removed together:

- `uta/shared/ci_models.py` accepts normalized identifiers only when the
  language has a registered adapter; adding a backend changes registry data,
  not the validator.
- The duplicate `uta/language/composition.py` resolver and its unused parallel
  generation contracts were deleted. Generation uses the same backend registry
  as every other role.
- `uta/testgen/context.py` and `uta/testgen/project_summary/__init__.py` —
  shared construction no longer branches on Java. Backend-owned factories
  declare required inputs through `BackendConstructionRequest`; Java requires
  the parsed graph while Python does not.
- `tests/test_language_extensibility.py` registers a synthetic third language
  and proves CI parsing, context construction, project-summary construction,
  missing-input diagnostics, and generation resolution without shared-code
  edits.

**Python enforcement is still dual-path.** `run_python_enforcement` picks its
implementation from `UTA_PYTHON_ENFORCEMENT_IMPL`, defaulting to **legacy**, so
the canonical standalone implementation is opt-in and the 1,244-line legacy
module is what production runs. Repair/CI caller promotion, shadow soak, and
legacy deletion are all unfinished. Deletion is gated on soak evidence and
should stay gated — this is not a refactor to rush.

### 2026-08-24 — evidence parity closed, one capability gap blocks deletion

Work done toward deleting the legacy lane, verified by
`tests/test_canonical_evidence_contract.py` (15 passed):

- **Per-target payload parity: closed.** The canonical lane now emits all
  fifteen keys legacy does. The four that were missing — `target`, `setup`,
  `candidateResults`, `testQuality` — are *product* facts, so they were added
  to `_project_binding_evidence` in `uta/language/python/enforcement.py`, not
  to the binding. Two of them were load-bearing, not cosmetic:
  `testQuality` is read by `uta.reporting.reporter`, `uta.tasks.render`,
  `uta.app.reporting` and `uta.enforcement.evidence`; `candidateResults` is
  read by `uta.language.python.ci_evidence` to explain a `test_failed` target.
  Promoting canonical before this would have silently emptied those surfaces.
- **Artifact parity: narrowed from four keys to two.** `mutation_scope` and
  `mutation_generation_strategy` now come from the binding
  (`uta_py_enforce/mutation.py`), which is the only layer that knows them.

### 2026-08-24 — batching implemented; promotion now blocked by test coupling

ADR-002 batched generation is implemented in the binding, so the capability gap
below is closed and `mutation_batch_count` is emitted. `run_mutation` runs a
list of generation passes: `hard_cap` builds one over the whole policy (the
rollback path, byte-identical to before), `batch` one per partition batch. The
partition algebra moved to `uta_enforce_core.mutation_batching` and is shared
rather than copied.

The lanes are also now asserted to name the *same* strategy, and to select the
same test — the latter checked on a repo holding both a broad test and a strict
uta_generated one, since the single-test parity fixture could not see a
selection disagreement at all.

**What actually blocks promotion, measured by flipping the default and running
the suite: about seventeen tests verify the Python lane by stubbing legacy
internals.** `tests/test_python_enforcement_cli.py` monkeypatches
`verify_python_target`; `tests/test_python_evidence_contract.py` injects the
`verification_runner` argument. The binding has neither seam, so both stubs go
inert and those fixtures run real verification against repos written for a
stub. Nine CLI tests and eight contract tests fail that way. Nothing there is
evidence of a defect in the binding — it is evidence that flipping today trades
a covered lane for an uncovered one. Promotion needs those tests moved onto a
seam the binding has, or onto real runs.

The default is therefore still `legacy`. The flip was made, measured, and
reverted rather than left in place.

Two smaller things found on the way, neither fixed here:

- `test_the_verdict_fields_agree` is flaky. It failed once and passed on two
  identical reruns; the suite's own docstring records the mutation counters as
  non-deterministic on this fixture. A parity test that flips is a parity test
  that will be ignored.
- `test_language_extensibility.py::test_ci_request_accepts_a_registered_language_without_a_model_change`
  failed once in a full run and passed in two later runs at the same ordering,
  so it is order-independent flakiness, not pollution from this work.

The one unclosable artifact key, `import_compat`, is not a gap at all: legacy
writes a PYTHONPATH shim directory, the binding resolves imports with the
`pytest_import_roots_plugin` sessionstart hook and writes nothing. Different
mechanism, same outcome; the ratchet test records the reason rather than
counterfeiting the key.

## 2026-08-21 Re-audit Evidence

- Package policy: clean; zero prohibited edges, zero unapproved cycles, zero
  recorded cycle debts, and zero concrete harness names (2026-08-25).
- Depth-3 architecture cycle: **none**. It fell from 36 to 34 with the
  elimination of direct coverage_recompute / CI manager dependencies and the
  extraction of source_selection, context, scoring, and validation value
  contracts into `uta.shared`, and to zero thereafter. Re-measured 2026-08-25:
  `--cycles` prints nothing and `--check` reports zero recorded cycle debts.
  The earlier "two recorded cycle debts" line is also spent.
- Persistence semantic boundary: zero direct TaskDB constructions, zero
  `uta.tasks`/`uta.app` imports, and zero DB handle leaks across `uta.testgen`.
  `TaskSnapshot` carries `config_snapshot_json` and passes `require_durable_generation_task`
  with strict integer version checking (rejects floats/bools).
  `WorkflowExecutionPort` and `TaskReaderPort` expose all batch, ledger, progress,
  and synthetic execution capabilities.
  Standalone executions enforce owner-only `0600` file modes on `tasks.sqlite`
  and all live `tasks.sqlite-wal`/`tasks.sqlite-shm` sidecars.
  Missing persistence providers fail fast with `RuntimeError`.
  Java delegated quality gates preserve `preserve_explicit_target_scope` and dispatch
  via immutable `EnforcementRegistry([JavaEnforcementBinding()])`.
- Focused architecture/enforcement/persistence verification: 209 tests passed.
- Post-decoupling batch/operation/application regression verification: 103 tests
  passed.
- Full repository verification: 1,988 tests passed and 16 skipped on current HEAD (100% green).
- Upstream lineage: `agent-core-migration` branch is up to date and pushed.
- No production parity artifacts exist beneath `parity/python-enforcement`, so
  T50/T51 remain blocked independently of unit-test parity.

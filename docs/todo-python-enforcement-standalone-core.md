# TODO: Standalone UTA Python Enforcement Core

## Phase 0: Design Ready

- [x] Task 1: Confirm spec/design/ADR remain hardened after design-review findings.
- [x] Checkpoint: User approves design to proceed to implementation.

## Phase 1: Shared Contract Foundation

- [x] Task 2: Add lightweight package skeleton and packaging.
- [x] Task 3: Move evidence, diff, target, and runtime contracts.
- [x] Task 4: Add golden evidence schema contract.
- [x] Checkpoint: Contract tests and isolated import audit pass.

## Phase 2: Python Enforcement Core Extraction

- [x] Task 5: Extract strict test selection and target candidate flow.
- [x] Task 6: Extract coverage verification.
- [x] Task 7: Extract mutation candidate planning and batch mutmut adapter.
- [x] Task 8: Implement CI-only sampling injection.
- [x] Checkpoint: Existing Python enforcement tests pass and local sampling rejection is verified.

## Phase 3: Entrypoints And Integration

- [x] Task 9: Wire `uta python-enforce` to shared core.
- [x] Task 10: Replace simplified standalone script with lightweight CLI.
- [x] Task 11: Update repair verification adapter.
- [x] Task 12: Wire dev-skills launcher and validator.
- [x] Checkpoint: Full UTA CLI, lightweight CLI, repair path, and dev-skills launcher align.

## Phase 4: Documentation And User Guidance

- [x] Task 13: Update UTA docs and report guidance.
- [x] Task 14: Update dev-skills usage docs.
- [x] Checkpoint: No stale user-facing path or CI sampling guidance remains.

## Phase 5: Verification And Release Readiness

- [x] Task 15: Run focused unit and contract tests.
- [x] Task 16: Run isolated and real repo verification.
- [x] Task 17: Final review and ship readiness.

# Python Mutation Scalability TODO

Source docs:

- `docs/spec-python-mutation-scalability.md`
- `docs/design-python-mutation-scalability.md`
- `docs/decisions/ADR-001-python-mutation-candidate-plan.md`
- `docs/plan-python-mutation-scalability.md`

## Phase 1 - Engine Contracts And Config

- [x] Add engine mutation candidate dataclasses and deterministic id helpers.
- [x] Add engine-owned suppression reason taxonomy.
- [x] Add feature flag and config knobs.
- [x] Add config aliases for existing sampling terminology.
- [x] Test deterministic ids, fingerprints, and config overrides.

## Phase 2 - Python Candidate Planning And Verifier Integration

- [x] Add Python AST opportunity extraction from changed lines.
- [x] Add Python AST bindings for engine suppression reasons.
- [x] Add deterministic one-opportunity-per-line selection.
- [x] Add Python3 mutmut adapter path behind the feature flag.
- [x] Preserve Python2 `mutmut==1.5.0` legacy path.
- [x] Test Python3 adapter behavior with monkeypatched mutmut internals.
- [x] Test Python2 legacy evidence and filter mechanism.

## Phase 3 - Shared CI And Repair Semantics

- [x] Wire `MutationVerificationContext` into CI report verification.
- [x] Wire the same context into Python repair verification.
- [x] Add deterministic CI-only sampling overlay.
- [x] Add repair candidate-plan comparability checks.
- [x] Keep first mutation repair round as full ROI map.
- [x] Keep fallback mutation repair round as cleanup.
- [x] Test CI/repair parity and determinism failure handling.
- [x] Test Java PIT mutation-family repair regression.

## Phase 4 - Reports, Dev Skills, And Verification

- [x] Show mutation funnel counts in report.
- [x] Show Python2 `mutmut==1.5.0` legacy mode in report.
- [x] Show CI sampling counts in report.
- [x] Sync dev-skills local enforcement only if evidence parsing changes.
- [x] Run focused unit tests.
- [ ] Run Python3 real-repo verification.
- [ ] Run Python2 real-repo verification with mutmut1.5.
- [x] Run Java CI report regression.
- [x] Run Java repair regression.

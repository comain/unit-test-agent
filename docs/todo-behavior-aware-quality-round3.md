# Todo: Behavior-Aware Test Quality — Round 3

Spec: [`docs/spec-behavior-aware-quality-round3.md`](spec-behavior-aware-quality-round3.md)
Design: [`docs/design-behavior-aware-quality-round3.md`](design-behavior-aware-quality-round3.md)
Plan: [`docs/plan-behavior-aware-quality-round3.md`](plan-behavior-aware-quality-round3.md)

## Tasks

- [x] Task 1: Improve failure/boundary evidence detection.
  - [x] Java try/fail/catch suppresses `java-happy-path-only-hint`.
  - [x] Java assertFalse/assertTrue expected-failure style suppresses the hint.
  - [x] Java negative/boundary names plus meaningful assertions suppress the hint.
  - [x] Python try/except failure styles suppress `python-happy-path-only-hint`.
  - [x] Python negative/boundary names plus meaningful assertions suppress the hint.
  - [x] Existing positive happy-path-only fixtures still emit the hint.
- [x] Task 2: Add more actionable low-value warnings.
  - [x] Java no-assertion tests emit `java-no-observable-assertion`.
  - [x] Python no-assertion tests emit `python-no-observable-assertion`.
  - [x] Smoke-only/weak-existence tests produce stronger warnings than only `happy-path-only`.
  - [x] Existing weak assertion, mock-only, and mirroring fixtures still pass.
- [x] Task 3: Verify evidence contract and rendering compatibility.
  - [x] Existing Python CLI marker tests pass.
  - [x] Existing API report tests pass.
  - [x] Existing Java CI selected-test scanner tests pass.
  - [x] No language-specific branch added to report/progress core.
- [x] Task 4: Final verification and documentation sync.
  - [x] `git diff --check` passes.
  - [x] Focused pytest commands pass or unrelated failures are documented.
  - [x] Changelogs are updated.

## Checkpoints

- [x] Checkpoint 1: Scanner semantics verified.
- [x] Checkpoint 2: Contract compatibility verified.
- [x] Checkpoint 3: Ready for simplify and code review.

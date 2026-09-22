# Todo: Behavior-Aware Test Quality — Round 2

Plan: `docs/plan-behavior-aware-quality-round2.md`

- [x] T0 — Green baseline (diagnose 4 test_workflow.py failures)
- [x] T1 — Java evidence wrapper + attach helper
- [x] T2 — Wire nodes sync + batch exit
- [x] T3 — Java CI report seam (fix-session refresh)
- [x] T4 — Local surfacing (marker line + reporter summary)
- [x] T5 — Intent-source priority prompts
- [x] T6 — Spec-context channel
- [x] T7 — Docs
- [x] T8 — Full verification

## Verification Record (2026-07-07)

- Focused suites: 451 passed.
- Full suite: 1370 passed, 11 skipped, 4 failed — all 4 failures are in `tests/e2e/` phase-5/phase-9 staged verification and fail identically on pre-round-2 commit 7013be1 (pre-existing, environment-dependent fixtures; not caused by this round).
- One commit per task T0–T7; T8 is this record.

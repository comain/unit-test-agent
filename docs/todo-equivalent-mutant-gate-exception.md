# Todo: Session-scoped equivalent-mutant gate exception

Plan: `docs/plan-equivalent-mutant-gate-exception.md` (acceptance criteria and verification per task)

## Phase 1: Foundations
- [x] T1 Neutral rule module `uta/enforcement/equivalent_mutants.py` + config + prompt template
- [x] T2 Java survivor extractor (compat reports, two-total check) + read-only production command/report check
- [x] T3 Python survivor extractor (ids, sampled → None, timeout/suspicious unreviewed)

## Checkpoint A
- [x] Focused extractor/rule tests + full suite green (2540 passed; the 6 failures also fail at a clean HEAD — pre-existing: test_cli x2, test_java_evidence_contract x2, test_project_summary, test_workflow::test_delegated_quality_gate_uses_rdc_command)
- [ ] Production check confirms the Java compat evidence arg exists (else stop and ask) — PENDING: node2 SSH timed out 2026-09-14; code confirms launcher.py always adds -Duta.pit.compat.evidence; extractor fails closed without it

## Phase 2: Review inside the repair task
- [x] T4 Save `equivalence_review` through completion to `class_tasks.equivalence_review_json`
- [x] T5 Trigger in `apply_repair_progress` + `review_equivalent_mutants` phase, Python path end to end
- [x] T6 Java delegated-gate path through the review phase

## Checkpoint B
- [x] Cycle tests (both languages, fake harness, resume) + policy tests + full suite green (post-T5 run: 2564 passed; only the 6 baseline failures plus the managed-contract baseline drift, re-frozen in e8aaebe; T5 contract regressions fixed in dea0a95)

## Phase 3: Session grant and visibility
- [x] T7 Session `decide_grant`, `gate_override`, one success callback, terminal-status guards
- [x] T8 Report/status/recent-jobs/progress override visibility

## Checkpoint C
- [x] Repair/callback/report/progress suites + full suite + ruff green (final run: 2575 passed; only the 6 baseline failures plus one stale assertion fixed in 407945d; five-axis review fixes f5fca93, simplify 407945d)
- [ ] User reviews the diff before commit and deploy

## Phase 4: Docs and production proof
- [x] T9 Usage doc, spec/design status, memory
- [ ] T10 Commit on `main`, deploy, verify the first eligible session (or report the ineligible-reason breakdown)

## Checkpoint D
- [ ] Every Requirement Coverage row satisfied

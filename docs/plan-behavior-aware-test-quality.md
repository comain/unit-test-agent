# Implementation Plan: Behavior-Aware Test Quality For UTA

Spec: [`docs/spec-behavior-aware-test-quality.md`](spec-behavior-aware-test-quality.md)
Design: [`docs/design-behavior-aware-test-quality.md`](design-behavior-aware-test-quality.md)
Tracking: non-Jira UTA internal tool work.

## 1. Overview

Implement warning-only behavior-aware test-quality signals for UTA-generated or repaired tests. The change is sliced so prompts improve first, then scanner contracts/rules, then evidence/report rendering.

## 2. Architecture Decisions

- Shared evidence contract lives in `uta/engine/test_quality.py`; engine code stays contract-only and does not import language scanner modules.
- Python/Java rules live in `uta/language/python/test_quality.py` and `uta/language/java/test_quality.py`.
- Warnings are advisory and embedded in existing evidence JSON; repair progress uses existing `task_events.payload_json`; no DB migration.
- Reports render existing evidence only; they do not re-scan files on request.

## 3. Requirement Coverage

| Source | Requirement / design decision | Covered by task(s) | Notes |
| --- | --- | --- | --- |
| spec 4.1 | Behavior-aware prompting | Task 1 | Prompt text only. |
| spec 4.2 | Shared test quality signal | Task 2 | Engine contract and serialization. |
| spec 4.2 | Python/Java deterministic rules | Task 3 | Language adapters. |
| spec 4.3 | CI report visibility | Task 4 | Renderer/template. |
| spec 4.3 | Repair progress visibility | Task 5 | Existing `task_events.payload_json` carrier. |
| spec 4.4 | No gate/schema behavior change | Tasks 2-5 | Tests assert advisory only. |
| design 5.1 | Scan selected/generated Python test files after generation/repair | Task 6 | Wire into Python batch where test content/path is available. |
| design 7.3 | Java scanner rules are reusable but not workflow-wired | Task 3 | Unit tests only in this iteration. |
| design 8 | Bounded performance/security | Tasks 2-3 | 128 KB file cap, finding caps, snippet caps. |

## 4. Task List

### Phase 1: Prompt Foundation

#### Task 1: Harden generation and repair prompts

Description: Update Java and Python prompts to require behavior-focused assertions, boundary/failure cases, and mutation-survivor-driven fixes.

Acceptance criteria:

- [x] Python generation/coverage/mutation prompts discourage weak assertions and implementation mirroring.
- [x] Java planning/generation/mutation prompts discourage weak assertions and implementation-detail-only tests.
- [x] Prompt rendering tests pass.

Verification:

```bash
python3 -m pytest -q tests/test_prompt_render.py tests/test_prompt_prefix_stable.py
```

Dependencies: None.

### Phase 2: Scanner Contract And Rules

#### Task 2: Add shared test-quality contract

Description: Add engine-level finding model, scanner protocol, serialization helpers, and safe aggregation.

Acceptance criteria:

- [x] `TestQualityFinding` serializes to report-safe dicts.
- [x] Aggregation returns warning count and bounded warning list.
- [x] No language-specific rule logic exists in engine code.

Verification:

```bash
python3 -m pytest -q tests/test_test_quality.py
```

Dependencies: Task 1.

#### Task 3: Add Java and Python scanner rules

Description: Add conservative language-specific warning rules for weak assertions, verify-only tests, and simple formula mirroring.

Acceptance criteria:

- [x] Python weak assertions are flagged.
- [x] Java weak assertions are flagged by unit-tested reusable rules.
- [x] Behavior/value assertions are not flagged by the same rules.
- [x] Scanner exceptions are non-fatal.

Verification:

```bash
python3 -m pytest -q tests/test_test_quality.py
```

Dependencies: Task 2.

### Phase 3: Evidence And UI

#### Task 4: Render test-quality warnings in CI report

Description: Teach report detail and HTML template to display aggregate and per-target test-quality warnings from evidence.

Acceptance criteria:

- [x] Report detail exposes `testQuality.warningCount`.
- [x] HTML shows "Test quality signals" only when warnings exist.
- [x] Warnings do not affect `canCreateFixSession`, enforcement status, or callbacks.

Verification:

```bash
python3 -m pytest -q tests/test_api_trigger_report.py
```

Dependencies: Task 2.

#### Task 5: Render compact warnings in repair progress

Description: Surface compact warning text in repair progress target rows from `stage_completed` task-event payloads.

Acceptance criteria:

- [x] Progress row can display advisory warning text.
- [x] No class-task schema change is introduced.
- [x] Existing Java/Python target task tables still render without warnings.

Verification:

```bash
python3 -m pytest -q tests/test_api_trigger_fix_sessions.py
```

Dependencies: Task 4.

### Phase 4: Workflow Wiring

#### Task 6: Attach warnings to Python generated/repair evidence

Description: Wire scanner into Python batch result/evidence where generated test path or content is available. Java workflow wiring is intentionally excluded from this iteration.

Acceptance criteria:

- [x] Python generated/repaired target results include `testQuality` warnings when weak generated tests are detected.
- [x] Absence of scanner warnings leaves evidence unchanged except for empty/default omission.
- [x] Scanner failure does not fail verification.

Verification:

```bash
python3 -m pytest -q tests/test_python_batch_generation.py tests/test_api_trigger_report.py
python3 -m pytest -q tests/test_engine_layering.py tests/test_cross_language_contracts.py
```

Dependencies: Tasks 2-5.

## 5. Checkpoints

- After Task 1: prompt-only tests pass; no runtime behavior change.
- After Task 3: scanner unit tests pass; no workflow wiring yet.
- After Task 6: focused workflow/report tests pass; warnings visible but non-blocking.

## 6. Risks And Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Noisy heuristics | Warning fatigue | Conservative rules, warning-only rollout. |
| Scanner slows repair | Longer task runtime | Scan selected test files only; simple line-based rules. |
| Architecture drift | Language logic in core | Engine contract only; language rules in adapters. |
| User thinks warning means failure | Confusing report | Report wording says gates remain coverage/mutation. |

## 7. Completion Criteria

- [x] All tasks complete.
- [x] Requirement coverage matrix remains complete.
- [x] Focused tests pass.
- [x] Simplification pass completed.
- [x] Code review reports no unresolved Critical/Important findings.

## 8. Changelog

- 2026-07-08 — Initial plan.
- 2026-07-08 — Completed implementation checklist after focused verification (`189 passed`).

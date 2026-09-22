# Implementation Plan: Behavior-Aware Test Quality — Round 3

Spec: [`docs/spec-behavior-aware-quality-round3.md`](spec-behavior-aware-quality-round3.md)
Design: [`docs/design-behavior-aware-quality-round3.md`](design-behavior-aware-quality-round3.md)

## Overview

Improve UTA's Java/Python test-quality scanners so low-value non-behavior-oriented test warnings are more precise and actionable. The implementation reuses the existing evidence/report pipeline and changes only language-owned scanner logic plus tests.

## Architecture Decisions

- Keep `uta/engine/test_quality.py` as the neutral evidence contract.
- Keep Java/Python syntax rules in `uta/language/{java,python}/test_quality.py`.
- Keep report/progress/local output consuming opaque rule ids/messages.
- Use bounded regex/text heuristics, not parser or LLM scoring.

## Requirement Coverage

| Source | Requirement / design decision | Covered by task(s) | Notes |
| --- | --- | --- | --- |
| spec §5.1 | Suppress `happy-path-only` for credible Java failure/boundary tests | Task 1 | try/fail/catch, assertFalse/assertTrue failure style, negative names |
| spec §5.1 | Suppress `happy-path-only` for credible Python failure/boundary tests | Task 1 | try/except failure style, negative names |
| spec §5.2 | Add no-observable/smoke-only actionable warnings | Task 2 | Java/Python symmetric rules |
| spec §5.2 | Preserve weak/mock/mirroring rules with clearer messages | Task 2 | Existing ids remain stable |
| spec §5.3 | Severity policy: high-confidence warnings, info happy-path hint | Task 1, Task 2 | Verified in unit tests |
| spec §5.4 / design §4 | Keep architecture language-clean | Task 3 | Cross-language/contract tests |
| spec §5.5 / design §5 | Report/progress wording remains advisory and useful | Task 3 | Existing rendering plus message assertions |
| spec §11 | Focused verification commands pass | Task 4 | Final verification |
| design §6 | Bounded, non-blocking scanner behavior | Task 3 | Existing wrapper tests plus no new unbounded scans |

## Task List

### Task 1: Improve Failure/Boundary Evidence Detection

**Description:** Add Java and Python scanner fixtures first, then extend language-owned helpers so real failure/boundary tests suppress broad `happy-path-only` hints.

**Acceptance criteria:**

- [ ] Java try/fail/catch and assertFalse/assertTrue expected-failure styles suppress `java-happy-path-only-hint`.
- [ ] Java negative/boundary test names plus meaningful assertions suppress the hint.
- [ ] Python try/except failure styles suppress `python-happy-path-only-hint`.
- [ ] Python negative/boundary test names plus meaningful assertions suppress the hint.
- [ ] Existing positive happy-path-only fixtures still emit the hint.

**Verification:**

- [ ] `python3 -m pytest -q tests/test_test_quality.py`

**Dependencies:** None

**Files likely touched:**

- `tests/test_test_quality.py`
- `uta/language/java/test_quality.py`
- `uta/language/python/test_quality.py`

**Estimated scope:** Medium

### Task 2: Add More Actionable Low-Value Test Warnings

**Description:** Add high-confidence no-observable/smoke-only warnings and clarify existing weak/mock/mirroring messages without changing evidence transport.

**Acceptance criteria:**

- [ ] Java no-assertion tests emit `java-no-observable-assertion`.
- [ ] Python no-assertion tests emit `python-no-observable-assertion`.
- [ ] Smoke-only/weak-existence tests produce stronger warnings than only `happy-path-only`.
- [ ] Existing weak assertion, mock-only, and mirroring fixtures still pass.

**Verification:**

- [ ] `python3 -m pytest -q tests/test_test_quality.py`

**Dependencies:** Task 1

**Files likely touched:**

- `tests/test_test_quality.py`
- `uta/language/java/test_quality.py`
- `uta/language/python/test_quality.py`

**Estimated scope:** Medium

### Task 3: Verify Evidence Contract And Rendering Compatibility

**Description:** Ensure new warnings flow through existing Python CLI, Java CI selected-test scanner, report rendering, and task summary without schema changes.

**Acceptance criteria:**

- [ ] Evidence payload remains additive and uses the existing `TestQualityFinding` shape.
- [ ] Report rendering still shows advisory warnings and unchanged gate wording.
- [ ] Local Python marker/report tests still pass.
- [ ] No language-specific branching is added to report/progress core.

**Verification:**

- [ ] `python3 -m pytest -q tests/test_python_enforcement_cli.py`
- [ ] `python3 -m pytest -q tests/test_api_trigger_report.py`
- [ ] `python3 -m pytest -q tests/test_api_trigger_enforcement.py`
- [ ] `python3 -m pytest -q tests/test_reporter.py tests/test_tasks.py tests/test_cross_language_contracts.py`

**Dependencies:** Task 2

**Files likely touched:**

- `tests/test_python_enforcement_cli.py`
- `tests/test_api_trigger_report.py`
- `tests/test_api_trigger_enforcement.py`
- report/progress files only if tests prove wording needs a change

**Estimated scope:** Small

### Task 4: Final Verification And Documentation Sync

**Description:** Run focused verification, update todo statuses, and record any implementation deviation in the spec/design/plan changelog.

**Acceptance criteria:**

- [ ] All focused commands from the spec pass or any unrelated failure is documented.
- [ ] `docs/todo-behavior-aware-quality-round3.md` is complete.
- [ ] Spec/design/plan changelogs mention implementation completion.

**Verification:**

- [ ] `git diff --check`
- [ ] focused pytest commands from Tasks 1-3

**Dependencies:** Task 3

**Files likely touched:**

- `docs/todo-behavior-aware-quality-round3.md`
- `docs/spec-behavior-aware-quality-round3.md`
- `docs/design-behavior-aware-quality-round3.md`
- `docs/plan-behavior-aware-quality-round3.md`

**Estimated scope:** Small

## Checkpoints

### Checkpoint 1: Scanner Semantics

- [ ] Task 1 and Task 2 tests pass.
- [ ] No report/progress pipeline changes were needed.

### Checkpoint 2: Contract Compatibility

- [ ] Task 3 integration tests pass.
- [ ] Evidence remains advisory-only.

### Checkpoint 3: Ready For Simplify And Review

- [ ] Task 4 complete.
- [ ] Diff is clean and focused.

## Risks And Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| New regex creates false positives | Developers ignore advisories | Add realistic fixtures, keep high-confidence warnings narrow |
| Rule ids churn breaks report expectations | Test/report compatibility failure | Keep existing ids stable; only add new ids |
| Scanner gets slow | Report/progress latency | Bounded file reads and block-level scans only |
| Core report learns language rules | Architecture drift | Keep rule internals in language adapters |

## Open Questions

- None blocking. Defaults from the spec are accepted: no source-target branch analysis, no prompt changes unless tests force them, no dev-skills doc change unless CLI wording changes.

## Changelog

- 2026-07-09 — Plan completed: Tasks 1-4 implemented and focused verification passed.

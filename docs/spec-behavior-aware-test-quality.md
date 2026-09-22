# Spec: Behavior-Aware Test Quality For UTA

## 1. Objective

UTA should reduce the "green bar illusion" for AI-generated unit tests: high coverage with weak assertions that do not catch real behavior regressions. The feature adds behavior-aware guidance and lightweight test-quality evidence around generated/repair tests without changing the core coverage or mutation gate semantics.

This is non-Jira UTA internal tool work. No external Jira workflow is required for this iteration.

## 2. Users And Success Criteria

Primary users:

- Developers reading UTA CI reports and repair progress.
- UTA repair/generation workflows for Java and Python targets.
- Maintainers reviewing whether generated tests are meaningful enough to trust.

Success criteria:

- UTA prompts explicitly require behavior-focused assertions, boundary/failure cases, and mutation-survivor-driven repairs.
- UTA can scan generated or repaired test files for weak signals such as placeholder assertions, implementation-detail-only assertions, and implementation mirroring risk.
- CI reports and repair progress can show non-blocking test-quality warnings when weak generated tests are detected.
- The scanner is language-agnostic at the engine contract level and language-specific in adapters/rules.
- Existing coverage, mutation, strict test selection, progress, and cost accounting behavior remains compatible.

## 3. Source Discovery

| Surface | Candidate module/file | Decision | Reason |
| --- | --- | --- | --- |
| Prompt generation | `uta/prompts/python_generate_test.txt`, `uta/prompts/generate_test.txt`, `uta/prompts/plan_tests.txt` | In scope | Generation prompts control first-pass test intent. |
| Prompt repair | `uta/prompts/python_fix_coverage.txt`, `uta/prompts/python_fix_mutations.txt`, `uta/prompts/fix_coverage.txt`, `uta/prompts/fix_mutations.txt` | In scope | Repair prompts should convert coverage/mutation gaps into behavior assertions. |
| Report rendering | `uta/api_trigger/reporting.py`, `uta/api_trigger/templates/report.html`, `uta/api_trigger/templates/repair_progress.html` | In scope | Users need warnings where they read gate results. |
| Python batch evidence | `uta/language/python/batch.py`, `uta/language/python/enforcement.py` | In scope | Python repair/enforcement already emits structured evidence and selected test paths. |
| Java scanner rules | `uta/language/java/test_quality.py` | In scope as reusable library and unit tests | Java workflow wiring is not included until Java repair has a safe selected test-file carrier. |
| Java workflow evidence | `uta/language/java/batch.py`, Java delegated gate paths | Out of scope for first iteration | Java repair uses Maven/PIT and does not always expose generated test paths in structured evidence. Prompts still improve Java behavior. |
| Mutation candidate planner | `uta/engine/mutation_candidates.py`, `uta/engine/mutation_repair.py` | Out of scope | Existing mutation planning already supplies survivor context; this feature consumes that signal. |
| Test selection | `uta/language/python/test_selection.py`, Java target-test discovery | Out of scope | Strict test selection is already implemented and should not change in this iteration. |
| DB schema | `uta/tasks/db.py` | Out of scope | Store warnings in existing JSON/evidence payloads and report-derived details; no migration required. |
| Dev-skills local enforcement | `/path/to/dev-skills` | Out of scope for first iteration | This iteration affects UTA report/generation behavior. Local enforcement sync can follow if warnings become a hard local gate. |

## 4. Requirements

### 4.1 Behavior-Aware Prompting

- Generation prompts must ask for tests that verify intended behavior, not just current implementation.
- Prompts must require boundary and failure-mode cases when the source has conditionals, numeric thresholds, retries, status transitions, or error handling.
- Mutation repair prompts must tell the agent to interpret survivors as missing behavior checks.
- Prompts must discourage weak assertions as the main evidence: non-null, truthiness, length-only, method-called-once, or assertions that only duplicate production formulas.

### 4.2 Test Quality Signal

- Add a shared engine-level contract for test-quality findings. The engine must not import language modules; language-owned code invokes concrete scanners.
- Findings must include severity, category, message, file path, and evidence snippet or rule id.
- First iteration findings are warnings, not hard gates.
- The scanner must be deterministic and bounded to selected/generated test files: max 128 KB per file, max 20 findings per file, max 50 findings per target, max 160 chars per evidence snippet.
- Python and Java rules may differ, but report/progress handling must use one language-neutral structure.

### 4.3 Report And Progress Visibility

- CI report should show a "Test quality signals" section when warnings exist.
- Repair progress should show per-target warnings in or near the target task table using existing `task_events.payload_json` as the no-migration carrier.
- The UI text should make clear that coverage/mutation gates remain authoritative, while warnings identify tests that may still be weak.

### 4.4 Compatibility

- No DB migration. Repair progress uses `task_events.payload_json` keyed by `class_task_id`.
- No change to coverage/mutation pass/fail semantics.
- No change to model routing, token accounting, cost accounting, progress stages, or repair task lifecycle.
- Existing Java and Python tests must continue to pass.

## 5. Commands

Development verification:

```bash
python3 -m pytest -q tests/test_api_trigger_report.py tests/test_api_trigger_fix_sessions.py
python3 -m pytest -q tests/test_python_batch_generation.py tests/test_workflow.py tests/test_api_trigger_enforcement.py
```

Focused scanner tests:

```bash
python3 -m pytest -q tests/test_test_quality.py
```

## 6. Project Structure

- `uta/engine/test_quality.py` — shared finding model, bounds, serialization, and aggregation only.
- `uta/language/python/test_quality.py` — Python rule implementation.
- `uta/language/java/test_quality.py` — Java rule implementation, unit-tested but not wired into Java workflow in this iteration.
- `uta/api_trigger/reporting.py` — report detail mapping for warnings.
- `uta/api_trigger/templates/*.html` — CI and repair UI rendering.
- `tests/test_test_quality.py` — scanner unit tests.
- Existing prompt files under `uta/prompts/` — behavior-aware instructions.

## 7. Code Style

Prefer simple deterministic scanners over AST-heavy analysis in this iteration.

```python
def scan_python_test_quality(path: Path) -> list[TestQualityFinding]:
    text = read_bounded_test_file(path)
    return PythonTestQualityScanner().scan_text(path=path, text=text)
```

Rules should be small functions with explicit names and stable `rule_id` values, not a generic regex blob.

## 8. Testing Strategy

- Unit-test scanner rules with small Python/Java test snippets.
- Unit-test report rendering with structured warning evidence.
- Unit-test repair-progress rendering if warnings are exposed through class task detail.
- Existing prompt rendering tests must remain stable or be updated intentionally.
- No real E2E is required for this first warning-only iteration.

## 9. Boundaries

Always:

- Keep language-specific rules behind language modules.
- Keep shared reporting/evidence language-neutral.
- Treat warnings as advisory in this iteration.
- Preserve existing UTA gates and task lifecycle behavior.

Ask first:

- Turning warnings into hard gates.
- Adding new third-party parsing dependencies.
- Changing DB schema or repair task scheduling.
- Syncing local dev-skills enforcement as a hard requirement.

Never:

- Make coverage/mutation pass/fail depend on heuristic warnings in this iteration.
- Scan broad repositories or production source for test quality; only scan selected/generated test files.
- Log secrets or full large test files into reports.

## 10. Acceptance Criteria

- [x] Spec/design/plan docs exist and trace the feature scope.
- [x] Prompts are updated for behavior-focused tests and survivor-driven repairs.
- [x] Shared test-quality finding contract exists.
- [x] Python scanner rules are wired into generated/repair evidence.
- [x] Java scanner rules detect weak assertions in unit tests but are not wired into Java workflow yet.
- [x] Report renderer displays test-quality warnings when evidence includes them.
- [x] Tests cover scanner, evidence mapping, and report rendering.
- [x] Full focused test suite passes.

## 11. Open Questions

- Should warnings become hard gates later? Deferred; this iteration is warning-only.
- Should local dev-skills show the same warnings? Deferred until UTA warning format stabilizes.

## 12. Changelog

- 2026-07-08 — Initial spec for behavior-aware test quality warnings and prompt hardening.
- 2026-07-07 — Design-review round applied: mirroring rule requires input-derived arithmetic, scanner covers class-based/unittest-style tests, happy-path-only hint rules added (info severity).

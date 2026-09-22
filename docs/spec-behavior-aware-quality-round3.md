# Spec: Behavior-Aware Test Quality — Round 3 Scanner Signal Improvements

## 1. Objective

Round 1 introduced behavior-aware test-quality findings. Round 2 wired those findings through Java, Python, local output, repair progress, and CI reports. Round 3 improves the scanner signal itself so reports provide useful suggestions for low-value, non-behavior-oriented tests without producing noisy false positives.

This is non-Jira UTA internal tool work. No Jira workflow applies; docs use stable topic paths.

Primary users:

- Developers reading UTA CI reports and repair progress.
- Developers running local UTA/dev-skills enforcement.
- UTA repair/generation prompts that receive scanner feedback as advisory context.

Success means the scanner catches more clearly low-value tests, stops warning on credible failure/boundary tests, and explains each warning with a concrete improvement suggestion. Coverage and mutation gates remain authoritative.

## 2. Assumptions

1. Test-quality signals stay advisory in this round; they must not fail CI, local enforcement, or repair sessions.
2. The scanner remains deterministic and lightweight. It should use bounded file reads and local syntax/text heuristics, not LLM scoring.
3. Scanner output should stay language-neutral at the evidence-contract level; Java/Python-specific heuristics belong in language adapters.
4. UTA should scan only selected/generated/unit-evidence test files, not every test file in the repo.
5. We should improve precision first. A warning that developers do not trust is worse than a missed advisory hint.

## 3. Problem Statement

Recent Java and Python CI reports show `*-happy-path-only-hint` warnings as the dominant signal. Two Java examples were false positives: both files already contained explicit failure, invalid-input, null/default, remote-exception, and retry/delete-failure tests. The root cause is narrow failure-path recognition:

- Java currently recognizes only `assertThrows`, `expectThrows`, `catchThrowable`, `assertThatThrownBy`, `ExpectedException`, and `@Test(expected=...)`.
- Python currently recognizes only `pytest.raises`, `raises(...)`, and `assertRaises`.

Common test styles are missed:

- Java `try { ... fail(); } catch (...) { ... }`.
- Java `try { ... assertFalse(true); } catch (...) { ... }`.
- Python `try/except` with `pytest.fail`, `self.fail`, `assert False`, or `raise AssertionError`.
- Boundary/failure tests expressed by method names and assertions on status/error messages rather than exception helpers.

The second issue is recall quality. The scanner emits weak-assertion and mock-call warnings, but recent real reports are dominated by one broad "happy path only" hint. Developers need more specific suggestions such as "this test only verifies a mock call", "this test has no observable assertion", or "expected value mirrors the same formula as production code".

## 4. Source Discovery

| Surface | Candidate module/file | Decision | Reason |
| --- | --- | --- | --- |
| Shared evidence contract | `uta/engine/test_quality.py` | In scope | Keep existing additive evidence shape; add no language imports. If suggestions need structure, add neutral optional fields only. |
| Java scanner | `uta/language/java/test_quality.py` | In scope | Holds Java rules and the too-narrow `_has_failure_path_test`. |
| Python scanner | `uta/language/python/test_quality.py` | In scope | Holds Python rules and the too-narrow `_has_failure_path_test`. |
| Existing scanner tests | `tests/test_test_quality.py` | In scope | Primary regression suite for false positives, recall, and rule semantics. |
| Java CI selected-test scanner | `uta/language/java/enforcement_runner.py` | Verify only | Already attaches scanner evidence; no flow change expected. |
| Python enforcement | `uta/language/python/enforcement.py` | Verify only | Already attaches scanner evidence and local marker lines; no flow change expected unless marker wording changes. |
| Report rendering | `uta/api_trigger/reporting.py`, `uta/api_trigger/templates/report.html` | In scope for wording only | Existing section renders warnings; update messages/suggestions if needed. |
| Repair progress rendering | `uta/tasks/render.py`, `uta/api_trigger/templates/repair_progress.html` | Verify only | Existing carrier should keep working; no new lifecycle state. |
| Prompt templates | `uta/prompts/*.txt` | Out of scope by default | Round 2 already added behavior-context prompting. This round improves scanner quality, not prompt policy, unless wording must mention new categories. |
| Dev-skills repo | `/path/to/dev-skills` | Out of scope unless CLI marker changes | Scanner output is surfaced through UTA output. Sync docs only if user-facing marker wording or usage changes. |
| Full source semantic analyzer | new parser/call graph module | Out of scope | Too large and likely slow/noisy for this round. |

## 5. Requirements

### 5.1 Reduce False Positives In Failure/Boundary Detection

The scanner must suppress `*-happy-path-only-hint` when a test file contains credible failure, boundary, or negative-path evidence.

Java recognizers must include:

- `try`/`catch` blocks containing `fail(...)`, `Assert.fail(...)`, `Assertions.fail(...)`, `assertFalse(true)`, or `assertTrue(false)` before the catch path.
- Exception assertions already supported today.
- Test method names containing failure/boundary intent words such as `reject`, `invalid`, `error`, `exception`, `fail`, `blank`, `empty`, `null`, `missing`, `timeout`, `duplicate`, `terminal`, `retry`, or `fallback`, when paired with at least one meaningful assertion.
- Assertions on error/status/message fields in the same test body.

Python recognizers must include:

- `try`/`except` blocks containing `pytest.fail(...)`, `self.fail(...)`, `assert False`, or `raise AssertionError`.
- `pytest.raises`, `unittest.assertRaises`, and existing supported forms.
- Test function names containing failure/boundary intent words such as `rejects`, `invalid`, `error`, `exception`, `fail`, `empty`, `none`, `missing`, `timeout`, `duplicate`, `fallback`, or `retry`, when paired with at least one meaningful assertion.
- Assertions on error/status/message fields in the same test body.

Name-based evidence may suppress a broad happy-path hint, but it must not create a high-confidence positive finding by itself.

### 5.2 Add More Actionable Low-Value Test Categories

Add or strengthen findings that are more actionable than a broad happy-path hint:

- `*-no-observable-assertion`: test only calls code and has no assertion, exception expectation, snapshot/value check, or explicit interaction verification.
- `*-smoke-only-no-behavior`: test asserts only that execution does not crash, or only asserts a fixture/object exists.
- `*-mock-only-no-state`: test verifies collaborator calls but has no observable return/state/output assertion. Existing Java/Python mock-call rules should be kept and made clearer.
- `*-weak-existence-assertion`: test only checks non-null/truthiness/non-empty collection. Existing weak assertion rules should be kept and messages should say what behavior to assert instead.
- `*-mirrors-implementation`: expected value is derived from the same input expression/formula as the target behavior. Existing mirroring rules should be kept and expanded only when deterministic.

Each finding must include:

- `ruleId`
- `category`
- `severity`
- `message`
- `filePath`
- best-effort `line`
- short `evidence`

Messages must include a concrete suggestion, for example: "Assert the calculated amount, state transition, emitted error, or persisted field that users depend on."

### 5.3 Confidence And Severity Policy

Use severity consistently:

- `warning`: high-confidence low-value tests, such as no assertions, mock-only, weak existence-only, or clear formula mirroring.
- `info`: low-confidence file-level suggestions, especially happy-path-only hints.

`*-happy-path-only-hint` must remain `info` and should fire only when:

1. the file has at least one test;
2. no credible failure/boundary evidence exists; and
3. no stronger warning already explains the issue for most test blocks, or the hint adds useful file-level context.

### 5.4 Keep The Architecture Clean

- `uta/engine/test_quality.py` remains language-agnostic.
- Java/Python rule implementations stay in `uta/language/{java,python}/test_quality.py`.
- Report/progress/local output consume the same evidence contract and do not branch on rule internals.
- New rules must be named symmetrically across languages where behavior is equivalent.

### 5.5 User-Facing Report Improvements

Report and repair-page wording should help a developer act on scanner feedback:

- Keep the existing advisory disclaimer: coverage and mutation gates remain authoritative.
- Group top rules by rule id as today.
- Ensure each warning message is useful without reading source code.
- If a `happy-path-only` hint is emitted, wording must say it is a low-confidence prompt to consider boundary/failure tests, not proof the test is bad.

## 6. Commands

Focused verification:

```bash
python3 -m pytest -q tests/test_test_quality.py
python3 -m pytest -q tests/test_python_enforcement_cli.py
python3 -m pytest -q tests/test_api_trigger_report.py
python3 -m pytest -q tests/test_api_trigger_enforcement.py
```

Optional broader regression:

```bash
python3 -m pytest -q tests/test_reporter.py tests/test_tasks.py tests/test_cross_language_contracts.py
```

Manual production-style validation after implementation:

```bash
# Run one Java CI enforcement report and one Python CI enforcement report on beta/node2.
# Confirm warning wording, counts, and pass/fail gate behavior are unchanged.
```

## 7. Project Structure

- `uta/engine/test_quality.py` — shared finding/evidence contract.
- `uta/language/java/test_quality.py` — Java scanner rules and helper recognizers.
- `uta/language/python/test_quality.py` — Python scanner rules and helper recognizers.
- `uta/api_trigger/templates/report.html` — report rendering if message layout needs adjustment.
- `uta/api_trigger/templates/repair_progress.html` and `uta/tasks/render.py` — verify existing warning display remains adequate.
- `tests/test_test_quality.py` — main scanner regression tests.
- `tests/test_python_enforcement_cli.py`, `tests/test_api_trigger_report.py`, `tests/test_api_trigger_enforcement.py` — integration/contract regression tests.

## 8. Code Style

Prefer small deterministic helper functions over one large regex. Rule detectors should be readable and individually testable.

Example shape:

```python
def _has_java_failure_or_boundary_evidence(test_name: str, body: str) -> bool:
    return (
        _has_exception_assertion(body)
        or _has_try_catch_fail_pattern(body)
        or (_name_suggests_negative_path(test_name) and _has_meaningful_assertion(body))
        or _asserts_error_or_status(body)
    )
```

Do not introduce parser dependencies for this round. Regex/text scanning is acceptable only when bounded, deterministic, and tested with realistic examples.

## 9. Testing Strategy

Unit tests must cover:

- Java `try/fail/catch` suppresses `java-happy-path-only-hint`.
- Java `assertFalse(true)` or `assertTrue(false)` inside expected-failure tests suppresses the hint.
- Java negative/boundary method names plus meaningful assertions suppress the hint.
- Python `try/except` with `pytest.fail`, `self.fail`, `assert False`, and `raise AssertionError` suppresses the hint.
- Python negative/boundary function names plus meaningful assertions suppress the hint.
- A real positive happy-path-only fixture still emits `*-happy-path-only-hint`.
- No-assertion/smoke-only tests emit a stronger warning.
- Mock-only and weak-existence tests still emit their existing stronger warnings.
- Formula mirroring still fires only for input-derived expected values, not literal examples.
- Evidence aggregation and report rendering remain backward compatible.

Regression fixtures should include the observed false-positive shapes:

- Java failure tests using `try { ... fail(); } catch (...) { ... }`.
- Java tests using `assertFalse(true)` in the unexpected-success branch.
- Python try/except failure tests without `pytest.raises`.

## 10. Boundaries

Always:

- Keep test-quality findings advisory.
- Keep coverage and mutation as the only gates.
- Keep scanner output deterministic.
- Keep language-specific logic behind language adapters.
- Prefer high-confidence warnings over broad hints.

Ask first:

- Making any test-quality finding fail CI.
- Adding parser or LLM dependencies.
- Scanning all repo tests instead of selected evidence tests.
- Changing dev-skills local enforcement docs.

Never:

- Mark a report failed solely because of scanner findings.
- Let scanner failures block enforcement.
- Log sensitive test contents beyond short bounded evidence snippets.
- Add language-specific rule branching to report/progress core rendering.

## 11. Acceptance Criteria

- The two observed Java false-positive classes no longer emit `java-happy-path-only-hint` when scanned from their existing failure/boundary test patterns.
- Equivalent Python try/except failure tests no longer emit `python-happy-path-only-hint`.
- A simple happy-path-only Java test still emits `java-happy-path-only-hint`.
- A simple happy-path-only Python test still emits `python-happy-path-only-hint`.
- No-assertion/smoke-only tests produce a more specific warning than `happy-path-only`.
- Existing weak assertion, mock-only, and mirroring tests remain covered.
- CI report and repair progress still render advisory warnings without changing pass/fail state.
- Focused verification commands in §6 pass.

## 12. Open Questions

1. Should this round scan source-target files to decide whether the target actually has error branches before emitting `happy-path-only`? Default: no; keep this lightweight and treat the hint as low-confidence.
2. Should scanner findings be fed back into repair prompts immediately, or only shown to users? Default: no prompt changes this round unless design finds existing prompt plumbing already consumes the evidence.
3. Should dev-skills usage docs mention test-quality advisories? Default: no unless user-facing CLI wording changes.

## 13. Changelog

- 2026-07-09 — Initial round-3 spec: improve scanner precision/recall, reduce `happy-path-only` false positives, and add more actionable low-value test categories.
- 2026-07-09 — Implementation completed: Java/Python scanners recognize common failure-path styles, add no-observable assertion warnings, preserve advisory-only gate semantics, and focused verification passed.

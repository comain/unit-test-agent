# Plan: Behavior-Aware Test Quality — Round 2

Spec: `docs/spec-behavior-aware-quality-round2.md` · Design: `docs/design-behavior-aware-quality-round2.md`
Non-Jira uta internal work — no Jira artifacts. Java coding-guidelines task: N/A (uta is a Python codebase; generated-Java style is owned by prompts, unchanged in structure this round).

## Dependency Order

```
T0 (baseline) ─→ T2 (Java wiring) ─→ T3 (report seam)
T1 (wrapper)  ─┘
T4 (local surfacing)   — independent
T5 (intent prompts) ─→ T6 (spec-context channel)
T7 (docs) ─→ T8 (full verification)
```

## Tasks

- [ ] **T0 — Green baseline**: diagnose the 4 failing `tests/test_workflow.py` tests (delegated-gate/RDC-repair area). Fix if code-caused; otherwise record cause in this plan.
  - Acceptance: `python3 -m pytest -q tests/test_workflow.py` green, or a written environmental explanation.
  - Verify: pytest run.
  - Files: TBD by diagnosis.

- [ ] **T1 — Java evidence wrapper + attach helper** (D1): `scan_java_test_quality_evidence(repo, paths)` and `attach_java_test_quality(repo_path, results)` in `uta/language/java/test_quality.py`; missing-file silent skip; exception → one `java-test-quality-scan-failed` info finding; path preference `candidate_test_file_paths` else `test_file_path`; skip results already carrying `testQuality`; never raise.
  - Acceptance: unit tests for existing/missing/weak/exception/path-preference/skip-if-present.
  - Verify: `pytest -q tests/test_test_quality.py`.
  - Files: `uta/language/java/test_quality.py`, `tests/test_test_quality.py`.

- [ ] **T2 — Wire two call sites** (D1): `_sync_task_results_if_available` enriches scoped results before sync; `run_java_batch_generation` enriches before returning; both wrapped so failure never breaks sync/run.
  - Acceptance: task-event payload for a weak Java test contains `testQuality`; rate-limited/error rows gain nothing; exit path skips already-enriched.
  - Verify: `pytest -q tests/test_workflow.py tests/test_tasks.py`.
  - Files: `uta/graph/nodes.py`, `uta/language/java/batch.py`, tests.

- [ ] **T3 — Java CI report seam** (D2): fix-session live-task refresh copies `testQualityWarningCount`/`testQualityTopRules` using `_test_quality_by_class_task_id` over `latest_events(limit=200)`; report renders fix-session warning info and feeds "Test quality signals" when `structured.testQuality` absent.
  - Acceptance: report detail for a Java task with a warning-bearing fix session shows the signals section; Python path unchanged.
  - Verify: `pytest -q tests/test_api_trigger_report.py tests/test_api_trigger_fix_sessions.py`.
  - Files: `uta/api_trigger/service.py`, `uta/api_trigger/reporting.py`, `uta/api_trigger/templates/report.html`, `uta/tasks/render.py` (helper export), tests.

- [ ] **T4 — Local surfacing** (D3): marker line in `format_evidence_markers` when `warningCount > 0`; `test_quality_warning_count`/`test_quality_top_rule` in `uta/reporting/reporter.py` per-class summary.
  - Acceptance: marker emitted/omitted correctly; reporter summary fields present; exit codes unchanged.
  - Verify: `pytest -q tests/test_python_enforcement_cli.py` + reporter test.
  - Files: `uta/language/python/enforcement.py`, `uta/reporting/reporter.py`, tests.

- [ ] **T5 — Intent-source priority prompts** (D5): priority list (spec context → docs/comments → existing tests → callers/collaborators → implementation-as-characterization) in plan/generate prompts; one line in the four fix prompts.
  - Acceptance: prompt render tests assert the instruction; existing render tests stay green.
  - Verify: `pytest -q tests/test_prompt_render.py`.
  - Files: `uta/prompts/plan_tests.txt`, `generate_test.txt`, `python_generate_test.txt`, `fix_*.txt`, `python_fix_*.txt`, tests.

- [ ] **T6 — Spec-context channel** (D4): `--spec-context` on `uta run`; `specContext` on API trigger request; resolution (file-or-text), 16 KB bound + truncation marker; threading via batch requests/state; delimited block injected after `{# CACHE_BOUNDARY #}` in plan/generate prompts; byte-identical rendering when absent.
  - Acceptance: end-to-end unit tests (CLI parse → state → prompt); byte-identity test; truncation test; API model accepts the field.
  - Verify: `pytest -q tests/test_prompt_render.py tests/test_python_batch_generation.py` + CLI/api model tests.
  - Files: `uta/cli.py`, `uta/api_trigger/models.py` (+ service threading), `uta/language/java/batch.py`, `uta/language/python/batch.py`, `uta/graph/nodes.py`, prompt files, tests.

- [ ] **T7 — Docs**: usage notes (marker format, `--spec-context`) in `docs/production-usage.md` or a round-2 usage section; spec/design changelogs updated; acceptance boxes checked.
  - Verify: docs render, checklist complete.

- [ ] **T8 — Full verification**: full focused suite green; summary of results recorded in todo doc.
  - Verify: suites from design §9 step 7.

## Requirement Coverage Matrix

| Spec acceptance criterion | Task(s) |
| --- | --- |
| 4 workflow failures diagnosed/fixed | T0 |
| Java wrapper with skip semantics | T1 |
| Chokepoint enrichment, no noise on error rows | T2 |
| Java warnings in progress + CI report | T2, T3 |
| Local marker line + run summary counts | T4 |
| Prompts name intent sources | T5 |
| Spec-context end-to-end, bounded, byte-identical absent | T6 |
| Full focused suite green | T8 |
| Design decisions D1–D6 | D1→T1/T2, D2→T3, D3→T4, D4→T6, D5→T5, D6→T0 |

No orphan tasks; every task traces to a spec criterion or design decision.

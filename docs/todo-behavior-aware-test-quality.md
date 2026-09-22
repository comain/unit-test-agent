# Todo: Behavior-Aware Test Quality For UTA

Plan: [`docs/plan-behavior-aware-test-quality.md`](plan-behavior-aware-test-quality.md)

- [x] Task 1: Harden generation and repair prompts.
- [x] Task 2: Add shared test-quality contract.
- [x] Task 3: Add Java and Python scanner rules.
- [x] Task 4: Render test-quality warnings in CI report.
- [x] Task 5: Render compact warnings in repair progress.
- [x] Task 6: Attach warnings to Python generated/repair evidence.

## Verification Log

- `python3 -m pytest -q tests/test_test_quality.py tests/test_prompt_render.py::test_behavior_aware_test_quality_rules_render_in_core_prompts tests/test_api_trigger_report.py::test_report_shows_test_quality_warnings tests/test_tasks.py::test_sync_results_exposes_test_quality_warnings_in_status_payload` -> 12 passed.
- `python3 -m pytest -q tests/test_python_enforcement_cli.py tests/test_python_batch_generation.py tests/test_cross_language_contracts.py tests/test_engine_layering.py` -> 75 passed.
- `python3 -m pytest -q tests/test_test_quality.py tests/test_prompt_render.py tests/test_api_trigger_report.py tests/test_tasks.py tests/test_python_enforcement_cli.py tests/test_python_batch_generation.py tests/test_cross_language_contracts.py tests/test_engine_layering.py` -> 189 passed.

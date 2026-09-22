# Plan — Diff-unrelated failing-test exclusion

Spec: `docs/spec-baseline-red-exclusion.md` · Design: `docs/design-baseline-red-exclusion.md` · ADR-015

Each task is independently committable and leaves the suite green. T1–T3 are pure and
change no behavior; the feature becomes observable at T5, and is off by default.

## T1 — Uncapped failing-test-class reader
`enforcement_runner/parsing.py` — `_failed_surefire_test_classes(repo_path)` reading
`TEST-*.xml` (always written, carries `name`/`failures`/`errors` as attributes), uncapped
and de-duplicated. `_failed_surefire_tests(limit=12)` untouched.
**Done when**: a fixture tree with 140 failing suites returns 140 FQNs; no failures → `[]`;
malformed XML skipped, never raised.

## T2 — The rule
New `enforcement_runner/failing_test_scope.py` — `FailingTestPartition` and
`partition_failing_tests(failing, changed_java, changed_production_java, repo_path, generated_tests)`.
Excluded iff not diff-touched **and** not referencing a changed class
(`_java_test_targets_type`).
**Done when**: all four combinations map as specified; UTA-generated tests are always
retained; `reason` is set for every empty-exclusion path.

## T3 — Command shaping
`planning.py` — positive Surefire inventory and PIT exclusion shapers. Use a deterministic
includes file on Surefire 2.13+ and guarded positive `-Dtest` on older versions;
FQN-validate every name and never emit a negative-only selector.
**Done when**: empty input → byte-identical command; only first-pass passing tests remain;
an oversized legacy selector is refused; metacharacters are dropped.

## T4 — Stale-artifact clearing
Reuse `_clear_jacoco_artifacts` (`maven/jacoco.py:32`) per affected module before a re-run.
**Done when**: a test proves `jacoco.exec` and `surefire-reports` are gone for each
affected module before the second run starts.

## T5 — Runner wiring + flag
`enforcement_runner/__init__.py` (`full_run` branch) and
`ci_failing_test_exclusion_enabled` in `uta/shared/config.py`.
**Done when**: flag off → byte-identical to today and no second run; flag on with no
failures → no second run; flag on with excludable failures → clear, re-run, classify;
would-empty-module-suite → skipped with reason.

## T6 — Evidence and verdict
`evidence.py`, `classification.py` — the four keys, attached regardless of return code
(the false-green case exits 0); coverage shortfall attributed; docstring order updated per D7.
**Done when**: keys are additive, present on a zero-exit run, and a coverage failure after
exclusion reads as *"covered only by failing tests unrelated to this change"*.

## T7 — Repair targets
`uta/language/java/ci.py` — excluded classes never reach `repair_target_ids`.
**Done when**: a task whose failures are all excluded produces no repair targets from them.

## T8 — Report rendering
`uta/app/context.py`, `uta/app/templates/report.html` — show excluded/retained and reason.
**Done when**: a run with exclusions renders both lists; a run without is unchanged.

## T9 — Production fixture test
`tests/` — from task `2d91c62a65f24aafb8eb9202c9001344`: 140 classes excluded, both
properties carry the same set.

## T10 — Docs
`docs/usage-baseline-red-exclusion.md`; decide and record whether AGENTS.md's dev-skills
gate sync binds (this changes gate semantics for UTA runs, not for local `mvn`).

# Spec — Diff-unrelated failing-test exclusion for Java enforcement

Status: approved (simplified rule, 2026-08-31)
Slug: `baseline-red-exclusion` (filenames kept stable; the rule is now an estimate, see §3)
Related: `docs/spec-java-ci-full-run.md`, `docs/test-enforce-usage.md`, TASK-82787
ADR: [ADR-015](decisions/ADR-015-baseline-red-test-exclusion.md)

Tracking: none. `unit-test-agent` is a personal project, not UTA feature work, so the
Jira/release-approval steps of the dev-skills workflow and the `doc/<JIRA>`
naming are deliberately skipped; artifacts follow this repo's `docs/spec-*.md`
convention.

## 1. Objective

**Both enforcement gates must judge the same test set.** Today they do not:

- Surefire runs with `-Dmaven.test.failure.ignore=true`, so a **failing** test still
  contributes every line it executed before failing to the JaCoCo diff-coverage number.
- PIT, via `parseSurefireConfig` → `skipFailingTests`, **drops** those same tests from
  mutation analysis.

Coverage is therefore the lenient gate, and it can pass on evidence the mutation gate
has already rejected. This change gives both stages the same exclusion list.

### Evidence

Production task `2d91c62a65f24aafb8eb9202c9001344` (`tempoon_cigarette_man`,
branch `TASK-40997-20260828`):

```
Tests run: 700, Failures: 0, Errors: 140    ← build continued (failure.ignore)
[test-enforcer] pitest.targets=1            ← obligation is one class
覆盖率 100.00% (1/1)                         ← coverage gate passed
PIT >> Sending 568 test classes to minion
PIT >> MINION : ExceptionInInitializerError ... connection timed out /10.0.0.12:9034
```

Two things are demonstrated and one is not, and the difference matters:

- **Demonstrated — the gates disagree.** PIT dropped 140 classes that JaCoCo counted.
- **Demonstrated — cost.** 568 test classes enter PIT's pre-scan for a one-class
  obligation; the run was killed at ~40 minutes inside it.
- **Not demonstrated — a coverage false green.** These 140 classes die at context init
  and cover nothing, so excluding them would not move this task's coverage number. The
  false green is possible by construction but has not yet been observed in production.
  Reporting the excluded set is what will confirm or refute it.

### Non-goals

PIT `targetTests` narrowing, rc-143/timeout classification, the PIT stall watchdog,
upstreaming into test-enforcer, and the wconfig reachability fix. Reasons in §7.

## 2. Scope discovery

Searched: `uta/language/java/enforcement_runner/**`, `uta/language/java/ci.py`,
`uta/language/java/maven/{pitest,jacoco}.py`, `uta/enforcement/enforcement.py`,
`uta/app/{workspace,context}.py`, `uta/app/templates/report.html`, `uta/shared/config.py`,
and the pitest-maven 1.15.0 plugin descriptor.

| Candidate | Role | Decision | Reason |
| --- | --- | --- | --- |
| `enforcement_runner/__init__.py` (`full_run` branch, ~L121) | Builds the CI verification command | **In scope** | The only place a module-wide exclusion attaches to the verdict run |
| `enforcement_runner/planning.py` | Owns the `_with_*` command shapers and `_java_test_targets_type` | **In scope** | New shaper plus reuse of the existing test↔class relation |
| `enforcement_runner/parsing.py` | `_failed_surefire_tests(limit=12)` reads `*.txt` reports | **In scope** | Needs an uncapped, authoritative class-set reader (from `TEST-*.xml`) |
| `enforcement_runner/evidence.py` | Publishes `failedSurefireTests` | **In scope** | New keys join the same contract; must attach regardless of return code |
| `enforcement_runner/classification.py` | Turns a run into a verdict | **In scope** | Coverage shortfall caused by exclusion must be attributed, not read as a weak generated test |
| `uta/language/java/maven/jacoco.py` `_clear_jacoco_artifacts` | Deletes stale `jacoco.exec`, jacoco site, surefire reports | **In scope (reuse)** | JaCoCo's agent appends; without clearing, the re-run would report run 1's coverage |
| `uta/language/java/ci.py` (`_failed_surefire_tests_from_evidence` → `repair_target_ids`) | Feeds failing classes to the repair loop | **In scope** | An excluded class must not become a repair target |
| `uta/app/context.py`, `uta/app/templates/report.html` | Hand-render named evidence keys | **In scope** | New keys are invisible otherwise |
| `uta/shared/config.py` | Command + budgets | **In scope** | One flag lives here |
| `uta/language/java/enforcement.py` (`run_java_enforcement`) | Standalone `java-enforce` envelope, drops `result.evidence` | **Out of scope** | Pre-existing gap; the new keys are absent there as all others are |
| `uta/language/java/maven/pitest.py` (`run_pitest`) | Repair-loop PIT invocation | **Out of scope** | Already runs a self-selected narrow test set |
| `uta/language/python/**` | Python enforcement (`mutmut`) | **Out of scope** | No JaCoCo/PIT split; the asymmetry has not been shown there |
| test-enforcer Maven plugin | Could own the rule so RDC matches | **Out of scope** | ADR-015; needs a plugin release + `uta_dev_gate.py` sync |
| Base-ref probe / worktree / cache | Would *measure* rather than estimate | **Out of scope** | ADR-015 rejected alternative: third Maven run, unbounded compile, and per-task `fresh=True` workspaces defeat any in-checkout cache |

### Verified facts

- pitest-maven 1.15.0 exposes `${excludedTestClasses}` as a user property on
  `mutationCoverage`; `skipFailingTests` has none (TASK-82787).
- PIT's `parseSurefireConfig` reads the Surefire plugin configuration, not `-Dtest`, so
  both properties are required.
- Surefire's `-Dtest` in this repo is fed **simple names** (`_surefire_test_selector`,
  `planning.py:56`); PIT's `excludedTestClasses` takes FQN globs. The two properties
  therefore denote the same *set* in different spellings — never the same bytes.
- JaCoCo's agent appends to `jacoco.exec` by default, and `_clear_jacoco_artifacts`
  already exists for exactly this hazard.

## 3. Behavior

### 3.1 The rule

After the enforcement run, a failing test class is **excluded** when both hold:

1. its source file is not in `<base_ref>...HEAD`, and
2. it does not reference any changed production class, per the existing
   `_java_test_targets_type` relation (construction, class literal,
   `extends`/`implements`/`instanceof`, typed declaration).

Everything else is **retained**: a test the diff touched, a test naming a changed class,
and any test UTA generated this task. This is an estimate of "was already red", not a
measurement; its blind spot (a test broken by the change through DI/reflection) is
stated in ADR-015 and is why the report always names the excluded set.

### 3.2 Applying it

Re-run enforcement with both stages excluded consistently:

- the complete first-pass Surefire inventory minus excluded failures — JaCoCo stage;
  Surefire 2.13+ reads a UTA-owned `surefire.includesFile`, while older versions use a
  size-guarded positive `-Dtest=<SimpleName>,…`
- `-DexcludedTestClasses=<fqn>,…` — PIT stage

Before the re-run, stale build output is cleared per affected module with
`_clear_jacoco_artifacts` — otherwise JaCoCo appends and coverage cannot change.

The positive inventory replaces caller-supplied Surefire selectors; PIT exclusions are
merged with caller-supplied `-DexcludedTestClasses=` values.
If exclusion would empty a module's test set, exclusion is skipped for that module
(PIT's `failWhenNoMutations` would otherwise fail the build) and the reason is recorded.

### 3.3 Verdict and evidence

- Coverage may legitimately drop; that is the correction. The summary must say
  *"N diff lines are covered only by failing tests unrelated to this change"*.
- Retained failing classes stay exactly as visible as today.
- Excluded classes are never handed to the repair loop as targets (`ci.py`).
- Evidence gains `excludedFailingTestClasses`, `retainedFailingTestClasses`,
  `exclusionReason`, `coverageBeforeExclusion`, attached **regardless of return code**
  (the false-green case exits 0, where today no Surefire evidence is attached at all).

## 4. Acceptance criteria

1. A failing test class untouched by the diff and not referencing a changed class is
   excluded from both properties in the emitted command.
2. A failing test class touched by the diff, or referencing a changed class, or
   generated by UTA this task, is **never** excluded.
3. The two properties denote the same set — same classes, `-Dtest` in simple names,
   `excludedTestClasses` in FQNs — asserted by a unit test.
4. The uncapped class reader returns every failing class; `_failed_surefire_tests`'s
   12-record presentation cap is untouched.
5. Stale `jacoco.exec` and `surefire-reports` are cleared for each affected module
   before the re-run; a test proves the second run cannot inherit the first's coverage.
6. With no failing tests, or with the flag off, the emitted command is byte-identical to
   today's and no re-run happens.
7. Excluding a module's entire test set is refused, with a recorded reason.
8. Excluded classes do not appear in `repair_target_ids`.
9. Replaying task `2d91c62a65f24aafb8eb9202c9001344`'s report shapes classifies all 140
   `ExceptionInInitializerError` classes as excluded, and the re-run command carries them.

## 5. Testing strategy

Unit tests in `tests/` with an injected `run_command` — no Maven, no git, no network:
rule (both conditions, all four combinations), command shaping and merge, name-form
divergence, uncapped reader against `TEST-*.xml` fixtures, artifact clearing, empty-suite
refusal, flag off, repair-target filtering, and a fixture built from the production task's
report shapes. Full `pytest` green before commit.

## 6. Boundaries

**Always**: keep Java specifics under `uta/language/java/` (AGENTS.md); commit to `main`;
keep evidence keys additive.

**Ask first**: raising `ci_enforcement_timeout_seconds`; changing
`-Dmaven.test.failure.ignore=true`; editing a target repository's POM.

**Never**: exclude a test the diff touches or that names a changed class; exclude
silently — every excluded class appears in the report.

## 7. Deferred, with reasons

| Deferred | Why not now |
| --- | --- |
| Measuring the baseline at the base ref | ADR-015: third Maven run, unbounded compile, no viable cache. Revisit if the estimate misfires |
| rc 143 / signalled-run classification | Real (the 40-min stall is misreported and drives a pointless repair session) but independent of gate semantics |
| PIT stall watchdog, `-DtimeoutConstant`, Java-path process-group termination | Cost control; note that `run_bounded_command` orphans forked JVMs on timeout |
| PIT `targetTests` narrowing | Test strength is non-monotone in the test set; needs a measured coverage index first |
| Upstreaming into test-enforcer | Plugin release + `uta_dev_gate.py` sync |
| wconfig reachability on the runner | Environment root cause of the 140 errors; infra, not code |

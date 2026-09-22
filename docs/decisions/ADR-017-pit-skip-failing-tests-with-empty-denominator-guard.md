# ADR-017: Drop PIT's Baseline-Red Tests, and Refuse the Score They Can Fake

## Status

Accepted. Supersedes the 2026-08-31 ship review NO-GO recorded in
`fd/maven-plugins` `6c1ae11` (branch `TASK-82787-20260831`).

## Date

2026-09-16

## Context

PIT runs in two passes. Pass 1 executes every test matching `targetTests` to build
a coverage map, then `DefaultCoverageGenerator.verifyBuildSuitableForMutationTesting`
aborts the whole run if any of them failed. Pass 2 is where "only related tests"
becomes true: `DefaultTestPrioritiser.pickTests` selects per mutant from the coverage
map. A changed class with a green test therefore gets no verdict at all when an
unrelated test in the same module is red — the run dies before pass 2 exists.

Production report `83225bfc60124d51b0b75dd4abe856b5` (`cvs_usercenter`,
`user-center-biz`) is the case. The mutated class `UserEnterTempSessionEventHandler`
had 100% diff coverage and its own green test. PIT aborted on
`RedisShardedTaskQueueTest`, a different package, which **passed Surefire** — 34
tests, 0 failures, 0 skipped, 0.499s — and failed only inside PIT's minion.

Two facts decided this ADR:

- **PIT's environment is not Surefire's.** `SurefireConfigConverter` carries over
  exactly `groups`, `excludedGroups`, `excludes` and `testFailureIgnore`. `argLine`,
  `systemPropertyVariables`, `forkCount`/`reuseForks` and `workingDirectory` are
  dropped, and all 891 test classes share one instrumented JVM. The failing tests
  are timing-sensitive: `leaseTtlMs = 30` gives a 10 ms heartbeat
  (`max(1, leaseTtlMs / 3)`) under `assertEquals(1, logs.size())`.
- **The failures are not stable.** Across the task's three PIT invocations, one run
  failed 3 methods and two failed 1. Anything that must name the failing tests in
  advance cannot terminate on a suite like this.

## Decision

**Set PIT's `skipFailingTests`, and fail any run whose mutation score has an empty
denominator.**

`skipFailingTests` is PIT's own answer: `DefaultCoverageGenerator$1.accept` is
`if (!isGreenTest() && skipFailingTests()) { drop } else { calculateClassCoverage(result); }`,
so a pass-1 failure never reaches `failingTestDescriptions` and never aborts. It needs
no prediction, so it terminates whatever the suite does.

The reason it was rejected in August stands on its own and is fixed separately: test
strength is killed/covered, so dropping every test that covers a changed class empties
the denominator and PIT reports 100% of nothing, while JaCoCo still counts the lines
those tests executed before failing. Both gates then pass on no evidence. UTA now
refuses that shape: a PIT statistics block with `generated > 0` and
`no coverage >= generated` is a failure, not a pass.

This is the same guard the Python path already carries
(`uta_enforce_core/evidence.py`, where `rate` is forced to `0.0` on
`mutation_no_tests` instead of taking the `100.0 if denominator == 0` branch).

## Mechanism

`skipFailingTests` has **no user property** in the pitest-maven 1.15.0 descriptor
(`<skipFailingTests implementation="boolean" default-value="false"/>`), so
`-DskipFailingTests=true` is silently ignored. That silent ignore is why
`-Dmaven.test.failure.ignore=true` never reached PIT on the observed report: PIT reads
`<testFailureIgnore>` from the Surefire *plugin configuration DOM*, a user property is
not in that DOM, and no POM in the repository declares one.

UTA's own Maven core extension binds it. `PitRuntimeCompatibility.afterProjectsRead`
already rewrites PIT's configuration DOM before the reactor executes anything, and
already injects `${...}` values for `targetTests` and `excludedTestClasses`;
`skipFailingTests` joins that list. `afterMojoExecutionSuccess` then reads the value
back off the PIT mojo and fails the build if it did not take, and prints it on the
`[uta-pit-compat] verified module=…` line.

This retires the TASK-82787 design note that PIT's own DOM is unusable. That was true of
a mojo at `initialize` — Maven computes a module's mojo configuration before its first
mojo runs — and is not true from `afterProjectsRead`.

No plugin release is required, and no POM in the target repository is modified.

## Consequences

- A module with legacy rot yields a mutation verdict for its changed class instead of
  none. That was the whole point of TASK-82787.
- The empty-denominator guard reads only when the compat extension reports the flag
  actually applied. Without it, an all-uncovered statistics block is the ordinary
  consequence of PIT mutating a whole class whose changed lines are covered and whose
  remainder is not, which this gate has accepted since 2026-05
  (`c760910`). Existing behaviour is unchanged when the flag is absent.
- **Known over-rejection.** With the flag on, a class whose *changed* lines yield no
  mutants at all — changed lines that are field declarations or logging, say — produces
  an all-uncovered block for genuinely innocent reasons and is now failed. The block is
  module-scoped, so nothing in it distinguishes that from the vacuous case. Failing
  closed is the right default for a gate, and the summary names what was measured;
  revisit if it is observed to misfire.
- Diff **coverage** stays lenient: JaCoCo still credits lines executed by a test that
  later failed. The guard catches the fully-vacuous case, not the partial one. ADR-015's
  diff-unrelated exclusion remains the answer there and is orthogonal to this.

## Alternatives rejected

**Collect PIT's failing classes from a first run, then re-run with
`-DexcludedTestClasses`.** Uses ADR-015's shipped mechanism and excludes before pass 1,
so the class never runs and never enters a denominator — strictly the more precise
instrument. Rejected because it costs a second enforcement run on a path that already
runs ~40 minutes, and because it must name the failing tests in advance: measured here,
the failing set differed between runs of the same workspace, so the exclusion loop has
no bound. Still the right tool for a *stably* red class, and the two compose.

**Ship the test-enforcer branch.** `FilterDiffMojo.configureSurefireTestFailureIgnore`
does the same job through Surefire's DOM. Rejected for now: it needs a plugin release
and a `uta_dev_gate.py` sync, and it reaches PIT by the longer route. Worth revisiting
so RDC and UTA behave identically, which is where this belongs eventually.

**Leave the abort in place.** Rejected: it is the status quo that produced a report
whose stated failure — "PIT baseline tests were not green" — named a test the change
never touched, could not be repaired by generating tests for the changed class, and
sent a one-click fix session to `rerun_failed`.

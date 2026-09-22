# ADR-015: Exclude Diff-Unrelated Failing Tests From Both Enforcement Stages

## Status

Accepted

## Date

2026-08-31

## Context

A UTA enforcement run executes two gates over one Maven module suite:

- JaCoCo diff line coverage, produced by the Surefire run
- PIT test strength, produced by `pitest:mutationCoverage` in the same `verify`

UTA runs that suite with `-Dmaven.test.failure.ignore=true` (TASK-82787: without it a
module with red legacy tests never reaches PIT at all). That flag has a consequence the
two gates do not share:

- **JaCoCo counts a failing test.** Every line it executed before failing is recorded.
  A diff line reached only by a red test is reported as covered.
- **PIT does not.** `parseSurefireConfig` maps `testFailureIgnore` onto
  `skipFailingTests`, so PIT drops those tests from coverage and kill counting.

The gates therefore judge different test sets, and coverage is the lenient one.
Observed on task `2d91c62a65f24aafb8eb9202c9001344` (`tempoon_cigarette_man`): 140 test
classes failing at Spring/Dubbo context init, 568 test classes shipped to the PIT
minion for a one-class obligation, diff coverage reported 100% (1/1).

## Decision

**Exclude from both stages every failing test class that the branch diff does not
relate to.** A failing test class is excluded when both hold:

1. its source file is not in `<base_ref>...HEAD`, and
2. it does not reference any changed production class, by the existing relation in
   `_java_test_targets_type` (`planning.py:226` — construction, class literal,
   `extends`/`implements`/`instanceof`, or a typed declaration).

Anything else — a test the diff touched, a test that names a changed class, a test UTA
generated this task — is never excluded and stays in the verdict.

The exclusion is applied to both Maven stages:

- a positive inventory of first-pass passing classes — restricts Surefire and JaCoCo
- `-DexcludedTestClasses=<class>,…` — removes them from PIT

Both are required. PIT's `parseSurefireConfig` reads the Surefire *plugin
configuration*, not the `-Dtest` user property, so neither alone reaches both stages.
`excludedTestClasses` is usable from the command line because pitest-maven 1.15.0
declares it with a user property (plugin descriptor, `mutationCoverage` goal), unlike
`skipFailingTests`, which does not (TASK-82787).

## Consequences

- Both gates observe the same test set, so a coverage pass and a mutation pass become
  statements about the same evidence.
- Diff coverage becomes stricter: lines reached only by an excluded failing test are no
  longer covered. Runs that pass today can fail, and the report must attribute that.
- PIT's pre-scan shrinks by the excluded set, which is the measured cost problem on the
  observed task (568 test classes for one target class).
- **This is an estimate, not a measurement.** Its blind spot is a test that the diff
  breaks without touching or naming — Spring/Dubbo wiring, reflection, an inherited
  base. Such a test would be excluded and its regression hidden. Condition 2 narrows
  that window to indirectly-wired tests only; nothing here closes it entirely. The
  report therefore always lists what was excluded, and the feature ships behind a flag
  that is off by default.

## Alternatives rejected

**Measure the baseline by re-running the failing classes at the base ref.** Strictly
more accurate: it distinguishes pre-existing rot from a regression with evidence rather
than inference, and it can never hide a break. Rejected for cost and complexity — it
needs a `git worktree` outside the checkout, a third Maven invocation whose compile
budget is unbounded on a repository where enforcement already runs ~40 minutes, and a
cache that CI's per-task `fresh=True` workspaces cannot serve. Revisit if the estimate
is observed to misfire.

**Stop passing `-Dmaven.test.failure.ignore=true`.** Restores agreement between the
gates by making any red test fail the build. Rejected: it re-creates the TASK-82787
blockage exactly — a module with pre-existing legacy rot yields no verdict at all, even
when the changed class is fully tested.

**Implement the rule in the test-enforcer Maven plugin.** Would make RDC and UTA behave
identically, which is where this belongs eventually. Rejected for now: it needs a plugin
release and a `uta_dev_gate.py` sync (AGENTS.md), and the rule should be proven first.
UTA-side Maven args work against any repository already on test-enforcer ≥ 1.0.15.

**Narrow PIT `targetTests` to the changed class's related tests instead.** Faster, and
it would also sidestep the red suite. Rejected as a *verdict* mechanism: test strength
is killed/covered, so a weakly-covering test lowers it — the metric is non-monotone in
the test set, and a narrowed run can report green where the full run reports red.
Narrowing stays where it is safe (the generation/repair loop).

## Addendum 2026-09-01 — say it in the dialect the reactor speaks

Enabling the rule against production traffic showed the mechanism, not the estimate, was
the risk. `-Dtest=!Foo` is only understood from Surefire 2.19. `shelf-hermes` builds on
2.5, where the `!` is read as part of a literal class name: nothing matched, every module
ran zero tests, and Surefire killed the build on `No tests were executed!` before any
gate reported — turning an actionable `diff line coverage 89.58% (43/48)` into no verdict
at all. `cvs-order-westeros` on surefire 3.5.4 excluded sixteen classes cleanly, which is
why the same code looked correct on one repository and catastrophic on another. Measured
over the last 150 records, ~27% of Java runs (4 apps: `cvs_ugc_user`, `opc_shelves_hermes`,
`cvs_usercenter`, `opc_roster_provider`) are on a Surefire below 2.19.

**Pin Surefire from UTA instead.** Rejected. Maven has no command-line override for a
plugin version — only a POM that writes the version as a property can be overridden, and
`shelf-hermes` hardcodes `<version>2.5</version>`. Rewriting the POM in UTA's workspace
would work mechanically but changes test discovery, provider selection and forking
between 2.5 and 3.x, so the gate would measure a build the project does not have; it also
breaks the report's promise that its printed Maven command reproduces the result, and
makes `coverageBeforeExclusion` incomparable across the two runs. An explicit per-repo
pin in the style of `repository_java_homes` stays available if a human wants to opt one
repository in.

**Name what should run instead of what should not.** Adopted. A positive `-Dtest` list
works at any Surefire version, keeps the project's own plugin, and needs `failIfNoTests
=false` — the spelling Surefire 2.5 itself suggests — because the list is reactor-wide.
`excludedTestClasses` still carries the drop list, since that property is PIT's and PIT
reads it at any version.

Two rules fall out of the same fact that `-Dtest` speaks simple names in both dialects:
an exclusion whose simple name is shared by multiple tests excludes every colliding class
(`simple-name-collision-expanded`), and a module whose whole suite would otherwise
be selected away keeps its tests (`would-empty-module-suite`, judged per module — a
repo-wide count says "plenty left" while one module runs none).

Above all, the re-run is now fail-safe: **if the first run produced gate evidence and the
re-run does not, the re-run is discarded and the first run's verdict stands**
(`rerun-produced-no-evidence`). The re-run exists to describe the same run more honestly,
never to trade a real verdict for silence. That invariant, not the version check, is what
makes the mechanism safe against the next reactor that reads a selector differently.

The positive dialect carries one asymmetry worth naming: it can only list tests the first
run actually reported. Maven is fail-fast, so a module after a failure is SKIPPED and
writes no Surefire reports — naming only what was seen would drop that module from the
re-run and report its covered lines as uncovered, a false failure the evidence guard
cannot catch because evidence is still produced. A reactor that stopped early therefore
does not get a positive list (`incomplete-test-inventory`).

## Addendum 2026-09-02 — preserve the first run's discovery universe

A negative-only `-Dtest=!Failure` has a second defect even on modern Surefire: setting
`test` overrides the plugin's normal includes and turns the remaining universe into
"everything except Failure". That made a nonstandard `FormGenerate` utility containing
an `@Test` method execute during the rerun even though the normal first pass never
selected it.

The rerun now always uses the complete first-pass inventory minus excluded failures.
Surefire 2.13+ reads a deterministic UTA-owned `includesFile`; a no-match
`surefire.includes` override prevents POM includes from broadening that file. Older
Surefire receives the same inventory in positive `-Dtest`, with a 96 KiB single-argument
guard. If the first reactor stopped early or the legacy selector exceeds the guard, UTA
keeps the original verdict rather than run an incomplete or truncated inventory.

Surefire does not remove XML reports for tests omitted by a later invocation. UTA takes
a metadata snapshot immediately before the first enforcement command and only admits
reports created or rewritten by that command into the positive inventory. This prevents
a reused workspace from carrying a previously broadened `FormGenerate` result forward.

## Addendum 2026-09-15 — name skipped modules from source

Refusing every reactor that stopped early (`incomplete-test-inventory`) turned out to
refuse the common case. The exclusion exists for a module whose suite is red, and PIT
aborts exactly that module ("Mutation testing requires a green suite"), so any module
ordered after it is SKIPPED. cvs-usercenter-web hit this: a diff-unrelated flaky test in
`user-center-biz` (third of four modules) was classified for exclusion, `user-center-web`
was skipped, and the exclusion was withdrawn — the report failed on a test the change
never touched.

A skipped module's tests are now read from its `src/test/java`, limited to Surefire's
default includes (`Test*`, `*Test`, `*TestCase`, plus `*Tests` when every Surefire in the
reactor is 3.x), and merged into the inventory before the ambiguity and module-emptying
guards run. The module is found by the display name the reactor summary printed
(`<name>`, else `artifactId`). If any skipped name maps to no module or to more than one,
the rerun is still refused as `incomplete-test-inventory`. Modules named this way are
recorded as `sourceInventoriedModules`.

Known limit: a module with custom Surefire `<includes>` is inventoried by the defaults.
`rerun-produced-no-evidence` still guards against a rerun that loses the gates entirely.

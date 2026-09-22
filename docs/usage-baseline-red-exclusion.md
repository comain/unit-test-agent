# Usage — Diff-unrelated failing-test exclusion

Spec: `docs/spec-baseline-red-exclusion.md` · ADR-015 · Off by default.

## What it does

A UTA enforcement run tolerates a red suite (`-Dmaven.test.failure.ignore=true`), which
is what lets a module with legacy rot reach PIT at all. The price is that the two gates
then judge different test sets:

- JaCoCo **counts** a failing test's executed lines toward diff coverage.
- PIT **drops** the same tests (`parseSurefireConfig` → `skipFailingTests`).

With this feature on, UTA takes the verdict again with the failing tests the change is
not answerable for removed from *both* stages.

A failing test class is excluded when **both** hold:

1. the branch diff does not touch its source file, and
2. it does not reference any changed production class (construction, class literal,
   `extends`/`implements`/`instanceof`, typed declaration, or a matching test name).

Everything else — a test the diff touched, a test naming a changed class, a test UTA
generated this task — stays in the verdict.

## Turning it on

```bash
UTA_CI_FAILING_TEST_EXCLUSION_ENABLED=true
```

Off (the default) is byte-identical to previous behavior: one Maven run, no extra
evidence keys, nothing cleared.

On, a run with excludable failures costs **one extra enforcement run**. Enable it per
environment or per repository rather than globally — on a repository where the first run
is already near its timeout, doubling it is a real risk (see `docs/spec-*` §7 deferred
items: the PIT stall watchdog is not built yet).

## Reading a report

The report gains, under the enforcement panel:

| Field | Meaning |
| --- | --- |
| `excludedFailingTestClasses` | Removed from both gates as unrelated to the change |
| `retainedFailingTestClasses` | Failing and related — still counted, still your problem |
| `exclusionReason` | Why nothing was excluded: `no-failures`, `all-failures-related`, `no-diff-available`, `would-empty-module-suite` |
| `coverageBeforeExclusion` | The coverage rate the first run reported, for comparison |

The Maven command shown in the report is the one that produced the verdict, including
the positive Surefire inventory and `-DexcludedTestClasses=…`, so "reproduce locally
with this command" stays accurate. Do not replace the inventory with negative-only
`-Dtest=!…`; that changes Surefire discovery.

**A coverage drop after exclusion is not a weak generated test.** The failure summary
says so explicitly: lines that only a failing unrelated test reached were never really
covered, and the generated test has to cover them for real.

## Limits worth knowing

- This is an **estimate** of "was already red", not a measurement. A test the change
  breaks through Spring/Dubbo wiring or reflection names nothing and is touched by
  nothing, so it can be excluded wrongly. Condition 2 narrows the window; it does not
  close it. Every excluded class is listed in the report for exactly this reason.
- Exclusion is skipped for a module whose entire test set would be excluded — PIT's
  `failWhenNoMutations` would fail the build and an empty Surefire selection would pass
  vacuously. The reason is recorded.
- Excluded classes are never handed to the repair loop as targets.

## Rollback

Set `UTA_CI_FAILING_TEST_EXCLUSION_ENABLED=false`. No migration, no schema change; the
evidence keys are additive and simply stop appearing.

## Local dev gate (AGENTS.md sync)

AGENTS.md requires syncing `dev-skills/scripts/uta_dev_gate.py` and its
`references/test-enforce-usage.md` when coverage/mutation gate semantics change. **No
sync is needed for this change**: the local gate does not run `verify` with
`-Dmaven.test.failure.ignore=true` (it has no `test.enforcement.enabled` or
`failure.ignore` invocation at all), so it never had the JaCoCo/PIT disagreement this
corrects. The exclusion is a property of UTA's CI runner, not of the enforcement
contract a developer runs locally. Revisit if the rule is ever upstreamed into the
test-enforcer plugin, where it *would* change what everyone's `mvn verify` does.

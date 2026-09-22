# ADR-019: Quarantine Unrelated Hanging Java Tests

## Status

Accepted.

## Date

2026-09-17

## Context

Surefire writes `TEST-*.xml` only after a test class finishes. Existing failing
test exclusion therefore cannot see a class that never returns. Under a shared
Surefire fork, one hanging class also prevents later classes from producing
reports, while the former command runner could leave forked JVMs alive after a
timeout or external signal.

Production task `ad3c157c37444c73aa19e0b6a3d49176` ended with return code 143,
no coverage or mutation evidence, and an unclosed
`Running ...ManagerExchangeBatchControllerTest` marker. It was classified as a
repairable gate failure and created a repair session even though generated tests
could not fix the stalled Spring context.

## Decision

Java enforcement runs in its own process session and watches combined spooled
stdout and stderr growth. After 120 silent seconds it terminates the complete
observed process tree and adds a structured stall marker. The last unclosed
Surefire `Running <class>` marker names the candidate hanging class.

The candidate uses the same relatedness rule as failing-test exclusion. A class
touched by the diff, referencing changed production code, or generated in the
current task remains in the verdict. An unrelated class is excluded from a
source-derived positive Surefire inventory and PIT's excluded-class list, then
the gate is retried once without resetting the total timeout.

Confirmed unrelated hangs are stored by repository for 14 days and
pre-excluded on later runs. A current diff that relates to the class overrides
the stored quarantine. Every detected, retained, newly quarantined, or
pre-quarantined class is included in report evidence.

Signal-terminated runs with no gate evidence are command errors with
`repairEligible: false`. Both watchdog and cross-task quarantine are enabled by
default and can be disabled independently for rollback.

## Consequences

- A hanging test costs at most one retry by default and cannot leave an
  enforcement-owned Surefire JVM behind.
- Quarantine can lower measured coverage by removing lines reached only by the
  hanging class; reports state this explicitly.
- The positive inventory is source-derived because classes queued behind a hang
  have no Surefire XML. If a complete safe inventory cannot be built, the run
  fails closed instead of excluding the class.
- SQLite gains the additive `java_hanging_tests` table. Expired rows remain as
  history but are ignored after the TTL.

## Alternatives Rejected

Relying on `-Dsurefire.timeout=900` cannot identify a class under
`forkMode=once`, cannot teach later runs, and fires later than the chosen stall
budget. Per-method timeouts and Surefire upgrades require target-repository POM
or test changes, outside UTA's boundary. Treating return code 143 as an ordinary
gate failure repeats the production bug by spending repair budget without gate
evidence.

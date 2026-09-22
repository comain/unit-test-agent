# Spec — Hanging-test detection and quarantine for Java enforcement

Status: implemented (2026-09-17)
Slug: `java-hanging-test-quarantine`
Related: `docs/spec-baseline-red-exclusion.md` (§7 defers exactly this work),
`docs/spec-java-ci-full-run.md`, `docs/test-enforce-usage.md`
ADR: `docs/decisions/ADR-019-java-hanging-test-quarantine.md`

Tracking: none. `unit-test-agent` is a personal project, not UTA feature work, so the
Jira/release-approval steps of the dev-skills workflow and the `doc/<JIRA>`
naming are deliberately skipped; artifacts follow this repo's `docs/spec-*.md`
convention.

## 1. Objective

**A test class that never returns must be detectable, attributable, and excludable.**
Today it is none of the three.

`ci_failing_test_exclusion_enabled` can already take a red suite out of both gates, but
its only input is `TEST-*.xml`:

```python
# enforcement_runner/parsing.py:246
for report_path in Path(repo_path).glob("**/target/surefire-reports/TEST-*.xml"):
    broken = int(root.get("errors") or 0) + int(root.get("failures") or 0)
    results[name] = results.get(name, False) or broken > 0
```

Surefire writes that file when a class *finishes*. A class that hangs writes nothing, so
it cannot appear in `_surefire_test_class_results`, cannot reach
`partition_failing_tests`, and cannot be excluded. Worse, under the default
`forkMode=once` it also blocks every class queued behind it, so one hang costs the whole
module's evidence. "Can skip failing" and "can skip hanging" are different capabilities;
only the first exists.

The three consequences, in order of cost:

1. **No evidence, no verdict.** The run produces neither a coverage nor a mutation
   number, and the task falls through to the generic exit-code branch.
2. **A pointless repair session.** That branch is repair-eligible, so UTA spends an LLM
   budget writing tests for a failure no test can fix.
3. **Orphaned JVMs.** `run_bounded_command` calls `subprocess.run` without
   `start_new_session`, so nothing terminates the forked Surefire JVM. Hung forks
   accumulate on the runner until somebody kills them by hand.

### Evidence

Production task `ad3c157c37444c73aa19e0b6a3d49176` (`opc_roster_provider`, branch
`TASK-41006-20260916`, runner `agent1.ai.ops.bj1.example.com`):

```
13:50:09  task created
13:56:23  Running com.example...ManagerExchangeBatchControllerTest
13:56:35  Lifecycle [com.example.common.metrics.Metrics] added      ← last line of stdout
          (~4 minutes of total silence)
14:00:27  returncode 143, stderr empty
```

`/reports/<id>/detail` for that task:

```
enforcement.status       = failed
enforcement.returncode   = 143
enforcement.stderr       = ""
evidence.coverage        = null
evidence.mutation        = null
evidence.pitMutation     = null
summary                  = UTA test-enforcement failed before evidence was produced
fixSessions[0]           = 654c24a2…  repair_task_created
```

What this demonstrates, and what it does not:

- **Demonstrated — the hanging class is identifiable from stdout UTA already captures.**
  `Running X` with no matching `Tests run:` names exactly one class.
- **Demonstrated — neither existing timer fires in time.** The fork started ~13:56:20, so
  `-Dsurefire.timeout=900` was due at ~14:11; `ci_enforcement_timeout_seconds=1800` from
  ~13:50 was due at ~14:20. The process died at 14:00:27, from an external SIGTERM.
- **Demonstrated — the failure is repeatable for this repo.** Same app, same day:
  `875dabf20c274be7bcda2bf755ca0117` burned the full 1800s and reported
  *UTA test-enforcement timed out*; `c8689ebd7a864df389dc2e72a4dc617f` reported
  `Coverage gate failed: 0.00% < 95.00% (0/15); 178 failing test class(es) … excluded`.
- **Demonstrated — rc 143 is repair-eligible today.** A fix session was created for a run
  that produced no gate output at all.
- **Not demonstrated — who sent SIGTERM.** No SSH access to `agent1`; the runner's
  daemon/supervisor logs and `dmesg` were not read. The most likely reading is a manual
  or scripted cleanup of the hung JVMs this bug leaves behind, which makes it a
  *consequence* of the orphaning in §1.3 rather than an independent cause. This spec
  does not depend on resolving it: a stall watchdog that kills its own process tree
  removes the thing a cleanup would be reaching for.

### Non-goals

Per-test-method timeouts, upgrading the target repo's Surefire 2.12.4, fixing why
`ManagerExchangeBatchControllerTest` hangs (a Spring context reaching an unreachable
dependency), Python `mutmut` stalls (already has a phase watchdog), and PIT's own
minion stalls. Reasons in §7.

## 2. Scope discovery

Searched: `uta/enforcement/enforcement.py`, `uta/language/java/enforcement_runner/**`,
`uta/language/java/{ci,enforcement}.py`, `uta/language/java/maven_compat/launcher.py`,
`uta/shared/{config,fix_sessions}.py`, `uta/tasks/storage/schema.py`,
`uta/app/{context,task_daemon}.py`, `uta/app/templates/report.html`.

| Candidate | Role | Decision | Reason |
| --- | --- | --- | --- |
| `uta/enforcement/enforcement.py` `run_bounded_command` | Java's command runner; `subprocess.run` + temp files, one total timeout, no polling | **In scope** | The only place that can observe silence while it happens; also the source of the orphaned forks |
| `uta/enforcement/enforcement.py` `run_resource_bounded_command`, `_terminate_process_tree_groups` | Already own a poll loop and a whole-tree kill for the Python lane | **In scope (reuse)** | The stall lane must not invent a second definition of "kill the tree" |
| `enforcement_runner/parsing.py` | Owns every reader of Maven output and Surefire reports | **In scope** | New reader: pair `Running X` against `Tests run:` to name the unfinished class |
| `enforcement_runner/__init__.py` (`_rerun_without_unrelated_failures`, `_run_command`) | Owns the exclude-and-rerun cycle | **In scope** | A hang must enter the same cycle, but triggered from a *killed* run rather than a completed one |
| `enforcement_runner/failing_test_scope.py` `partition_failing_tests` | Decides related vs unrelated | **In scope** | Must accept hanging classes as input and apply the same relatedness rule |
| `enforcement_runner/evidence.py` `_with_failing_test_scope_evidence` | Publishes the exclusion contract | **In scope** | New keys join the same contract |
| `enforcement_runner/classification.py` (final `returncode != 0` branch) | Turns rc 143 into *failed before evidence was produced* | **In scope** | A signalled run is not a gate verdict and must not be repair-eligible |
| `uta/language/java/ci.py` (`repair_target_ids`, `excludedFailingTestClasses` filter) | Feeds targets to the repair loop | **In scope (reuse)** | A quarantined class must not become a repair target, exactly as an excluded one does not |
| `uta/tasks/storage/schema.py` | `CREATE TABLE IF NOT EXISTS` + additive-column migration helper | **In scope** | Cross-task quarantine needs one additive table |
| `uta/shared/config.py` | Command and budgets | **In scope** | Two flags and one threshold |
| `uta/app/context.py`, `uta/app/templates/report.html` | Hand-render named evidence keys | **In scope** | New keys are invisible otherwise |
| `uta/shared/fix_sessions.py` `can_create_fix_session` | Reads `repairEligible` | **In scope (reuse)** | Already honours `repairEligible is False`; only the producer changes |
| `uta/language/java/maven_compat/launcher.py` | Has a SIGTERM handler that exits 143 | **Out of scope** | Not on this path — `_run_command` calls `prepare_command`, not `launcher.main` |
| `-Dsurefire.timeout=900` in `ci_enforcement_command` | Fork-level budget | **Out of scope (kept)** | Left as the outer backstop; see §3.5 |
| Target repositories' POMs / Surefire version | Could gain per-class timeouts | **Out of scope** | Boundaries §6: UTA does not edit target POMs |
| `uta/language/python/**` | Has its own phase watchdog | **Out of scope** | The Surefire-report blind spot does not exist there |

### Verified facts

- `_surefire_test_class_results` reads only `TEST-*.xml`; a class that never completes
  produces no such file (`parsing.py:246`).
- `run_bounded_command` delegates to `subprocess.run` with `stdout=`/`stderr=` temp files
  and a single `timeout=` (`enforcement.py:27`). No `start_new_session`, no polling.
- `run_resource_bounded_command` already polls at 0.5s, spools to temp files, and calls
  `_terminate_process_tree_groups`, which SIGKILLs every descendant group before
  SIGTERMing the root (`enforcement.py:75`, `:195`).
- Output is spooled to files, so stall detection is `stdout_file.tell()` not advancing —
  no extra pipe, no extra thread contending for the stream.
- `classification.py`'s final `returncode != 0` branch attaches no `repairEligible`, and
  `can_create_fix_session` defaults to eligible (`fix_sessions.py:44`).
- `ci.py:217` already drops `excludedFailingTestClasses` from repair targets.
- `QualityGateStatus` already has `command_error`; no new enum member is needed.
- The 1800s budget is per *mvn invocation*, not per task. `c8689ebd…`'s enforcement
  finished in ~22 min (rc 1); its 2.5h wall time is the repair session that followed.
  Nothing about that timer is broken, and this spec does not change it.

## 3. Behavior

### 3.1 Detecting the stall

`run_bounded_command`'s real-subprocess branch becomes a poll loop of the shape
`run_resource_bounded_command` already uses: `Popen(..., start_new_session=True)`,
spool to temp files, poll every 0.5s.

A run is **stalled** when the combined spooled output has not grown for
`ci_enforcement_stall_seconds` (default 120) *and* the total timeout has not yet expired.
Nothing else counts as a stall: a slow-but-talking build is not stalled, and a quiet build
that finishes on its own is never observed.

On a stall the whole process tree is terminated through `_terminate_process_tree_groups`,
and the runner receives a `CompletedProcess` carrying the bounded output plus a
`UTA_ENFORCEMENT_STALLED stalled-after=<n>s` marker on stderr — deliberately the same
shape as the existing `UTA_RESOURCE_EXHAUSTED` marker, so there is one convention for
"the runner ended this, not Maven".

`start_new_session=True` is not incidental. It is what makes the whole-tree kill possible
and is what stops hung forks accumulating on the runner.

### 3.2 Naming the class

From the run's own stdout, pair Surefire's per-class markers:

```
Running com.example.FooTest          ← opens
Tests run: 12, Failures: 0, …        ← closes
```

The **last unclosed `Running`** is the hanging class. If stdout closes every `Running` it
opened, no class is named and the stall is attributed to the reactor rather than to a
test (§3.4).

This reader is FQN-exact because `Running` prints the fully qualified name — it does not
inherit `_failed_surefire_tests`'s Test/IT-suffix regex weakness, and it does not touch
that function's 12-record presentation cap.

### 3.3 Quarantine and re-run

A named hanging class is fed into `partition_failing_tests` alongside the failing ones and
is judged by **the same relatedness rule** — untouched by the diff *and* not referencing a
changed production class. A hanging class that the diff touches, or that names a changed
class, or that UTA generated this task, is **never** quarantined: the change may well be
what hung it, and hiding that is the false green this whole area exists to avoid.

If the class is quarantinable, enforcement is re-run through the existing
exclude-and-re-run path — same `-Dtest`/`surefire.includesFile` positive inventory, same
`-DexcludedTestClasses`, same `_clear_module_test_artifacts` beforehand.

Bounds, because a stall costs a full build each time:

- at most `ci_enforcement_stall_retries` (default 1) stall-driven re-runs per enforcement;
- a class named twice in one enforcement is not re-run a third time;
- the total-timeout clock is **not** reset by a re-run.

### 3.4 Verdict

| Situation | Status | `repairEligible` | Summary |
| --- | --- | --- | --- |
| Stall, class named and quarantined, re-run produces evidence | whatever the gates say | as today | today's summary plus *"test class X was excluded because it did not terminate within Ns"* |
| Stall, class named but **retained** (related to the diff) | `command_error` | `False` | *"UTA test-enforcement stopped because test class X did not terminate within Ns; it is related to this change, so it was not excluded"* |
| Stall, no class named | `command_error` | `False` | *"UTA test-enforcement stopped after Ns without output"* |
| Stall retries exhausted | `command_error` | `False` | names every class quarantined so far |
| Signalled run with no evidence (rc 143/137, or negative rc) and no stall detected | `command_error` | `False` | *"UTA test-enforcement was terminated by a signal before evidence was produced"* |

The last row is the narrow fix for `ad3c157c…`: it stops a signal-killed run from being
read as a gate verdict and from spending an LLM budget. It is deliberately scoped to runs
that produced **no** evidence — a signalled run that already emitted gate output keeps
today's classification order untouched.

Coverage may legitimately drop when a quarantined class is removed, exactly as it does for
excluded failing classes. That is the correction, and the summary must say so.

### 3.5 Cross-task quarantine

A confirmed hanging class is recorded per repository in a new
`java_hanging_tests` table (`repo_slug`, `test_class`, `module`, `first_seen_at`,
`last_seen_at`, `observations`) and pre-excluded on the **first** run of subsequent
enforcements for that repository, saving the wasted build.

Guards, so a quarantine cannot quietly become permanent:

- it applies only while the class stays unrelated to the current diff — a diff that
  touches the class clears it for that task, and it runs;
- an entry unseen for `ci_hanging_test_quarantine_ttl_days` (default 14) expires;
- pre-exclusion is always reported, so a report never omits a class silently.

`-Dsurefire.timeout=900` stays in `ci_enforcement_command` as the outer backstop. Note
what it cannot do: under `forkMode=once` it is one budget for the whole forked JVM
(191 test classes in `roster-provider`), it kills the fork without naming a culprit, and
it teaches the next run nothing. It is a floor, not a mechanism.

### 3.6 Evidence

Additive keys on the existing contract, attached **regardless of return code**:

- `hangingTestClasses` — named this run
- `quarantinedTestClasses` — actually excluded from the re-run
- `retainedHangingTestClasses` — named but related, so kept
- `preQuarantinedTestClasses` — excluded up front from the cross-task table
- `stallSeconds`, `stallRetries`, `stallReason`
- `terminationSignal` — set when the run ended on a signal

## 4. Acceptance criteria

1. A run whose spooled output stops growing for `ci_enforcement_stall_seconds` is
   terminated, and the whole process tree is gone — asserted against a fake process tree,
   not Maven.
2. The stall terminator and the memory guard call the same `_terminate_process_tree_groups`;
   no second tree-kill implementation exists.
3. Given stdout with `Running A / Tests run: … / Running B` and no close for `B`, the
   reader names exactly `B`; given fully paired output it names nothing.
4. A hanging class untouched by the diff and not referencing a changed class is
   quarantined and appears in both the `-Dtest` inventory and `-DexcludedTestClasses` of
   the re-run command.
5. A hanging class touched by the diff, or referencing a changed class, or generated by
   UTA this task, is **never** quarantined; the run ends `command_error`,
   `repairEligible: False`, and the summary names the class.
6. Stall-driven re-runs never exceed `ci_enforcement_stall_retries`, and a re-run does not
   reset the total timeout.
7. A signalled run with no evidence classifies as `command_error` with
   `repairEligible: False`; `can_create_fix_session` returns `False` for it. Replaying
   `ad3c157c…`'s report shapes produces exactly this and creates no fix session.
8. Quarantined and pre-quarantined classes do not appear in `repair_target_ids`.
9. A cross-task quarantine entry is ignored for a task whose diff touches that class, and
   expires after the TTL.
10. With both flags off, the emitted command and every classification are byte-identical
    to today's, and no polling behaviour is observable — proven by the existing Java
    enforcement tests staying green unmodified.
11. A build that keeps producing output past the stall threshold is never terminated.

## 5. Testing strategy

Unit tests in `tests/` with an injected `run_command` and synthetic process trees — no
Maven, no JVM, no network: stall detection at the boundary (grows / stops growing /
finishes quietly), tree termination, the `Running`/`Tests run:` reader over fixture
stdout (paired, unpaired, interleaved modules, no markers at all), the relatedness rule
across all four combinations for a hanging class, command shaping and the simple-name vs
FQN divergence the existing exclusion already handles, retry bounds, timeout not reset,
signalled-run classification and `can_create_fix_session`, repair-target filtering,
quarantine write/read/TTL/diff-clears-it, and both flags off. A fixture built from
`ad3c157c…`'s captured stdout and detail JSON anchors 7. Full `pytest` green before commit.

## 6. Boundaries

**Always**: keep Java specifics under `uta/language/java/` (AGENTS.md); commit to `main`;
keep evidence keys additive; reuse the existing exclusion cycle rather than adding a
parallel one.

**Ask first**: raising `ci_enforcement_timeout_seconds`; changing `-Dsurefire.timeout`;
defaulting either new flag to on.

**Never**: edit a target repository's POM or Surefire version; quarantine a class the diff
touches or that names a changed class; quarantine silently — every quarantined and
pre-quarantined class appears in the report; leave a forked JVM running after the runner
has decided the run is over.

## 7. Deferred, with reasons

| Deferred | Why not now |
| --- | --- |
| Per-test-method timeouts (`@Test(timeout=…)`, JUnit `Timeout` rule) | Requires editing target repositories' tests; Boundaries §6 |
| Upgrading target Surefire past 2.12.4 | Target-repo POM change; would give better per-class control but is not UTA's to make |
| Root-causing `ManagerExchangeBatchControllerTest` | A Spring context reaching an unreachable dependency on the runner — infra, not code, and the same class of problem as TASK-82787's wconfig reachability |
| Confirming who sent SIGTERM to `ad3c157c…` | No SSH access to `agent1`; §3.1's tree-kill removes the reason a cleanup would exist, so the answer changes nothing in this design |
| PIT minion stall detection | PIT stalls after gate evidence has started; different signal, different verdict, and `spec-baseline-red-exclusion` §7 already lists it |
| Surfacing quarantine state in the fix-session UI | Report evidence first; the UI follows once the keys have production shapes |
| Alerting on a repository whose quarantine list keeps growing | Needs the table to exist and accumulate real data first |

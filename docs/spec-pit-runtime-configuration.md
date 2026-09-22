# Spec: PIT Runtime Configuration Compatibility

Status: Approved 2026-09-08. Non-Jira UTA tooling iteration, confirmed 2026-09-08.
Design: revised following user disposition to fix all five review findings.
Design re-review passed; usage draft and implementation plan are available.

## Contents

1. [Objective And Evidence](#1-objective-and-evidence)
2. [Scope](#2-scope)
3. [Behavior Contract](#3-behavior-contract)
4. [Architecture And Delivery Constraints](#4-architecture-and-delivery-constraints)
5. [Verification](#5-verification)
6. [Boundaries And Risks](#6-boundaries-and-risks)
7. [Workflow And Open Decisions](#7-workflow-and-open-decisions)
8. [Changelog](#8-changelog)

## 1. Objective And Evidence

Let UTA run Java diff enforcement correctly when a target repository's explicit
PIT configuration freezes properties before test-enforcer computes module scope.
Do not require edits or commits to the target repository, or a test-enforcer release.

Observed on report `c3a75efa07774cc4a404f0f44014c3f5`, repair session
`ad2884f9a92342529c39c9de2fe3b2ef`, repo task 164, target task 2738:

- The repair target is `com.example.dms.order.core.biz.Test`.
- `-pl biz -am` builds the required `service` dependency before `biz`.
- Coverage reports no applicable changed Java lines for `service`.
- PIT nevertheless runs there and fails with `No mutations found`; `biz` is skipped.
- The effective service POM has literal `skip=false` and a stale
  `targetClasses` value naming `ThirdApiMonitorCollector` at plugin and execution levels.
- PIT 1.15.0's installed descriptor declares native `skipPitest` binding for `skip`.
  Test-enforcer's source updates module properties, which cannot replace already
  interpolated explicit plugin configuration.

This evidence does not establish a recent UTA regression. The target's coverage
and mutation have not been measured successfully; no target pass is implied.

## 2. Scope

| Candidate | Decision | Reason |
| --- | --- | --- |
| `uta/language/java/enforcement_runner/` and Java enforcement binding | In | Common Java Maven invocation, including reruns |
| CI verification, repair precheck, repair verification, final enforcement | In | Must use identical configuration normalization semantics |
| UTA-owned Maven lifecycle extension and its packaging | In | Agreed direction: normalize effective configuration in memory |
| Invocation/report evidence and integration guidance | In | Explain normalized settings and reproduce actual invocation |
| `plugins/dev-skills/scripts/uta_dev_gate.py` and `references/test-enforce-usage.md` in plugins repo | In | Sync Java hard-gate behavior and usage guidance; no copied implementation |
| Direct class-level Java PIT invocation | Audit | Shares PIT but not necessarily diff enforcement; retain its deliberate class scope |
| Test-enforcer source and release chain | Out | No new library release required for this iteration |
| Target-repository POM/source/test changes | Out | Fix tooling without modifying application checkout contents |
| Python, agent-core model routing, task DB/API schema | Out | Java execution compatibility only; no migration required |
| Test.java's existing-test discovery | Out | Separate issue; do not disguise it as a PIT configuration fix |

## 3. Behavior Contract

### 3.1. Scope Authority

- Maven dependency modules remain in the build. Do not remove `-am` to avoid gates.
- Test-enforcer remains the authority for filtered mutation targets per module.
- A dependency with no applicable filtered mutation targets skips PIT, not compilation.
- A module with applicable mutation targets runs PIT against those targets, not stale
  application defaults. Coverage retains its existing filtering and thresholds.
- No replacement git-diff classifier, test-filename heuristic, or inferred pass.

### 3.2. Configuration Normalization

- Override only explicit PIT settings that conflict with UTA-owned scope and gates.
- Cover plugin-level and execution-level settings after inheritance/profile merging.
- Preserve unrelated settings: mutation operators, timeouts, report formats, JVM
  options, and intentional exclusions not contradicted by the authoritative scope.
- Keep selected-test semantics aligned with the existing invocation, including
  baseline-test exclusions and positive selectors.
- CI-owned coverage/mutation thresholds must not be weakened by repository settings.
- The design must enumerate each normalized parameter, its authoritative source,
  and its exact PIT descriptor binding. Removing configuration blindly is forbidden.
- A changed target with unexplained missing evidence still fails. Do not globally
  set `skipPitest=true`, ignore nonzero Maven exits, or waive empty-mutation errors.

### 3.3. Isolation And Evidence

- Repository POMs, source, and existing Maven extension configuration remain byte-identical.
- Use the same normalization for initial CI, baseline rerun, target repair, and final rerun.
- Record compatibility artifact version and activation, affected modules/parameter names,
  and the effective reproduction command. Do not expose secrets or entire POM contents.
- If required normalization cannot be installed or activated, return an actionable
  tooling error, not a gate success. Unaffected invocations retain existing behavior.
- Activation is not completion: require fresh same-invocation, per-module mutation
  execution/evidence for every enforcer-obligated module. Earlier green summaries must
  not mask skipped modules, unrecognized failures, or stale reports.
- Reject reserved mutation-scope overrides from Maven user/system properties, including
  CLI, Maven config and JVM options. Verify filter-diff actually completed before PIT;
  configured executions alone do not prove ordering or authority.
- Pin the shared local-dev artifact by immutable revision and independently expected
  digest. Applicable Java local gates must use the same launcher and completion checks.

## 4. Architecture And Delivery Constraints

The agreed direction is a UTA-owned Maven lifecycle extension loaded for UTA's
invocation via `maven.ext.class.path`, modifying only the in-memory Maven model.
Lifecycle ordering and property precedence require a real Maven proof in design/build;
an effective-POM inspection alone is not sufficient acceptance evidence.

- Python orchestration follows existing Java adapter patterns. No Java branching in
  shared workflow, progress, cost, or task code.
- Java extension must run on production Java 8 and Maven 3.9.10/PIT 1.15.0 with the
  latest released test-enforcer, confirmed as 1.0.15 from Nexus on 2026-09-08.
  Preserve the existing minimum 1.0.15 requirement; historical 1.0.13 is RED-only.
- Preserve any existing extension classpath; never overwrite it silently.
- Build/package the extension as part of UTA delivery, not by compiling in each target
  repository. Pin/version the artifact and verify its provenance before loading it.
- Local dev-skills consumes the same compatibility artifact/contract without duplicating
  Java normalization code. Its distribution and invocation are design acceptance items.
- Follow existing Python formatting and test conventions; use structured Maven/XML APIs,
  not regex POM editing. Comments document scope authority and lifecycle timing invariants.

## 5. Verification

### 5.1. Development RED/GREEN

First reproduce with a real multi-module Maven fixture: unchanged `service`, changed
`biz`, explicit stale `skip=false` and target settings. Without the fix, PIT aborts
`service` before `biz`. With the fix, `service` builds and skips mutation, while `biz`
executes mutation and reports its actual verdict.

Additional mandatory cases:

1. Changed `biz` deliberately fails the mutation threshold; the overall run fails.
2. Changed `biz` passes with sufficient tests; the overall run passes.
3. Both modules have applicable targets; neither is incorrectly skipped.
4. Mixed plugin/execution overrides, inherited profiles, and existing extensions.
5. Selected tests, baseline rerun handling, and centrally owned thresholds survive.
6. Missing/unloadable artifact produces a clear tooling failure.
7. Repository file hashes match before/after success, failure, and timeout.
8. Cross-layer tests prove CI, repair, and final reruns use identical normalization.
9. Python invocation remains unchanged; Java class-level scope is not broadened.

Use pytest for orchestration/contract tests and Java tests plus actual Maven integration
for extension behavior. Baseline commands:

```sh
python -m pytest tests/test_api_trigger_enforcement.py tests/test_maven_parsers.py -q
```

The design/plan must add exact new extension build and real-reactor test commands;
mock-only verification or skipped real Maven tests cannot satisfy this iteration.

### 5.2. Production Verification

- User authorized production replay of the failing `dms-order-core` case on
  2026-09-08. Iterate on tooling fixes and fresh verification runs until the
  reported PIT/module-skipping defect is resolved. This does not authorize
  forced successful callbacks, weaker gates, or unrelated application edits.
- Deploy via Git pull and the versioned artifact; restart only when UTA is idle.
- Rerun the original failing case from its recorded base/head and inspect actual
  module/PIT logs. Confirm the dependency skip and that the changed module is reached.
- Run a known-good Java regression case and a known-failing changed-target case.
- Verify local-dev invocation parity and report guidance.
- Record any genuine application/test failures separately. Tooling success does not
  require turning the application gate green, nor authorizes changing thresholds.
- Roll back the matched UTA code/artifact pair if normalization loses target scope.

## 6. Boundaries And Risks

Always: failing test first; preserve unrelated edits; derive scope from test-enforcer;
retain failure signals and task/progress/cost compatibility; sync dev-skills guidance.

Ask first: expanding into a test-enforcer release, changing supported Maven/PIT versions,
modifying target-repository files, or interrupting running production work.

Never: global PIT bypass, fabricated passes, disabled mutation thresholds, secret output,
editing third-party jars, or loading an unverified extension supplied by the target repo.

Principal risks are Maven lifecycle precedence, loss of user configuration, extension
classpath conflicts, artifact distribution drift, and false passes. Address them with
an explicit parameter ownership table, fail-closed activation, and real reactor tests.

## 7. Workflow And Open Decisions

Spec approved. User disposition on initial design review: fix all five findings.
Revised design passed independent review. Next gate: implementation plan approval.
The following are required design decisions, not permission to omit work:

- Exact lifecycle hook and precedence against execution-specific plugin configuration.
- Explicit parameter allowlist and handling of threshold aliases/properties.
- Artifact build, trusted distribution, and local-dev installation/reproduction flow.
- Supported Maven/PIT matrix and deterministic failure for unsupported combinations.

## 8. Changelog

- 2026-09-08: Initial non-Jira spec, grounded in task 2738 and the observed effective POM;
  requires shared invocation behavior, unchanged target files, and staged real verification.
- 2026-09-08: Added explicit authorization for iterative production verification
  of the original dms-order-core failure after implementing and testing the fix.
- 2026-09-08: Incorporated approved review safeguards for module completion, property
  authority, execution ordering, exclusion parity and immutable local-dev distribution.
- 2026-09-08: Approved second-review revisions: inspect actual injected PIT parameters,
  require private invocation-specific XML evidence, and use latest released enforcer
  1.0.15 without relaxing existing version checks or silently editing application POMs.

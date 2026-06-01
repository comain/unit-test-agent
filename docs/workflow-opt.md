# Workflow Optimization Notes

## Goal

Reduce wasted context churn in the current `plan -> code -> verify -> re-code` flow by keeping verification closer to the live agent session, instead of bouncing back out to the outer workflow after every failed validation.

## Problem With Current Flow

Today the workflow behaves roughly like this:

1. agent plans/writes code
2. UTA runs compile/test/coverage/mutation outside the session
3. if a gate fails, UTA sends a new fix prompt
4. the agent re-enters with extra context/setup overhead

This has a few problems:

- verification feedback arrives too late
- each failure reopens a new reasoning cycle
- the agent can repeat setup/exploration instead of staying in the current task state
- coverage and mutation loops can become expensive and indirect

## Better Pattern: Finish-Intercept Gate

Treat agent completion as a **submission attempt**, not final success.

When the agent signals it is done:

1. intercept the completion
2. run local verification immediately
3. if verification passes, allow exit
4. if verification fails, block exit and feed the result back into the same live session
5. let the agent revise and try to finish again

This keeps the agent in the same working context and avoids unnecessary outer-loop reentry.

## Proposed Verification Pipeline

The finish intercept should run staged gates in this order:

1. compile gate
2. test gate
3. coverage gate
4. mutation gate

### Compile Gate

Run `mvn test-compile` or the repo/module-specific equivalent.

If compile fails:

- block finish
- return compiler output to the same session
- ask the agent to fix compile errors only

### Test Gate

Run targeted tests for the generated test file/class.

If tests fail:

- block finish
- return failing test output to the same session
- ask the agent to fix runtime/assertion/setup issues

### Coverage Gate

Coverage should be part of the intercept loop, not just a final passive status check.

If `coverage < coverage_gate`:

- block finish
- return current coverage and uncovered-path hints
- ask the agent to add path reach, not just rewrite assertions

Important rule:

- do **not** run mutation while coverage is still below gate

### Mutation Gate

Mutation should be the last gate, only after compile/test/coverage pass.

If `mutation_score < mutation_gate`:

- block final exit
- return surviving-mutant or weak-test feedback
- ask the agent to harden assertions or add targeted cases

Mutation is slower and noisier, so it should stay late in the gate order.

## Bounded Retry Policy

Interception must be bounded. Otherwise coverage or mutation can loop forever.

Recommended default budgets:

- compile intercept attempts: `2-3`
- test intercept attempts: `2-3`
- coverage intercept attempts: `2`
- mutation intercept attempts: `1-2`

Rules:

- count attempts per class, not just per session
- only consume an attempt when a real finish attempt occurred
- do not burn retries on transient polling/network glitches

When the retry budget is exhausted:

- stop intercepting
- return a structured failure
- preserve the best achieved verification metrics

## Recommended Result Tracking

Per class, track:

- `compile_intercept_attempts`
- `test_intercept_attempts`
- `coverage_intercept_attempts`
- `mutation_intercept_attempts`
- `best_coverage`
- `best_mutation_score`
- `intercept_exhausted`

This makes later analysis and tuning much easier.

## Why This Should Be Better

Benefits of the intercept pattern:

- less repeated context/setup work
- tighter feedback loop for compile/test/coverage
- lower chance of restarting broad exploration after a near-complete attempt
- better alignment with how a human reviewer would block a broken submission

## Suggested Rollout

Implement in stages:

1. intercept compile + test in the same session
2. add coverage as a bounded intercept gate
3. add mutation as a late bounded intercept gate
4. record per-class intercept metrics in reports

## Open Questions

- Should compile/test/coverage all run on every finish attempt, or can some results be reused when files did not change?
- Should mutation run only once after coverage passes, or allow one mutation-hardening retry by default?
- Should the agent receive raw verifier output, or a normalized smaller summary plus key excerpts?

## Likely Implementation Shape

At the workflow level:

- treat OpenCode `stop` as a submission attempt
- before accepting completion, run the local verifier pipeline
- if a gate fails, inject the result into the same session and continue polling
- only return to the outer workflow when:
  - all enabled gates pass, or
  - the intercept budget is exhausted

This should replace much of the current outer `verify -> send fix prompt -> verify again` churn.

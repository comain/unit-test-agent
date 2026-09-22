# ADR-008: UTA owns an authoritative workflow-operation ledger

Status: proposed
Date: 2026-08-17
Context: [spec](../spec-executable-generation-agent-turn.md), [design](../design-executable-generation-agent-turn.md)

## Context

Graph checkpoints do not transactionally include model edits, files, Git,
compiler/test tools or the product database. `task_events` is append-only,
has no unique operation key, and is designed for presentation. Neither can
prove whether a side effect completed safely after a crash.

## Decision

UTA adds `workflow_operations`, keyed by a stable `operation_id`, as product
truth for every deterministic and model phase. It records workflow/unit/phase
identity, attempt, status, pre-input and resulting-workspace fingerprints,
artifact paths/hashes, prerequisite operation IDs, schema version, session,
timestamps and normalized error.

Model phases use separate `turn` and `interpret` operation steps. The atomic
turn artifact contains the complete JSON-safe `AgentTurnResult` accounting
projection, so adoption after a crash reconstructs sessions, tokens, timing,
diagnostics, retrospective, patch count and recovery—not only business
evidence.

Each cycle and each side-effecting model phase enters a reconciliation node
that chooses `run`, `reuse_result`, `adopt_result`, `verify_existing_edit`,
`retry_no_effect`, `fail_indeterminate`, or `fail_unsafe`. In particular, a
STARTED no-edit operation with a valid atomically-renamed result is adopted; an
unchanged workspace with no result is failed and retried under a new execution
ordinal of the same logical attempt;
an invalid final artifact is quarantined rather than replayed.

The retry uses a separate `execution_ordinal` in the operation ID while
retaining the language-policy logical `attempt`. It therefore does not consume
compile/test/coverage/mutation repair budgets. One crash replay is allowed by
default; a second indeterminate no-effect crash fails explicitly.
Result artifacts are written with temp-file + fsync + atomic rename before a
transaction completes the ledger row; derived progress events are emitted
afterward. `task_events` may duplicate after a crash and is deduplicated only
for display.

For a model turn, agent-core invokes UTA's result sink only after the
post-turn unsafe-diff guard accepts the workspace and the phase session closes.
A guard rejection leaves the operation STARTED and writes no completed result
envelope, so restart reconciliation cannot reuse a result that failed safety
validation. A guard-failure/crash test is part of this decision's acceptance
evidence.

## Consequences

- An absent graph checkpoint can reconstruct the earliest safe phase from
  product evidence.
- A crash after a model edit routes through deterministic verification instead
  of replaying the turn.
- UTA requires an additive DB migration and fault-injection tests at every
  write boundary.
- The ledger is product-specific; agent-core stays free of test-generation
  schemas and artifact policy.

## Alternatives considered

**Use task events as the idempotency store.** Rejected: inserts are
unconditional and presentation delivery is intentionally at-least-once.

**Use the LangGraph checkpoint as product truth.** Rejected: it cannot
atomically cover external side effects and would couple correctness to one
provider's storage schema.

**Blindly rerun after a missing checkpoint.** Rejected: model edits and tool
cost can be duplicated against an already-changed workspace.

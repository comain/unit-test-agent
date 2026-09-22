# ADR-007: agent-core owns durable workflow start and resume semantics

Status: proposed
Date: 2026-08-17
Context: [spec](../spec-executable-generation-agent-turn.md), [design](../design-executable-generation-agent-turn.md)

## Context

An executable LangGraph 1.x probe showed that a stable thread ID is necessary
but insufficient. Invoking a pending lineage with a fresh state mapping
re-enters at the graph entry; only `invoke(None, config)` resumes its pending
node. Supplying a mapping to a completed lineage also re-executes the graph.
This can repeat an expensive or side-effecting model operation.

## Decision

agent-core exposes `invoke_workflow(graph, identity, initial_state,
recursion_limit)`. It inspects `graph.get_state(identity config)` and applies
exactly four cases:

1. no checkpoint tuple and no values: invoke with `initial_state` and report
   `started`;
2. valid snapshot with pending next nodes: invoke with `None` and report
   `resumed`;
3. valid snapshot with no next nodes: return stored values without invoking and
   report `reused_completed`;
4. deserialization or invariant failure: raise `WorkflowCheckpointError`.

Products do not directly invoke a durable graph. A clean rerun mints a new
workflow-run identity; corruption is never treated as absence.

## Consequences

- Expensive entry nodes are not replayed merely because an outer product write
  was interrupted.
- Provider-specific snapshot/config details remain behind agent-core.
- The API has contract tests against the installed LangGraph implementation
  for absent, pending, completed, corrupt and clean-rerun cases.
- Consumers must handle a corrupt-lineage error explicitly.

## Alternatives considered

**Always invoke with initial state.** Rejected by executable evidence: it
restarts pending and completed lineages.

**Let each product inspect snapshots.** Rejected: every consumer would encode
LangGraph semantics and repeat the same failure mode.

**Treat completed as a new run.** Rejected: clean rerun is a deliberate product
action and requires a new identity.

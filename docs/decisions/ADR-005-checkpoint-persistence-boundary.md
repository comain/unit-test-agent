# ADR-005: Checkpoint persistence lives behind an agent-core API

Status: proposed
Date: 2026-08-17
Context: [spec](../spec-executable-generation-agent-turn.md), [design](../design-executable-generation-agent-turn.md)

## Context

Resuming a nested workflow after a worker restart needs LangGraph
checkpointing. `build_graph` already accepts a `checkpointer` and passes it to
`compile()`; nothing supplies one.

The obvious implementation is for UTA to construct a `SqliteSaver` and hand it
over. That puts a LangGraph class in product code, in a project whose stated
boundary is that LangGraph specifics stay behind agent-core's workflow API —
and cr_plugin, a second consumer, would inherit the same pattern by example.

Checkpoint state is also easy to confuse with product truth. A checkpoint that
is treated as authoritative turns a deleted file into lost tasks.

## Decision

We will add `agent_core.workflow.checkpoints`, exposing
`open_checkpointer(path)` as a **context manager**, a saver-compatible neutral
wrapper with identity-based deletion, and `WorkflowRunIdentity` as a frozen
value type. `langgraph.checkpoint.sqlite` is imported there and nowhere else.
The wrapper creates/verifies its parent directory as `0700` and database as
`0600`, and rejects unsafe permissions or symlinks.

Storage is separate from the product database:

```
<state>/uta_tasks.db                         product truth
<state>/workflow-state/checkpoints.sqlite    graph execution checkpoints
<state>/workflow-state/results/...           normalized operation artifacts
```

The dedicated `workflow-state` directory is created as `0700`; this avoids
changing or rejecting the existing task-DB parent, which is commonly `0755`.
Checkpoint and operation-artifact files are `0600`.

The product database stays authoritative for task status, attempts, artifacts,
results and user-visible progress. The checkpointer owns only serializable
graph state, the next node, interrupts and nested position.

Run identity carries the topology version **in the thread id**:

```
{product}:{cycle_name}:{version}:{task_id}:{unit_id}:{workflow_run_id}
```

Not in `checkpoint_ns`. An earlier revision specified that, and the
[spike](../spikes/spike-executable-generation-agent-turn.md) showed it does not
work: LangGraph resolves `checkpoint_ns` as a **subgraph path**, so
`checkpoint_ns="generation-cycle:v1"` makes `get_state` raise
`ValueError: Subgraph generation-cycle not found`. Two design reviews read that
line and neither caught it; one probe did.

`unit_id` identifies a **stable persisted batch**, not a target: Java generates
over a batch of classes per turn (`generation.py:2971-4611`), and a per-target
key would leave Java's identity ambiguous. A Python target is a batch of one.
UTA stores `<workflow_run_id>/<unit_id>` in its existing `class_tasks.batch_key`
field so a restart cannot regroup the remaining classes. A retry reuses the
identity; a clean rerun mints a new `workflow_run_id`; an incompatible
topology change increments the version in `thread_id`.

## Consequences

- Product code never imports a saver, so the backend can be replaced without
  touching a consumer.
- A second consumer gets the contract rather than the pattern.
- An absent checkpoint starts product reconciliation; valid operation-ledger
  evidence prevents repeated side effects. A corrupt checkpoint fails rather
  than silently becoming a new lineage.
- `context` — which holds credentials, handles and callables — is bound at
  build time and never serialized, so it cannot reach the checkpoint file.
- A context manager rather than a factory, because the saver holds a SQLite
  connection and a daemon that leaks one per run fails slowly and confusingly.
- Products delete a lineage by `WorkflowRunIdentity`; they never query or
  mutate LangGraph tables directly.
- Start/resume/completed semantics are owned separately by the public
  invocation API in [ADR-007](ADR-007-durable-workflow-invocation.md); a saver
  and thread ID alone do not provide safe resume.

## Alternatives considered

**UTA constructs the saver.** Rejected: it is the one thing the spec's "never
do" list names, and it exports the coupling to every future consumer.

**One database for product truth and checkpoints.** Rejected: it invites
treating graph position as business state, and couples a schema we own to one
LangGraph owns.

**Checkpoint per node inside loops.** Rejected: finer than a node buys nothing
resumable and multiplies writes.

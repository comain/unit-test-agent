# ADR-004: The generation cycle is an executed nested workflow

Status: proposed
Date: 2026-08-17
Context: [spec](../spec-executable-generation-agent-turn.md), [design](../design-executable-generation-agent-turn.md)

## Context

`generation-cycle.yaml` declares eleven phases and their repair routes.
Nothing executes it. `graph/generation_cycle.py` loads the spec to publish
`GENERATION_PHASES` and to decorate a result; the work is done by a single
composite backend call, `run_generation_cycle(state)`.

So the file is documentation that looks like code. The phases shown to an
operator are a list read from a spec the executor never consults, which means
the UI can report a phase that never ran, and a maintainer can change a route
with no effect whatsoever.

## Decision

We will compile `generation-cycle.yaml` with agent-core's `build_graph` and
invoke it as a nested workflow, once per target batch. Its declared nodes and
conditional routes determine execution.

The child is compiled **once** with agent-core's existing `build_graph`, at
outer-graph build time, and bound into the outer graph's build-time context.
The neutral outer node invokes it per stable unit with that unit's workflow
identity.

**Not** `NodeRegistry.add_workflow`, which an earlier revision of this ADR
proposed and which cannot work: `workflow/registry.py` is deliberately
LangGraph-free (`workflow/__init__.py:8-9`), `build_graph` raises `ImportError`
at import without the extra (`graph.py:29-37`), and `NodeRegistry.extend()`
copies only `nodes` and `selectors` (`registry.py:92-101`) — so a registered
workflow would be **silently dropped** by both consumers.

The [spike](../spikes/spike-executable-generation-agent-turn.md) confirmed this
exact shape works: a separately compiled child, invoked from an ordinary node
with its own checkpointer and `thread_id`, keeps an independent checkpoint
lineage. Safe resume/reuse is supplied by the inspection algorithm in
[ADR-007](ADR-007-durable-workflow-invocation.md), not by the ID alone.

## Consequences

- A route change in the YAML changes behaviour. That is the point.
- The per-target boundary becomes real, which is what gives checkpoint
  identity something to key on (`ADR-005`).
- Phase handlers become individually testable, which the composite was not.
- The outer graph gains one node and loses a large opaque one.

## Alternatives considered

**Inline the eleven phases into the outer graph.** Rejected: it dissolves the
per-target boundary the checkpoint identity needs, and produces one graph with
both target iteration and phase repair in it, which is harder to read than
either.

**Keep the composite and generate the YAML from it.** Rejected: it makes the
documentation accurate while leaving the topology unchangeable without code,
which is the opposite of the goal.

**Add `build_graph(subgraphs=…)`.** Rejected after the spike: the existing
builder already compiles the child and build-time context already carries it
to the outer node. A second subgraph registry would duplicate composition
without adding a capability.

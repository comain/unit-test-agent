# Spike: verifying the mechanics the design depends on

Date: 2026-08-17
Design: [design-executable-generation-agent-turn.md](../design-executable-generation-agent-turn.md)
Method: executable probes against the installed agent-core and LangGraph 1.x.

Two review rounds found the same class of defect in this design: API shapes
asserted in prose that do not survive contact with the symbol. Rather than
guess a third time, each contested claim became a probe that either runs or
does not. Results below are program output, not argument.

## Results

| # | Question | Answer |
| --- | --- | --- |
| N-1 | What does `run_harness_node(on_failure=…)` accept? | **`"fail"` or `"skip"` only**, default `"fail"` (`node.py:167`). `"state"` raises `ValueError`. The design must use `"skip"` and read the outcome. |
| N-5 | Must `run_harness_node` change to accept a session? | **No.** A session-shaped runner is accepted today and returns `NodeOutcome(status="accepted")`. Listing this as work was wrong; it is already done. |
| N-3 | Is `recursion_limit` a compile argument? | **No** — `StateGraph.compile` has no such parameter; it is invoke-time. **But** `compiled.with_config(recursion_limit=…)` returns a `CompiledStateGraph`, **keeps `get_state`/`update_state`**, and the limit is enforced (`GraphRecursionError`). The review's objection that this breaks the resume API does not hold on LangGraph 1.x. |
| N-4 | Does a separately compiled child, invoked from an ordinary node, get checkpointed? | **Yes**, when given its own checkpointer and `thread_id`. Both lineages appear in `checkpoints`; child state is independently recoverable. |
| N-4b | Can a pending graph resume without repeating the expensive phase? | **Yes, only with `invoke(None, config)`.** After a crash following `expensive`, a rebuilt graph had `next=('crashy',)` and executed only `crashy` when resumed without fresh input. |
| N-8 | Is the same thread ID plus fresh state safe resume? | **No.** A fresh mapping restarted a pending graph at entry, and also re-entered a completed graph. This requires an inspect/start/resume/reuse API; identity alone is insufficient. |
| N-6 | Can one selector carry both the outcome and the attempts bound? | **Yes.** A single selector returning `passed | repair | exhausted` bounded the loop at 2 attempts. No extra node needed. |
| N-7 | Does `NodeRegistry().extend(shared_registry)` provide `agent_turn`? | **Yes.** UTA must call it — it does not today (zero references in `uta/`). |
| N-2 | Does today's `agent_turn` read `state["prompt_file"]`? | **Yes.** Keeping that read is what preserves compatibility; the revision-2 sketch replacing it with `context["prompt_for"]` would have broken it. |

## The finding neither review made

`checkpoint_ns` is **not** a free-form namespace. LangGraph resolves it as a
**subgraph path**: passing `checkpoint_ns="generation-cycle:v1"` makes
`get_state` raise `ValueError: Subgraph generation-cycle not found`.

ADR-005 specified exactly that, and it does not work. The topology version
belongs in the `thread_id`:

```
uta:generation-cycle:v1:{task_id}:{unit_id}:{workflow_run_id}
```

which the probe then resumed correctly with `None`. This is the kind of thing only running
it finds — both reviews read the ADR and neither caught it.

## Consequences for the design

**Cheaper than believed.** N-5 removes a listed change; N-3 and N-4 remove the
the implementation-coupling objection to the checkpoint approach; N-6 removes
the need for extra nodes to bound repair loops. N-8 restores one critical
requirement: agent-core must own invocation semantics, not only identity.

**Corrections required.** `on_failure="skip"` not `"state"` (N-1); keep the
`prompt_file` read (N-2); version in `thread_id`, not `checkpoint_ns`; add the
`shared_registry` extend to UTA's change table (N-7); inspect the snapshot and
use fresh input only for an absent lineage, `None` for pending, and stored
values for completed (N-8).

## Reproducing

Probes are in the session scratch directory and are throwaway by intent — they
assert against a fake runner and a two-node graph, not against UTA. Anything
worth keeping becomes a real test in the repository that owns the behaviour,
per the verification plan.

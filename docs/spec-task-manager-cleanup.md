# Task Manager Cleanup and Accounting Provenance

## Decision record

This is non-Jira tool work, explicitly confirmed by the requester on
2026-08-19. This document is the requirements source of truth. It must be
approved before the design phase begins.

## Objective

Make `uta.tasks.manager.TaskManager` a small, language- and agent-agnostic
task facade. Remove its remaining OpenCode-specific execution, configuration,
and accounting responsibilities. Task accounting must only report costs that a
provider supplied; UTA must never invent a cost from a model-name price table.

The result should let a future harness/provider work without changes to task
management, while preserving durable task lifecycle, target result, budget,
and reporting behavior.

## Scope discovery

| Candidate | Evidence | Decision | Reason |
| --- | --- | --- | --- |
| `uta/tasks/manager.py` | 2,382 lines; owns creation, lifecycle, results, reporting, OpenCode routing, external DB recovery, and price estimation | In scope | It is the mixed-responsibility boundary to simplify. |
| `uta/testgen/graph/durable_cycle.py` | Calls the former task-level fallback path | In scope | Durable agent turns must use agent-core fallback instead of task requeue. |
| `agent_core.harness.opencode` and `agent_core.harness.runner` | `OpenCodeHarness` already executes `run_turn_with_fallback` | In scope as dependency verification only | Agent-core is the replacement capability; no duplicate routing API belongs in UTA. |
| `uta/testgen/delivery.py` | Imports `_estimate_cost_from_tokens` for live rollups | In scope | Cost persistence must use supplied provider cost only. |
| `uta/testgen/task_guard.py` | Enforces explicit and estimated task budget limits | In scope | Unknown cost must not silently become a fake numeric cost or weaken an explicit hard cap. |
| `uta/tasks/render.py` and `uta/app/task_commands.py` | Render and describe recovered/estimated token-cost data | In scope | They must describe recorded versus unavailable cost without OpenCode DB verification. |
| `uta/tasks/db.py`, `uta/tasks/models.py` | Persist nullable provider/estimate cost fields | In scope only for compatibility-safe persistence/query changes | Existing historical records remain readable; no destructive schema rewrite is required. |
| `uta/shared/config.py` OpenCode runtime settings | Used by agent-core harness configuration | Out of scope | Provider configuration belongs at the agent-core/harness boundary, not in task management. |
| Language Java/Python phase and verification packages | Use task lifecycle/result APIs but do not own provider routing | Out of scope except consumer migration | Keep language behavior unchanged and agent agnostic. |
| Existing old OpenCode task records | May contain routing snapshots and recovered/inferred costs | Compatibility scope | Read them safely but do not create, update, or rely on these values. |

## Requirements

### 1. Task-manager composition

- Split the manager by cohesive responsibility: task creation/estimation,
  lifecycle/queue control, result and usage accounting, and task status/report
  projection.
- Keep `TaskManager` as the stable public entrypoint used by application and
  tests. It may delegate to focused services, but callers should not need to
  understand the composition.
- Shared services must take a `TaskDB` or narrow task-domain inputs; they must
  not import a harness, provider configuration, or language implementation.

### 2. Remove task-level OpenCode routing and fallback

- Delete OpenCode provider-chain snapshots, token snapshots, model-health
  snapshots, selected-model events, and fallback-history mutation from task
  creation, resume, and queue operations.
- Delete `TaskManager.stop_and_resume_for_provider_fallback` and the workflow
  path that asks it to requeue a task.
- A provider fallback stays within the agent-core harness turn. UTA receives
  normalized turn outcomes and records product-level failure/result state only.
- Retire OpenCode-only tests, commands, configuration reads, and docs that
  describe task requeue as provider fallback.

### 3. Remove external OpenCode SQLite recovery

- Delete `DEFAULT_OPENCODE_DB`, OpenCode session/message parsing, token
  recovery, and `opencode_db_path` command/API parameters.
- Do not repair missing task usage by scanning an agent's local database.
- Task summaries must use durable task/operation result records only and state
  that usage is unavailable when no durable value was recorded.

### 4. Cost and estimate provenance

- Delete `_estimate_cost_from_tokens` and every hard-coded model-price branch.
- Persist `provider_cost_usd` and derived actual cost only when a normalized
  provider result supplied a numeric cost. Never label an inferred value as
  provider cost.
- Preserve explicit `hard_cap_usd` enforcement without guessing. Reject a
  currency-capped task when its selected agent cannot guarantee authoritative
  provider cost; block a running cost-capped task before its next paid turn if
  cost becomes unavailable.
- Historical estimates may use completed-task observations only when a
  recorded provider-cost provenance value exists. If no measured history
  exists, store a clearly unavailable estimate rather than fallback
  token/cost/time constants.
- Preserve historical rows containing inferred values as historical data; do
  not rewrite them during this refactor.

### 5. Public behavior and compatibility

- Preserve task IDs, branch/target selection, statuses, result fields, task
  event APIs unrelated to provider routing, and report paths.
- Replace OpenCode verification fields in task summary/rendering with durable
  accounting provenance: `recorded`, `unavailable`, `allocated`, or
  `legacy_unverified`. Existing numeric rows are `legacy_unverified` rather
  than presumed authoritative.
- Existing callers receive the hard-cut summary shape; removed
  `opencode_db_path` CLI/API options must fail clearly, rather than silently
  being ignored.
- Agent-core owns universal turn-cost capability for every harness: it reports
  authoritative/unavailable cost, carries a stable operation identity, and
  runs the neutral next-turn gate. UTA supplies task policy and persists the
  product ledger. The two repositories deploy as a coordinated hard cut, with
  no compatibility release. No persisted-task amount rewrite is part of this
  work.

## Commands and operations

- Task creation, status, stop/resume, clean rerun, and reporting commands
  remain available.
- The task report command no longer accepts or displays an OpenCode local DB.
- Provider fallback is configured and observed through agent-core/harness
  mechanisms; UTA task commands expose only durable task outcomes.

## Project structure target

```text
uta/tasks/
  manager.py          # stable, thin public facade
  creation.py         # task/target creation and measured estimates
  lifecycle.py        # queue, stop/resume, retry and priority operations
  accounting.py       # usage/cost provenance and result rollups
  reporting.py        # task summary/status projection
  db.py               # persistence primitives
```

Names may vary only if the final design demonstrates a clearer established
package convention. `manager.py` must no longer contain provider-specific
imports or model pricing.

## Code style and boundaries

- Core task/accounting code is provider-, agent-, and language-agnostic.
- Agent-core owns harness provider fallback and model/runtime configuration.
- UTA owns product task lifecycle, durable result projection, and user-facing
  reporting.
- Use explicit provenance fields/enums rather than boolean flags or guessed
  numeric values.
- Do not add a compatibility bridge that keeps the deleted behavior reachable.

## Testing strategy and acceptance criteria

1. Source-boundary tests prove task modules do not import OpenCode routing,
   OpenCode configuration, or local agent SQLite parsing.
2. Durable agent-core fallback is exercised without task requeue or persisted
   provider-routing metadata.
3. Creation with no measured history produces an unavailable estimate, not a
   hard-coded token, time, or cost estimate; measured history continues to
   produce measured estimates.
4. Result rollups aggregate each durable operation once, record
   provider-supplied cost, preserve `None`/unavailable cost otherwise, and
   never calculate a cost from tokens. Shared class allocations are distinct
   from provider charges.
5. Explicit task, class, and global hard-cap behavior is covered for known and
   unknown cost; unknown cost never creates a false budget pass or false
   numeric total.
6. Task summaries, dashboard, repair, and CLI reports use durable records
   only; no OpenCode DB
   option, query, or recovery remains.
7. Existing task lifecycle, branch, target, and language Java/Python durable
   integration tests remain green.
8. `TaskManager` is materially reduced and contains no provider-specific
   configuration, fallback, external recovery, or price logic.
9. Every agent-core harness implements the same cost contract and rejects a
   required currency cap before paid work if it cannot report an authoritative
   provider charge.
10. Every paid attempt has one monotonic operation-local ordinal across retry,
    fallback, and recovery. A cost sink retries synchronously, then creates an
    owner-only spool artifact for repair/adoption without a model replay.
11. Global caps use serialized admission; legacy-unverified capped tasks block
    on resume; deployment drains/fences old workers before migration.

## Non-goals

- Changing the agent-core provider-routing policy or provider configuration
  format.
- Repricing, backfilling, or rewriting historical task accounting.
- Changing language-specific test generation and verification policy.
- Migrating persisted legacy tasks; that is explicitly excluded from this
  refactor.

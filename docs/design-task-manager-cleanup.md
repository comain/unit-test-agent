# Design: Task Manager Cleanup and Accounting Provenance

## Document status

Derived from the approved non-Jira [spec](spec-task-manager-cleanup.md) on
2026-08-19. This is a coordinated hard cut across agent-core and UTA. Both
repositories use the new universal turn-cost contract together; there is no
old/new compatibility or rollback release.

## Table of contents

1. [Goals and non-goals](#goals-and-non-goals)
2. [High-level design](#high-level-design)
3. [Data and process flow](#data-and-process-flow)
4. [Repository detail](#repository-detail)
5. [Contracts and schema compatibility](#contracts-and-schema-compatibility)
6. [Tradeoffs](#tradeoffs)
7. [Capacity reliability and security](#capacity-reliability-and-security)
8. [Failure-mode handling](#failure-mode-handling)
9. [Rollout](#rollout)
10. [Verification](#verification)
11. [First-principles check](#first-principles-check)

## Goals and non-goals

### Goals

- Make `TaskManager` a stable, thin facade over coherent task services.
- Remove all OpenCode-specific configuration snapshots, task-level fallback
  requeueing, and agent-local SQLite recovery from UTA task management.
- Record only provider-supplied cost and make unknown cost explicit.
- Preserve durable Java/Python task lifecycle and product result reporting.

### Non-goals

- Changing agent-core model routing or fallback policy.
- Backfilling or rewriting historical records.
- Migrating persisted legacy tasks or changing language verification policy.

## High-level design

Agent-core will add one universal turn-cost capability plus session fallback:

- fallback-aware phase sessions: the session `run_turn` contract invokes the
  existing bounded `run_turn_with_fallback` policy and returns only the final
  exhausted outcome;
- `TurnCost`: every harness returns `recorded` with a raw provider charge or
  `unavailable`; it never calls a price table. The normalized result carries
  the immutable turn operation identity and an exact sum of raw attempt costs.
  A mandatory `on_cost` port receives each attempt before workspace
  acceptance/close so paid rejected turns remain charged.
- `CostGate`: before each paid attempt, agent-core calls the product-provided
  neutral gate. A required cap rejects before a provider call when the harness
  cannot report an authoritative charge; unavailable cost blocks the next
  paid attempt/turn.

`TaskManager` will then become a compatibility facade that constructs four
focused, provider-neutral services over one `TaskDB`:

```text
application / workflow / report command
                 |
            TaskManager
  +--------------+---------------+----------------+
  |              |               |                |
creation     lifecycle       accounting        reporting
  |              |               |                |
branch+task     state/event      durable usage      task DB only
target rows     operations       + result rollup    summary/status
```

The facade retains the established public methods and delegates without
changing task IDs or lifecycle semantics. Modules depend only on `TaskDB`,
task models, target helpers, and result data. They never import provider
configuration, model routing, harness sessions, or language adapters.

The agent-core session contract will use a neutral per-candidate session
factory, rather than calling the process-only runner directly. Each candidate
gets isolated configuration/workspace and a fresh session; it closes before
the next candidate; cancellation stops the chain; raw costs accumulate across
attempts; and only the final snapshot/session ID enters the normalized result.
UTA writes ordinary product state and never selects/requeues a provider.

## Data and process flow

### New task

```text
CLI/CI config (product-only) → CreationService
  → TaskDB repo task + targets + task_created event
  → measured history query (one aggregate query)
  → estimate snapshot: measured | unavailable
```

`uta.app.cli._task_config_snapshot` will stop placing OpenCode provider,
model, token, candidate, or fallback fields in the persisted product snapshot.
Harness configuration remains runtime configuration in agent-core.

### Durable generation and accounting

```text
agent-core session turn with fallback
  → per-attempt `on_cost` durable accounting entry
  → normalized turn result (usage, operation identity, authoritative cost)
  → UTA durable cycle / delivery
  → AccountingService.sync_results
  → class rows, repo aggregate, task events
  → ReportingService status/summary/live status
```

If cost is unavailable, accounting stores usage but leaves cost unavailable.
It must not call a pricing function, inspect a model ID, or open an agent-local
database. `on_cost` writes each operation/attempt exactly once before product
workspace acceptance; task currency cost is an SQL sum of immutable entries.
Before every paid attempt, the agent-core gate reads that ledger through a UTA
callback and rejects missing-authority or exhausted-cap work.

### Fallback

```text
UTA agent_turn → agent-core phase session.run_turn
  → agent-core run_turn_with_fallback (same turn, bounded candidates)
  → normalized completed/error result
  → UTA durable outcome handling
```

The former `stop_and_resume_for_provider_fallback` path and its task events
are deleted. A final exhausted-provider outcome is treated as an ordinary
durable provider error; it does not mutate a task configuration snapshot.

## Repository detail

### Module changes

| Module | Responsibility after change | Main removals/migrations |
| --- | --- | --- |
| `uta/tasks/manager.py` | Public façade plus generic identity helpers | OpenCode imports/config, routing, recovery, price table; delegates to services. |
| `uta/tasks/creation.py` | Branch/task/target creation and measured estimates | Replace fallback estimate constants with unavailable estimates. |
| `uta/tasks/lifecycle.py` | Queue/start/stop/resume/retry/priority and generic task events | Delete provider fallback requeue operation. |
| `uta/tasks/accounting.py` | Immutable operation/attempt accounting ledger, provenance, SQL rollups, result synchronization, commit rollups | Delete model-price derivation and external session recovery. |
| `uta/tasks/reporting.py` | Task status, summary, live-status projection | Report durable accounting provenance only. |
| `uta/testgen/delivery.py` | Supplies normalized result/usage to accounting | Stop importing the deleted estimator; write provider cost only if supplied. |
| `uta/testgen/graph/durable_cycle.py` | Durable outcome orchestration | Delete task-manager provider-fallback call path. |
| `uta/app/cli.py`, `uta/app/generation_commands.py`, `uta/app/task_commands.py`, `uta/app/repair.py`, `uta/app/reporting.py`, `uta/tasks/render.py` | Product task configuration and UX | Remove provider snapshot/requeue paths, `--opencode-db`, OpenCode verification rendering, and all unknown-to-zero cost rendering. |
| tests/docs | Lock neutral boundaries and new provenance | Delete OpenCode-only recovery/fallback/price tests and documentation. |

`TaskManager` target is fewer than 400 lines: constructor, `db_path`, stable
delegating methods, and small generic helpers retained only where an existing
dependency truly needs them. The services are not a generic framework; they
are four direct classes/functions grouped by existing task responsibility.

### Intra-repository control flow

1. Creation validates/persists a durable product config snapshot using the
   existing generation-engine normalization.
2. Creation runs one indexed aggregate SQL query over completed comparable
   tasks. It includes an estimated currency value only if recorded
   provider-cost history exists; otherwise its estimate source is
   `unavailable` and numeric estimate fields are `NULL`/absent. The schema
   adds the supporting provenance filter/index rather than claiming the
   existing language-only index is sufficient.
3. Lifecycle retains current transactional status guards, including durable
   engine eligibility. Provider errors remain regular terminal/retry outcomes;
   there is no provider-specific lifecycle branch.
4. The `on_cost` callback inserts an immutable accounting row keyed by
   `(operation_id, attempt_ordinal)` before product workspace acceptance. A
   unique key makes resume/retry idempotent; task/class totals are SQL-derived
   from these rows, never independently incremented. It records
   `recorded` when numeric cost exists, `unavailable` when it does not,
   `allocated` only for a declared shared-batch class allocation, and
   `legacy_unverified` for old numeric rows. It never treats an allocation as
   a provider-reported class charge.
5. Reporting reads only task DB rows and presents the selected provenance. It
   never initiates recovery or external I/O.

## Contracts and schema compatibility

### Persisted fields

No destructive migration is required. Existing nullable fields are retained:

- Add `workflow_operation_accounting`, keyed by `(operation_id,
  attempt_ordinal)`, with immutable provider cost, provenance, token usage,
  and class allocation reference. Add nullable `cost_provenance` fields to
  repo/class rows as derived display projections. Existing rows read as
  `legacy_unverified`; new writes are exactly `recorded`, `unavailable`, or
  (class row only) `allocated`.
- `provider_cost_usd` is the only new-write source for recorded currency cost.
- `actual_cost` mirrors recorded `provider_cost_usd` for compatibility; it
  remains `NULL` when unknown. A class allocation uses a distinct allocation
  field/payload so it cannot be mistaken for provider cost.
- `estimated_cost_usd` remains nullable.
- `estimate_snapshot_json` records `estimate_source` as `historical_recorded`
  or `unavailable`; unavailable snapshots omit numeric cost/token/time
  estimates.

This is an additive schema migration with explicit default reader semantics;
no amount is backfilled. It includes a composite/partial historical-estimate
index covering repository, module, language, completion status, and recorded
cost provenance. The coordinated hard cut prohibits older UTA writers after
the migration, preventing inferred-cost writes from corrupting provenance.

### Summary contract

`build_task_summary(task_id, *, recalc_project_coverage=False)` removes
`opencode_db_path`. Its token section becomes a versioned task-ledger view:

```json
{
  "tokens": {
    "recorded": {"input": 0, "cache_read": 0, "output": 0,
                 "reasoning": 0, "total": 0, "cost_usd": null,
                 "cost_provenance": "unavailable"},
    "coverage": {"classes_with_usage": 0, "classes_without_usage": 0}
  }
}
```

The hard cut removes the previous verification views. The CLI removes
`--opencode-db` immediately with a clear incompatible-option error; silently
ignoring it would conceal the removal.

### Budget policy

- Turn limits continue to be enforced before a turn, independent of money.
- Every agent-core harness exposes `TurnCost` and `CostGate`; no product-side
  provider capability discovery exists. At UTA task creation/resume, the
  application builds the configured harness and invokes neutral cost-gate
  preflight. Required caps reject before queue mutation if the harness cannot
  report authoritative provider cost.
- An explicit `hard_cap_usd` and estimate-derived cap are evaluated only from
  recorded provider cost.
- If a cost-capped running task receives unavailable cost, agent-core blocks
  before its next paid attempt/turn and UTA records `budget_cost_unknown`;
  resume is allowed only after an operator removes the cap or changes agent
  policy.
- The daemon excludes unknown-cost tasks from a global currency-cap queue;
  global cap accounting never coalesces unknown to zero.
- Accounting writes are atomic immutable inserts followed by SQL-derived
  projections; no external provider or DB reads are added on the hot path.

## Tradeoffs

- We will hard-cut both repositories to the universal agent-core `TurnCost` /
  `CostGate` contract. Shared code owns candidate ordering, cost collection,
  isolation, and next-turn admission; UTA owns product ledger persistence and
  cap policy. A compatibility release was rejected because it preserves old
  inferred-cost writers.
- We will expose unavailable cost rather than calculating a price. A numeric
  guess is more convenient but incorrect and incompatible with provider- and
  agent-agnostic task management. See [ADR-010](decisions/ADR-010-task-accounting-provenance.md).
- We will use direct task-domain modules rather than a new service container.
  The manager needs decomposition, not another abstraction layer.
- We will preserve historical values without backfill. Repricing historical
  records would be non-authoritative and is outside the requested cleanup.

## Capacity, reliability, and security

- Creation keeps one composite-indexed aggregate query per new task; no per
  target query is added. Target creation will use the existing DB bulk insert
  path (or add one) rather than opening one write transaction per target.
- Result rollup remains bounded by the current result batch. It retains the
  existing aggregate task DB query and removes OpenCode DB scans that could
  read up to 12 sessions and every assistant message per missing class.
- Summary becomes task-DB-only: one task lookup, one class list, one aggregate
  query, and optional existing coverage recomputation. It removes external
  filesystem/database reads.
- Provider routing secrets, token status, and agent-local session contents are
  no longer copied into task configuration snapshots or reports.
- Existing `TaskDB` transaction boundaries remain responsible for lifecycle
  consistency. Accounting errors preserve the task result and mark accounting
  unavailable rather than re-running a model turn.

## Failure-mode handling

| Failure | Detection | Handling | Blast-radius containment |
| --- | --- | --- | --- |
| Provider fallback chain exhausted | Final normalized durable turn error, never `fallback_eligible` after all candidates | Record terminal provider error/result; no requeue or snapshot mutation | One turn/unit; no duplicate paid work. |
| Provider omits cost | `TurnCost(unavailable)` | Reject cost-capped creation; for a running cost-capped task block before next paid turn | No invented cost or false budget pass. |
| Cost sink fails | `on_cost` callback error | Agent-core stops before product acceptance or any next paid attempt; UTA leaves the operation unreconciled for explicit repair | No unaccounted paid work. |
| Old task has inferred cost/routing fields | Provenance classification | Read as `legacy_unverified`; never update using old OpenCode data | Historical data remains readable, new writes are clean. |
| CLI caller passes removed option | Click option validation | Fail before command execution with migration guidance | No silent wrong report. |

## Rollout

1. Land and deploy agent-core universal `TurnCost` / `CostGate` and the
   fallback-aware session contract.
2. Immediately land and deploy the matching UTA hard cut: immutable accounting
   ledger, provenance migration, task-domain split, and source-boundary tests.
   Old UTA binaries are not permitted after the migration.
3. Run focused task lifecycle/accounting/reporting tests, then full UTA suite.
4. Verify a managed durable canary has no `opencode_*` config/event data,
   exposes a provider-recorded or unavailable cost correctly, and completes
   without an agent-local SQLite read.
5. Recovery is forward-only: repair a failed deployment with the coordinated
   revisions. Restore a pre-cut SQLite backup only before any post-cut
   accounting row is written; do not run old UTA against a migrated database.

## Verification

- Agent-core unit/integration: every harness satisfies `TurnCost`/`CostGate`;
  phase-session fallback attempt lifecycle, final exhausted semantics,
  reported/zero/missing cost, cost-sink failure, and multi-attempt aggregation.
- Unit: creation measured/unavailable estimates; accounting operation
  deduplication and shared-batch allocation; currency and token budget
  behavior; every status/summary/dashboard/repair/report provenance surface.
- Boundary: production source scan prohibits OpenCode config/router/local-DB
  imports in `uta.tasks`; no `_estimate_cost_from_tokens` remains.
- Integration: Java and Python durable cycles exercise agent-core fallback
  without task stop/requeue; result accounting remains task-DB-only.
- CLI: summary JSON/table output has no OpenCode DB option or verification
  table and gives clear unavailable-cost output.
- Regression: full UTA test suite, type/compile/lint checks, and dirty-tree
  review that excludes unrelated user files.
- Production proof: query one new task config/event payload for absence of
  `opencode_` keys; observe a completed durable task with task-ledger
  accounting provenance; process tracing confirms no `opencode.db` open.

## First-principles check

1. **Goal:** make task management neutral and maintainable while reporting only
   trustworthy task accounting.
2. **Simplest right solution:** split existing responsibilities into four
   direct task modules and reuse agent-core fallback; no new framework, price
   registry, migration, or compatibility adapter is introduced.
3. **Production proof:** a canary task completes with no provider snapshot or
   external DB read and reports `recorded` or `unavailable` provenance from
   durable task data.
4. **Worst case and guard:** an invented/unknown cost permits paid work beyond
   a requested cap, a rejected turn loses its charge, or fallback replays paid
   calls. Agent-core `TurnCost`/`CostGate`, the immutable product accounting
   ledger, fail-closed admission/next-turn checks, and final same-turn fallback
   semantics prevent those actions.

## Review disposition

The independent design review on 2026-08-19 identified three Critical and six
Important findings. The requester approved their fixes on the same date:
agent-core-owned universal cost/gate capability; fail-closed currency caps;
additive provenance with legacy classification; operation-deduplicated
shared-batch accounting; hard-cut summary/rollout; all cost/report surface
migration; app-level fallback removal; and coordinated deployment. A fresh
review is required after these updates.

## Final hard-cut safety rules

- `on_cost` retries synchronously; after retry exhaustion agent-core writes a
  confined cost-spool artifact and fails closed. UTA's adopt command inserts
  that same `(operation_id, paid_attempt_ordinal)` row without replaying a
  turn. Harness exceptions after submission emit `unavailable` cost records.
- `paid_attempt_ordinal` is one monotonic sequence per operation across
  acceptance retry, fallback candidate, and recovery; it never resets. If any
  paid attempt is unavailable, the normalized aggregate is unavailable.
- `workflow_operation_accounting` is the sole currency source. `sync_results`
  validates/report aggregates only and never inserts or increments currency.
- Global-cap admission holds a serialized DB lease through the provider-attempt
  reservation; bounded overshoot is not permitted. Legacy-unverified capped
  tasks cannot resume until cap removal or clean rerun.
- Hard cut procedure: stop dequeue; drain/terminate workers; verify zero old
  writers; fence DB protocol version; deploy both revisions; migrate; run
  pre-traffic smoke/canary; reopen dequeue. Recovery is forward-only.

## Review waiver

On 2026-08-19 the requester explicitly waived the remaining crash window
between provider submission and durable attempt reservation. A worker/host
failure in that interval may require manual provider reconciliation and can
otherwise permit an unrecorded replay. This risk is accepted to proceed with
the hard-cut implementation; all other reviewed safeguards remain required.

# Executable generation-cycle operator guide

Status: durable-only repository implementation complete. Production deployment
remains separately authorized; K2 beta replay/capacity evidence was explicitly
skipped and no real-traffic p95 claim is made.

## Durable-only cutover

Every new task records `generation_engine_version=2` and executes the
declarative cycle. The compatibility boolean is dual-written true for rollback
only and is not an engine selector. Every run records
`generation_engine_selected` (`durable_v2`) and
the task status payload reports `started`, `resumed`, and `reused_completed`
workflow disposition counts. These are the first checks when a checkpoint
appears not to resume.

Before production deployment, drain all workers and create a consistent SQLite
backup using `.backup`/the SQLite backup API; run `PRAGMA integrity_check` on
the backup and do not copy or restore live WAL/SHM sidecars. Then run:

```text
uta tasks audit-generation-cutover --task-db /path/to/tasks.db
```

Exit 0 with zero blockers is required. Exit 2 identifies legacy/invalid
non-terminal tasks or inconsistent active child rows. Finish or cancel those
tasks before deployment. If their work is still needed, submit a new task after
the durable-only build starts; persisted legacy tasks are never migrated or
resumed in place.

## Live progress

Open the ordinary report page. Deterministic work appears in the unit timeline;
each model session has its own tab. A tab shows bounded public-safe reasoning
synopses and tool activity/context, result summaries, status, timing and usage.
It never exposes raw reasoning, commands, tool output or prompts. Parallel
sessions are never merged.

The browser reconnects to the fix session's `/progress/events` endpoint with
its last event ID.
Temporary disconnects backfill missed rows. A terminal event closes the
stream. The existing JSON detail endpoint remains available as a snapshot
fallback.

Progress is sampled and bounded: at most two ordinary updates per second per
session, 2,000 per session, and 20,000 rows or 20 MiB per task. If capped, the
report shows `progress_truncated`; phase outcomes and terminal summaries remain
available. Detailed progress rows expire 30 days after task completion.

## Resume states

- `started`: no checkpoint exists; the cycle begins with product-evidence
  reconciliation.
- `resumed`: a pending checkpoint continues at its stored next node.
- `reused_completed`: the stored terminal state is returned without executing
  the graph again.
- checkpoint corruption: the task stops with `WorkflowCheckpointError`; it is
  never silently restarted.

If a worker stopped after a model edit, reconciliation may verify the existing
edit before opening another session. `fail_unsafe` means stored identity,
fingerprints, prerequisites or edited paths conflict and require diagnosis.
Recovery evidence includes clean relevant source/test bytes as well as dirty
workspace content. A model turn that changes repository `HEAD` is rejected:
commit and push remain the outer delivery node's responsibility.

Ordinary recovery of a durable task preserves the same lineage:

```text
uta tasks resume TASK_ID
```

This remains true with `--force-rerun-failed` or `--force-rerun-all`; those
flags change which product rows are requeued, not their workflow/batch keys.

For a confirmed corrupt or deliberately abandoned durable lineage, use the separate
clean-rerun command:

```text
uta tasks clean-rerun-generation 123 \
  --reason "checkpoint corruption: TASK-456" \
  --confirm-task-id 123
```

The task must be `STOPPED` or terminal and have no active lease. The repeated
task ID is a non-interactive confirmation, and a reason is mandatory. This
operation mints a new generation run and can repeat model work; add
`--force-rerun-failed` or `--force-rerun-all` only when that is intended. It
atomically records the operator, reason, every prior/new run identity and
requeue policy.
Superseded checkpoints, ledger evidence and artifacts remain read-only for the
30-day audit/retention period.

The command also repairs all-missing keys, mixed missing/known keys and several
parseable old run IDs in one transaction, retaining every known old lineage for
cleanup. It refuses a malformed non-null key because no safe checkpoint
identity can be derived; capture that diagnostic for engineering repair rather
than editing the database.

Do not delete the SQLite file or edit LangGraph tables. On
`WorkflowCheckpointError`, first capture the error and task report, confirm the
task is no longer leased, then use the clean command above. A command failure
leaves the old identity and all batch keys unchanged.

## Storage and retention

The product DB contains authoritative operation evidence. The dedicated
`workflow-state/checkpoints.sqlite` contains graph position and serializable
state; `workflow-state/results/` contains normalized operation artifacts.
`workflow-state/prompts/` contains managed durable prompt artifacts and their
safe identity sidecars. These paths are outside target Git
repositories, are never report content, and share the workflow lineage's
30-day retention. Standalone Java/Python commands allocate a run UUID under
`standalone-generation`, colocate a temporary task DB, checkpoints, results,
and prompts, hold an advisory lease while live, remove the entire root when the
command closes, and leave crash orphans for the same 30-day pruner. Prompt
placement uses `UTA_RUNNER_HOME` or
`~/.local/share/uta`, not `UTA_TASK_DB_PATH`, and refuses a resolved path inside
the target repository. The directory and files must remain owner-only (`0700`
and `0600`). Do not edit LangGraph tables or move prompts into `.uta_cache`.

The workflow application closes its bounded progress sink before closing the
SQLite checkpointer. Progress flush failures are diagnostic-only and never
change the authoritative turn or operation result.

The daemon prunes eligible terminal lineages at startup and every six hours;
default retention is 30 days. Operators can run:

```text
uta tasks prune-workflows --older-than-days 30
```

The command resolves eligible product identities and delegates deletion to
agent-core. Investigate when checkpoint storage reaches 2 GiB or 80% of its
volume. The representative 1 MiB/120-step fixture uses 243.52 MiB per unit, so
nine retained units already cross the absolute alert and a 20-unit task can
reach 4.756 GiB. Treat the alert as an early stop signal, not a capacity
guarantee. Before production, derive the retained-byte budget from beta p95
payload size, super-step count, units per task, 30-day task count, and actual
volume size. See `docs/evidence-prompt-checkpoint-capacity.md` for the measured
matrix and formulas.

## Deployment evidence

Record the durable engine selection, full executed phase route,
gate outcomes, token and timing comparison, checkpoint bytes, session-tab SSE
events, reconnect cursor behavior, and a deliberate worker restart after an
expensive operation. The stable operation ID must execute only once and the
resume/reused-completed counter must increment.
For shared prompt acceptance, also record the submitted prompt SHA-256 against
the frozen durable fixtures, confirm artifact paths are below
`workflow-state/prompts`, inspect `inputs.json` against the safe schema, and
prove `git diff --cached --name-only` contains no prompt or inputs artifact.
Run these checks on one managed durable canary and one standalone call. K2 was
skipped, so this repository change does not claim beta p95 capacity or authorize
production deployment.

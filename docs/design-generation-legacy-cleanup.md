# Generation legacy cleanup design

This design implements `docs/spec-generation-legacy-cleanup.md` in UTA only.
agent-core 0.6.1 already supplies the neutral harness, `agent_turn`, prompt
materialization, progress, and SQLite checkpointer contracts.

## Architecture

There is one execution topology:

```text
batch entrypoint
  -> managed task binding OR ephemeral standalone binding
  -> outer test-generation graph
  -> persisted stable unit
  -> generation-cycle.yaml
  -> prompt -> agent_turn -> interpret -> deterministic verification
  -> outer delivery/finalization
```

Language adapters bind phase operations and a neutral harness. They do not
provide a composite generation method and do not unwrap a legacy runtime.

## Persisted cutover

Persisted-task migration is intentionally omitted. New task creation always
stores `generation_engine_version=2` and also writes the historical
`generation_cycle_v2_enabled=true` compatibility field. The cleanup code never
branches on that boolean, but dual-writing it makes rollback to the current
durable-capable release select the durable path. Before a
worker changes a task to `RUNNING`, acquisition and direct execution validate
that a non-terminal task carries that version. An old/missing/false snapshot is
left unchanged and refused with the exact supported action: finish/cancel the
old task before deployment, then submit a new task after the durable-only build
starts. `resume_task`, force-unblock, and existing clean-rerun entrypoints apply
the same check before mutation and cannot requeue legacy history in place.

Terminal legacy rows remain readable history. They are not resumable in place.
The pre-deploy audit is read-only and must report zero legacy rows in
`CREATED`, `QUEUED`, `RUNNING`, `STOP_REQUESTED`, or `STOPPED`, and zero active
class rows under an otherwise terminal task. An active class row is any status
not in `TERMINAL_CLASS_STATUSES`; unknown repo or class statuses are blockers.
It never updates snapshots,
statuses, batch keys, prompt-scope rows, or lineage. Deployment requires all
workers drained between the audit and restart.

Engine classification strictly parses `config_snapshot_json` as a JSON object.
Version is durable only when its JSON type is integer (not boolean) and value
is `2`; malformed JSON, non-object JSON, strings, booleans, missing values, and
other integers are legacy/invalid blockers. The audit scans `repo_tasks` once,
joins active class counts by indexed `repo_task_id`, prints stable JSON
`{task_id,status,reason}`, exits 0 for zero blockers and 2 otherwise, and never
opens a write transaction.

`TaskDB.acquire_next_task` performs the version check inside its existing write
transaction and before its `RUNNING` update. `resume_task`, clean rerun, and
force-unblock check before their first acknowledgement, snapshot, status, or
class mutation. Lower-level task creation normalizes omitted configuration and
rejects an explicit legacy value, covering callers that bypass `TaskManager`.

## Standalone durable binding

`open_standalone_generation_execution` owns one confined UUID lease directory
`<application-root>/standalone-generation/<uuid>` and creates inside it:

- `tasks.sqlite` with one synthetic repo task and class rows;
- owner-only `workflow-state/{checkpoints,results}` directories;
- `prompts/<workflow-run>/<unit>/<operation>` durable prompt directories;
- an invocation context with local/no-op progress and product cancellation.

The batch request is copied with internal `task_id`/`task_db_path`. The returned
public `final_state` uses an explicit allowlist and restores the caller's
original `task_id`/`task_db_path`; it excludes workflow/unit/operation IDs,
backend context, checkpoint paths, and synthetic events recursively. On normal
completion, all handles close before the owner removes the entire confined
root. On crash, the OS releases its `flock`; the 30-day pruner handles the whole
root with ancestry, symlink, UUID, and active-lease checks.

Standalone is a single-call API. It does not promise cross-call stop/resume or
the product task-level stop/requeue provider fallback. The configured neutral
harness may perform its own bounded provider routing inside a turn; if the
normalized result still says fallback-eligible, standalone returns the same
terminal failure/result fields as today and never mutates or enqueues its
synthetic task. Managed execution retains product stop/requeue fallback and SSE.

The public standalone state projection retains exactly `results`,
`session_ids`, `session_token_usage`, `session_retrospect`,
`phase_token_usage`, `phase_timings`, `current_stage`, `error`, `finished`,
`stopped_early`, `current_batch`, `current_target_batch`, `current_class`, and
delivery/result summary fields already present in the language result. It sets
`task_id` and `task_db_path` back to their original values and recursively
rejects `workflow_run_id`, `unit_id`, `operation_id`, `backend_context`, and
checkpoint/result/prompt paths. Java and Python snapshot tests freeze this
projection against their pre-cleanup public result contract.

## Code deletion and extraction

Delete:

- `AgentRuntime`, `create_agent_runtime`, and `uta.testgen.llm_session`;
- the legacy selector and `generation_cycle_v2_enabled` state/config surface;
- Java `generate_and_validate` and Python `run_python_batch_request` composite
  orchestration plus runtime-factory compatibility bridges;
- backend protocol/module-level composite cycle methods;
- legacy prompt directory/audit/retention runtime paths;
- the concrete-client `resume-gates` path.

Keep `create_agent_harness` in `uta/testgen/harness.py` and add a neutral
`harness_factory` invocation-context seam shared by Java and Python. It replaces
Python runtime/client factory signature introspection and supports scripted
tests without concrete-agent knowledge.

Move Java verification port factories to
`uta/language/java/phases/ports.py`, generation-plan helpers to the existing
`generation_plan.py`, and prompt-only helpers to their owning phase modules.
Move Python test-file/path/verification helpers to
`uta/language/python/test_artifacts.py` and `verification.py`. A source test
forbids durable phase/backend imports from the composite modules before their
orchestration is deleted.

Legacy prompt retention remains as a narrow data-lifecycle reader for one
30-day retention window after the last legacy task. It has no execution caller.
The table creation and read-only pruner remain in fresh/upgraded schemas during
that window; the audit writer and legacy prompt creation are removed now. The
isolated table/reader/pruner are deleted in a later schema release only after
retention reports zero artifacts.

## Failure handling

| Failure | Behavior |
| --- | --- |
| pre-deploy audit finds legacy task | refuse deployment/execution; print task and finish/cancel/new-task guidance |
| old producer writes legacy snapshot | creation/acquisition guard refuses it before RUNNING |
| standalone setup failure | close handles and remove newly created safe scope |
| standalone crash | OS releases lease; bounded orphan retention applies |
| durable checkpoint/result corruption | existing explicit workflow checkpoint error; no legacy fallback |
| provider failure | existing normalized diagnostics and provider fallback policy |
| downgrade attempt | refuse; require backup restore for a pre-durable binary |

## Capacity, security, and performance

The managed path adds no new per-turn storage beyond the already measured
durable workflow. Standalone storage has the same bounds but is normally
deleted immediately. K2 was skipped, so the documented worst-case SQLite
figures remain planning bounds rather than beta p95 evidence.

The cutover audit is one linear read-only SQLite scan with no network calls;
the operator command reports row count and elapsed time rather than pretending
an unknown production DB has a fixed bound.
Paths are resolved and confined, symlinks are rejected, directories
are `0700`, files are `0600`, and synthetic identities are excluded from
product progress/report APIs.

## Rollout

1. Drain workers, then create a consistent SQLite backup using the SQLite
   backup API/`.backup`; run `PRAGMA integrity_check` on the backup. Do not copy
   a live database file or restore stale WAL/SHM sidecars.
2. Stop/cancel or finish all old non-terminal work.
3. With all workers drained, run the read-only cutover audit and require zero
   blockers; do not create more work with the old binary afterward.
4. Deploy the cleanup build and restart workers.
5. Submit required replacement work as new tasks, then verify one managed
   canary and one standalone run.
6. Roll back to the current durable-capable release, which reads the dual-written
   true compatibility flag. Restore the database backup for any older release.

This repository change does not execute steps 1-5 in production.

## Verification

- focused cutover-audit, standalone, graph, phase, prompt, retention, and
  delivery suites after each slice;
- source-boundary checks in the same commit as deletion;
- full UTA suite (`.venv312/bin/python -m pytest -q`) and CR consumer suite
  against agent-core 0.6.1 (`pytest -q` in `cr_plugin`);
- documentation/diff/compile checks;
- detailed commits with remote ref verification.

Post-deploy evidence is: the audit returns zero blockers; the managed canary
has completed `workflow_operations` plus workflow terminal events; and the
standalone call leaves no leased execution root after close. Because K2 was
skipped, no beta real-traffic capacity or p95 claim is made.

# Production Hardening Plan

## Summary

UTA production hardening targets a single-host venv runner with branch-only publishing. The runner should use a local SQLite task database as the source of truth for production progress, support daemon mode that continuously polls for runnable work, and provide both terminal and HTML live monitoring.

Core finalized decisions:

- Use the existing `docs/` directory for production planning documentation.
- Start with a single-host venv runner, not CI or a distributed worker service.
- Use SQLite as the v1 local task store.
- Model one repo production request as a repo task, with one child task per class or batch.
- Support task management at both repo-task and class-task level, including create, start, stop, resume, cancel, and priority management.
- Run daemon mode by polling the task DB, executing runnable tasks, and waiting when no task is available.
- Reuse the same active branch for multiple tasks against the same repo unless explicitly overridden.
- Commit and push independently after each class or batch.
- Add terminal live monitoring and auto-refreshing HTML status.
- Track estimated and actual token, cost, and elapsed-time totals.
- Enforce runtime diff safety only around LLM-authored changes, not deterministic UTA nodes.
- Document which configuration changes require daemon restart, which affect only newly started tasks, and which can take effect immediately.

## Key Changes

Add task management facilities:

- `uta/tasks/db.py`: SQLite schema, migrations, transactional updates, and query helpers.
- `uta/tasks/models.py`: status enums, priority model, and typed row dataclasses.
- `uta/tasks/manager.py`: create, start, stop, resume, cancel, and reprioritize operations.
- `uta/tasks/scheduler.py`: runnable task selection, same-repo locking, daemon polling, and heartbeat updates.
- `uta/tasks/render.py`: terminal and HTML/JSON status rendering helpers.

Add CLI management commands:

- `uta tasks create --repo PATH --module MOD [--class-fqn FQN ...] [--all] [--priority N] [--branch-name NAME|--new-branch]`.
- `uta tasks start <task-id>` to start or continue one task.
- `uta tasks start --next` to start the highest-priority runnable task once.
- `uta tasks daemon` to continuously execute runnable tasks from the DB.
- `uta tasks stop <task-id> [--reason TEXT]` to request cooperative stop.
- `uta tasks resume <task-id>` to resume stopped unfinished child rows.
- `uta tasks cancel <task-id>` to cancel not-yet-started work.
- `uta tasks reprioritize <task-id> --priority N`.
- `uta tasks reprioritize-class <class-task-id> --priority N`.
- `uta tasks list [--status STATUS] [--repo PATH]`.
- `uta tasks show <task-id> [--show-classes]`.
- `uta tasks watch <task-id> [--interval 2] [--show-sessions]`.
- `uta tasks export <task-id> --format json|html`.

Add production execution entrypoints:

- `uta run --production --task-id <id>` executes one task.
- `uta run --production --resume-task <id>` resumes unfinished child rows.
- Manifest runner creates tasks first; either the daemon or `start --next` executes them.

Add deployment and usage docs later, outside this docs-only step:

- `docs/production-usage.md` for operator-facing usage.
- README production quick start.
- `design/architecture.md` updates for task DB, daemon scheduler, live monitoring, restart-sensitive config, and safety boundaries.

## Task DB Schema

Use SQLite as the local v1 task store. Default DB path:

- `$UTA_RUNNER_HOME/uta_tasks.db` when `UTA_RUNNER_HOME` is configured.
- Otherwise `~/.local/share/uta/uta_tasks.db`.

Configuration:

- `--task-db PATH`
- `UTA_TASK_DB_PATH`
- `--task-id ID`
- `--resume-task ID`

Tables:

- `schema_version`
- `repo_branches`
- `repo_tasks`
- `class_tasks`
- `task_events`
- `task_control`
- `runner_heartbeats`

`repo_branches` stores active branch records:

- `id`
- `repo_path`
- `base_ref`
- `branch_profile`
- `branch_name`
- `active`
- `created_at`
- `updated_at`
- `last_task_id`

Branch lookup key:

- `(repo_path, base_ref, branch_profile)`

`repo_tasks` stores one row per production task request:

- `id`
- `repo_path`
- `repo_slug`
- `module_filter`
- `selection_json`
- `branch_id`
- `branch_name`
- `base_ref`
- `status`
- `priority`
- `config_snapshot_json`
- `budget_config_snapshot_json`
- `estimate_snapshot_json`
- `total_classes`
- `completed_classes`
- `passed_classes`
- `failed_classes`
- `skipped_classes`
- `coverage_avg`
- `coverage_min`
- `mutation_avg`
- `mutation_min`
- `input_tokens`
- `cache_read_tokens`
- `output_tokens`
- `reasoning_tokens`
- `total_tokens`
- `estimated_cost_usd`
- `provider_cost_usd`
- `estimated_elapsed_seconds`
- `actual_elapsed_seconds`
- `budget_used_ratio`
- `latest_report_path`
- `latest_live_status_path`
- `latest_commit`
- `remote_ref`
- `error`
- `created_at`
- `started_at`
- `updated_at`
- `finished_at`

Repo task statuses:

- `CREATED`
- `QUEUED`
- `RUNNING`
- `STOP_REQUESTED`
- `STOPPED`
- `COMPLETED`
- `FAILED`
- `CANCELLED`

`class_tasks` stores one child row per class or batch:

- `id`
- `repo_task_id`
- `class_fqn`
- `module`
- `batch_key`
- `status`
- `priority`
- `stage`
- `attempt_count`
- `llm_turn_count`
- `test_file_path`
- `test_count`
- `test_file_lines`
- `coverage`
- `mutation_score`
- `surviving_mutants`
- `total_mutants`
- `session_ids_json`
- `phase_token_usage_json`
- `input_tokens`
- `cache_read_tokens`
- `output_tokens`
- `reasoning_tokens`
- `total_tokens`
- `estimated_input_tokens`
- `estimated_cache_read_tokens`
- `estimated_output_tokens`
- `estimated_reasoning_tokens`
- `estimated_total_tokens`
- `estimated_cost_usd`
- `provider_cost_usd`
- `estimated_elapsed_seconds`
- `actual_elapsed_seconds`
- `started_at`
- `finished_at`
- `commit_sha`
- `pushed_at`
- `error`

Class task statuses:

- `PENDING`
- `QUEUED`
- `RUNNING`
- `STOPPED`
- `PASS`
- `FAIL`
- `MUTATION_FAIL`
- `BUDGET_EXCEEDED`
- `PROVIDER_RATE_LIMITED`
- `PUSH_FAILED`
- `UNSAFE_DIFF`
- `LLM_STALLED`
- `CANCELLED`

`task_events` is an append-only audit log:

- `id`
- `repo_task_id`
- `class_task_id`
- `ts`
- `event_type`
- `stage`
- `severity`
- `message`
- `payload_json`

Event types:

- `task_created`
- `task_started`
- `task_stopped`
- `task_resumed`
- `task_cancelled`
- `task_reprioritized`
- `class_reprioritized`
- `scheduler_selected`
- `scheduler_idle`
- `stage_started`
- `llm_progress`
- `budget_warning`
- `budget_blocked`
- `gate_result`
- `deterministic_change`
- `commit_created`
- `push_verified`
- `rate_limited`
- `unsafe_diff`
- `task_completed`
- `task_failed`

`task_control` stores cooperative control requests:

- `repo_task_id`
- `class_task_id`
- `requested_action`
- `reason`
- `created_at`
- `handled_at`

`runner_heartbeats` stores daemon runner liveness:

- `runner_id`
- `hostname`
- `pid`
- `started_at`
- `heartbeat_at`
- `current_repo_task_id`
- `status`
- `loaded_config_hash`
- `message`

## Daemon Mode

Add `uta tasks daemon`.

Daemon behavior:

- Polls the task DB every `--poll-interval` seconds, default `10`.
- Updates `runner_heartbeats` every `--heartbeat-interval` seconds, default `15`.
- Selects the highest-priority runnable repo task not blocked by same-repo locking.
- Runs selected task with `uta run --production --task-id <id>`.
- Logs `idle` and waits when no runnable task exists.
- Continues polling until stopped by process signal or explicit shutdown control.

Runnable task statuses:

- `CREATED`
- `QUEUED`
- `STOPPED` only after resume.
- `FAILED` only when `--include-failed` is passed or the operator explicitly resumes the task.

Priority rules:

- Lower integer means higher priority.
- Default repo-task priority: `100`.
- Default class-task priority: inherited from parent task.
- Scheduler ordering: priority ascending, then created time ascending.
- Within a repo task, child selection orders by class priority, then original candidate order.
- Reprioritizing a running class does not interrupt it; it affects subsequent child selection.

Same-repo locking:

- Same-repo concurrent execution is disabled by default.
- Scheduler uses SQLite transactional selection and `repo_tasks.status=RUNNING` to avoid two daemons taking the same repo branch.
- `--allow-same-repo-concurrency` is opt-in and requires different branch names.

Stop behavior:

- `uta tasks stop <task-id>` sets `STOP_REQUESTED`.
- Workflow checks stop requests before starting new LLM turns, Maven gates, mutation attempts, commits, pushes, and between child classes.
- Already-running deterministic commands are allowed to finish.
- Stop leaves unfinished child rows resumable and marks parent `STOPPED`.

Resume behavior:

- `uta tasks resume <task-id>` returns stopped unfinished child rows to `QUEUED`.
- Terminal child rows are skipped unless `--force-rerun-failed` or `--force-rerun-all` is provided.

Cancel behavior:

- `uta tasks cancel <task-id>` cancels not-yet-started work.
- Running work requires stop first unless a future `--force` mode is explicitly implemented.

## Config Restart Rules

Document config restart behavior in `docs/production-usage.md`.

Requires daemon restart:

- Python package or source code changes.
- `UTA_TASK_DB_PATH`.
- `UTA_RUNNER_HOME`.
- Daemon polling and heartbeat settings.
- OpenCode binary path.
- OpenCode spawn command.
- Environment credentials that are only passed through daemon process env.
- Global logging destination.

Does not require daemon restart:

- Creating a new task.
- Changing task priority.
- Changing class priority.
- Stop, resume, or cancel requests.
- Watching or exporting status.

Takes effect only when a task starts:

- Provider and model settings.
- Pricing defaults.
- Budget defaults.
- Timeout multipliers.
- Branch naming defaults.
- External source directories.
- OpenCode external-directory allowlist.

Does not affect already running tasks:

- Any setting captured in `repo_tasks.config_snapshot_json`.
- Any budget setting captured in `repo_tasks.budget_config_snapshot_json`.
- Any estimate setting captured in `repo_tasks.estimate_snapshot_json`.

Control-table actions are exceptions:

- Stop, resume, cancel, and priority updates are DB state changes, not config changes.
- They should be observed by the daemon without restart.

Live status must display:

- Daemon config hash.
- Task config snapshot hash.
- Whether detected config changes require daemon restart, next-task restart, or no restart.

## Cost And Estimate Tracking

Add per-task estimate fields in both DB and live status:

- Estimated input tokens.
- Estimated cache-read tokens.
- Estimated output tokens.
- Estimated reasoning tokens.
- Estimated total tokens.
- Estimated cost.
- Estimated elapsed time.
- Actual token, cost, and time counters.
- Remaining estimated token, cost, and time for unfinished child rows.

Estimate source priority:

1. Repo cost estimator output, when available.
2. Historical local task DB records for matching repo/module/class complexity.
3. Fallback defaults.

Store estimate source:

- `repo_estimate`
- `historical_db`
- `fallback_default`

Budget policy:

- Budgets are protective guardrails, not tight throttles.
- Warning thresholds at `50%`, `75%`, and `90%`.
- Warnings log and create `task_events` but do not abort.
- Hard abort only before a new LLM turn would exceed loose repo, class, or turn cap.

Benchmark-based loose hard caps:

- Repo hard cap: `estimate_cost * 2.0`.
- Class hard cap: `estimated_class_cost * 3.0`, minimum `$2.00`.
- Turn hard cap: `max(estimated_turn_cost * 3.0, $1.00)`, bounded by remaining repo and class hard caps.

Cost source:

- Prefer provider-returned `usage.cost` when available.
- Otherwise compute from pricing registry.
- Pricing registry must support separate input, cache-read, and output/reasoning rates.
- Kimi K2.6 cache-read defaults to `0.25x` input.

Live monitoring must show:

- Actual vs estimated tokens.
- Actual vs estimated cost.
- Actual vs estimated elapsed time.
- Budget used percentage.
- Projected final budget percentage.
- Remaining hard-cap budget.
- Cache-hit ratio.
- Highest-cost stage.

## Runtime Safety And Branching

Runtime safety is scoped to LLM-authored diffs.

LLM safety guard:

- Snapshot git diff before each LLM turn.
- Inspect new diff after the LLM turn.
- Reject disallowed LLM-authored changes.
- Mark the child task `UNSAFE_DIFF`.

Allowed LLM-authored paths:

- Generated test files for the current class or batch.
- `.uta_cache/`.
- `.uta_reports/`.
- `opencode.json`.
- Stage introspect and plan artifacts.

Disallowed LLM-authored paths:

- `src/main/java/**`.
- Unrelated test files.
- Build files.
- Scripts.
- Config files.
- Any path outside the current batch allowlist.

Deterministic UTA nodes are not blocked by this guard:

- Baseline setup.
- Maven support changes.
- Mockito/POM normalization.
- Surefire skip rewrite.
- Pair shim.
- Cache generation.
- Report generation.

Deterministic changes must be auditable:

- Log as `deterministic_change`.
- Store in `task_events`.
- Include in report metadata.

Branch reuse:

- Multiple tasks for the same repo reuse the active branch by default.
- Override with `--branch-name`.
- Create a fresh active branch with `--new-branch`.
- Each class or batch creates an independent commit and push.
- After push, verify the remote ref equals local `HEAD`.

Before each class task starts:

- Verify the current branch matches the repo task branch.
- Verify no unsafe unrelated diff exists.

Before commit:

- Stage only current batch generated tests plus allowed artifacts.
- Never use broad `git add .`.

## Documentation And Usage

Add `docs/production-usage.md` later with:

- Single-host deployment.
- DB initialization.
- Creating tasks manually.
- Creating tasks from a manifest.
- Running one task.
- Running daemon mode.
- Stopping, resuming, cancelling, and reprioritizing tasks.
- Branch reuse and override examples.
- Live terminal monitoring.
- Live HTML monitoring.
- Estimate and budget interpretation.
- Config restart rules.
- Troubleshooting provider limits, stalls, unsafe diffs, and push failures.

Update `README.md` later with a short production quick start:

```bash
scripts/deploy_single_host.sh
uta tasks create --repo /path/to/repo --module service --all --priority 100
uta tasks daemon
uta tasks watch <task-id> --show-sessions
```

Update `design/architecture.md` later with:

- Task DB entity diagram.
- Daemon scheduler state machine.
- Config snapshot and restart model.
- Live status data flow.
- LLM-only safety boundary.
- Branch reuse model.

## Tests And Acceptance

Task DB tests:

- Schema migration is idempotent.
- Repo task CRUD works.
- Class task CRUD works.
- Branch record CRUD works.
- Event append/query works.
- Runner heartbeat CRUD works.
- Estimate fields persist and aggregate correctly.

Scheduler tests:

- Daemon selects the highest-priority runnable task.
- Daemon idles when no runnable task exists.
- Same-repo lock prevents branch collision.
- Heartbeat row updates while daemon runs.
- Failed tasks are not automatically runnable unless explicitly configured.

Task management tests:

- `create` persists repo and child rows.
- `start --next` respects task priority.
- `stop` is cooperative and leaves task resumable.
- `resume` requeues unfinished child rows only.
- `cancel` cancels not-yet-started work.
- `reprioritize` changes repo task ordering.
- `reprioritize-class` changes next child selection ordering.

Branch tests:

- Same repo tasks reuse active branch by default.
- `--branch-name` uses an explicit branch.
- `--new-branch` creates a new active branch.
- Each child completion commits and pushes independently.
- Push verification detects remote mismatch.

Workflow integration tests:

- Candidate selection creates child tasks.
- Stage transitions update child task stage.
- LLM progress writes task events.
- Stop request is honored before new LLM turns and between classes.
- Class completion writes metrics, session IDs, report path, commit SHA, and push time.

Live monitoring tests:

- `live_status.json` includes task priority, class priority, statuses, estimates, actuals, budget percentages, and session IDs.
- `live_status.html` auto-refreshes and renders estimated token, cost, and time fields.
- `uta tasks watch --once --show-sessions` prints session IDs.

Cost and safety tests:

- Budget warnings do not stop the task.
- Budget hard cap blocks the next LLM turn.
- Provider-returned cost overrides computed price.
- Kimi cache-read cost uses `0.25x` input.
- Unsafe LLM diff marks `UNSAFE_DIFF`.
- Deterministic setup changes are logged but not blocked.

Documentation acceptance:

- `docs/production-harden.md` exists and contains this plan.
- README and architecture updates are intentionally out of scope for this docs-only step unless requested separately.

## Assumptions

- SQLite is sufficient for v1 because production starts on one runner host.
- Daemon mode is cooperative and DB-driven, not a distributed queue.
- Priorities are integers where lower values mean higher priority.
- Stop is cooperative, not process-kill-first.
- Same-repo concurrent execution is disabled by default to avoid branch conflicts.
- Repo tasks can share a branch while retaining separate task IDs, metrics, and child class rows.
- Running tasks use config snapshots for stable behavior.
- Most config changes affect newly started tasks, not currently running tasks.

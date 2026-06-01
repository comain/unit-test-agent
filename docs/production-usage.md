# Production Usage

This guide covers the v1 single-host production runner. It uses a local SQLite task DB, a venv-installed `uta` CLI, one active branch per repo by default, terminal monitoring, and auto-refreshing HTML status.

## Deploy

```bash
scripts/deploy_single_host.sh
```

Defaults:

- `UTA_RUNNER_HOME=$HOME/.local/share/uta`
- `UTA_TASK_DB_PATH=$UTA_RUNNER_HOME/uta_tasks.db`
- venv at `.venv` unless `UTA_VENV_DIR` is set

## Create Tasks

Create a repo task that scans all production classes:

```bash
uta tasks create --repo /path/to/repo --module service --all --priority 100
```

Create a task for explicit classes:

```bash
uta tasks create --repo /path/to/repo \
  --class-fqn com.example.FooService \
  --class-fqn com.example.BarService
```

Branch behavior:

- Same repo tasks reuse the active branch by default.
- Use `--branch-name NAME` to force a branch.
- Use `--new-branch` to allocate a fresh branch record.

## Run Tasks

Run the daemon:

```bash
scripts/start_daemon.sh --poll-interval 10
```

Retry previously failed repo tasks from the daemon only when explicitly requested:

```bash
scripts/start_daemon.sh --include-failed
```

Run the daemon in the current shell instead of the background:

```bash
scripts/start_daemon.sh --foreground --poll-interval 10
```

Daemon launcher behavior:

- Sources `.env` before startup.
- Exports `JAVA_HOME`, `UTA_MAVEN_BIN`, and `MAVEN_HOME` into `PATH`.
- Writes the live daemon log to `$UTA_RUNNER_HOME/daemon.log` by default.
- Writes the daemon pid to `$UTA_RUNNER_HOME/daemon.pid` by default.

Run the highest-priority task once:

```bash
uta tasks start --next
```

Run a specific task through the production entrypoint:

```bash
uta run --production --task-id 123
```

Resume a stopped task:

```bash
uta tasks resume 123
uta run --production --resume-task 123
```

Resume failed child rows without re-running passing classes:

```bash
uta tasks resume 123 --force-rerun-failed
```

Force every child row to run again:

```bash
uta tasks resume 123 --force-rerun-all
```

## Manage Tasks

```bash
uta tasks list
uta tasks show 123 --show-sessions
uta tasks stop 123 --reason "maintenance window"
uta tasks cancel 123 --reason "obsolete request"
uta tasks reprioritize 123 20
uta tasks reprioritize-class 456 10
uta tasks export 123 --format json --output task-123.json
```

## Monitor

Terminal live view:

```bash
uta tasks watch 123 --interval 2 --show-sessions
```

HTML status is written under the target repo:

```text
/path/to/repo/.uta_reports/status.html
/path/to/repo/.uta_reports/live_status.json
```

The HTML page refreshes automatically and includes task status, branch, class rows, coverage/mutation, test counts, session IDs, estimated fields, actual token counters, and recent task events.
It also surfaces daemon heartbeat, estimated remaining cost/tokens/time, cache hit ratio, and budget usage.

## Config Restart Rules

Requires daemon restart:

- Python package or source code changes.
- `UTA_TASK_DB_PATH`.
- `UTA_RUNNER_HOME`.
- Daemon polling settings.
- OpenCode binary/spawn environment.
- Credentials only present in daemon process environment.
- Global logging destination.

Takes effect only when a task starts:

- Provider and model settings.
- Pricing defaults.
- Budget defaults.
- Timeout multipliers.
- Branch naming defaults.
- External source directories and OpenCode allowlists.

Does not require daemon restart:

- Creating tasks.
- Changing task or class priority.
- Stop, resume, or cancel requests.
- Watching or exporting status.

Running tasks use their captured config and budget snapshots for stable behavior. Stop/cancel requests are DB control actions and are safe to issue while the daemon is running.

## Runtime Safety

Production runs enforce the two main safety boundaries from the hardening plan:

- Stop/cancel controls are checked at workflow stage and LLM-turn boundaries, then leave the task resumable.
- LLM-authored diffs are checked after model tool execution. New paths outside the current generated test files and allowed UTA artifacts are marked `UNSAFE_DIFF` and block the task.
- Loose budget hard caps are checked before LLM turns. Budget warnings are recorded as task events; hard-cap violations mark the task `BUDGET_EXCEEDED`/`FAILED`.
- Production branch and pre-existing dirty-path checks run before each class/batch. Branch mismatch or unsafe unrelated dirty files mark the task unsafe instead of letting the LLM write into an ambiguous worktree.
- Successful class/batch commits are pushed immediately. Remote-ref mismatch or push failure is recorded as `PUSH_FAILED`.

Deterministic UTA setup and reporting changes remain auditable task events and are not blocked by the LLM diff guard.

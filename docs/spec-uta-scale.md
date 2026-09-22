# Spec: uta scale, reliability, budget, and measurability

## Objective

uta successfully ran end-to-end on one Java repo in production (~40 hours active wall time, 204 classes, all passing). The next step is running 100+ repos reliably, predictably, and within budget.

This spec covers four capabilities added together as one coherent phase:

1. **Scalability** — bounded-parallel queue-driven daemon, gated on making OpenCode parallel-safe first.
2. **Reliability** — structured retry, quarantine, and crash-resume using the existing SQLite state already in place.
3. **Budget guardrails** — per-repo cost cap derived from a 3-sample probe that doubles as real work; hard abort on breach.
4. **Measurability** — close the cost-recording gap, tighten the event schema, add a batch-level live dashboard alongside the existing task view.

**User:** sole operator running 100+ repos through a managed daemon on a single production node (`worker0:/data/w/unit-test-agent`).

---

## Tech Stack

- Python 3 (existing), Click CLI, SQLite via `TaskDB`
- OpenCode: migrating from `OpenCodeServer` (HTTP poll, fixed port) to `OpenCodeProcess` (stdin/stdout stream, no port)
- Maven 3.9.11 + Java at `/home/w/java/default`
- Rich terminal UI (existing `render.py`)
- Production node: `worker0.example.com`, accessed as `deploy` via `sudo -u deploy -i` from `$SSH_USER`

---

## Commands

```bash
# Queue a repo for processing
scripts/enqueue.sh <git-url> [--module M] [--branch B]

# Start daemon (single-task mode, current default)
uta tasks daemon --task-db .uta_runner/uta_tasks.db

# Start daemon with parallelism (after migration complete)
uta tasks daemon --max-parallel 2 --task-db .uta_runner/uta_tasks.db

# Live dashboard — task view (existing)
uta tasks dashboard --task <id>

# Live dashboard — batch view (new)
uta tasks dashboard --batch

# On-demand reports
uta tasks report repo <slug>
uta tasks report batch

# Stop a task
uta tasks stop <task_id> [--reason "..."]

# Unblock a quarantined or budget-exceeded repo
uta tasks unblock <task_id>
```

---

## Project Structure

```
uta/
  tasks/
    scheduler.py       — acquire_next_task (already parallel-safe via transaction)
    manager.py         — TaskManager: mark_failed, mark_poisoned, mark_budget_exceeded
    db.py              — TaskDB: schema, queries
    render.py          — live dashboard renderer (task view; extend with batch view)
    models.py          — task row model

  opencode/
    process.py         — OpenCodeProcess (parallel-safe, stdin/stdout stream)
    server.py          — OpenCodeServer (LEGACY, fixed port — to be removed this phase)
    client.py          — already uses OpenCodeProcess; stays unchanged
    stream.py          — JSONL stream parser
    tiered_router.py   — model/provider routing

  cli.py               — CLI commands; four call sites to migrate from Server to Process

benchmark/
  estimates/
    estimator.py       — NEW: per-repo cost estimator (3-tier, consumes uta_tasks.db)
  runs/                — benchmark run artifacts
  comparison.md        — model comparison notes

scripts/
  enqueue.sh           — NEW: one-liner queue entry point
  start_daemon.sh      — existing daemon launcher (extend with --max-parallel)
  deploy_single_host.sh

.uta_runner/
  uta_tasks.db         — primary state + metrics store (30 MB, growing)
  daemon.log           — daemon stdout/stderr
  daemon.pid

.uta_reports/          — nightly rollup JSON outputs
```

---

## Bottleneck data (from prod run)

Calibration basis: `sample-inbound-core`, 204 classes, all PASS.

| Metric | Value |
|---|---|
| Active wall time | ~40 hours |
| Per-class LLM agent time (sum) | 3.3 hrs (~8% of wall) |
| Avg per-class agent time | 58 s, 3.3 LLM turns |
| Top stages by count | test_execution (386), plan (319), generate (294), mutation_test (202), compile_verify (189) |
| Daemon resume_count | 26 restarts |
| Class-level task_failed (recovered) | 176 |
| Tokens | 186 M total |
| Estimated cost | $201.46 (actual cost not recorded — measurement gap) |

**Conclusion:** bottleneck is CPU/JVM — mvn compile, test execution, pitest mutation. Not the LLM API. Parallelism is gated by CPU/RAM on the node, not API quota.

---

## Parallel-readiness: current state and what this spec fixes

| Layer | Before this spec | After |
|---|---|---|
| `task_daemon` loop | Sequential — one subprocess, waits for exit | Worker pool with `MAX_PARALLEL_REPOS` slots |
| `OpenCodeServer` | Fixed port; kills sibling processes on startup | Removed from pipeline |
| `OpenCodeProcess` | Used by client.py only | Used by full pipeline |
| `~/.m2` local repo | Shared, unsafe for concurrent mvn | Per-worker dir `/var/tmp/uta-m2-<worker_id>/` |
| Same-repo guard | `allow_same_repo_concurrency=False` already in `acquire_next_task` | Unchanged — keep it |
| Scheduler/DB | Already transactional, multi-runner safe | Unchanged |

---

## Implementation: four areas

### 1. Scale

**Step 1 — OpenCode Server → Process migration (gates everything else)**

Migrate the four remaining `OpenCodeServer` call sites in `cli.py` to `OpenCodeProcess`:
- `cli.py:1341` — workflow runner setup
- `cli.py:1404` — server start in `run` command
- `cli.py:1575` — coverage fix loop
- `cli.py:1617` — mutation fix loop

`OpenCodeServer` is HTTP-poll + fixed port. `OpenCodeProcess` is stdin/stdout stream with no port. Each call site needs adapting to the stream API, not a drop-in rename. The migration plan in `docs/opencode-spawn.md` covers the phases in detail.

Acceptance: two concurrent `uta run` invocations complete without either killing the other's OpenCode process.

**Step 2 — Per-worker mvn local repo**

Add `UTA_MAVEN_REPO_LOCAL` env var (or `--maven-repo-local` flag). Propagate to all `subprocess` mvn invocations as `-Dmaven.repo.local=<path>`. Default: `/var/tmp/uta-m2-<os.getpid()>/`. Clean up on task exit.

**Step 3 — Daemon worker pool**

Refactor `task_daemon` (`cli.py:1162`) from:
```python
while True:
    task = scheduler.acquire_next(...)
    proc = subprocess.Popen(cmd)
    while proc.poll() is None:
        sleep(interval)
```
to a pool of `MAX_PARALLEL_REPOS` slots:
```python
# conceptual shape
pool = {}  # task_id → Popen
while True:
    while len(pool) < MAX_PARALLEL_REPOS:
        task = scheduler.acquire_next(...)
        if not task: break
        pool[task_id] = subprocess.Popen(cmd)
    reap_finished(pool)
    sleep(interval)
```
`acquire_next_task` is already transactional — no scheduler changes needed.

`MAX_PARALLEL_REPOS` read from env (`UTA_MAX_PARALLEL_REPOS`), default `1`. Raise to 2–3 only after measuring JVM heap usage on the box.

**Step 4 — Enqueue script**

`scripts/enqueue.sh <git-url> [--module M] [--branch B]`:
- Clone (or `git fetch --all && git checkout`) into `/data/w/code/<slug>`.
- Insert row into `repo_tasks` via `uta tasks enqueue` (or direct sqlite3 insert).
- Print task id.

---

### 2. Reliability

Evidence from prod: 26 daemon restarts, 176 class-level failures all recovered, multiple distinct error classes.

**Resume** (already works): daemon restart picks up `RUNNING` rows from `repo_tasks` and continues. `resume_count` already tracked.

**Retry policy** (per error class):

| Error | Action |
|---|---|
| Transient API (429, 5xx, network) | Exponential backoff, retry up to 3× |
| Build/compile failure | Fail the class, continue with next |
| Unsafe LLM diff (already guarded) | Fail the class, continue |
| Internal errors (JSON decode, tree_sitter, file-not-found) | Retry once with state reset; if repeats, fail the class |
| Agent dead loop | Budget abort (see §4) |

**Quarantine**: after 2 repo-task-level failures, mark status `POISONED`. Skip until operator runs `uta tasks unblock <id>`. `BUDGET_EXCEEDED` is a distinct state — also requires operator unblock, but is not counted toward the quarantine N.

**Idempotency**: `uta run` on a `COMPLETED` task is a no-op unless `--force`.

---

### 3. Measurability

**Schema gaps to close:**

- `repo_tasks.provider_cost_usd` is NULL in the prod run even though tokens are recorded. Fix: aggregate `SUM(class_tasks.provider_cost_usd)` into `repo_tasks` on task completion. Also track per-turn cost in the stream parser and write through.
- Stage durations: `task_events` has `stage_started` but no `stage_completed`. Add `stage_completed` events (mirrors `stage_started`), enabling `duration = completed_ts - started_ts` without window functions.
- `scheduler_idle` events: 204k of 208k events are idle heartbeats. Move to `runner_heartbeats` table (already exists) at one-per-interval. Keep `task_events` for real events only.

**Reports:**

- On-demand: `uta tasks report repo <slug>` and `uta tasks report batch` print to stdout and write JSON to `.uta_reports/`.
- Nightly: cron job materializes `repo_summary` and `batch_summary` into `.uta_reports/report_<date>.json`.

**Live terminal dashboard (batch view — new):**

Extends existing task-level view. Toggle: `uta tasks dashboard --batch` or auto-activate when a batch is running.

Displays (2 s refresh):
```
Batch status — 3 running / 14 queued / 22 done / 1 failed / 0 poisoned
Cost: $38.20 this hour  |  $312.40 total  |  no batch cap set
Throughput: 1.8 repos/day  |  ETA: ~8 days for remaining 14

Worker 1  sample-inbound-core   generate      OrderBizImpl        $41.20 / $90 cap  (46%)
Worker 2  picking-core           mutation_test PickingBizImpl      $12.50 / $55 cap  (23%)
Worker 3  receipt-core           plan          ReceiptFlowService  $2.10  / $60 cap  (4%)
```

---

### 4. Budget

**No separate probe phase.** The estimator's calibration samples are the first 3 real classes of the run. Their test code is committed; their cost counts against the repo budget.

**Run flow:**

1. **Pre-run size scan (free):** parse repo — LOC, # Java classes, # packages, # existing tests, dep count. Use historical regression from `uta_tasks.db` if ≥ 5 completed runs exist; otherwise use cold-start formula `$201.46 / 204 classes = ~$0.99/class`, scaled by size ratio.

2. **Coarse cap:** `coarse_cap_usd = preliminary_estimate × 2.0`. Set on `repo_tasks.budget_config_snapshot_json` before any LLM work.

3. **First 3 classes — 3-tier sample** (mirrors `scripts/benmark.sh`):
   - 1 small class (lowest LOC in the candidate set)
   - 1 ~90th-percentile class by LOC
   - 1 ~95th-percentile class by LOC
   - Real work: tests committed, cost charged against the budget.

4. **Refine cap after sample 3:**
   - Extrapolate: `refined_estimate = observed_cost_per_class_weighted × total_classes`.
   - `hard_cap_usd = refined_estimate × 2.0`.
   - Persist to `repo_tasks.estimate_snapshot_json`.

5. **Continue with refined cap.** Cost tracked per turn from `provider_cost_usd` in the stream; summed into `class_tasks` and aggregated into `repo_tasks`.

6. **Enforcement:** when `running_cost > hard_cap`, immediately kill the in-flight class agent run (`SIGTERM` to the OpenCodeProcess), mark `repo_tasks.status = BUDGET_EXCEEDED`, save checkpoint, stop. Requires operator `uta tasks unblock <id>` to resume.

7. **Global batch cap (optional):** `UTA_BATCH_CAP_USD` env var. When cumulative `SUM(provider_cost_usd)` across all `RUNNING` + `COMPLETED` tasks exceeds this, daemon stops dequeueing new tasks.

**Code home:** `benchmark/estimates/estimator.py` (new file). Reads `uta_tasks.db` for history, reads repo filesystem for size signals, outputs `(preliminary_estimate, coarse_cap, sample_order)`.

---

## Success criteria

- [ ] Two concurrent `uta run` processes complete without OpenCode port collisions.
- [ ] `uta tasks daemon --max-parallel 2` drives two repos simultaneously end-to-end.
- [ ] A daemon crash mid-run: after restart, the task resumes from the last completed class without re-running already-completed classes.
- [ ] A task that exceeds `hard_cap_usd` is aborted at the class boundary and marked `BUDGET_EXCEEDED`; the next task in the queue starts automatically.
- [ ] A task with 3+ consecutive failures is marked `POISONED` and skipped; `uta tasks unblock` resumes it.
- [ ] `repo_tasks.provider_cost_usd` is non-NULL for all new task runs.
- [ ] `uta tasks dashboard --batch` shows all active workers, queue depth, rolling cost, and per-repo ETA, refreshing every 2 s.
- [ ] `scripts/enqueue.sh <git-url>` clones the repo and prints a task id within 60 s.
- [ ] `uta tasks report batch` prints cost, coverage, throughput, and status breakdown for all tasks in the DB.

---

## Boundaries

**Always:**
- Keep `allow_same_repo_concurrency=False` — never let two workers check out the same repo path.
- Per-worker `-Dmaven.repo.local` — never share `~/.m2` between concurrent runs.
- Record `provider_cost_usd` per class on every run.
- Persist budget snapshots to `estimate_snapshot_json` before the run starts.

**Ask first:**
- Raising `MAX_PARALLEL_REPOS` above 1 — requires JVM heap measurement on the box first.
- Changing the 2× safety factor — has cost implications across all future runs.
- Schema migrations to `uta_tasks.db` — must be backward-compatible or guarded by `schema_version`.
- Removing `OpenCodeServer` entirely — verify no dev-mode or external use remains.

**Never:**
- Skip the `hard_cap` check on cost breach — this is the only thing preventing a runaway agent from spending unbounded budget.
- Mark a `BUDGET_EXCEEDED` or `POISONED` task as auto-recovered — always require human unblock.
- Write to the same `/data/w/code/<slug>` working tree from two concurrent workers.
- Use Jira / pipeline tooling — this is a personal project.

---

## Non-goals

- HA / failover.
- Multi-node fan-out (deferred — build the `MAX_PARALLEL_REPOS` knob and `runner_heartbeats` seam here; fan-out comes when the single node saturates).
- Web or remote dashboard.
- Multi-tenant cost accounting.
- A dedicated `uta analyze` bottleneck command (the bottleneck data was a one-time input to this spec).

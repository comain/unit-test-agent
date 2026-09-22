# Implementation Plan: uta scale, reliability, budget, measurability

Spec: `docs/spec-uta-scale.md`

## Architecture notes

**OpenCode migration is smaller than it looks.**
`OpenCodeClient` is already process-based — `poll_completion` calls `self._process.run_turn(...)`, never HTTP. `server.start/stop` in `cli.py` only exists for `OpenCodeAuthClient` (HTTP) used in `_ensure_model_auth`. Migration = replace that one auth probe with a process-based one + delete server lifecycle. Main generation loop untouched.

**SQLite concurrency is already safe.**
`acquire_next_task` uses a transaction. Daemon worker pool just calls it from N slots.

**Cost recording is a write-path fix, not a schema change.**
`provider_cost_usd` column exists on both `class_tasks` and `repo_tasks`; it just isn't written.

**Maven local repo isolation is NOT needed.**
uta only calls `mvn compile`, `mvn test-compile`, `mvn test`, pitest, jacoco — never `mvn install`. The `~/.m2` repo is effectively read-only during a run. No write contention between parallel workers.

**Clone root is configurable.**
`/data/w/code/` is the prod default but must not be hardcoded. Controlled by `UTA_CLONE_ROOT` env var.

---

## Task List

### Phase 1: Parallel Safety

---

#### Task 1: Remove vestigial OpenCodeServer from the run pipeline

**Description:** `cli.py` starts an HTTP OpenCode server only for `_ensure_model_auth`. Replace with a process-based probe (`_probe_openai_auth_ready` at `cli.py:656` already exists), then delete `server = OpenCodeServer(...)`, `server.start()`, and `server.stop()` from the `run` command (~L1404–1508) and the `resume-gates` command (~L1617–1882).

**Acceptance criteria:**
- [ ] `OpenCodeServer` not imported in `cli.py`
- [ ] `server.start()` / `server.stop()` removed from both pipeline locations
- [ ] Auth check replaced with a process-based call
- [ ] `uta run` completes a single-class run without starting an HTTP server

**Verification:**
- [ ] `lsof -i :<opencode_port>` shows no listener during a run
- [ ] `pytest tests/ -x -q` passes

**Dependencies:** None

**Files:**
- `uta/cli.py` (`run` ~L1341–1508, `resume-gates` ~L1575–1882)

**Scope:** S

---

#### Task 2: Daemon worker pool (`MAX_PARALLEL_REPOS`)

**Description:** Refactor `task_daemon` (`cli.py:1162`) from a single Popen+poll loop to a pool of `MAX_PARALLEL_REPOS` slots. Each slot independently calls `scheduler.acquire_next()`, spawns a subprocess, and reaps on exit. Read from `UTA_MAX_PARALLEL_REPOS` env var (default `1`). Expose as `--max-parallel` CLI flag.

**Acceptance criteria:**
- [ ] `uta tasks daemon --max-parallel 2` drives two `uta run` subprocesses concurrently
- [ ] When one task finishes, the slot refills from the queue without restarting the daemon
- [ ] Heartbeat fires per active slot
- [ ] `--max-parallel 1` is identical to current behavior

**Verification:**
- [ ] Enqueue 3 tasks; `daemon --max-parallel 2`; observe 2 running then 1 final
- [ ] `pytest tests/ -x -q` passes

**Dependencies:** Task 1

**Files:**
- `uta/cli.py` (`task_daemon`)

**Scope:** M

---

#### Task 3: Enqueue CLI and script

**Description:** Add `uta tasks enqueue <git-url> [--module M] [--branch B]` subcommand. Clones (or `git fetch`+checkout) into `$UTA_CLONE_ROOT/<slug>` (default `/data/w/code`), inserts a row into `repo_tasks`, returns the task id. Wrap in `scripts/enqueue.sh` for convenience. `UTA_CLONE_ROOT` must be read from config, never hardcoded.

**Acceptance criteria:**
- [ ] `uta tasks enqueue <git-url>` prints a task id and the task appears in `uta tasks list` as `QUEUED`
- [ ] `UTA_CLONE_ROOT` controls the clone destination
- [ ] Running the same git-url twice does not create a duplicate `RUNNING` task
- [ ] `scripts/enqueue.sh <git-url>` is a thin wrapper around the CLI

**Verification:**
- [ ] `scripts/enqueue.sh <repo-url>` → task id → `uta tasks list` shows `QUEUED`

**Dependencies:** None (parallel with Task 2)

**Files:**
- `uta/cli.py` (`uta tasks enqueue` subcommand)
- `uta/config.py` (`clone_root` setting)
- `scripts/enqueue.sh` (new)

**Scope:** S

---

#### Task E1: Phase 1 end-to-end test

**Description:** Write an e2e test that exercises the full parallel-safe pipeline with 2 concurrent tasks. Run `daemon --max-parallel 2` against two small real or fixture repos, verify both complete, verify no OpenCode port collisions in logs.

**Acceptance criteria:**
- [ ] `tests/e2e/test_phase1_parallel.py` runs two tasks in parallel and both pass
- [ ] No `_terminate_stale_listeners` kills observed in either run log
- [ ] Test is tagged `@pytest.mark.e2e` and excluded from default `pytest` run; run with `pytest -m e2e`

**Verification:**
- [ ] `pytest -m e2e tests/e2e/test_phase1_parallel.py` exits 0

**Dependencies:** Tasks 1, 2, 3

**Files:**
- `tests/e2e/test_phase1_parallel.py` (new)

**Scope:** S

---

### Checkpoint: Phase 1

- [ ] `uta run` works without OpenCode HTTP server
- [ ] `uta tasks daemon --max-parallel 2` runs two repos in parallel
- [ ] `uta tasks enqueue` and `scripts/enqueue.sh` populate the queue using `UTA_CLONE_ROOT`
- [ ] `pytest tests/ -x -q` passes
- [ ] `pytest -m e2e tests/e2e/test_phase1_parallel.py` passes

---

### Phase 2: Reliability

---

#### Task 4: Structured retry classification

**Description:** In the class-task failure handler, map each error string to one of four retry policies: `transient` (exponential backoff, max 3×), `class_fail_continue` (fail class, next), `internal_retry_once` (reset + retry once), `budget_abort` (Phase 4). Map the real prod error patterns:
- `429`/5xx → `transient`
- `Baseline compilation failed` → `class_fail_continue`
- `Unsafe LLM-authored patch`, `Pre-existing unsafe dir` → `class_fail_continue`
- `tree_sitter.Query`, `Expecting value: line 1`, `[Errno 2] No such file` → `internal_retry_once`

**Acceptance criteria:**
- [ ] Each pattern routes to the correct policy
- [ ] `transient` retries with backoff, logged
- [ ] `internal_retry_once` resets class state, retries exactly once, then fails the class

**Verification:**
- [ ] `pytest tests/test_retry_classifier.py -x -q` covers all patterns

**Dependencies:** None

**Files:**
- `uta/tasks/manager.py`
- `tests/test_retry_classifier.py` (new)

**Scope:** S

---

#### Task 5: Quarantine (POISONED) and unblock

**Description:** After `UTA_QUARANTINE_THRESHOLD` repo-task-level failures (default `2`, configurable), mark `repo_tasks.status = POISONED` and skip in `acquire_next_task`. Add `uta tasks unblock <task_id>` to reset to `QUEUED`. `BUDGET_EXCEEDED` (Phase 4) is a separate status — also add it to the skip list now.

**Acceptance criteria:**
- [ ] After N failures (where N = `UTA_QUARANTINE_THRESHOLD`, default 2), task is `POISONED` and not dequeued
- [ ] `uta tasks unblock <id>` resets to `QUEUED`
- [ ] `BUDGET_EXCEEDED` tasks also skipped in `acquire_next_task`
- [ ] N is read from `UTA_QUARANTINE_THRESHOLD` env var

**Verification:**
- [ ] `pytest tests/test_task_quarantine.py -x -q` covers state transitions

**Dependencies:** Task 4

**Files:**
- `uta/tasks/db.py` (`acquire_next_task` skip list)
- `uta/tasks/manager.py` (`mark_poisoned`, `unblock`)
- `uta/config.py` (`quarantine_threshold`)
- `uta/cli.py` (`uta tasks unblock`)
- `tests/test_task_quarantine.py` (new)

**Scope:** S

---

#### Task E2: Phase 2 end-to-end test

**Description:** e2e test that injects a transient error (mock 429) and verifies backoff+retry; injects a class-level failure N times and verifies `POISONED` state; verifies `unblock` restores the task.

**Acceptance criteria:**
- [ ] `tests/e2e/test_phase2_reliability.py` covers transient retry, quarantine, and unblock
- [ ] Tagged `@pytest.mark.e2e`

**Verification:**
- [ ] `pytest -m e2e tests/e2e/test_phase2_reliability.py` exits 0

**Dependencies:** Tasks 4, 5

**Files:**
- `tests/e2e/test_phase2_reliability.py` (new)

**Scope:** S

---

### Checkpoint: Phase 2

- [ ] Prod error patterns each route to correct retry policy
- [ ] A task quarantined after N failures; `uta tasks unblock` restores it
- [ ] `pytest tests/ -x -q` passes
- [ ] `pytest -m e2e tests/e2e/test_phase2_reliability.py` passes

---

### Phase 3: Measurability

---

#### Task 6: Fix provider_cost_usd recording

**Description:** `class_tasks.provider_cost_usd` and `repo_tasks.provider_cost_usd` are NULL in all prod runs. Extend the stream parser to compute cost per turn from a pricing table (`UTA_COST_PER_1M_INPUT_TOKENS`, `UTA_COST_PER_1M_OUTPUT_TOKENS`, configurable). Aggregate into `class_tasks` on class completion and into `repo_tasks` on task completion. Write even on class failure (partial spend).

**Acceptance criteria:**
- [ ] `class_tasks.provider_cost_usd` is non-NULL for every completed or failed class
- [ ] `repo_tasks.provider_cost_usd = SUM(class_tasks.provider_cost_usd)`
- [ ] Pricing configurable via env vars

**Verification:**
- [ ] `uta run --max-files 1`; `SELECT provider_cost_usd FROM class_tasks WHERE ...`; non-NULL

**Dependencies:** None

**Files:**
- `uta/opencode/stream.py` or `process.py`
- `uta/tasks/manager.py`
- `uta/config.py`

**Scope:** S

---

#### Task 7: Add stage_completed events

**Description:** Pair every `stage_started` with a `stage_completed` event so duration is a simple join, not a window function. Emit on stage exit (success or failure) with the same `stage` value and `class_task_id`.

**Acceptance criteria:**
- [ ] Every `stage_started` has a matching `stage_completed` in `task_events`
- [ ] Duration computable: `SELECT e2.ts - e1.ts FROM task_events e1 JOIN task_events e2 ON e1.class_task_id=e2.class_task_id AND e1.stage=e2.stage WHERE e1.event_type='stage_started' AND e2.event_type='stage_completed'`

**Verification:**
- [ ] `uta run --max-files 1`; verify pairing in DB

**Dependencies:** None

**Files:**
- `uta/cli.py` or `uta/graph/nodes.py`
- `uta/tasks/manager.py`

**Scope:** S

---

#### Task 8: Stop writing scheduler_idle to task_events

**Description:** 98% of `task_events` rows are `scheduler_idle` (204k of 208k), making the table expensive to scan. Remove that write from `scheduler.py`; idle state is already recorded in `runner_heartbeats`.

**Acceptance criteria:**
- [ ] No new `event_type='scheduler_idle'` rows in `task_events`
- [ ] `runner_heartbeats` still updates every `heartbeat_interval` seconds when idle

**Verification:**
- [ ] Idle daemon for 60 s; `SELECT COUNT(*) FROM task_events WHERE event_type='scheduler_idle'` = 0

**Dependencies:** None

**Files:**
- `uta/tasks/scheduler.py`

**Scope:** XS

---

#### Task 9: On-demand report CLI

**Description:** `uta tasks report batch` and `uta tasks report repo <slug>`. Print a text table (status breakdown, cost est vs actual, coverage stats, throughput, elapsed) and write JSON to `.uta_reports/`. Works while a task is `RUNNING`.

**Acceptance criteria:**
- [ ] `uta tasks report batch` covers all tasks in the DB
- [ ] `uta tasks report repo <slug>` shows per-class breakdown
- [ ] JSON written to `.uta_reports/report_<slug|batch>_<ts>.json`

**Verification:**
- [ ] Run both commands against the prod DB; visually verify numbers match DB

**Dependencies:** Tasks 6, 7

**Files:**
- `uta/cli.py` (`uta tasks report` command group)
- `uta/tasks/render.py`

**Scope:** M

---

#### Task 10: Batch dashboard view

**Description:** Extend `uta tasks dashboard` with `--batch` mode (auto-activate when ≥1 task is running). Shows active workers (repo, stage, current class, cost vs cap), queue depth by status, rolling cost, throughput, batch ETA. 2 s refresh.

```
Batch — 2 running / 14 queued / 22 done / 1 failed
Cost $38.20/hr  |  $312.40 total  |  Throughput 1.8 repos/day  |  ETA ~8 days

Worker 1  sample-inbound-core  generate   OrderBizImpl    $41/$90 (46%)
Worker 2  picking-core          mutation   PickingBizImpl  $12/$55 (23%)
```

**Acceptance criteria:**
- [ ] `uta tasks dashboard --batch` renders and refreshes every 2 s
- [ ] Worker rows reflect `class_tasks.current_stage` and `class_tasks.class_fqn`
- [ ] Queue counts match `repo_tasks` status breakdown

**Verification:**
- [ ] Start daemon with 2+ tasks queued; watch dashboard for 30 s

**Dependencies:** Tasks 2, 6, 9

**Files:**
- `uta/tasks/render.py`
- `uta/cli.py`

**Scope:** M

---

#### Task E3: Phase 3 end-to-end test

**Description:** e2e test that runs a 3-class task, then verifies: `provider_cost_usd` is non-NULL, paired `stage_completed` events exist, report CLI outputs non-empty JSON, dashboard renders without error.

**Acceptance criteria:**
- [ ] `tests/e2e/test_phase3_measurability.py` covers all four measurability checks
- [ ] Tagged `@pytest.mark.e2e`

**Verification:**
- [ ] `pytest -m e2e tests/e2e/test_phase3_measurability.py` exits 0

**Dependencies:** Tasks 6, 7, 8, 9

**Files:**
- `tests/e2e/test_phase3_measurability.py` (new)

**Scope:** S

---

### Checkpoint: Phase 3

- [ ] `provider_cost_usd` non-NULL on every new run
- [ ] `task_events` no longer flooded with idle rows
- [ ] `uta tasks report batch` prints correct numbers
- [ ] `uta tasks dashboard --batch` renders and refreshes
- [ ] `pytest tests/ -x -q` passes
- [ ] `pytest -m e2e tests/e2e/test_phase3_measurability.py` passes

---

### Phase 4: Budget

---

#### Task 11: Budget estimator + 3-tier sample scheduling (merged)

**Description:** Two tightly coupled pieces delivered together.

**Part A — `benchmark/estimates/estimator.py`:** Given a repo path and an optional `uta_tasks.db`, scan size signals (LOC, # Java classes, # packages, # existing tests). Cold-start formula: `$0.99/class` (calibrated from prod run: $201.46 / 204 classes), scaled by class count. If db has ≥ 5 completed runs, use linear regression on `(class_count, total_cost)` instead. Output: `(preliminary_usd, coarse_cap_usd, sample_order)` where `sample_order` = [p0, p90, p95] classes by LOC.

**Part B — sample scheduling in `cli.py`:** Process the 3 sample classes first in the run pipeline. After all 3 complete, call the estimator again with observed actuals (avg cost/class) to produce a refined estimate. Set `hard_cap_usd = refined_estimate × 2.0`. Persist to `repo_tasks.estimated_cost_usd` and `repo_tasks.estimate_snapshot_json = {method, preliminary, refined, coarse_cap, hard_cap, sampled_classes, timestamp}`.

**Acceptance criteria:**
- [ ] `python -m benchmark.estimates.estimator <repo_path>` prints estimate, cap, sample order
- [ ] Cold-start used when db has < 5 runs; regression used otherwise (both logged)
- [ ] First 3 classes processed in a run match the p0/p90/p95 sample order
- [ ] After class 3, `repo_tasks.estimated_cost_usd` and `estimate_snapshot_json` are updated
- [ ] Running against `sample-inbound-core` produces an estimate in the $150–$250 range

**Verification:**
- [ ] `python -m benchmark.estimates.estimator <path>` → plausible output
- [ ] `uta run --max-files 5`; after 3 classes inspect DB for updated estimate
- [ ] `pytest tests/test_estimator.py -x -q` passes

**Dependencies:** Task 6

**Files:**
- `benchmark/estimates/estimator.py` (new)
- `uta/cli.py` (class ordering + post-sample refinement)
- `uta/tasks/manager.py` (`update_estimate`)
- `uta/config.py` (`clone_root`)
- `tests/test_estimator.py` (new)

**Scope:** M

---

#### Task 12: Budget enforcement — abort on cap breach

**Description:** At each class boundary in the `run` subprocess, check `SUM(provider_cost_usd)` for the current repo task. If `running_cost > hard_cap_usd`, send `SIGTERM` to the in-flight OpenCode process, call `manager.mark_budget_exceeded(task_id)`, and exit. The daemon's pool reaps the slot and picks the next task. `BUDGET_EXCEEDED` is already in the skip list (Task 5).

**Acceptance criteria:**
- [ ] A task with `hard_cap_usd = 0.01` is aborted after the first class with status `BUDGET_EXCEEDED`
- [ ] Daemon continues with the next queued task without restarting
- [ ] Checkpoint is preserved — `uta tasks unblock` + daemon restart resumes from the aborted class

**Verification:**
- [ ] Integration test: set tiny cap, run task, verify `BUDGET_EXCEEDED` and clean handoff
- [ ] `pytest tests/ -x -q` passes

**Dependencies:** Tasks 5, 6, 11

**Files:**
- `uta/cli.py` (budget check at class boundary)
- `uta/tasks/manager.py` (`mark_budget_exceeded`)

**Scope:** M

---

#### Task 13: Global batch cap

**Description:** Read `UTA_BATCH_CAP_USD` env var. Before dequeueing a new task, daemon checks `SUM(provider_cost_usd)` across all non-CREATED tasks. If ≥ cap, log `[BATCH CAP REACHED]` and pause dequeueing. Already-running tasks complete. Raising/removing the env var and restarting resumes.

**Acceptance criteria:**
- [ ] Daemon stops dequeueing when batch spend ≥ `UTA_BATCH_CAP_USD`
- [ ] In-flight tasks complete normally
- [ ] Restarting daemon with raised cap resumes

**Verification:**
- [ ] `UTA_BATCH_CAP_USD=0.01`; enqueue 2 tasks; only 1 starts

**Dependencies:** Task 12

**Files:**
- `uta/cli.py` (`task_daemon`)
- `uta/config.py` (`batch_cap_usd`)

**Scope:** S

---

#### Task E4: Phase 4 end-to-end test

**Description:** e2e test covering: estimator produces a plausible output; first 3 classes in a run match sample order; a task with a tiny cap is aborted at `BUDGET_EXCEEDED` and the next task starts; global batch cap pauses the queue.

**Acceptance criteria:**
- [ ] `tests/e2e/test_phase4_budget.py` covers all four budget scenarios
- [ ] Tagged `@pytest.mark.e2e`

**Verification:**
- [ ] `pytest -m e2e tests/e2e/test_phase4_budget.py` exits 0

**Dependencies:** Tasks 11, 12, 13

**Files:**
- `tests/e2e/test_phase4_budget.py` (new)

**Scope:** S

---

### Checkpoint: Phase 4 (Final)

- [ ] Estimator produces plausible estimate for `sample-inbound-core`
- [ ] First 3 classes match p0/p90/p95 sample order
- [ ] Task aborted at `BUDGET_EXCEEDED`; next task starts automatically
- [ ] Global batch cap pauses queue correctly
- [ ] All success criteria in `docs/spec-uta-scale.md` are met
- [ ] `pytest tests/ -x -q` passes
- [ ] `pytest -m e2e tests/e2e/` passes

---

## Parallelization opportunities

| Tasks | Order |
|---|---|
| 1 — OpenCode migration | First |
| 2 (daemon pool), 3 (enqueue) | After Task 1; parallel with each other |
| E1 | After Tasks 1, 2, 3 |
| 4 (retry), 5 (quarantine) | Sequential; parallel with Phase 3 tasks |
| 6 (cost), 7 (stage events), 8 (idle events) | All independent; run in parallel |
| 9 (report) | After Tasks 6, 7 |
| 10 (dashboard) | After Tasks 2, 6, 9 |
| E2, E3 | After their respective tasks |
| 11 (estimator + scheduling) | After Task 6 |
| 12 (cap enforcement) | After Tasks 5, 6, 11 |
| 13 (batch cap) | After Task 12 |
| E4 | After Tasks 11, 12, 13 |

---

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `_ensure_model_auth` needs live HTTP server (auth is more complex than assumed) | High — blocks Task 1 | Audit the auth flow before starting; may need a process-based auth wrapper |
| JVM heap pressure at `MAX_PARALLEL=2` OOMs the prod node | Med | Measure `free -h` before raising parallelism; add pre-flight RAM check |
| Cold-start estimate wildly off for outlier repos (huge enums, generated code) | Med | Surface estimate vs actual delta in reports; adjust formula by repo type |
| SQLite WAL contention with N writers | Low | WAL mode already set; monitor `SQLITE_BUSY` in daemon logs |

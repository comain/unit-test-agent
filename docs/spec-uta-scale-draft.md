# Draft: uta scale / reliability / budget spec

Status: **DRAFT — discussion notes, not yet a spec.** Iterate freely. Will graduate to `docs/spec-uta-scale.md` once accepted.

## Context (what we already have)

- uta runs end-to-end on a single Java repo; ~40 hours of *active* wall time per repo (calendar span was longer due to a long pause during the prod run; see Bottleneck section).
- Successfully ran in production at `worker0:/data/w/unit-test-agent` driven by a managed daemon (`bin/`, `.uta_runner/daemon.log`, `.uta_runner/uta_tasks.db`).
- Persistent state already lives in **SQLite** (`.uta_runner/uta_tasks.db`, ~30 MB). Schema is rich: `repo_tasks`, `class_tasks`, `task_events`, `repo_branches`, `runner_heartbeats`, `task_control`. Per-class actuals (tokens, llm_turn_count, attempt_count, elapsed) are already captured. Per-class **cost** field (`provider_cost_usd`) is currently NULL — measurement gap.
- Benchmark→estimate scaffolding already exists: `scripts/benmark.sh`, `benchmark/runs/`, `benchmark/estimates/`, `benchmark/comparison.md`. Repo-task `estimated_cost_usd` was populated ($201.46 for the production run). Estimator reuses 3-tier sampling.
- Models in production: small = `openai/gpt-5.4`; benchmark default big = `deepseek/deepseek-v4-pro` via OpenRouter (provider pinning supported).
- A terminal task-level live view already exists; this spec extends it with a whole-batch view.
- Existing repo cache lives at `/data/w/code/...` on the production node.

## Goal of this spec

Make uta runnable across **100+ repos** with:
1. **Scalability** — predictable throughput; parallelism that matches the actual bottleneck.
2. **Reliability** — survive crashes, transient API failures, and runaway loops without losing progress.
3. **Measurability** — per-run + aggregate cost / coverage / runtime / outcome visible.
4. **Budget guardrails** — per-repo hard cap derived from a per-repo estimate; abort on breach.

## Bottleneck analysis (decided from prod data)

Mined from `.uta_runner/uta_tasks.db` for the one production run (`sample-inbound-core`, 204 classes, all PASS):

| Metric | Value |
|---|---|
| Calendar span (started_at → finished_at) | 184.9 hours (7.7 days) — **inflated by a long pause mid-run** |
| Active wall time (operator estimate) | **~40 hours** |
| Sum of per-class LLM agent time | 3.3 hours (~**8% of active wall**) |
| Avg per-class agent time | 58 s, 3.3 LLM turns |
| Daemon resume_count | 26 (process died/restarted 26 times) |
| Class-level `task_failed` events (eventually recovered) | 176 |
| Total tokens | 186 M |
| Estimated cost | $201.46 (actual not recorded — see §3 measurement gap) |

**Implication:** The bottleneck is **not** the LLM API. ~92% of active wall time is the surrounding loop: `mvn compile` / `test_execution` / `mutation_test` (pitest) plus orchestration overhead and downtime from 26 restarts. Top stages by event count: `test_execution` (386), `plan` (319), `generate` (294), `mutation_test` (202), `compile_verify` (189).

**Parallelism implication for §1:**
- Adding nodes **does** help — per-repo cost is CPU-bound, not API-bound.
- Bounded parallel on one node is constrained by CPU/RAM/disk-IO of mvn+pitest, not by API quota.
- The order of work flips: **multi-node fan-out becomes attractive earlier than originally thought**, but is still deferred from this spec — Phase-1 is bounded parallel on one node with the knob and the lease/lock seam ready.

## Current state of parallel-readiness (in-scope for this spec)

| Layer | State | Verdict |
|---|---|---|
| `task_daemon` loop (`cli.py:1167`) | `subprocess.Popen` then poll until exit — one task at a time | Sequential |
| Scheduler/DB (`tasks/scheduler.py`, `tasks/db.py:599`) | `acquire_next_task` is transactional; `allow_same_repo_concurrency` flag exists | **Already safe** for multiple runners on one DB |
| `OpenCodeServer` (`opencode/server.py`) | Fixed port; `_terminate_stale_listeners()` kills any process on that port | **Hostile to parallel** — two concurrent runs kill each other |
| `OpenCodeProcess` (`opencode/process.py`) | stdin/stdout, no port, designed for parallel per `docs/opencode-spawn.md` | Parallel-friendly |
| Pipeline use of Server | `cli.py:1341, 1404, 1575, 1617` still import `OpenCodeServer` | **Migration incomplete** |

**Implication:** Making `MAX_PARALLEL_REPOS > 1` safe requires completing the `OpenCodeServer → OpenCodeProcess` migration started in `docs/opencode-spawn.md`. **This migration is in scope for this spec — it is the first deliverable of §1, not optional cleanup.**

**Other parallelism gotchas to address:**
- **Maven local repo (`~/.m2`)** — concurrent `mvn install` on the same artifact can corrupt the local repo. Per-worker `-Dmaven.repo.local=/var/tmp/uta-m2-<worker_id>/` or a read-only shared cache + per-worker overlay.
- **JVM memory pressure** — N parallel mvn+pitest JVMs each want a heap. Need to bound `MAX_PARALLEL_REPOS` by `available_ram / per_repo_jvm_budget`.
- **`/data/w/code/<slug>` checkout** — if the same repo is somehow enqueued twice, two workers would clobber the same working tree. The `allow_same_repo_concurrency=False` default already blocks this; keep it that way.

## Four axes

### 1. Scale — queue + daemon

Phase-1 work, in dependency order:

1. **Finish OpenCode `Server → Process` migration (in scope).** Migrate the four `OpenCodeServer` call sites in `cli.py` (lines 1341, 1404, 1575, 1617) to `OpenCodeProcess`. Server and Process APIs differ — Server is HTTP poll; Process is stdin/stdout stream — so each call site needs adapting, not just a drop-in rename. Verify by running two concurrent `uta run` invocations that they coexist without port-kill collisions.
2. **Per-worker mvn local repo.** Add `--maven-repo-local` flag and propagate to all `mvn` invocations. Default to `/var/tmp/uta-m2-<worker_id>/`.
3. **Daemon worker pool.** Convert `task_daemon` from a single Popen+wait loop to a pool of `MAX_PARALLEL_REPOS` slots, each driving its own task subprocess. `acquire_next_task` is already transactional, so no scheduler changes needed.
4. **Queue entry — one-liner script** `scripts/enqueue.sh <git-url> [--module M] [--branch B]`. Clones (or `git fetch`+checkout) into `/data/w/code/<slug>`, inserts a row into `repo_tasks`, returns the task id.
5. **Knob defaults.** `MAX_PARALLEL_REPOS=1` until JVM/CPU sizing measured on the box; tentative ceiling 2–3 on the prod node based on bottleneck data.

**Single node only in this spec.** Lease/lock/coordinator design is sketched as future work but not built. `runner_heartbeats` already provides the seam.

### 2. Reliability — checkpoint + retry + quarantine

The 26 resumes and 176 `task_failed` events on the prod run prove this layer is real, not theoretical.

- **Resume:** per-repo and per-class checkpoint already in SQLite. Daemon restart picks up `RUNNING`/`PAUSED` rows and continues from the last completed stage. `resume_count` already tracked on `repo_tasks`.
- **Retry classification** (real failure modes seen in prod):
  - Transient API (429/5xx, network) → exponential backoff, max N retries.
  - Build/compile failure (`Baseline compilation failed`, etc.) → fail the class, continue with next.
  - Unsafe LLM-authored diff (`Pre-existing unsafe dir`, `Unsafe LLM-authored patch`) → fail the class, continue. Already guarded.
  - Internal errors (`tree_sitter.Query` object, JSON decode, `[Errno 2] No such file`) → retry once with state reset; if repeats, fail the class.
  - Agent loop / dead loop → see budget abort below.
- **Quarantine:** after **2** failed task-level attempts, mark the repo `POISONED` and skip until a human reviews. `BUDGET_EXCEEDED` is a **separate state** that requires explicit operator unblock, not counted toward the quarantine N.
- **Idempotency:** re-running a `COMPLETED` task is a no-op unless `--force`.

### 3. Measurability — SQLite + rollup reports + live dashboard

**Schema gap to close (concrete):**
- `repo_tasks.provider_cost_usd` is currently NULL even when estimate is set. Backfill from per-turn `provider_cost_usd` aggregation. (Tokens are recorded; only cost is missing.)
- Per-stage timing: `task_events` has `stage_started` (2268 events) but no paired `stage_completed`. Either add the paired event, or compute durations via `LEAD(ts)` window over `stage_started` partitioned by class. Materialize into a `stage_durations` view for easy querying.
- `scheduler_idle` events (204 k of 208 k) dominate the table. Either downsample (one per minute) or move to a separate heartbeat table to keep `task_events` cheap to scan.

**Reports — both cadences:**
- On-demand: `uta report repo <name>`, `uta report batch`.
- Nightly: cron-scheduled rollup writes `repo_summary` and `batch_summary` JSON into `.uta_reports/`.

**Live terminal dashboard:**
- Existing task-level view stays.
- New **batch view** showing:
  - active workers and what each is currently doing (repo, stage, current class)
  - queue depth (waiting / running / done / failed / poisoned / budget-exceeded)
  - rolling cost: last hour spend, batch-total spend vs batch-cap (if set)
  - per-repo: % through estimated budget, % through wall-clock cap, current ETA
  - throughput: repos/day rolling avg, projected batch completion time
- Toggle: `uta dashboard --task <id>` (existing) vs `uta dashboard --batch` (new) vs default (auto: batch view if a batch is active).
- **Refresh:** 2 s.

### 4. Budget — estimate-then-cap, with probe-as-real-work

The probe and the real run are the same workflow. No separate "probe phase" that wastes spend.

- **Pre-run (cheap, no LLM cost):** parse repo size signals — LOC, # Java classes, # packages, # existing tests, deps. Use historical regression from `uta_tasks.db` if available, otherwise the cold-start `$/kLOC` formula calibrated from the production run ($201.46 / 186 M tokens / 204 classes / repo size).
- **Coarse cap at task start:** `coarse_cap_usd = preliminary_estimate × 2.0`. Used to bound spend during the first 3 classes.
- **Tier-2 sampling = first 3 real classes.** Mirrors `scripts/benmark.sh`'s 3-sample design:
  - 1 small class (low LOC)
  - 1 ~90th-percentile-sized class
  - 1 ~95th-percentile-sized class
  - These are *real* work — their tests are committed, their cost counts against the repo budget.
- **After the 3 samples — refine the cap:**
  - Compute extrapolated total cost from observed per-class actuals scaled to the remaining 201 classes.
  - Update `repo_tasks.estimated_cost_usd` and `hard_cap_usd = refined_estimate × 2.0`.
  - Persist `estimate_snapshot_json` so the refinement is auditable.
- **Continue with refined cap** for the rest of the repo.
- **Enforcement (in daemon):**
  - Track running cost per repo (sum of per-turn `provider_cost_usd`).
  - When `running_cost > hard_cap`, **abort the in-flight class immediately** (kill the agent run) to prevent dead-loop spend. Mark `BUDGET_EXCEEDED`, persist checkpoint, move on.
  - `BUDGET_EXCEEDED` requires human unblock to resume.
  - Optional global batch cap: when exceeded, pause the queue (no new tasks dequeued).
- **Code home:** `benchmark/estimates/` — extend with `estimator.py` reusing the `scripts/benmark.sh` sampling harness and reading `uta_tasks.db` for history.

## Non-goals

- HA / failover.
- Multi-node fan-out (deferred — but the bottleneck data argues for prioritizing it sooner than originally thought).
- Web dashboard.
- Multi-tenant cost accounting.
- A `uta analyze` command for bottleneck reports — the bottleneck analysis is a one-time spec-planning input, not a built feature.
- Jira / pipeline integration (this is a personal project — see `feedback-non-uta-project` memory).

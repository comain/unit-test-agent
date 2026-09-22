# Tasks: Executable Test-Generation Cycle Using agent-core `agent_turn`

Plan: [plan-executable-generation-agent-turn.md](plan-executable-generation-agent-turn.md)
Status legend: `[ ]` pending · `[~]` in progress · `[x]` done
Iteration 2 plan approved: 2026-08-18

## Iteration 2 — shared prompt construction

### Phase H — agent-core 0.6.0

- [x] **H1** Add `RenderedPrompt`/`render_sections`, per-call newline overlay,
      and opt-in thread-safe source cache; prove byte/concurrency compatibility.
      agent-core commit `6de8338`; 19 focused and 1,216 full tests passed.
- [x] **H2** Add opt-in strict bounded metadata, secure modes/symlink checks,
      and recoverable artifact writes without changing legacy defaults.
      agent-core commit `45c9611`; 35 focused and 1,232 full tests passed.
- [x] **H3** Run clean CR candidate suite, full agent-core suite, document,
      release/push `v0.6.0`, and verify remote refs.
      agent-core commit/tag `d0d14af`/`v0.6.0`; 1,232 agent-core and 293 CR
      tests passed on Python 3.11.12. Remote `main`, local tag, and remote tag
      all resolve to `d0d14afa912bcc752e0cb16632b1602a110ca55a`.

> **Checkpoint H** — provider release and CR consumer gate green.

### Phase I — UTA facade and legacy runtime

- [x] **I1** Freeze all prompt golden bytes, pin released 0.6.0, and replace
      UTA's Jinja prompt loader with the shared render-only facade.
      All nine template APIs and five Java/Python legacy/durable composers have
      exact byte contracts; 42 focused and 1,834 full tests passed. A fresh
      Python 3.12.10 environment resolved `v0.6.0` to `d0d14af`.
- [x] **I2** Add the canonical managed/standalone artifact scope resolver and
      fixed-key safe metadata projection.
      Managed/taskless paths, cleanup, ancestry, symlink, `0700`, fixed-shape,
      and 4 KiB boundaries passed 14 focused and 1,848 full tests.
- [x] **I3** Move legacy `AgentRuntime` normal/recovery prompts to strict shared
      materialization outside the repository.
      Safe identity-only `0600` prompt/input pairs and zero-turn failure passed
      10 focused, 107 caller-focused, and 1,851 full tests.

> **Checkpoint I** — prompt parity and secure legacy materialization green.

### Phase J — consumers and lifecycle

- [x] **J1** Wire Java managed and taskless standalone scope ownership.
      One invocation scope now reaches every legacy Java runtime through the
      workflow state, with managed identity and immediate taskless cleanup.
- [x] **J2** Wire Python managed and taskless standalone scope ownership.
      One cached runtime reuses the invocation scope across targets; scoped
      factories and historical one-argument factories both pass the explicit
      compatibility contract, while durable binding rejects the provisional
      legacy identity. The combined Java/Python gate passed 246 tests.
- [x] **J3** Materialize durable prompts through agent-core and checkpoint
      `prompt_file` plus `prompt_inputs_file`.
      The reconciled task/run/unit/operation identity now owns an external
      `0600` pair; incomplete pairs rematerialize, lineage mismatches fail
      before a session, and completed checkpoint replay preserves both paths.
      Durable adapters now construct only the configured neutral harness,
      without creating an unused legacy runtime or provisional scope.
- [x] **J4** Extend managed retention, standalone close cleanup, and 30-day
      crash-orphan pruning.
      Durable prompts follow exact workflow-run/unit identities. Managed
      legacy invocation UUIDs are recorded in the product audit and linked
      transactionally by clean reruns, so active replacements prune only the
      superseded scopes; old terminal tasks use validated UUID discovery.
      Standalone scopes close immediately and the same pass removes only
      confined 30-day crash orphans; live scopes are lease-protected.
- [x] **J5** Enforce whole-product prompt source boundaries and real-Git
      non-staging behavior.
      AST guards cover production agent prompt construction while excluding
      HTML report rendering. Real-Git delivery tests prove legacy/durable
      prompt artifacts stay out of the index without blocking legitimate
      generated `inputs.json` resources or existing cache deliverables.

> **Checkpoint J** — all engines/languages share one secure prompt boundary.

### Phase K — acceptance

- [ ] **K1** Run clean full suites/review/capacity gates; update both READMEs
      and operator docs; commit/push in verified atomic slices.
      Local gates are complete: agent-core 1,240 passed; UTA 1,882 passed/11
      skipped; focused migration/retention 149 passed; independent review has
      no findings. Prompt ceilings pass. The 1 MiB/120-step checkpoint fixture
      reaches 243.52 MiB per unit, so the 30-day/50%-volume gate remains open
      until K2 supplies beta p95 payload, steps, units, task volume and disk
      capacity.
- [ ] **K2** Run default legacy canary then durable beta replay; record hashes,
      safe roots/inputs, non-staging, missing-value, restart, and SSE evidence.

> **Checkpoint K** — Iteration 2 complete. Production enablement and legacy
> deletion remain blocked on separate Iteration 1 G3 authorization.

## Phase A — agent-core (must ship first)

- [x] **A1** `workflow/checkpoints.py` — `open_checkpointer` (context manager),
      `WorkflowRunIdentity` with the version in `thread_id`, not `checkpoint_ns`
- [x] **A2** `workflow/execution.py` — `invoke_workflow` owns the
      start/resume/reuse decision. *Rewritten from the original
      `build_graph(subgraphs=…)` task, which design revision 6 rejects as a
      second registration mechanism.*
- [x] **A3a** `recovered` on `TurnLoop`/`NodeOutcome`, true only when the
      in-session nudge produced the accepted answer
- [x] **A3b** `runtime/progress.py` — projection moved out of `sse.py`
      (dependency inverted), `ProgressEnvelope`, one shared kind dispatch
- [x] **A3c** `AgentTurnResult` + `normalize_turn_outcome`; the progress-sink
      subsystem (`ProgressBudget`, `ProgressBatcher`, `AgentProgressSink`);
      `agent_turn` delegating to `run_harness_node` behind `result_mode`
- [x] **A4** guard contract — `before_turn(state, config)` returns a token,
      `after_turn(state, config, token)` runs in a `finally`
- [x] **A5** release **0.5.0**
- [x] **A6** ⟵ **gate** cr_plugin's full suite against this tree: **293
      passing**. agent-core `main` is **not pushed** (11 commits ahead); the
      push is what would reach cr_plugin, which pins `@main`.

> **Checkpoint 1** — A5 and A6 green before any UTA work.

## Phase B — UTA scaffolding

- [x] **B1** pin agent-core 0.5.0 (a release, not an editable path).
      agent-core `main` + tag `v0.5.0` pushed; UTA verified against the
      packaged release, not the editable path — 1594 passed, 11 skipped
- [x] **B2** rewrite `generation-cycle.yaml` — 69 nodes, defaults on every
      branch, bounded repair routes, the two missing phases, render/turn/
      interpret triples. `GENERATION_PHASES` now publishes `operator_phase`
      (13 labels) rather than node names. 34 topology tests, mutation-checked.
      Fixed a defect this introduced: the spec was re-parsed per state update
      (~29ms × N), which presented as a hung suite, not a failure
- [x] **B3** `uta/testgen/graph/cycle.py` — the seven nodes and sixteen
      selectors the topology names, `build_cycle_workflow(context, …)` and
      `run_cycle(…)` through `invoke_workflow`. Extends `shared_registry`, so
      `agent_turn` is agent-core's rather than a copy
- [x] **B4** `CycleState` — ids and evidence only; live objects travel in
      `context`, which is never checkpointed
- [x] **B5** batched `find_class_tasks` in the guards *(landed as a strict
      improvement to the current path)*

> **Checkpoint 2 — met.** The cycle executes end-to-end against a scripted
> backend (precheck → plan → generate → verify → measure → gate → complete),
> repairs route back through deterministic verification, an exhausted repair
> loop terminates, a cancelled turn pauses without completing, and a completed
> lineage resumes as `reused_completed` rather than re-running the model.
> *Not yet real:* the ledger and backend are ports with no production
> implementation — that is Phase C/D.

## Phase C — durable UTA runtime

- [x] **C1** operation ledger schema/repository — stable IDs, fingerprints,
      idempotent start/complete/fail/cancel transitions
- [x] **C2a** stable operation identity and owner-only atomic result artifacts
- [x] **C2b** seven-way reconciliation, rehydration, crash ordinals, and
      agent-core `on_result`/deterministic result ports
- [x] **C3** context-managed workflow application — checkpointer, stable batch
      identity, cancellation/resume, outer result validation
- [x] **C4** clean rerun/retention — authoritative supersessions, confirmed CLI,
      ordinary-resume identity preservation, scheduled/operator pruning
- [x] **C5** persisted progress storage — atomic task caps, batch append/cursor,
      terminal event transaction, SSE store adapter

> **Checkpoint 3** — scripted topology runs through the production ledger,
> checkpointer, progress store, and restart boundary.

## Phase D — Java decomposition *(the bulk)*

- [x] **D1** `precheck_existing_tests` (+ class-level, diff-enforcer, delegated
      precheck repair) — ~290 lines
- [x] **D2** `verify_compile` + `fix_compile_*` — ~185
- [x] **D3** `verify_tests` — ~35 + inline
- [x] **D4** `fix_tests_*` (coverage- and mutation-driven loops) — ~215
- [x] **D5** `measure_coverage` + `fix_coverage_*` — ~215
- [x] **D6** `measure_mutation` + `fix_mutation_*` — ~140
- [x] **D7** `delegated_quality_gate` + repair — ~250
- [x] **D8** the remainder of `generate_and_validate` — planning, generation,
      per-class test repair, completion. **Split further after D1–D7; not
      estimated here.**

> **Checkpoint 4** — Java on the new graph; Java parity suite green.

## Phase E — Python *(construction, not decomposition)*

- [x] **E1** real compile-equivalent evidence (syntax + import contract),
      not an unconditional `skipped`
- [x] **E2** split the single `_run_python_repair_loop` into the four repair
      phases the topology declares
- [x] **E3** `run_python_batch_request` → phase handlers
- [x] **E4** explicit `skipped` with a reason where genuinely unsupported

> **Checkpoint 5** — both languages on the new graph; both parity suites green.

## Phase F — cutover and observability

- [x] **F1** persisted default-off `generation_cycle_v2_enabled` selection and
      immutable task snapshot
- [x] **F2** read-only fix-session SSE route, cursor/backfill/terminal contract,
      and per-session report tabs
- [x] **F3** started/resumed/reused-completed counters and full accounting parity
- [x] **F4** compatibility boundary test and delayed-deletion gate; keep legacy
      adapters until separately authorized production observation/drain

## Phase G — verification and rollout

- [x] **G1** full suites, compile/import check, architecture/code review,
      README/operator docs, detailed commits, push and remote-ref verification.
      Final UTA verification: **1,796 passed, 11 skipped**; agent-core:
      **1,208 passed**. Review fixes included post-cycle delivery ordering,
      bounded progress-sink cleanup, committed-source recovery evidence, and
      rejection of agent-created commits.
- [ ] **G2** beta replay of a production task; deliberate restart and
      session-isolated SSE evidence in `remote-deployment.md`
- [ ] **G3** production enablement and legacy deletion — **requires separate
      explicit authorization and is not authorized by build continuation**

## Open decisions carried into implementation

- [x] D8's internal slicing — resolved as language-owned phase modules after
      D1–D7 established the boundaries
- [x] Verify `uta/language/*` is quiescent before D starts

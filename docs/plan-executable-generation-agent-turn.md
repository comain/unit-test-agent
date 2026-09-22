# Plan: Executable Test-Generation Cycle Using agent-core `agent_turn`

Status: Iterations 1 and 2 approved; Iteration 2 approved 2026-08-18
Date: 2026-08-17
Spec: [spec-executable-generation-agent-turn.md](spec-executable-generation-agent-turn.md)
Design: [design-executable-generation-agent-turn.md](design-executable-generation-agent-turn.md)
([agent-core](design-executable-generation-agent-turn-agent-core.md),
[UTA](design-executable-generation-agent-turn-uta.md))
Spike: [spike-executable-generation-agent-turn.md](spikes/spike-executable-generation-agent-turn.md)
Tasks: [todo-executable-generation-agent-turn.md](todo-executable-generation-agent-turn.md)

## Iteration 2 plan — shared prompt construction

This plan implements the frozen 2026-08-18 prompt boundary. It preserves the
completed Iteration 1 tasks below. Provider work lands and releases in
agent-core first; UTA then consumes the released tag. No production enablement
or legacy deletion is authorized by this plan.

### Iteration 2 dependency graph

```text
H1 section rendering ─┐
                      ├─> H3 CR gate + agent-core 0.6.0 release
H2 strict artifacts ──┘                         │
                                               v
                                    I1 UTA pin + prompt facade
                                               │
                           I2 artifact scope + safe metadata
                                               │
                                    I3 legacy AgentRuntime
                                               │
                         ┌─────────────────────┼─────────────────────┐
                         v                     v                     v
                    J1 Java scope        J2 Python scope       J3 durable node
                         └─────────────────────┼─────────────────────┘
                                               v
                                  J4 retention -> J5 Git boundary
                                               │
                                               v
                               K1 full verification -> K2 beta evidence
```

Hard ordering:

- H1/H2 precede H3 because the candidate CR suite must exercise the complete
  additive API before agent-core is released.
- H3 precedes I1 because UTA must pin a pushed release, never an editable or
  snapshot dependency.
- I1/I2 precede I3/J because all UTA execution paths share one renderer,
  metadata projection, and artifact-scope contract.
- J1 and J2 are independent after I3; J3 shares only I1/I2 and can proceed in
  parallel when worktree ownership is coordinated. `cycle.py` is currently
  modified by another workstream, so implementation must verify it is
  quiescent before J3.
- J4 follows all artifact producers so retention covers their final canonical
  layouts. J5 then proves no producer can cross the Git boundary.
- K2 follows a fully verified build. The legacy canary is first because legacy
  prompt migration is not gated by `generation_cycle_v2_enabled`.

### Phase H — agent-core 0.6.0

#### H1 — Add byte-compatible section rendering

**Description:** Extend the existing `PromptLibrary` with `RenderedPrompt`,
`render_sections`, opt-in thread-safe source caching, and a per-call Jinja
environment overlay for trailing-newline compatibility.

**Acceptance criteria:**

- `stable + volatile` is the exact full text for boundary present/absent and
  first-marker-only cases; missing variables remain strict.
- `keep_trailing_newline=False` matches legacy UTA section bytes without
  trimming; mixed concurrent policies cannot mutate the shared environment.
- Existing `render` and `render_to_file` snapshots remain unchanged.

**Verification:**

- `/home/user/saas/agent-core/.venv/bin/python -m pytest tests/test_prompts.py -q`
- Focused benchmark records cached/uncached 4 KiB, 64 KiB, and largest-template
  read/render time.

**Dependencies:** None.

**Files likely touched:** `agent-core/src/agent_core/prompts.py`,
`agent-core/tests/test_prompts.py`.

**Estimated scope:** Small.

#### H2 — Add strict secure prompt materialization

**Description:** Add opt-in strict metadata, byte limit, requested file mode,
symlink rejection, and recoverable temp/replace writes to the existing
materializer while preserving every old default.

**Acceptance criteria:**

- Strict mode accepts recursively JSON-safe values with string keys and finite
  numbers, produces deterministic bytes, and rejects coercion/oversize data.
- Requested `0600` applies to temporary/final files; symlink targets fail; a
  partial pair is safely overwritten on retry.
- Existing callers retain `default=str` compatibility and current modes unless
  they opt into strict behavior.

**Verification:**

- `/home/user/saas/agent-core/.venv/bin/python -m pytest tests/test_prompts.py -q`
- `/home/user/saas/agent-core/.venv/bin/python -m pytest -q`

**Dependencies:** None; merge after H1 because both touch `prompts.py`.

**Files likely touched:** `agent-core/src/agent_core/prompts.py`,
`agent-core/tests/test_prompts.py`.

**Estimated scope:** Small.

#### H3 — Gate consumers and release agent-core 0.6.0

**Description:** Document/version the additive API, install the candidate SHA
in a clean CR environment, run CR and agent-core suites, then tag and push
0.6.0 before any UTA pin change.

**Acceptance criteria:**

- CR's full suite passes with the recorded candidate direct-URL commit.
- agent-core full suite passes; version/docs describe newline, cache, strict
  metadata, and unchanged defaults.
- tag `v0.6.0` and its remote ref resolve to the verified commit.

**Verification:**

- Run the exact clean-environment CR commands in the approved agent-core
  detail design.
- Verify local/remote SHA equality for `main` and `v0.6.0` after push.

**Dependencies:** H1, H2.

**Files likely touched:** `agent-core/pyproject.toml`, `agent-core/README.md`,
agent-core release notes/changelog if present.

**Estimated scope:** Small.

> **Checkpoint H.** agent-core 0.6.0 is released, remotely verified, and green
> in both its own and CR's clean consumer suites.

### Phase I — UTA shared facade and legacy path

#### I1 — Pin 0.6.0 and migrate the prompt facade

**Description:** Freeze pre-migration golden bytes, pin the released agent-core,
replace UTA's Jinja loader with `PromptLibrary(cache_source_text=True)`, and
retain the one-release `.render(...)` compatibility facade.

**Acceptance criteria:**

- Frozen single-render full bytes and split stable/volatile/concatenated bytes
  match separately for all nine templates, Java/Python composers,
  defaults/optional branches, Unicode, and trailing newlines. The historical
  one-LF difference at the boundary is preserved without normalization.
- Direct UTA prompt rendering is strict; legitimate empty values have explicit
  defaults; deprecated facades promise only `.render` and shared exceptions.
- The installed agent-core direct URL identifies pushed `v0.6.0`, not an
  editable checkout.

**Verification:**

- `.venv312/bin/python -m pytest tests/test_prompt_render.py tests/test_prompt_prefix_stable.py -q`
- Inspect installed distribution metadata in a clean UTA environment.

**Dependencies:** H3.

**Files likely touched:** `pyproject.toml`, `uta/testgen/prompts/loader.py`,
`uta/testgen/prompts/__init__.py`, prompt fixture/test files.

**Estimated scope:** Medium.

#### I2 — Add the application-owned artifact scope

**Description:** Add the canonical managed/standalone scope resolver and one
named-argument safe metadata projection under `uta.testgen.prompts`.

**Acceptance criteria:**

- Managed layout is exactly
  `managed/<task>/<workflow-run>/<unit>/<operation>`; standalone commands get
  one stable UUID under `standalone/<run>`.
- root comes only from `UTA_RUNNER_HOME` or the default UTA application state;
  resolved roots equal to/below the target repository and symlink escapes fail
  before writes or sessions.
- both engines receive the same fixed-key, strict, <=4 KiB metadata shape;
  feedback/provider/recovery text and arbitrary state cannot be supplied.

**Verification:**

- New focused resolver/projection tests cover managed, taskless, repo-local DB,
  configured root inside repo, symlink, permissions, and stable identity.

**Dependencies:** I1.

**Files likely touched:** new `uta/testgen/prompts/artifacts.py`, prompt package
exports, new focused tests.

**Estimated scope:** Medium.

#### I3 — Move legacy AgentRuntime to the shared scope

**Description:** Require `AgentRuntime` to receive a prompt artifact scope and
use strict agent-core materialization for normal and recovery turns.

**Acceptance criteria:**

- legacy prompt/inputs files are `0600` outside the repository and use the
  shared safe projection; no repository fallback exists.
- attempts retain safe numeric identity, while feedback and recovery reasons
  never enter `inputs.json`.
- scope/materialization failure occurs before a harness turn starts.

**Verification:**

- `.venv312/bin/python -m pytest tests/test_agent_runtime.py -q`
- failure-injection spy asserts zero session/turn starts.

**Dependencies:** I2.

**Files likely touched:** `uta/testgen/agent_runtime.py`,
`tests/test_agent_runtime.py`.

**Estimated scope:** Small.

> **Checkpoint I.** Shared rendering and secure legacy materialization pass
> golden parity before language entrypoint plumbing begins.

### Phase J — UTA consumers, lifecycle, and security

#### J1 — Wire Java managed and standalone scopes

**Description:** Open one scope per Java invocation and pass it through legacy
generation and durable binding without requiring a task DB.

**Acceptance criteria:**

- Java standalone CLI with no task/task DB opens a session and reuses one run
  UUID across turns; normal exit removes the scope.
- managed Java uses the canonical task/workflow/unit/operation layout.
- runtime factories used by tests/embedders receive an explicit compatible
  scope contract rather than regaining repository storage.

**Verification:**

- Focused Java generation/backend and CLI compatibility tests.

**Dependencies:** I3.

**Files likely touched:** `uta/language/java/adapter.py`,
`uta/language/java/generation.py`, `uta/app/generation_commands.py`, focused
Java tests.

**Estimated scope:** Medium.

#### J2 — Wire Python managed and standalone scopes

**Description:** Apply the same invocation-owned scope to Python request,
runtime factory, backend binding, and standalone CLI.

**Acceptance criteria:**

- Python standalone CLI with no task/task DB opens a session, keeps one run UUID
  across targets/turns, and cleans up normally.
- managed Python uses the same canonical layout and metadata contract as Java.
- custom runtime factories remain supported through an explicit adapter seam.

**Verification:**

- `.venv312/bin/python -m pytest tests/test_python_batch_generation.py tests/test_python_cli_autopush.py tests/test_cli_language_options.py -q`

**Dependencies:** I3.

**Files likely touched:** `uta/language/python/generation.py`,
`uta/language/python/adapter.py`, `uta/language/python/generation_backend.py`,
`uta/app/cli.py`, focused Python tests.

**Estimated scope:** Medium.

#### J3 — Materialize durable prompts and checkpoint both paths

**Description:** Replace direct durable prompt writes with strict agent-core
materialization and add `prompt_inputs_file` to real cycle state.

**Acceptance criteria:**

- `generation_prompt` returns both external owner-only paths using the current
  reconciled operation identity; no direct write remains.
- checkpoint round-trip/restart preserves both strings; an incomplete pair is
  rematerialized before any `agent_turn`.
- rendering/metadata/path failures open no harness session.

**Verification:**

- `.venv312/bin/python -m pytest tests/test_generation_cycle_build.py tests/test_generation_workflow_application.py tests/test_cycle_state_serialization.py -q`

**Dependencies:** I1, I2. Confirm the existing uncommitted `cycle.py` work is
quiescent before editing.

**Files likely touched:** `uta/testgen/graph/cycle.py`,
`uta/testgen/graph/cycle_state.py`, focused graph/checkpoint tests.

**Estimated scope:** Medium.

#### J4 — Extend prompt retention

**Description:** Extend the existing workflow retention owner to delete managed
prompt identities with eligible lineages, remove standalone scopes on context
close, and prune only validated standalone crash orphans after 30 days.

**Acceptance criteria:**

- eligible managed paths are deleted by exact task/run/unit identity; active or
  unsafe paths remain untouched.
- standalone normal cleanup is immediate; age pruning is root-confined and
  rejects symlinks/malformed identities.
- pruning result/reporting accounts for prompt artifacts without changing
  checkpoint/operation ordering.

**Verification:**

- `.venv312/bin/python -m pytest tests/test_workflow_retention.py tests/test_workflow_retention_schedule.py -q`

**Dependencies:** J1, J2, J3.

**Files likely touched:** `uta/tasks/workflow_retention.py`, artifact-scope
module, retention tests.

**Estimated scope:** Medium.

#### J5 — Enforce prompt source and Git boundaries

**Description:** Remove remaining direct Jinja prompt imports/writes and add
architecture plus real-Git delivery tests covering all production agent-prompt
paths.

**Acceptance criteria:**

- AST/source test covers `uta/testgen` and `uta/language`, narrowly allowing
  unrelated HTML reports; Java's unused Jinja import is gone.
- a repository with no `.uta_cache` ignore stages generated deliverables but no
  prompt, inputs, legacy `agent_turns`, durable `cycle-prompts`, or workflow
  state artifact.
- existing generated-test and dependency-cache delivery remains compatible.

**Verification:**

- Focused lane-layering, delivery, prompt, Java, and Python tests using a real
  temporary Git repository.

**Dependencies:** J1-J4.

**Files likely touched:** source-boundary tests, delivery integration tests,
`uta/language/java/generation.py`; change `delivery.py` only if the real test
exposes an existing staging violation after out-of-repo placement.

**Estimated scope:** Medium.

> **Checkpoint J.** Java, Python, legacy, and durable paths share one renderer,
> scope resolver, metadata contract, retention owner, and proven Git boundary.

### Phase K — verification, documentation, and beta proof

#### K1 — Full verification and documentation

**Description:** Run both complete suites, compile/import/source checks, measure
prompt capacity, review the diff, and update agent-core/UTA README and operator
documentation to match the released behavior.

**Acceptance criteria:**

- no prompt exceeds 1 MiB, ceiling-task projection does not exceed 400 MiB, and
  30-day projection stays below 1 GiB and 50% of the state volume.
- agent-core and UTA full suites pass from clean environments with released
  0.6.0; CR remains green.
- docs describe standalone storage/cleanup, legacy canary, rollback, safe
  metadata, and no prompt report exposure.

**Verification:** Run all commands from the spec, `git diff --check`, import
compile excluding Python-2 fixtures, staged-secret/path review, and remote-ref
verification after each atomic push.

**Dependencies:** J5.

**Files likely touched:** both repository READMEs, approved design/usage
changelogs if implementation evidence differs, capacity evidence artifact.

**Estimated scope:** Medium.

#### K2 — Legacy canary and durable beta evidence

**Description:** On beta, first replay a default legacy task, then a durable-v2
task, recording exact submitted prompt hashes, artifact roots/modes, safe
inputs, zero-session missing-value behavior, Git staging, and restart/SSE
evidence.

**Acceptance criteria:**

- legacy and durable hashes match frozen fixtures and actual submitted bytes;
  artifacts resolve outside Git and inputs match the fixed safe schema.
- missing-value injection opens zero sessions; `git diff --cached` contains no
  prompt artifact.
- durable restart does not repeat a stable expensive operation and session SSE
  remains isolated. Failure rolls beta back to the previous UTA build/pin.

**Verification:** Record commands, task IDs, hashes, path checks, screenshots or
event excerpts, and remote deployed revisions in `remote-deployment.md`.

**Dependencies:** K1 and explicit beta deployment/replay authorization at
execution time.

**Files likely touched:** `remote-deployment.md` evidence only.

**Estimated scope:** Medium.

> **Checkpoint K.** Iteration 2 is complete after both canaries pass. Production
> enablement and legacy deletion remain separately authorized Iteration 1 G3.

### Iteration 2 requirement coverage

| Source | Requirement or decision | Tasks |
| --- | --- | --- |
| Spec I2 R1 | generic strict rendering, sections, artifacts, configurable names | H1, H2 |
| Spec I2 R2 | UTA owns templates/defaults/domain policy, no local engine | I1, J5 |
| Spec I2 R3/R8 | durable returns/checkpoints both artifact paths | J3 |
| Spec I2 R4/R5 | legacy/durable convergence and exact bytes | H1, I1, I3, J3 |
| Spec I2 R6/R9 | deterministic safe metadata shared by both engines | H2, I2, I3, J3 |
| Spec I2 R7 | no direct Jinja or prompt writes in production prompt paths | I1, J3, J5 |
| Spec I2 R10/R11 | standalone identity and resolved out-of-repo root | I2, J1, J2 |
| Spec I2 R12 | ungated legacy canary and previous-build rollback | K1, K2 |
| Success 1-5 | shared API, byte parity, strict failure, checkpoint state, source boundary | H1-H2, I1-I3, J3, J5 |
| Success 6-7 | agent-core neutrality and provider-first released dependency | H1-H3, I1, K1 |
| Success 8 | durable flag remains default-off; no deletion | K1, K2; Iteration 1 G3 remains blocked |
| Success 9 | no Git staging and 30-day lifecycle | I2, J4, J5, K2 |
| Success 10 | both canaries and rollback proof | K2 |
| Design | newline overlay and source-cache concurrency | H1 |
| Design | strict modes, partial pair, permissions, symlink safety | H2, I2, J3 |
| Design | managed/standalone canonical layouts and cleanup | I2, J1, J2, J4 |
| Design | facade compatibility window | I1, K1 |
| Design | CR candidate, tag, clean UTA install | H3, I1, K1 |
| Design | capacity gates and production proof | K1, K2 |
| Usage | storage, retention, canary, rollback operator guidance | J4, K1, K2 |

No orphan task exists: every H task implements the shared-provider contract;
I/J tasks implement UTA ownership, compatibility, security, or lifecycle; K
tasks prove the approved rollout contract. Production enablement remains out of
scope and is not hidden in any task.

### Iteration 2 risks

| Risk | Mitigation |
| --- | --- |
| Golden fixtures accidentally capture post-migration output | capture/freeze from the pre-edit UTA revision before changing loader; review fixture provenance |
| Shared `prompts.py` tasks conflict | H1 then H2 sequentially with focused tests/atomic commits |
| Current uncommitted `cycle.py` work is overwritten | inspect ownership/quiescence before J3; stage only explicit files |
| Java/Python custom runtime factories break | explicit scope adapter contract and standalone/managed compatibility fixtures |
| Legacy production behavior changes without v2 flag | legacy canary first; previous-build/dependency rollback recorded and tested |
| Prompt data crosses Git boundary | dedicated application root, resolved ancestry rejection, real-Git staging test |
| Artifact growth exceeds operator budget | hard beta thresholds plus managed/standalone retention and existing state-volume alert |

## Contents

1. [Iteration 2 plan — shared prompt construction](#iteration-2-plan--shared-prompt-construction)
2. [Shape of the work](#shape-of-the-work)
3. [Dependency graph](#dependency-graph)
4. [Phase A — agent-core](#phase-a--agent-core)
5. [Phase B — UTA scaffolding](#phase-b--uta-scaffolding)
6. [Phase C — durable UTA runtime](#phase-c--durable-uta-runtime)
7. [Phase D — Java decomposition](#phase-d--java-decomposition)
8. [Phase E — Python](#phase-e--python)
9. [Phase F — cutover and observability](#phase-f--cutover-and-observability)
10. [Phase G — verification and rollout](#phase-g--verification-and-rollout)
11. [Requirement coverage](#requirement-coverage)
12. [Risks this plan carries](#risks-this-plan-carries)

## Shape of the work

Measured, from the design:

| | Size |
| --- | --- |
| agent-core changes | ~400 lines + tests, 3 modules |
| UTA scaffolding | ~600 lines, YAML rewrite + graph wiring |
| Durable UTA runtime | additive ledger/artifact/checkpoint/progress/retention slices |
| **Java decomposition** | **4,611-line file; `generate_and_validate` is 1,641 lines alone** |
| **Python** | 1,874-line file, **one** repair loop where Java has five — partly construction, not decomposition |
| Cutover + reporting | default-off selection, SSE/session tabs, parity and counters |

Phase D is the largest part of the project. Any schedule that treats "Java" as one task is wrong;
it is sliced below by the loops the AST pass found, each of which is an
independently testable unit.

## Dependency graph

```
A1 checkpoints ─┐
A2 invocation  ──┼─▶ A5 release 0.5.0 ─▶ B1 pin ─▶ B2..B5 graph scaffold
A3 agent_turn  ──┤        ▲                              │
A4 guards      ──┘        │                              ▼
                    A6 cr_plugin gate         C1..C5 ledger/runtime/checkpoint
                                                         │
                                      ┌──────────────────┴──────────────────┐
                                      ▼                                     ▼
                              D1..D8 Java phases                    E1..E4 Python phases
                                      └──────────────────┬──────────────────┘
                                                         ▼
                                           F1..F4 cutover/observability
                                                         ▼
                                             G1 review → G2 beta replay
```

Hard orderings, each for a stated reason:

- **A6 before A5** — cr_plugin tracks `agent-core@main`, so its suite gates the
  merge, not the release.
- **A5 before B1** — UTA consumes a released version; editable wiring is for
  development only.
- **B2–B5 before C** — the executable neutral graph and batch guard exist
  before the product ledger/checkpoint runtime is attached.
- **C before D and E** — both language adapters need the same durable operation
  and application boundary; building it twice would violate the design.
- **D and E before F** — both languages target the same topology; it
  must exist first or they will diverge.
- **F before G** — beta evidence is meaningful only after persistence,
  progress, reporting, and both-language parity are complete.

D and E are independent after C, but this autonomous run executes them
sequentially so every commit starts from a verified tree.

## Phase A — agent-core

### A1 — `workflow/checkpoints.py`

Add `open_checkpointer(path)` (context manager) and `WorkflowRunIdentity`
(frozen, `thread_id` derived with the topology version **in the thread id** —
not `checkpoint_ns`, which LangGraph resolves as a subgraph path).

*Acceptance:* a checkpointer opens, writes, closes its connection; two
identities with different versions produce disjoint lineages; a missing
`langgraph-checkpoint-sqlite` raises at open with the install command.
*Verify:* `pytest tests/test_checkpoints.py`; assert no `sqlite3` connection
leaks after the context exits.

### A2 — `workflow/execution.py`

`invoke_workflow` inspects the lineage and owns absent start, pending resume
with `None`, completed-state reuse, and explicit corrupt-state failure.

*Acceptance:* a pending/completed lineage never re-enters an expensive node;
provider/deserialization errors become `WorkflowCheckpointError`; a clean
rerun uses a new versioned identity.
*Verify:* `pytest tests/test_workflow_execution.py`.

### A3 — `agent_turn` delegates to `run_harness_node`

Session scope, exact legacy activation boundary, JSON-safe normalized result,
no-throw task-scoped progress sink, post-turn guard ordering, and the neutral
`on_result` durability port.

*Acceptance:* parameterized legacy behavior is exact; normalized turns capture
session/accounting before close; projection/publish/flush never abort a turn;
guard acceptance and session close precede `on_result`; a sink failure stops
the checkpointed state update.
*Verify:* `pytest tests/test_agent_turn_normalized.py tests/test_progress_batcher.py tests/test_workflow_nodes.py -k agent_turn`.

### A4 — guard contract

`before_turn(state, config) -> snapshot`, `after_turn(state, config, snapshot)`,
`after_turn` in a `finally`, before durable result completion.

*Acceptance:* a guard that raises does not leak a session; `after_turn` runs
when the turn raises.
*Verify:* `pytest tests/test_workflow_nodes.py -k guard`.

### A5 — release 0.5.0

Version bump, changelog, tag, push.
*Acceptance:* `pip install 'agent-core==0.5.0'` resolves; full suite green.

### A6 — cr_plugin compatibility gate ⟵ **checkpoint**

Run cr_plugin's full suite against the agent-core branch **before merge to
main**.

*Acceptance:* cr_plugin green. If red, the branch does not merge.
*Rationale:* `cr_plugin/pyproject.toml:20` tracks `@main`, so a regression
reaches a production service on its next image build. This is the only gate
that protects it.

> **Checkpoint 1.** A5 + A6 green before any UTA work starts.

## Phase B — UTA scaffolding

### B1 — pin agent-core 0.5.0

*Acceptance:* `pyproject.toml` names the release, not an editable path; suite
green.

### B2 — rewrite `generation-cycle.yaml`

Bounded repair routes, a `default` on **every** branch, `continue` in the
vocabulary, `precheck_existing_tests` and `delegated_quality_gate` added,
render/turn/interpret triples for the seven model-driven phases.

*Acceptance:* every branch has a default; every repair loop has an
attempts-exhausted route to `complete_generation`; the spec builds; a
graph-construction test enumerates the declared routes and finds no unroutable
outcome.
*Verify:* `pytest tests/test_generation_cycle_spec.py`.

### B3 — build and invoke the child

Build the neutral child and extend `shared_registry` so `agent_turn` is the
agent-core node. Invoke it through `run_cycle(..., identity, checkpointer)`;
production application ownership comes in Phase C.

*Acceptance:* the cycle is compiled and invoked per unit; a test observes
**real execution** of declared routes, not a loaded spec.
*Verify:* `pytest tests/test_generation_cycle_execution.py` — the single most
important test in this plan, because a topology that describes what the code
does not do is invisible to every other level.

### B4 — `CycleState` and the serialization boundary

Ids and evidence only; live objects (`BatchGenerationRequest`,
`runtime_factory`, `AgentRuntime`) move to `context`, resolved inside phase
nodes.

*Acceptance:* `CycleState` round-trips through the checkpoint serializer and
contains no callable or credential-shaped key; a phase node resolves its
backend from an id.
*Verify:* `pytest tests/test_cycle_state_serialization.py`.

### B5 — batched class lookup in the guard

Add `find_class_tasks(task_id, fqns)`; use it in `llm_guard_before/after`.

*Acceptance:* one query per batch instead of one per class; guard behaviour
unchanged.
*Verify:* existing guard tests, plus a query-count assertion.
*Note:* a strict improvement to the current path; may land before the rest.

> **Checkpoint 2.** The cycle executes end-to-end against a scripted backend
> before either language is touched.

## Phase C — durable UTA runtime

These slices close the intentionally scripted ports from Checkpoint 2 before
either language is migrated. Each is additive and keeps the legacy engine as
the default-off rollback path.

### C1 — operation ledger schema and repository

Add `workflow_operations`, stable operation IDs, status transitions, product
fingerprints, and idempotent start/complete/fail/cancel APIs.

*Acceptance:* immutable conflicts fail unsafe; STARTED→terminal transitions are
atomic/idempotent; pre-migration DBs upgrade twice without drift.
*Verify:* focused DB migration/operation tests plus `tests/test_tasks.py`.

### C2 — atomic operation artifacts and reconciliation

Write versioned result envelopes with temp/fsync/rename/fsync ordering; add the
seven reconciliation outcomes, rehydration, successor ordinals, and UTA's
agent-core `on_result` port.

*Acceptance:* fault injection at every write boundary never blindly replays an
edited workspace; adopted turns restore all accounting; unsafe guard failure
cannot produce reusable completion.
*Verify:* `tests/test_generation_operations.py` and cycle integration tests.

### C3 — stable workflow application and batch identity

Add context-managed `open_workflow_application`, owner-only state paths,
persisted batch keys, production checkpointer wiring, cancellation pause/resume,
and result validation before outer commit.

*Acceptance:* absent/pending/completed/corrupt behaviors match agent-core;
restart preserves unit membership; no connection/session/sink leaks.
*Verify:* application lifecycle and restart integration tests.

### C4 — clean rerun and retention

Add transactional supersession schema, the confirmed
`clean-rerun-generation` command, ordinary-resume identity preservation, and
startup/six-hour/operator retention.

*Acceptance:* all-missing/mixed/multi-run identities are audited atomically;
malformed keys fail before mutation; known superseded lineages prune only when
eligible.
*Verify:* manager/CLI/retention fault and permission tests.

### C5 — persisted progress and SSE storage port

Add atomic task progress budgets, batch append/cursor reads, terminal-event
transaction, retention, and the UTA event-store adapter consumed later by the
report route.

*Acceptance:* concurrent appenders cannot exceed row/byte caps; exactly one
truncation marker commits; final/operation truth survives delivery failure.
*Verify:* DB concurrency and event-store contract tests.

> **Checkpoint 3.** Production UTA can run the scripted topology with durable
> operation evidence, restart semantics, and bounded persisted progress.

## Phase D — Java decomposition

Sliced by the units the AST pass found. Each is: extract → phase handler →
tests → parity. Each lands independently with the legacy path still present.

| Task | Extracts | Lines |
| --- | --- | --- |
| D1 | `_precheck_existing_tests` (+ class-level, diff-enforcer, delegated-precheck repair) | ~290 |
| D2 | `_compile_test` + `run_compile_fix_loop` → `verify_compile`, `fix_compile_*` | ~185 |
| D3 | `_run_test_selector` + test verification → `verify_tests` | ~35 + inline |
| D4 | `_run_coverage_test_fix_loop`, `_run_mutation_test_fix_loop` → `fix_tests_*` | ~215 |
| D5 | `run_coverage_fix_loop`, `_run_focused_coverage_fix_round` → `measure_coverage`, `fix_coverage_*` | ~215 |
| D6 | `_run_focused_mutation_fix_round` + mutation stage → `measure_mutation`, `fix_mutation_*` | ~140 |
| D7 | `_run_delegated_quality_gate_fix_loop` + helpers → `delegated_quality_gate` | ~250 |
| D8 | the remainder of `generate_and_validate` — planning, generation, per-class test repair, completion | **~1,641 minus the above** |

*Acceptance, each:* the phase runs as a graph node; its evidence and outcome
match the legacy path for the same input; `_create_phase_session` is not called
(sessions are `agent_turn`'s).
*Verify, each:* focused tests + the existing Java generation suite green.

D8 is the largest and must be split further once D1–D7 reveal what remains;
planning it precisely now would be guessing.

> **Checkpoint 4.** Java executes the new graph; full Java parity suite green.

## Phase E — Python

**Not symmetric with Java.** Python has `run_python_batch_request` (433) and
**one** `_run_python_repair_loop` (171) where Java has five loops. So D is
partly construction.

| Task | Work |
| --- | --- |
| E1 | `_verify_generated_test` + `_validate_generated_test_import_contract` → real compile-equivalent evidence, not `skipped` |
| E2 | split `_run_python_repair_loop` into the compile/test/coverage/mutation repair phases the topology declares |
| E3 | `run_python_batch_request` → phase handlers |
| E4 | explicit `skipped` for phases genuinely unsupported, with evidence |

*Acceptance:* Python executes the **same** topology as Java; a phase that is
genuinely unsupported returns `skipped` with a reason, and the compile
equivalent does not.
*Verify:* Python generation suite green; a test asserts both languages compile
the same spec.

> **Checkpoint 5.** Both languages on the new graph; both parity suites green.

## Phase F — cutover and observability

### F1 — persisted default-off engine cutover

Add `generation_cycle_v2_enabled` to the immutable task snapshot and select the
declarative engine only for opted-in tasks. Legacy adapters remain through beta
and production observation.

*Acceptance:* default false; restart cannot switch engines; engine selection is
audited; rollback affects only new tasks.

### F2 — progress/reporting route and tabs

Bind agent-core's task-scoped progress sink; expose the read-only fix-session
SSE route with cursor/backfill/terminal semantics and per-session report tabs.

*Acceptance:* parallel sessions stay in separate timelines; the eleven
existing `GENERATION_PHASES` labels are **unchanged**, with `precheck_existing_tests`
and `delegated_quality_gate` added (11 → 13); reconnect neither loses nor
duplicates an event; the public-safe detail policy is preserved.

### F3 — resume/accounting parity and counters

Emit started/resumed/reused-completed disposition counters and prove result,
session, token, timing, diagnostics, retrospective, and patch-count parity.
*Rationale:* without it, a checkpointer that silently never resumes is
indistinguishable from a working one.

### F4 — compatibility boundary and delayed deletion gate

Add a source-boundary test that describes the eventual deletion set, but keep
`AgentRuntime`, `run_agent_node`, and composite adapters while legacy snapshots
or rollback still require them. Actual deletion is a post-production task,
only after explicit authorization, a green observation window, and zero
non-terminal legacy snapshots.

> **Checkpoint 6.** Both languages use the default-off production path; live
> session-isolated progress and restart/accounting parity are green.

## Phase G — verification and rollout

### G1 — full verification, review, and documentation

Run focused and full suites, compile/import checks, architecture/code review,
and update both READMEs plus the operator guide. Commit and push every finished
slice; verify remote refs.

### G2 — beta replay

Replay a production task on beta; record in `remote-deployment.md`.
*Acceptance:* live session-isolated phase events; deterministic gate results
equivalent to the current path; deliberate restart does not repeat the stable
expensive operation ID.

### G3 — production and legacy deletion

**Requires separate explicit authorization** (spec "ask first"). It is not
authorized by autonomous build continuation. Only after authorization and the
approved observation/drain conditions may the adapters and cutover flag be
deleted.

## Requirement coverage

Every spec success criterion maps to at least one task, and every task traces
back:

| # | Criterion | Tasks |
| --- | --- | --- |
| 1 | cycle compiled and invoked; real route execution observed | B2, B3 |
| 2 | every model phase through `agent_turn` | A3, B2, B3, D*, E*, F4 |
| 3 | agent-core owns sessions/recovery/progress/cleanup | A3, A4 |
| 4 | no production `run_agent_node`/`AgentRuntime.run_node`/concrete lifecycle calls | D*, E*, F4; physical deletion deferred to G3 rollout gate |
| 5 | same topology both languages | B2, D*, E* |
| 6 | repair bounded, returns through verification | B2, D2, D4–D7, E2 |
| 7 | resume preserves completed phases/artifacts | A1, A2, C1–C3, F3 |
| 8 | versioned identity; clean rerun distinct | A1, C3, C4 |
| 9 | progress identifies phase/attempt/session/gate/reason | C5, F2, F3 |
| 10 | public behaviour and result fields compatible | D*, E*, F1, F3 |
| 11 | full suites green, then beta replay | A5, D, E, G1, G2 |
| 12 | reviewable agent-core-first commits and verified refs | phase order, A6, G1 |
| 13 | pending resumes, completed reuses, corrupt fails | A2, C3, C4 |
| 14 | ordered reconnectable SSE and session tabs | C5, F2, G2 |

Design/data/operations coverage: C1/C2 cover the authoritative operation
ledger and artifact durability; C3 covers checkpoint ownership and permissions;
C4 covers clean-rerun/retention; C5/F2 cover budgets, storage and SSE; F1/F4/G3
cover default-off rollback and delayed deletion. No orphan tasks: B5 traces to
the guard-cost finding and F3 to the required production resume signal.

## Risks this plan carries

| Risk | Mitigation |
| --- | --- |
| D8 is unplannable until D1–D7 land | it is explicitly deferred, not estimated; split it before implementation |
| Python is construction, not decomposition | E is sized separately; do not schedule it as "same as Java" |
| A node split silently renames an operator label | F2 pins the existing eleven in a test |
| A concurrent refactor is active in `uta/language/*` | verify the tree is quiescent before D starts |
| Two reviews missed a checkpoint API defect that one probe found | anything asserting an API shape gets a probe before it enters a task |
| The worktree contains unrelated deployment/docs edits | stage explicit task files only; never use broad adds or rewrite those changes |
| Production deletion is irreversible during rollback window | G3 remains blocked on separate authorization, observation, and drain evidence |

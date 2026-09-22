# Spec: Executable Test-Generation Cycle Using agent-core `agent_turn`

Status: approved — requirements resolved on 2026-08-17
Date: 2026-08-17
Work tracking: non-Jira internal tooling work, explicitly approved by the user
Affected repositories: `agent-core`, `unit-test-agent`
Design document: [docs/design-executable-generation-agent-turn.md](design-executable-generation-agent-turn.md)
Usage document: [docs/usage-executable-generation-agent-turn.md](usage-executable-generation-agent-turn.md)

## Pending (Iteration 2 — Shared Prompt Construction, approved 2026-08-18)

### Objective

Complete the reusable-agent boundary by making agent-core the single owner of
prompt rendering and prompt-artifact mechanics used by UTA. UTA continues to
own the test-generation templates, domain values, phase selection, cache
policy, safety validation, and response interpretation; it must not maintain a
second Jinja environment, template loader, or prompt-file writer.

This iteration closes the current partial migration: durable generation phases
already execute agent-core's shared `agent_turn`, while their preceding
`generation_prompt` node still renders through `uta.testgen.prompts.loader`
and writes the prompt file directly. The retained legacy path uses
agent-core's `materialize_prompt` only after UTA has independently rendered the
template.

This is a continuation of non-Jira internal tooling work. It does not authorize
production enablement, deletion of rollback adapters, or a change to
`generation_cycle_v2_enabled` defaults.

### Required behavior

1. agent-core exposes one agent-agnostic prompt-construction contract that:
   - loads product-owned templates from a package or directory;
   - renders with strict undefined-variable checking;
   - supports an optional stable/volatile cache boundary without knowing UTA;
   - lets a consumer select trailing-newline behavior explicitly, so migration
     compatibility does not require trimming rendered text;
   - returns the stable and volatile rendered sections as well as their exact
     concatenation;
   - materializes the prompt and a deterministic, JSON-readable metadata
     artifact;
   - keeps prompt filenames, metadata filenames, and destination directories
     caller-configurable.
2. UTA supplies its template source, domain values, defaults, cache-boundary
   marker, and domain validation to that contract. Java/Python modules may
   choose templates and construct values, but must not instantiate Jinja or
   write prompt files themselves.
3. Durable `generation_prompt` returns both `prompt_file` and
   `prompt_inputs_file`; the shared `agent_turn` remains unchanged and consumes
   the materialized prompt file.
   Both files live under the owner-only workflow-state root outside the target
   repository and share its 30-day lifecycle.
4. The retained legacy path uses the same agent-core rendering contract, so
   legacy and durable execution cannot drift in undefined-variable behavior,
   section splitting, or rendered text.
5. Existing prompt text is byte-compatible for every currently supported Java
   and Python prompt fixture. A template that references an actually missing
   value now fails before opening an agent session rather than silently
   rendering an empty string; UTA must provide explicit defaults where an empty
   value is legitimate.
6. Prompt metadata is deterministic and limited to an explicit UTA-owned safe
   projection. It must not accidentally serialize credentials, environment
   mappings, live objects, raw provider data, or arbitrary workflow state.
   agent-core's strict metadata mode rejects non-JSON-safe values and payloads
   over the caller's byte limit rather than coercing them with `str()`.
7. UTA production prompt-construction modules contain no direct `jinja2`
   import and no direct prompt-file `write_text` call after cutover. Template
   files and domain-specific validation remain in UTA.
8. `CycleState` declares both artifact paths explicitly, and checkpoint
   round-trip/restart tests prove that neither field is discarded.
9. Legacy `AgentRuntime` and durable `generation_prompt` use one UTA-owned safe
   metadata projection and the same out-of-repository artifact root. Raw
   feedback, provider failure content, prompt values, and recovery reasons are
   excluded from both paths.
10. UTA resolves prompt storage through one application-owned resolver. Managed
    tasks and standalone Java/Python commands both receive an invocation-stable
    run identity and owner-only storage beneath UTA's application-state root;
    neither mode requires the target repository to contain a task database.
11. The resolver compares symlink-resolved paths and rejects any prompt root
    equal to or below the target Git repository before a session opens.
12. Because legacy prompt construction also changes, validation includes an
    explicit legacy canary and rollback is the previous UTA build/dependency
    pin—not the durable-v2 feature flag.

### Commands

```text
# agent-core focused and full verification
cd /home/user/saas/agent-core
.venv/bin/python -m pytest tests/test_prompts.py tests/test_workflow_nodes.py -q
.venv/bin/python -m pytest -q

# cr_plugin clean consumer verification (after installing candidate SHA)
cd /home/user/saas/cr_plugin
.venv-agent-core-candidate/bin/python -m pytest -q

# UTA focused and full verification
cd /path/to/unit-test-agent
.venv312/bin/python -m pytest tests/test_prompt_render.py tests/test_prompt_prefix_stable.py tests/test_generation_cycle_build.py -q
PYTHONDONTWRITEBYTECODE=1 .venv312/bin/python -m pytest -q
```

The implementation must also run import/compile checks for both repositories
without compiling the Python-2 fixture tree as Python 3.

### Project structure

- `agent-core/src/agent_core/prompts.py` owns template loading, strict
  rendering, stable/volatile section mechanics, and prompt artifacts.
- `agent-core/src/agent_core/workflow/nodes.py` continues to own the generic
  `render_prompt` and `agent_turn` workflow nodes.
- `unit-test-agent/uta/testgen/prompts/` owns UTA templates, domain defaults,
  and domain safety validation through a thin agent-core adapter.
- `unit-test-agent/uta/testgen/graph/cycle.py` orchestrates the domain backend
  and shared prompt artifact; it does not implement rendering or file I/O.
- Existing Java/Python phase modules continue to select templates and build
  domain values only.

### Code style

Cross-repository interfaces remain typed, neutral, and data-oriented:

```python
rendered = prompts.render_sections(template_name, values=domain_values)
artifact = prompts.materialize(
    rendered,
    directory=turn_directory,
    metadata=safe_prompt_metadata,
)
return {
    "prompt_file": str(artifact.path),
    "prompt_inputs_file": str(artifact.inputs_path),
}
```

The concrete API name is a design decision. Product names, language branches,
task databases, and UTA-specific cache markers must not appear in agent-core.

### Testing strategy

- agent-core contract tests cover strict undefined variables, absent/present
  section markers, byte-preserving concatenation, package/directory sources,
  both trailing-newline policies, deterministic strict metadata (including
  rejection of unsupported objects/non-finite floats/oversized data), and
  artifact filenames.
- UTA parity tests parameterize every production template and assert the shared
  renderer produces the exact previous stable section, volatile section, and
  full prompt for representative Java and Python values. Frozen pre-migration
  fixtures cover every template, both language composers, optional/default
  branches, stable/no-boundary templates, and Unicode.
- graph tests assert `generation_prompt` produces both artifact paths and that
  no harness/session opens when rendering fails. Checkpoint round-trip and
  restart tests prove `prompt_inputs_file` survives.
- a real Git integration test uses a target repository that does not ignore
  `.uta_cache`, runs delivery, and proves neither legacy nor durable prompt
  artifacts appear in `git diff --cached`.
- source-boundary tests scan every production agent-prompt path under
  `uta/testgen` and `uta/language`, preventing direct Jinja construction and
  prompt-artifact writes. HTML report rendering is the only narrow allowlist.
- standalone Java and Python CLI tests run without `task_id` or `task_db_path`,
  create an application-state run identity, open a model session, and clean up
  their prompt scope. A configured task DB inside the target repository still
  produces artifacts outside it.
- both full repository suites must pass before either consumer commit is
  pushed; agent-core is committed and pushed before UTA.

### Boundaries

Always:

- preserve rendered prompt content and existing domain validation;
- keep templates and domain defaults in UTA;
- use agent-core's released public API from UTA, not an editable path;
- keep prompt artifacts outside the target repository and owner-only;
- preserve unrelated worktree changes in both repositories.

Ask first:

- change template wording, phase prompts, cache semantics, or model policy;
- add a dependency, change artifact retention, or expose prompt inputs in a
  public report;
- enable durable v2 outside the already approved beta scope.

Never:

- move Java/Python test-generation policy into agent-core;
- store credentials, environment mappings, or live objects in prompt metadata;
- silently fall back to permissive undefined-variable rendering;
- delete the legacy rollback path or enable production as part of this
  iteration.

### Success criteria

1. Durable and legacy UTA prompt construction both use agent-core's shared
   rendering/materialization API.
2. Every existing supported prompt fixture renders byte-for-byte identically,
   including stable/volatile boundaries.
3. A missing template variable fails before an agent session is created.
4. Durable checkpoint state contains `prompt_file` and `prompt_inputs_file`,
   but no renderer, template object, or unrestricted values mapping.
5. UTA prompt-construction production code has no direct Jinja environment and
   does not directly write prompt artifacts.
6. Agent-core remains product-, language-, and harness-agnostic.
7. Focused and full suites pass in both repositories, with agent-core released
   and pushed before the UTA consumer update.
8. The feature flag remains persisted and default-off; no production rollout
   or legacy deletion occurs.
9. Delivery cannot stage prompt artifacts even when the target repository has
   no `.uta_cache` ignore rule, and the workflow retention owner deletes those
   artifacts after the configured 30-day period.
10. Default legacy and durable-v2 canaries both match frozen prompt hashes and
    safe artifact contracts before rollout; reverting the UTA build restores
    the pre-migration prompt path independently of the v2 flag.

### Open questions

There are no requirement-level open questions. The design must choose the
smallest public agent-core API that supports rendered sections and artifacts,
define the safe metadata projection, and provide a compatibility migration for
UTA's current permissive templates without changing their output.

## Objective

Make UTA's detailed test-generation lifecycle an actually executed declarative
workflow and make agent-core's shared `agent_turn` workflow node the only path
used by that lifecycle for model-driven work.

Today, `generation-cycle.yaml` describes plan, generation, verification, and
repair topology, but the outer graph delegates execution to a large composite
backend function. UTA also wraps agent-core's lower-level harness APIs in its
own `AgentRuntime` and `run_agent_node` lifecycle. This leaves the declared
workflow unable to control or resume individual generation phases and
duplicates generic session/recovery behavior outside agent-core.

The target users are:

- UTA maintainers, who need small, independently testable workflow nodes;
- language-backend authors, who should implement domain policy without owning
  an agent lifecycle;
- operators, who need accurate live phase, session, repair-attempt, and failure
  progress;
- other agent-core consumers, which may reuse the enhanced `agent_turn`
  capability without depending on UTA.

## Required Behavior

### 1. Executable generation workflow

`uta/testgen/graph/generation-cycle.yaml` must be built and invoked as a nested
workflow for each selected target batch. Its declared nodes and conditional
routes must determine execution; publishing the phase list as metadata is not
sufficient.

The lifecycle must represent these human-meaningful phases:

1. plan tests;
2. generate tests;
3. verify and, when allowed, repair compilation or equivalent loadability;
4. verify and, when allowed, repair test execution;
5. measure and, when allowed, repair coverage;
6. measure and, when allowed, repair mutation strength;
7. complete the target result and return to outer delivery/target iteration.

Repair edits must return through deterministic verification before their gate
is re-evaluated. Every retry loop must be bounded and represented by a declared
workflow route.

### 2. Shared `agent_turn` execution

Every model-driven phase in the generation cycle must execute through
agent-core's registered `agent_turn` workflow node. This includes planning,
initial generation, compile repair, test repair, coverage repair, mutation
repair, and any delegated quality-gate repair.

The shared capability must support, without naming an agent implementation:

- configured harness selection;
- reusable phase sessions and explicit session scopes;
- prompt artifacts and recovery-prompt artifacts;
- model and timeout policy supplied by workflow state/configuration;
- cancellation checks;
- progress streaming;
- bounded stalled-turn recovery in the same session;
- generic before/after turn guards;
- normalized completion, timeout, cancellation, provider/rate-limit, usage,
  session, patch-count, and retrospective information;
- reliable session cleanup after success, failure, cancellation, or graph
  interruption.

OpenCode, Pi, and future harness-specific event/session formats must remain
inside their agent-core adapters. Agent-core also owns the neutral JSON-safe
progress envelope and redaction/detail policy; UTA persists and renders that
contract without parsing provider logs or inventing a second filter.

### 3. Product and language boundaries

agent-core owns reusable agent workflow mechanics. UTA owns test-generation
policy. Language packages own language mechanics.

- `agent-core.workflow` owns the reusable `agent_turn` node contract.
- `agent-core.harness` owns configured harnesses, sessions, turn execution,
  recovery, and neutral diagnostics.
- `uta.testgen` owns the generation topology, neutral phase state, routing
  outcomes, budgets, workspace policy integration, progress projection, and
  result aggregation.
- `uta.language.java` owns Java prompts, Maven compilation, JUnit/Surefire,
  JaCoCo, PIT, Java artifacts, and Java result interpretation.
- `uta.language.python` owns Python prompts, syntax/import validation, pytest,
  coverage.py, mutmut, Python artifacts, and Python result interpretation.

An unsupported language phase must return an explicit neutral `skipped`
outcome. Neutral graph code must not branch on a language name.

### 4. Compatibility and migration

Existing batch and task entrypoints must keep their external behavior while the
internal workflow changes. Existing result fields—including target status,
session IDs, phase token usage, timings, test paths, coverage, mutation data,
and provider-limit details—must remain available unless a later approved design
defines a migration.

Compatibility adapters may exist during vertical migration, but final
production workflow code must not call:

- `uta.testgen.llm_session.run_agent_node`;
- `AgentRuntime.run_node`;
- direct create/send/poll/delete agent lifecycle methods.

Legacy adapters must be removed only after both Java and Python execute the
new graph and parity tests pass.

### 5. Resume, observability, and failure semantics

The nested workflow must preserve enough neutral state to identify the current
target, phase, attempt, session scope, prompt artifact, deterministic evidence,
and terminal reason. A resumed task must not repeat a completed expensive phase
unless its required artifact/evidence is missing or invalid.

Live progress and stored reports must expose:

- current generation phase;
- current repair attempt and bounded maximum;
- per-session agent progress/events;
- deterministic gate outcome and evidence summary;
- phase token usage and timing;
- cancellation, timeout, provider-limit, and terminal failure reason.

Parallel sessions must remain isolated in report tabs/streams; events from
different sessions must not be merged into one undifferentiated timeline.

Workflow execution checkpoints must use agent-core's shared SQLite-backed
persistence capability. Product code configures persistence and workflow-run
identity but must not instantiate or depend directly on LangGraph saver
classes. The LangGraph-specific implementation stays behind agent-core's
workflow API so a later CR migration can consume the same contract.

Checkpoint storage must be separate from the product database:

```text
<state>/uta_tasks.db                         # product truth
<state>/workflow-state/checkpoints.sqlite    # LangGraph checkpoints
<state>/workflow-state/results/...           # normalized operation artifacts
```

The product database remains authoritative for task status, attempts,
artifacts, business results, and user-visible progress. The checkpointer owns
only serializable graph state, the next executable node, interrupts, and nested
workflow position. Rebuilding a graph after process restart must resume it
with a stable, versioned identity equivalent to:

```text
thread_id = uta:generation-cycle:v1:{task_id}:{unit_id}:{workflow_run_id}
```

A retry of the same workflow run reuses its identity; an explicit clean rerun
creates a new workflow-run ID. An incompatible topology change must use a new
version in the thread identity. `checkpoint_ns` is reserved by LangGraph for
subgraph addressing and is not a product namespace. Checkpointing does not
make filesystem, Git, compiler,
test, mutation, or model side effects transactional, so phase handlers must
retain stable operation IDs and idempotency checks. UTA must persist those
operations in a product-owned evidence ledger with a unique operation ID;
`task_events` remains a derived presentation stream, not the authoritative
exactly-once record.

Starting and resuming a graph are distinct operations. The agent-core API must
inspect the stored snapshot: an absent lineage starts with initial state, a
pending lineage resumes with no new input, and a completed lineage returns its
stored terminal state without executing a node. A corrupt lineage fails
explicitly and must never silently restart.

UTA must expose stored progress as a read-only SSE stream under the existing
report surface. It must support cursor-based backfill and reconnect,
heartbeats while a task is active, terminal close, and isolation of concurrent
agent sessions into separate report tabs. Task visibility must match the
existing report-detail route.

### 6. Session and phase policy

Each model-driven phase and each deliberate repair attempt starts a fresh
agent session. This keeps phase accounting and parallel progress timelines
isolated and prevents unrelated phase context from leaking into later repairs.
Only bounded stalled-turn recovery may reuse the current session, because it
is recovery of the same logical turn rather than a new phase or repair attempt.

Python's compile-equivalent phase performs syntax and import/loadability
validation. It must return neutral compile evidence through the same graph
contract as Java compilation rather than reporting the phase as unconditionally
`skipped`. A language backend may still return an explicit `skipped` result
when that validation is genuinely unsupported for the target.

## Scope Discovery

### In scope

| Repository/module | Current responsibility | Decision and reason |
| --- | --- | --- |
| `agent-core/src/agent_core/workflow/nodes.py` | Shared `agent_turn` and `render_prompt` nodes | In scope: `agent_turn` is the required shared execution boundary. |
| `agent-core/src/agent_core/harness/` | Harness registry, reusable sessions, `run_harness_node`, recovery, diagnostics | In scope: it already owns most required mechanics; gaps must be closed here rather than in UTA. |
| `agent-core/src/agent_core/workflow/graph.py` and registry/spec APIs | Declarative graph construction and shared/product registry composition | In scope: the nested cycle must be built from the shared workflow APIs. |
| New agent-core workflow persistence adapter/API | SQLite checkpointer lifecycle, run identity, resume/restart semantics, and cleanup | In scope: products configure persistence without importing LangGraph saver implementations. |
| `agent-core/tests/test_workflow_nodes.py`, harness node/session tests | Shared API contract coverage | In scope: compatibility and new session/recovery behavior require contract tests. |
| `uta/testgen/graph/generation-cycle.yaml` | Detailed lifecycle topology | In scope: it must become executable. |
| `uta/testgen/graph/{generation_cycle,nodes,state,workflow}.py` | Current metadata publication and outer graph | In scope: invoke the nested graph and carry neutral state. |
| `uta/testgen/{agent_runtime,llm_session}.py` | UTA-owned session and guarded turn wrappers | In scope for migration and eventual removal/reduction. |
| `uta/language/java/{generation,generation_backend}.py` | Composite Java planning/generation/repair workflow | In scope: split into executable phase handlers without moving Java mechanics into neutral code. |
| `uta/language/python/{generation,generation_backend}.py` | Composite Python generation/repair workflow | In scope: implement the same neutral lifecycle and explicit skipped phases. |
| `uta/testgen/{progress,session_analysis}.py`, task/report consumers | Phase/session observability and accounting | In scope: preserve and expose nested workflow progress. |
| `uta/tasks/db.py` and migrations | Product-owned workflow-operation evidence ledger and indexed event cursor reads | In scope: required for safe replay and reconnectable progress. |
| `uta/app/routes.py` and report renderer | Read-only SSE endpoint and session-isolated live report tabs | In scope: required by live progress acceptance. |
| Graph, runtime, Java/Python workflow, report, and scripted/real E2E tests | Behavioral verification | In scope: prove parity and actual node execution. |
| `README.md` in both repositories and UTA operator usage docs | Public architecture and rollout guidance | In scope if interfaces or operator-visible progress change. |

### Out of scope

| Candidate | Reason excluded |
| --- | --- |
| Migrating `cr_plugin`, Corbell, or other agent-core consumers to the enhanced node/checkpointer | The shared API must be suitable for a later CR migration, but changing CR's current whole-task replay behavior is a separate follow-up. |
| Changing Maven, JaCoCo, PIT, pytest, coverage.py, or mutmut gate semantics | This migration changes orchestration, not quality policy. |
| Adding a new harness implementation | The result must support future harnesses, but implementing Pi or another adapter is separate. |
| Changing trigger write APIs, Git delivery, or authentication | The read-only event endpoint follows existing report visibility; trigger and authentication semantics remain unchanged. |
| Production deployment | Beta replay is required evidence; production rollout requires a separate explicit authorization. |
| Refactoring unrelated application, enforcement, or reporting code | Scope is limited to what is necessary for workflow execution and phase observability. |

No production-traffic query is required: this work changes internal workflow
execution and shared library contracts, not a user-facing endpoint or business
data contract.

## Tech Stack

- Python 3.11 or newer.
- agent-core 0.5.x shared harness/workflow APIs.
- LangGraph 1.x through agent-core's optional `langgraph` capability.
- `langgraph-checkpoint-sqlite` through the same optional capability, wrapped by
  agent-core rather than imported by product workflow code.
- YAML workflow specifications through agent-core's optional `yaml` capability.
- pytest 8.x for unit, integration, and scripted E2E verification.
- UTA language tooling remains Maven/JUnit/JaCoCo/PIT for Java and
  pytest/coverage.py/mutmut for Python.

The enhanced contract requires an agent-core minor version bump and release.
UTA must consume that released version before relying on the new contract.
Temporary local editable dependency wiring may be used only for development
verification and must not be the deployed dependency declaration.

## Commands

Run from the relevant repository root.

### agent-core

```bash
python3 -m pip install -e '.[dev,yaml,langgraph]'
python3 -m pytest -q
python3 -m compileall -q src tests
python3 -m build
```

### unit-test-agent

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
python3 -m compileall -q uta tests
```

Focused test commands and the beta replay command will be fixed in the design
and implementation plan once phase-handler locations are approved.

## Project Structure

```text
agent-core/
  src/agent_core/workflow/   shared workflow nodes, specs, registries, builder,
                             and backend-hidden checkpoint persistence API
  src/agent_core/harness/    agent-neutral turns, sessions, recovery, diagnostics
  tests/                     shared API and workflow contract tests

unit-test-agent/
  uta/testgen/graph/         outer and nested declarative workflows
  uta/testgen/               neutral generation policy, progress, accounting
  uta/language/java/         Java prompts, artifacts, verification, interpretation
  uta/language/python/       Python prompts, artifacts, verification, interpretation
  uta/tasks/, uta/reporting/    persisted progress and user-facing reports
  tests/                     unit, contract, scripted E2E, staged/real E2E tests
  docs/                      non-Jira spec, design, plan, usage, and ADR documents
```

## Code Style

Shared workflow nodes remain small functions that consume neutral state,
configuration, and runtime context. Domain mechanics stay behind a backend
operation rather than appearing as language branches:

```python
def verify_compile(state, config, context):
    backend = context["generation_backend"]
    evidence = backend.verify_compile(state)
    return {
        "compile_evidence": evidence,
        "compile_outcome": evidence.outcome,
    }
```

Conventions:

- use descriptive phase/outcome names rather than provider terminology;
- return graph-state updates instead of mutating unrelated global state;
- use typed protocols/dataclasses for cross-repository public contracts;
- preserve thin registry/export modules;
- keep compatibility shims explicit, documented, tested, and temporary;
- do not duplicate agent-core session, recovery, or fallback mechanics in UTA.

## Testing Strategy

### agent-core contract tests

- `agent_turn` success, failure-as-state, cancellation, configured output key;
- reusable session selection and isolation;
- dynamic model/timeout resolution;
- stalled-turn recovery with a recovery prompt;
- before/after guard behavior, including exceptions;
- progress callback forwarding;
- rate-limit/fallback and neutral diagnostic projection;
- deterministic cleanup and session snapshots;
- SQLite checkpoint creation, absent start, pending resume without fresh input,
  completed-state reuse, corrupt-lineage failure, clean restart,
  namespace/version isolation, permissions, connection cleanup, and retention;
- nested workflow resume after process/runner reconstruction;
- product-facing APIs do not require importing LangGraph saver classes.

### UTA workflow tests

- the nested YAML is built and invoked, not merely loaded as metadata;
- selectors cover every declared outcome;
- repair loops route back through deterministic verification and are bounded;
- stop/resume behavior does not repeat valid completed phases;
- restart reconstructs the nested graph from its stable run identity and
  resumes from the persisted node rather than replaying the full task;
- Java and Python use the same neutral graph topology;
- unsupported phases are explicit `skipped` results;
- source-boundary tests prohibit legacy agent execution APIs after cutover.
- fault-injection across artifact, operation-ledger, event and checkpoint write
  boundaries exercises every reconciliation outcome;
- SSE route tests cover cursor backfill, `Last-Event-ID`, heartbeat,
  disconnect, terminal close, task isolation and per-session tabs.

### Behavioral parity and E2E tests

- existing Java and Python generation suites remain green;
- scripted E2E covers success and every repair branch;
- full clean-worktree test suites pass in both repositories;
- a production task replayed on beta shows live, session-isolated phase events
  and produces equivalent deterministic quality results.

No coverage percentage target is introduced by this refactor. Changed behavior
must have direct tests, and no existing failing test may be removed or weakened
to obtain a green build.

## Boundaries

### Always do

- update and push agent-core before updating the UTA consumer;
- release a minor agent-core version before declaring the UTA consumer ready;
- preserve agent-agnostic public interfaces;
- keep generation topology language-neutral;
- run focused tests after each vertical slice and full suites before push;
- preserve session, token, timing, failure, and deterministic evidence fields;
- keep workflow/spec/design/plan documentation synchronized with scope changes;
- preserve unrelated worktree edits and generated reports.

### Ask first

- change an externally consumed agent-core public API incompatibly;
- add a dependency or modify CI/deployment configuration;
- make task-schema or public API changes beyond the approved
  `workflow_operations` ledger and read-only report SSE endpoint;
- change quality-gate semantics, retry budgets, or default model policy;
- expand migration to another product repository;
- deploy to production.

### Never do

- introduce OpenCode/Pi conditionals into UTA workflow code;
- import or instantiate LangGraph checkpointer implementations from UTA;
- implement a second reusable-session or recovery framework in UTA;
- represent the detailed cycle as metadata while executing an opaque composite;
- merge events from parallel sessions into one timeline;
- silently restart an expensive completed phase during resume;
- delete compatibility code before both language paths pass parity tests;
- overwrite unrelated local changes, secrets, or runtime artifacts.

## Success Criteria

1. `generation-cycle.yaml` is compiled and invoked for every generated target
   batch, and tests observe real execution of its declared phase routes.
2. Every model-driven cycle phase runs through agent-core's registered
   `agent_turn` node.
3. agent-core owns reusable sessions, stall recovery, progress forwarding,
   neutral diagnostics, and cleanup used by those turns.
4. UTA production workflow code contains no call to `run_agent_node`,
   `AgentRuntime.run_node`, or concrete create/send/poll/delete lifecycle APIs.
5. Java and Python execute the same neutral topology; language-specific tools
   and prompts remain inside their language packages.
6. Compile/test/coverage/mutation repair paths are bounded and always return
   through deterministic verification.
7. Stop/resume preserves completed phase artifacts and does not unnecessarily
   repeat completed expensive model or mutation phases.
8. Restarting a worker with the same versioned workflow-run identity resumes
   from agent-core's SQLite checkpoint, while a clean rerun or incompatible
   topology version starts from a distinct checkpoint lineage.
9. Live and persisted progress identifies phase, attempt, session, gate
   outcome, timing, token usage, and terminal reason without cross-session
   event mixing.
10. Existing public batch/task behavior and result fields remain compatible.
11. Full clean-worktree suites pass in agent-core and UTA, followed by a green
    beta replay of a production task.
12. The migration is delivered as reviewable agent-core-first commits with
    verified remote refs and no unrelated worktree changes included.
13. Pending workflow state resumes without replay; completed state returns
    without re-entry; corrupt state fails explicitly.
14. The report receives ordered, reconnectable live events over SSE and keeps
    concurrent session events in separate tabs.

## Resolved Decisions

1. A new agent session is created for every model-driven phase and deliberate
   repair attempt. Only bounded stalled-turn recovery reuses the current
   session.
2. agent-core provides a backend-hidden SQLite checkpointer capability. UTA
   stores checkpoints in a separate SQLite file, retains its product database
   as the source of truth, and resumes with stable versioned workflow-run
   identities.
3. Python's compile-equivalent phase performs syntax and import/loadability
   validation and returns neutral compile evidence.
4. The exact production task for beta replay is selected immediately before
   final acceptance, based on availability of a representative task that
   exercises generation and at least one repair route. This is an acceptance
   input, not a design blocker.
5. agent-core receives a minor version bump and release; UTA consumes that
   released version rather than an unreleased branch revision.
6. UTA adds a product-owned `workflow_operations` evidence ledger and a
   read-only report SSE endpoint; both are additive migrations.

There are no unresolved requirements blocking design generation.

## Changelog

- 2026-08-18 — Iteration 2 second-review correction — preserve standalone
  Java/Python execution through application-state run identities, enforce the
  resolved Git boundary, and require legacy-canary/previous-build rollback.
- 2026-08-18 — Iteration 2 review correction — require native newline
  compatibility, owner-only out-of-repository prompt storage, explicit cycle
  state, strict metadata, shared legacy metadata, complete source boundaries,
  retention, and executable cross-repository verification.
- 2026-08-18 — Iteration 2 pending — require UTA legacy and durable prompt
  construction to converge on agent-core while preserving domain ownership and
  byte-compatible prompt output; affected sections: prompt construction,
  testing, repository boundaries, rollout.
- 2026-08-17 — Iteration 1 — initial executable generation-cycle specification
  approved and implemented through the default-off rollout boundary.

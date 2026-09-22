# Detail Design — agent-core

Status: approved — Iteration 2 frozen on 2026-08-18
Overview: [design-executable-generation-agent-turn.md](design-executable-generation-agent-turn.md)
Spec: [spec-executable-generation-agent-turn.md](spec-executable-generation-agent-turn.md)

## Approved (Iteration 2 — Shared Prompt Construction)

### Changes in this repo

`agent_core.prompts` gains one additive value type and one additive method;
existing callers remain source- and behavior-compatible:

```python
@dataclass(frozen=True)
class RenderedPrompt:
    stable: str
    volatile: str

    @property
    def text(self) -> str:
        return f"{self.stable}{self.volatile}"


class PromptLibrary:
    def __init__(
        self,
        source: str | Path,
        *,
        cache_source_text: bool = False,
        ...,
    ) -> None: ...

    def render_sections(
        self,
        name: str,
        *,
        boundary: str,
        keep_trailing_newline: bool | None = None,
        values: Optional[Mapping[str, Any]] = None,
        **kwargs: Any,
    ) -> RenderedPrompt: ...
```

The method loads raw template text through the existing package/directory
source resolver. If `boundary` is present, the first occurrence divides stable
and volatile template text; if absent, stable is empty and the entire template
is volatile. Each section renders through the library's existing
`StrictUndefined` environment. `keep_trailing_newline=None` inherits the
library's current `True` policy; an explicit boolean selects Jinja's native
policy on a per-call `Environment.overlay`. The method never mutates the shared
library environment, so concurrent callers cannot leak policy. UTA passes
`False`, exactly matching current
`Template(...)` behavior. `RenderedPrompt.text` is exact concatenation; it
performs no trimming or other whitespace normalization. A syntax error caused
by splitting an unclosed Jinja block is re-raised with template and boundary
context.

The existing `render`, `render_to_file`, and module-level
`materialize_prompt` defaults and behavior do not change. The materializer
gains additive keyword-only controls: `strict_metadata=False`,
`metadata_max_bytes=None`, and `file_mode=None`. Strict mode accepts only
recursively JSON-safe values with string mapping keys and finite numbers,
serializes with deterministic key ordering and no `default=str`, and rejects
an oversized payload. UTA selects strict mode, 4 KiB, and `0o600`.
`RenderedPrompt` is exported from `agent_core.prompts`; it is not coupled to
workflow, harnesses, caching providers, or UTA.

### Data and control flow

```text
source/name -> optionally cached _template_text -> raw partition(boundary, first occurrence)
            -> strict render stable -> strict render volatile
            -> RenderedPrompt(stable, volatile) -> consumer composition
            -> existing materialize_prompt(text, safe metadata)
```

An empty boundary is rejected with `ValueError`; accepting it would match every
template at position zero and make a configuration typo indistinguishable from
an intentional all-volatile prompt. Missing templates preserve
`TemplateNotFound`; missing values preserve `UndefinedError`. No fallback
environment exists.

### API compatibility and tradeoffs

This is a minor, additive public API and releases as **0.6.0**. The design does
not overload `render()` with a tuple return and does not change
`render_to_file`, because either would break current CR/UTA callers. It also
does not attach provider cache policy to section labels. Source-text caching is
a separate, generic constructor option: `False` remains the default, while UTA
selects `True` to preserve its current process-lifetime `_read_prompt` behavior
without retaining a product-side loader.

Raw partitioning is chosen over a custom Jinja extension. A custom extension
would add parser machinery for a single delimiter, while raw partitioning
matches UTA's established semantics and keeps the marker caller-owned.

### Capacity, reliability, and security

There are no network, database, subprocess, or asynchronous calls. One call
per model turn reads one small template and compiles/renders at most two Jinja
fragments. Source caching is opt-in and bounded by the finite template-name
set. It uses the standard thread-safe `lru_cache` wrapper rather than a mutable
product cache. A focused benchmark records uncached/cached read and two-section render
latency for representative 4 KiB, 64 KiB, and largest UTA templates; no RPC or
DB cost exists and workflow retry limits bound calls.

`RenderedPrompt` contains prompt text and must never be placed in logs by
agent-core. Strict materialization rejects symlink targets, writes temporary
files at the requested mode, replaces each destination, and returns only after
both final files exist. A crash can leave a partial pair, but consumers do not
publish state until return and safely overwrite that no-effect operation on
retry. No template values are serialized inside `render_sections`.

### Failure modes and verification

Contract tests cover boundary present/absent/first-only, boundary at either
edge, strict missing variables in either section, both native trailing-newline
policies, split-block diagnostics, byte-exact concatenation, Unicode,
package/directory sources, concurrent mixed-policy calls, opt-in cache
behavior, and unchanged old method
snapshots. Materialization tests cover strict recursive types, string keys,
finite floats, deterministic bytes, size limit, symlink rejection, `0600`
mode, partial-write recovery, and preservation of legacy defaults.

The full agent-core suite runs before tag `v0.6.0`. Before merge, a clean CR
environment installs the candidate commit explicitly and runs:

```text
cd /home/user/saas/cr_plugin
python -m venv .venv-agent-core-candidate
.venv-agent-core-candidate/bin/pip install -e '.[dev]'
.venv-agent-core-candidate/bin/pip install --force-reinstall \
  'agent-core[api,langgraph,yaml] @ git+https://git.example.com/example-org/agent-core.git@<candidate-sha>'
.venv-agent-core-candidate/bin/python -m pytest -q
```

The installed direct-URL commit is recorded. After the suite passes,
agent-core is tagged/pushed as `v0.6.0`; UTA pins that released tag in a fresh
environment and verifies the installed direct URL contains `v0.6.0`, never an
editable path or temporary snapshot.

> **Revision 6** orders durability after guard acceptance and cleanup, defines
> the progress sink/flush contract, and pins the full legacy behavior matrix.
> **Revision 5** defines the normalized turn DTO and the only safe
> start/resume/completed invocation algorithm, preserves the config-free node
> contract, removes cross-phase session reuse, and makes checkpoint file
> permissions explicit. **Revision 4** removes the unneeded subgraph registry after the UTA detail
> demonstrated that the existing `build_graph` API already supports the
> separately compiled child proven by the spike. It also makes invocation
> config and checkpoint deletion agent-core-owned APIs. **Revision 3** applied the
> [spike](spikes/spike-executable-generation-agent-turn.md): `on_failure` takes
> only `fail`/`skip`, `run_harness_node` already accepts a session, the prompt
> keeps coming from `state["prompt_file"]`, and `recursion_limit` goes through
> `with_config` — which keeps the resume API, contrary to review. Revision 2
> corrected revision 1's rebuild of machinery `run_harness_node` already owns.

## Contents

1. [What already exists](#what-already-exists)
2. [Changes in this repo](#changes-in-this-repo)
3. [`agent_turn`](#agent_turn)
4. [Checkpoint persistence](#checkpoint-persistence)
5. [Safe workflow invocation](#safe-workflow-invocation)
6. [Nested workflow support](#nested-workflow-support)
7. [Guards](#guards)
8. [Control flow](#control-flow)
9. [Public API](#public-api)
10. [Tradeoffs](#tradeoffs)
11. [Capacity, reliability, security](#capacity-reliability-security)
12. [Failure modes](#failure-modes)

## What already exists

This matters because revision 1 got it wrong and designed a rebuild.

`harness/node.py::run_harness_node` **already owns** the turn lifecycle:
attempts, accept/parse, recorder bookkeeping, `on_failure` semantics, and
bounded in-session recovery — it takes `recover`, `recoverable` and
`recovery_prompt` directly (`node.py:135-137`). UTA already calls it
(`uta/testgen/agent_runtime.py:104`).

So UTA does **not** own a duplicate turn lifecycle. What UTA owns is product
policy: where a prompt artifact lives, which continue-prompt belongs to which
phase (`llm_session.py:24-59`), and the workspace/budget guard. Two of those
three are correctly UTA's and stay there.

`session_recovery()` from `harness/recovery.py` is **not** usable from a
session: it needs a client exposing `send_message`/`poll_completion`
(`recovery.py:85-89`), and `HarnessSession` exposes only
`run_turn`/`session_id`/`snapshot`/`close`. `run_harness_node(recovery_prompt=…)`
is the mechanism that works, and it is the one we use.

## Changes in this repo

| Module | Change | Size |
| --- | --- | --- |
| `workflow/nodes.py` | `agent_turn` delegates to `run_harness_node`, opening a session per configured scope | moderate |
| `workflow/checkpoints.py` | **new** — `open_checkpointer`, neutral saver-compatible wrapper, `WorkflowRunIdentity.invoke_config`, lineage deletion | moderate |
| `workflow/execution.py` | **new** — provider-owned start/resume/completed inspection and invocation | small |
| `runtime/progress.py` | **new extraction** — JSON-safe progress envelope and shared summary/redacted-detail projection, reused by `runtime/sse.py` and workflow nodes | small |
| `harness/turns.py`, `harness/node.py` | add compatible `recovered: bool = False` to `TurnLoop`/`NodeOutcome`; set it only when an in-session recovery result is accepted | small |
| `pyproject.toml` | version **0.5.0** | trivial |

`langgraph-checkpoint-sqlite` needs no change: it is already declared under
both `dev` and the `langgraph` extra (`pyproject.toml:20,23`). Revision 1
claimed otherwise in a table headed "measured" and had not measured it.

## `agent_turn`

### Today

```python
result = runner.run_turn(prompt_file=…, repo_path=…, model_id=…, timeout_seconds=…, is_cancelled=…)
return {output_key: result, "turn_status": …, "turn_text": …}
```

One turn. No attempts, no session, no recovery.

### Target

`agent_turn` becomes a thin adapter between graph state and
`run_harness_node`. It does **not** reimplement attempts or recovery.

```python
@dataclass(frozen=True)
class AgentTurnResult:
    status: Literal[
        "completed", "skipped", "failed", "cancelled", "timed_out",
        "rate_limited", "stalled",
    ]
    text: str
    session_id: Optional[str]
    usage: Mapping[str, int | float]
    retrospective: Mapping[str, JSONValue]
    patch_count: int
    recovered: bool
    attempts: int
    elapsed_seconds: float
    diagnostics: Mapping[str, JSONValue]
    raw_log_path: Optional[str]

@dataclass(frozen=True)
class AgentProgressEvent:
    sequence: int
    session_id: Optional[str]
    phase: str
    kind: str
    summary: str
    detail: Optional[str]
    tool: Optional[str]
    status: Optional[str]

class ProgressBatcher:
    """Task-scoped, per-session buffering around a product append-batch port."""
    def publish(self, event: AgentProgressEvent) -> None:
        """Queue/sample an event; never propagate an append/timer exception."""
        ...
    def flush(self, *, timeout_seconds: float = 5) -> ProgressFlushResult: ...
    def close(self) -> None: ...

class AgentProgressSink(Protocol):
    def publish(self, event: AgentProgressEvent) -> None:
        """Best-effort callback safe to invoke inside provider polling."""
        ...
    def flush(self, *, timeout_seconds: float = 5) -> ProgressFlushResult: ...
    def record_failure(self, *, stage: Literal["projection", "delivery"]) -> None:
        """Latch a sanitized diagnostic; never raises and accepts no raw error."""
        ...

@dataclass(frozen=True)
class ProgressReservation:
    events: int
    serialized_bytes: int

@dataclass(frozen=True)
class AppendBatchResult:
    admitted_events: int
    admitted_serialized_bytes: int
    truncated: bool

@dataclass
class ProgressBudget:
    max_events: int
    max_serialized_bytes: int
    used_events: int = 0
    used_serialized_bytes: int = 0

    def reserve(self, *, events: int, serialized_bytes: int) -> Optional[ProgressReservation]: ...
    def commit(self, reservation: ProgressReservation, admitted: AppendBatchResult) -> None: ...
    def release(self, reservation: ProgressReservation) -> None: ...

@shared_registry.node("agent_turn")
def agent_turn(state, config, context) -> Dict[str, Any]:
    runner = _require(context, "runner", "agent_turn")
    if _is_legacy_config(config):
        return _legacy_direct_turn(runner, state, config, context)
    if _cancelled(context):
        result = _cancelled_result()
        _persist_result(context.get("on_result"), state, config, result)
        return _project_normalized(result, config)

    repo_path = Path(state["repo_path"])
    session = (
        open_harness_session(runner, repo_path=repo_path,
                             model_id=_resolve(config, state, "model_id"))
        if config["session_scope"] == "phase" else None
    )
    guard_token = None
    guard_started = False
    result = None
    progress_sink = context.get("progress_sink")
    flush_result = ProgressFlushResult.not_configured()
    try:
        if before_turn := context.get("before_turn"):
            guard_token = before_turn(state, config)
            guard_started = True
        outcome = run_harness_node(
            session or runner,
            name=config.get("label") or "agent_turn",
            repo_path=repo_path,
            prompt=_prompt_source(state, context, config),
            recovery_prompt=_recovery_prompt(state, context, config),
            attempts=int(config.get("attempts", 1)),
            recorder=context.get("recorder"),
            on_failure=config.get("on_failure", "fail"),
            is_cancelled=context.get("is_cancelled"),
            on_progress=_progress_callback(
                progress_sink.publish if progress_sink else None, session, config,
                detail_policy=config.get("progress_detail", "summary"),
            ),
            model_id=_resolve(config, state, "model_id"),
            timeout_seconds=_resolve(config, state, "timeout_seconds"),
        )
        snapshot = session.snapshot() if session is not None else None
        result = _normalize(outcome, snapshot)
    finally:
        try:
            if guard_started and (after_turn := context.get("after_turn")):
                after_turn(state, config, guard_token)
        finally:
            try:
                # This helper converts timeout/append failure to a diagnostic
                # result; progress delivery never changes operation truth.
                flush_result = _flush_progress(progress_sink)
            finally:
                if session is not None:
                    session.close()

    # Result durability is allowed only after the product's post-turn safety
    # validation and session cleanup have both succeeded. It still precedes
    # this node's state return and therefore its LangGraph checkpoint.
    result = _with_progress_flush_diagnostic(result, flush_result)
    _persist_result(context.get("on_result"), state, config, result)
    return _project_normalized(result, config)
```

The snapshot is deliberately captured **before** close: retrospective and
patch count exist only on `SessionSnapshot`. Cleanup remains in `finally`.
Normalized mode accepts an `AgentProgressSink`, adapts its `publish` method to
the harness callback, and flushes it before closing the phase session. The
flush helper never raises: timeout or append failure is logged and attached to
the normalized result diagnostics, while operation truth and the post-turn
safety verdict remain authoritative. A later UTA phase-completed event is
therefore always ordered after the flush attempt. Legacy mode does not inspect
or flush this new sink.

`AgentTurnResult` is public, frozen and JSON-safe. Status precedence is:
cancelled → timed out → rate limited → stalled after recovery budget → failed
→ skipped → completed. Provider exceptions and codes are normalized into
`diagnostics`; no provider event or exception object enters the DTO. Missing
usage, retrospective and patch data normalize to empty maps/zero, never
provider-specific sentinels. `recovered` comes from
`NodeOutcome.recovered`/`TurnLoop.recovered` and is true only when the bounded
same-session recovery result itself passed acceptance—not merely when recovery
was attempted.

`on_result(state, config, AgentTurnResult)` is a neutral durability port used
only in normalized mode. For an executed turn it runs after the pre-close
session snapshot **and only after** `after_turn` accepts the workspace and the
session closes, but before the node returns/checkpoints. UTA's implementation
atomically writes the turn envelope and completes the already-STARTED product
operation. If `after_turn` rejects an unsafe diff or session cleanup raises,
the port is not called: the row remains `STARTED`, no node update is returned,
and product reconciliation must classify the workspace rather than reuse an
unguarded result. If the port itself raises, `agent_turn` raises
`ResultPersistenceError`; guards and session are already clean and no state
update is returned. Pre-turn cancellation has no guard or session to validate,
so its normalized cancelled envelope may be persisted directly. Legacy mode
never calls the port. A configured normalized workflow may require it during
build validation; agent-core itself does not know the product schema.

`_progress_callback` receives neutral `TurnProgress` from the selected harness,
projects it through shared `project_turn_progress`, adds phase/session/sequence,
and publishes `AgentProgressEvent` to the supplied sink. The **returned
callback** is the outer no-throw boundary: it wraps both projection/sanitization
and `sink.publish`. A projection failure is logged to the private service log,
then calls the sink's no-throw `record_failure(stage="projection")` without
passing raw exception text; even a broken failure recorder is swallowed. The default
`summary` policy preserves today's generic public messages. Explicit
`public_detail` reuses today's `sanitize_public_progress_detail`: at most 180
characters of a reasoning/text synopsis or safe tool context, with
ANSI/control stripping, credential/opaque-value redaction, absolute-path and
URL masking, and rejection of command-like or structured content. It never
emits raw reasoning, model text, commands, provider-native objects, prompts,
environments, or tool output. This common projection is also used by
`RuntimeProgressPublisher`; the existing framework-agnostic
`runtime.sse.stream_task_events` remains the shared wire, cursor, keepalive and
terminal-close implementation. Products provide only an event-store port and
HTTP response object; they do not implement their own agent filter or SSE loop.

The callback and `AgentProgressSink.publish` together form a hard no-throw
boundary because harnesses call them inline while polling a provider. The
callback catches projection/sanitizer failures; the sink catches timer and
`append_batch` failures and latches a sanitized delivery diagnostic. None can
abort or reclassify the model turn. On an append failure the
batch remains pending for the final bounded flush. Ordinary pressure may drop
the oldest sampled entries and increments `dropped_count`; reserved
per-session slots keyed by critical kind coalesce the latest error, rate-limit
and final updates so ordinary traffic cannot evict them.
`flush(timeout_seconds=5)` makes one bounded retry,
returns the latched failure/drop counts in `ProgressFlushResult`, and also never
raises. A persistent database outage may leave progress unavailable, but the
operation ledger and later derived phase/terminal summaries remain the source
of truth.

An in-memory budget reservation is provisional. A successful product
`append_batch` returns its atomically admitted row/byte counts and commits that
amount; a rejected remainder or exception releases the corresponding token
under the same task lock before the batch is retained. Retry therefore neither
double-counts nor leaks capacity. UTA's transactional counters remain
authoritative across processes and restart.

Configuration is intentionally opt-in:

| Key | Default | Meaning |
| --- | --- | --- |
| `session_scope` | `"none"` | `"none"` or `"phase"`; cross-phase `"reuse"` is not supported |
| `result_mode` | `"legacy"` | `"legacy"` returns today's raw result shape; `"normalized"` returns `AgentTurnResult` as a JSON-safe mapping |
| `attempts` | `1` | passed straight through |
| `model_id`, `timeout_seconds` | `None` | a literal, or `{"from_state": "key"}` |
| `output_key` | `"turn_result"` | unchanged |
| `on_failure` | `"fail"` in normalized mode | UTA explicitly selects `"skip"`; legacy mode ignores this harness-node option |
| `progress_detail` | `"summary"` | `"summary"` or `"public_detail"`; UTA explicitly selects bounded public-safe detail under its existing report visibility boundary |

UTA sets `session_scope: phase` and `result_mode: normalized` on every cycle
turn. Bounded stalled-turn recovery reuses that one phase session internally
through `run_harness_node`; a new phase or deliberate repair attempt always
opens a new session.

`ProgressBatcher` is the default task-scoped `AgentProgressSink`
implementation and prevents streaming models from turning progress into a DB
write loop. It multiplexes independent buffers by `session_id`; each session
keeps at most 256 pending events and coalesces repeated
`(kind, tool, status, detail)` updates, persists at most two sampled progress
events per second in batches, and caps ordinary progress at 2,000 rows per
session. Those buffers share one `ProgressBudget`; UTA sets
20,000 ordinary events or 20 MiB serialized payload, whichever is reached
first, initialized from already-persisted usage on restart. `reserve` and the
single truncation-marker decision are protected by the task sink's lock; the
product append-batch port remains the cross-process
authoritative reservation. On pressure it drops oldest ordinary updates and increments
`dropped_count`; reserved/coalesced error/rate-limit/final updates are never
evicted by ordinary traffic. A single
task-scoped `progress_truncated` summary records drops. `agent_turn` flushes before the
phase-completed event and session close; both inline publish and final flush
swallow timer/append failures, and flush returns the latched diagnostic without
changing operation truth. Products supply
an `append_batch` port, while
agent-core owns timing, coalescing, bounds and drop policy.

`result_mode` is the activation boundary. Omitted/`legacy` always executes the
current direct `runner.run_turn` path, even when existing keys such as
`output_key`, model or timeout are configured. New options (`session_scope`,
attempts, recovery and guards) are rejected unless `result_mode: normalized`
is explicit, preventing a partial opt-in from changing an old workflow.

### Where the prompt comes from

Revision 2 took it from `context["prompt_for"]` — a single callable in a
single build-time context — which breaks two things at once: the graph has one
context but ~7 distinct turn nodes, and today's node reads
`state["prompt_file"]` (`nodes.py:128-130`), so a config-free call would
`KeyError` instead of behaving as it does today. The compatibility claim was
false.

`_prompt_source` resolves in this order:

1. `context["prompt_for"][config["label"]]` — a per-phase renderer, when the
   product registered one;
2. `state["prompt_file"]` — **today's behaviour**, and the fallback that keeps
   a config-free `agent_turn` working exactly as it does now.

A preceding `*_prompt` node writing `state["prompt_file"]` therefore needs no
new mechanism at all: it is the existing contract, which is why UTA's
render/turn/interpret triple works.

`context` also supplies `recovery_prompt_for`, `recorder`, `is_cancelled`,
`progress_sink`, `on_result` — ports/callables bound at build time, deliberately **not** in state
(see [serialization](#capacity-reliability-security)).

Normalized mode adds `turn_session_id`, `turn_usage`, `turn_diagnostics`,
`turn_recovered`, `turn_retrospective` and `turn_patch_count`. Legacy mode adds
nothing and retains the raw `output_key` value.

### Compatibility

cr_plugin does not use `agent_turn` at all — its only shared node is
`prepare_workspace` (`code-review.yaml:12`). So the node signature is not the
cr_plugin risk; the dependency pin is (see the overview's rollout, C-1).
Parameterized contract snapshots pin the complete legacy surface, not only a
config-free happy path:

- `runner` is required first; `prompt_file` and then `repo_path` are validated
  before the cancellation check;
- cancellation always returns exactly
  `{"turn_status": "cancelled", "turn_result": None}`, even when a custom
  `output_key` is configured, and never adds `turn_text`;
- success returns the raw runner result under the configured/default
  `output_key` plus the existing `turn_status` and `turn_text` keys;
- configured `model_id` and `timeout_seconds` and the existing cancellation
  callback are forwarded exactly once and unchanged; and
- every normalized-only option is rejected unless
  `result_mode: normalized` is explicit.

The enhanced path is entered only when that activation boundary is explicit.
Normalized lifecycle tests use ordered spies to prove
`run → snapshot → after_turn accepted → progress flush → session close →
on_result → return`. Separate cases make `after_turn` raise, crash/reconstruct
the product row, and assert `on_result` was never called; inject an append
failure from `publish` during provider polling and prove the turn continues,
the batch stays bounded/retryable and the later flush diagnostic is projected;
inject malformed progress that makes projection fail and prove the outer
callback latches only `stage=projection` while the turn continues;
make final flush time out/fail and assert successful cleanup/persistence; and
make the result sink fail and assert no node update/checkpoint is produced.

## Checkpoint persistence

New module `agent_core/workflow/checkpoints.py`. `langgraph.checkpoint.sqlite`
is imported **here and nowhere else**.

```python
@dataclass(frozen=True)
class WorkflowRunIdentity:
    product: str
    task_id: str
    unit_id: str              # a batch for Java, a target for Python — see I-2
    workflow_run_id: str
    cycle: str                # "generation-cycle"
    version: str              # "v1"

    @property
    def thread_id(self) -> str:
        # The version lives HERE, not in checkpoint_ns: LangGraph resolves
        # checkpoint_ns as a subgraph path, and a version string there makes
        # get_state raise "Subgraph ... not found". Found by spike; two
        # reviews read the earlier version and missed it.
        return (f"{self.product}:{self.cycle}:{self.version}"
                f":{self.task_id}:{self.unit_id}:{self.workflow_run_id}")

    def invoke_config(self, *, recursion_limit: int) -> Mapping[str, Any]: ...


@contextmanager
def open_checkpointer(path) -> Iterator[WorkflowCheckpointer]: ...

class WorkflowCheckpointer(BaseCheckpointSaver):
    # A real subclass, not a Protocol. Building A1 found two reasons:
    # `compile` does isinstance(checkpointer, BaseCheckpointSaver) and rejects
    # duck typing outright; and the base *declares* the protocol methods as
    # NotImplementedError, so they resolve before __getattr__ and a delegating
    # wrapper inherits the failures rather than forwarding. All twenty-two are
    # forwarded explicitly.
    def delete(self, identity: WorkflowRunIdentity) -> None: ...
```

- **Context manager, not factory** — the saver holds a SQLite connection, and a
  daemon leaking one per run fails slowly and confusingly.
- **Identity is a value type** — one `thread_id` construction, not an f-string
  at each call site.
- **Invocation config is produced here** — products do not know the
  `configurable.thread_id` key or where LangGraph expects `recursion_limit`.
- **Version is in the thread id** — an incompatible topology takes a new
  version, so an old lineage is ignored rather than resumed into moved nodes.
  Not `checkpoint_ns`, which LangGraph reserves for subgraph addressing.
- **Missing extra raises at open**, with the install command, as
  `WorkflowSpec.from_file` already does for PyYAML.
- **Deletion is by identity** — products implement retention without querying
  or deleting LangGraph tables directly.
- **Owner-only storage is enforced** — the wrapper creates the parent directory
  as `0700` and the SQLite database as `0600`, verifies both after open, and
  rejects a symlink or group/world-accessible existing path rather than
  silently inheriting unsafe permissions.

## Safe workflow invocation

The executable spike invalidated the earlier assumption that supplying the
same `thread_id` is sufficient to resume. With LangGraph 1.x, invoking a
pending lineage with a fresh mapping starts again at the entry node; invoking
a completed lineage with a mapping also re-enters it. Only a pending invocation
with `None` resumes from the stored next node.

Products therefore never call the compiled graph directly for durable work.
`agent_core.workflow.execution` owns this algorithm:

```python
@dataclass(frozen=True)
class WorkflowInvocationResult:
    disposition: Literal["started", "resumed", "reused_completed"]
    state: Mapping[str, JSONValue]

def invoke_workflow(
    graph: DurableWorkflow,
    *,
    identity: WorkflowRunIdentity,
    initial_state: Mapping[str, JSONValue],
    recursion_limit: int,
) -> WorkflowInvocationResult:
    config = identity.invoke_config(recursion_limit=recursion_limit)
    try:
        snapshot = graph.get_state(config)
    except Exception as exc:
        # Provider/deserialization failures are never interpreted as absence.
        raise WorkflowCheckpointError(identity.thread_id) from exc
    if _is_absent(snapshot):
        return WorkflowInvocationResult("started", graph.invoke(initial_state, config))
    try:
        _validate_snapshot(snapshot, identity)
    except Exception as exc:
        raise WorkflowCheckpointError(identity.thread_id) from exc
    if snapshot.next:
        return WorkflowInvocationResult("resumed", graph.invoke(None, config))
    return WorkflowInvocationResult("reused_completed", snapshot.values)
```

Absence is narrowly defined as no checkpoint tuple and no stored values.
Deserialization errors, a checkpoint with missing identity/version fields, or
a pending snapshot without a next-node sequence are corrupt—not absent. The
API never converts corruption into a fresh run. A clean rerun must mint a new
`workflow_run_id` explicitly.

Contract tests use the installed LangGraph implementation and cover: absent
start; pending `None` resume; completed result reuse before any outer-product
commit; corrupt-state failure; and clean rerun under a new identity. Each test
counts an expensive entry node and proves it executes exactly once.

## Nested workflow support

No new registry or graph-builder mechanism is required. The spike proved that
an ordinary child returned by the existing `build_graph`, compiled once with a
checkpointer and invoked through `invoke_workflow` from an outer node with its
own thread identity, retains an independent checkpoint lineage. The invocation
helper—not the thread ID alone—prevents re-entry.

UTA therefore compiles the child and outer graphs separately:

```python
cycle = build_graph(
    cycle_spec,
    generation_registry.extend(shared_registry),
    context=context,
    checkpointer=checkpointer,
    state_schema=CycleState,
)
outer = build_graph(
    outer_spec,
    outer_registry,
    context={**context, "generation_cycle": cycle},
    state_schema=AgentState,
)
```

The outer node calls `invoke_workflow(cycle, identity=...,
initial_state=..., recursion_limit=120)`. This leaves all LangGraph state
inspection and configuration knowledge in agent-core while reusing the
builder exactly as it exists today.

Revision 3 proposed `build_graph(subgraphs=…)`. It is rejected because it
would introduce a second registration/composition mechanism to recreate what
`build_graph` plus build-time context already provide. `NodeRegistry` remains
LangGraph-free and `NodeRegistry.extend()` continues to copy only nodes and
selectors.

`recursion_limit` remains invoke-time, as the spike measured. The identity
helper returns a normal config mapping, so the compiled graph retains
`get_state` and `update_state`; there is no `with_config` wrapper for the
product to construct or retain.

## Guards

Revision 1 proposed zero-argument `before_turn`/`after_turn` and cited
`harness/fallback.py` as the proven shape. Both were wrong:
`fallback.py:82-83` guards take `(guard_state, targets, phase)` and wrap a
client poll, not a node.

The real guard, `uta/testgen/task_guard.py:41`, is
`llm_guard_before(state, batch, phase)`. So the contract is:

```python
before_turn(state, config) -> Any        # returns a snapshot token
after_turn(state, config, snapshot) -> None
```

`state` and `config` are what a node already has; the product closes over
whatever else it needs. `after_turn` runs in a `finally`, so a turn that
raises still releases its guard.

## Control flow

```
agent_turn
  ├─ legacy mode? direct run_turn and exact legacy projection
  ├─ normalized cancelled?             → persist cancelled result and return
  ├─ resolve model/timeout (config | state)
  ├─ normalized mode: open one phase session, or use runner for scope none
  ├─ before_turn(state, config)        → snapshot
  ├─ run_harness_node(...)             # attempts + bounded recovery live here
  ├─ capture and normalize SessionSnapshot before close
  ├─ after_turn(..., snapshot)         # in a finally; must accept
  ├─ progress_sink.flush()             # bounded; failure becomes diagnostic
  ├─ close phase session               # nested finally; must succeed
  ├─ on_result(..., normalized result) # durable only after safety acceptance
  └─ return state update               # then LangGraph may checkpoint
```

## Public API

Added: `workflow.checkpoints.open_checkpointer`, `WorkflowCheckpointer`, and
`WorkflowRunIdentity` with `invoke_config` and identity-based deletion;
`workflow.execution.invoke_workflow`, `WorkflowInvocationResult`, and
`WorkflowCheckpointError`; JSON-safe `AgentTurnResult`, `AgentProgressEvent`,
`AgentProgressSink`, `ProgressReservation`, `AppendBatchResult`,
`project_turn_progress`, `ProgressBatcher`, and `ProgressBudget`; and the existing
`runtime.sse.stream_task_events` event-store port.
The normalized-node context also accepts the neutral `on_result` durability
port; legacy mode ignores it.

`build_graph` is unchanged. Its existing `context`, `checkpointer` and
`state_schema` parameters already provide the composition UTA needs.

`run_harness_node` already accepts a session as its runner. Its only change is
the additive `NodeOutcome.recovered` projection, sourced from the additive
`TurnLoop.recovered`; existing construction/call sites retain the default
`False`.

Changed compatibly: `agent_turn` config keys, every one defaulted.

No removals. Version **0.5.0** — new public API is a minor bump, not the
"0.4.x" revision 1 wrote while calling it minor.

## Tradeoffs

| Decision | Chosen | Rejected | Why |
| --- | --- | --- | --- |
| Turn lifecycle | delegate to `run_harness_node` | reimplement in `agent_turn` | It already owns attempts and bounded recovery; a second implementation is a second thing to keep correct |
| Nested child | existing `build_graph` twice, child in outer context | new subgraph registry or `NodeRegistry.add_workflow` | the spike proves existing composition resumes correctly; both alternatives add machinery without capability |
| Recovery | `run_harness_node(recovery_prompt=…)` | `session_recovery()` | The latter needs a client; a session has none |
| Session modes | config-free `"none"`; explicit `"phase"` | cross-phase `"reuse"` | compatibility is exact by default; isolation is explicit; stalled recovery stays inside one phase session |
| Checkpointer | context manager | factory | Connections leak otherwise, invisibly |
| Durable invocation | inspect then start/resume/reuse completed | always call `invoke(initial_state, config)` | LangGraph 1.x demonstrably re-enters pending and completed lineages when fresh input is supplied |

## Capacity, reliability, security

**Session cost.** Verified, not assumed: `OpenCodeClient.create_session` is a
`uuid4()` and a dict entry, `delete_session` is a `pop` (`client.py:140-155`).
`OpenCodeHarness.open_session` constructs a client, whose `__init__` only
builds a URL. So a session per phase is process-local bookkeeping, not a model
call.

**Checkpoint storage.** The installed LangGraph 1.x SQLite saver measured
307,200 bytes for 27 super-steps and 1,191,936 bytes for 120 with an 8 KiB
state. The consumer therefore uses about 6 MiB normal and 23 MiB at the
ceiling for 20 units **for that fixture**. Before beta the contract benchmark
also runs 256 KiB and 1 MiB JSON-safe normalized-result fixtures across 27 and
120 super-steps; the consumer sets its capacity budget from the measured worst
p95 and then verifies it against beta data.

The wrapper configures SQLite WAL mode and a bounded busy timeout at open, and
closes the connection in its context-manager exit. It exposes identity-based
deletion so products can implement retention without importing a saver or
issuing SQL against LangGraph tables.

The parent directory and database are owner-only (`0700`/`0600`). Permission,
symlink, create/open, WAL and cleanup behavior are covered by filesystem
contract tests on supported POSIX platforms.

**Serialization.** `context` is bound at build time and is **not** serialized;
state is. Therefore state must carry no callable, handle or credential. This
is a contract test in agent-core *and* a round-trip assertion in the consumer,
because the failure it prevents — a credential in a checkpoint file — is not
visible from either side alone.

## Failure modes

| Failure | Behaviour |
| --- | --- |
| No `runner` in context | `MissingContextError` at node run |
| `session_scope=phase` on a non-resumable harness | `SessionUnsupportedError`; UTA validates the selected harness capability before acquiring work and never silently loses phase isolation |
| No recovery prompt | recovery disabled; stalls consume the attempt budget |
| `before_turn` raises | turn does not run; phase session still closes; exception surfaces |
| turn or interpretation raises after guard | `after_turn` runs, then session closes; exception surfaces; no result is persisted |
| After-turn guard rejects an unsafe diff | session closes; the result sink is not called; STARTED evidence is reconciled and cannot be reused as a guarded completion |
| Session close raises | the result sink is not called; STARTED evidence is reconciled; exception surfaces |
| Progress flush times out or append fails | bounded flush returns a diagnostic; session closes and operation truth is unchanged; later phase completion remains ordered after the attempt |
| Progress projection or inline publish fails | outer callback/sink swallow it and latch only a sanitized stage diagnostic; provider polling and operation truth continue |
| Checkpoint extra missing | raises at `open_checkpointer` with the install command |
| Checkpoint locked or unwritable | open/write error surfaces; execution never silently falls back to no checkpoint |
| Pending checkpoint receives fresh input | impossible through the public API; `invoke_workflow` supplies `None` |
| Completed checkpoint invoked again | stored terminal state is returned without node execution |
| Corrupt checkpoint | `WorkflowCheckpointError`; never treated as absence |
| Recursion limit hit | LangGraph raises; surfaced as a terminal reason naming the limit, not a hang |
| Two runs share a `thread_id` | prevented by `workflow_run_id`; a clean rerun mints a new one |
| Cleanup targets the wrong run | deletion accepts `WorkflowRunIdentity`, not a raw SQL predicate or path fragment |
| Rich progress contains a credential or huge output | shared projector redacts credential patterns, strips controls and truncates each detail field; contract tests cover reasoning/text/tool cases |
| Progress rate exceeds storage capacity | shared batcher coalesces/samples, enforces session and shared task event/byte budgets, emits one truncation summary, and never drops final/error updates |
| Product result sink fails | `ResultPersistenceError` after guard/session cleanup but before node return/checkpoint; product reconciliation adopts, verifies, retries no-effect or fails indeterminate |

## Changelog

| Date | Revision | Change |
| --- | --- | --- |
| 2026-08-17 | 5 | Added normalized `AgentTurnResult`, exact config-free compatibility, phase-only sessions, progress forwarding and pre-close snapshot capture; added safe start/resume/completed invocation and owner-only checkpoint storage. |
| 2026-08-17 | 6 | Moved result durability after post-turn safety acceptance and session cleanup; defined progress sink flushing/failure semantics, parameterized exact legacy snapshots, explicit checkpoint error wrapping and large-result capacity fixtures. |
| 2026-08-18 | 7 (pending) | Add strict stable/volatile prompt rendering to the existing `PromptLibrary` as an additive 0.6.0 API; preserve every existing prompt method. |
| 2026-08-18 | 8 (pending) | Resolve prompt review: native newline compatibility, opt-in source caching, strict bounded metadata, secure/partial-write behavior, and executable CR/release verification. |
| 2026-08-18 | 9 (pending) | Make per-call newline policy concurrency-safe through an environment overlay and require mixed-policy concurrency coverage. |
| 2026-08-18 | 10 (implemented) | Release additive 0.6.1 opaque artifact identities so products never constrain or expose provider-owned session IDs in filesystem paths. |

# Spec: Opik Tracing

## Decision Status

- Jira: N/A. User explicitly confirmed this is non-Jira tool work.
- Design doc: Pending. Create `docs/design-opik-tracing.md` before implementation.
- Usage doc: Pending. Create `docs/usage-opik-tracing.md` before rollout.
- Implementation status: Spec only. Implementation is intentionally postponed.

## Objective

Add optional tracing to UTA so operators can inspect one batch run, API trigger
task, or repair session as a nested execution trace. The trace must show both
UTA business workflow phases and LLM turn metrics while keeping UTA's existing
task DB, reports, progress, and cost accounting as the source of truth.

The first tracing backend is self-hosted open-source Opik. UTA must not depend
on Comet Cloud for this feature.

Success means an operator can answer these questions from a local or internal
Opik instance:

- Which UTA phase consumed time or tokens?
- Which provider/model was used for each LLM turn?
- Which task, report, fix session, language, target, and backend produced a
  given LLM span?
- Do API trigger triggered runs and batch-mode runs expose the same core fields?

## Requirements

- Tracing must be optional, disabled by default, and fail open.
- Tracing implementation belongs in the language-agnostic engine/shared
  workflow layer, not in Java or Python language implementation modules.
- The trace contract must be language-agnostic. Java, Python, and future
  language backends may provide language metadata, but must not call Opik SDKs
  directly.
- The trace contract must be agent-backend agnostic. Current OpenCode JSONL
  events are one adapter; future PI-agent or other agent event streams must fit
  the same normalized event interface.
- LLM metrics must always include both provider and model identity:
  `provider`, `model`, and exact `provider/model` when available.
- API trigger triggered runs and batch-mode runs must emit the same stable root
  trace fields, with unavailable fields represented as empty or null values
  rather than omitted.
- UTA must use self-hosted open-source Opik via a base URL. No Opik Cloud
  workspace or cloud API key is required for the supported v1 path.
- Prompt and response payload capture is disabled by default. Metadata, status,
  timing, token buckets, and correlation ids are the default payload.
- OpenCode plugin telemetry is out of scope for v1 because UTA currently runs
  OpenCode in isolated `--pure` process mode.
- Target-drift detection is explicitly out of scope for this spec. This spec
  may record changed-file/tool metadata if already available as generic agent
  evidence, but it must not add drift policy, drift gates, or unsafe-diff
  behavior changes.

## Commands

Implementation is postponed. The expected verification commands for the later
implementation are:

- Focused unit tests:
  - `python3 -m pytest tests/test_opik_tracing.py tests/test_agent_event_adapters.py`
- OpenCode stream regression tests:
  - `python3 -m pytest tests/test_opencode_process.py tests/test_opencode_config.py`
- CI and batch behavior tests:
  - `python3 -m pytest tests/test_tasks.py tests/test_api_trigger.py`
- Full safety pass:
  - `python3 -m pytest`

Manual verification after implementation:

- Start local/self-hosted Opik.
- Run one small batch task with tracing enabled.
- Run one API trigger style task or fixture with tracing enabled.
- Confirm both traces contain the same common metadata keys and LLM child span
  fields.

## Project Structure

Expected later implementation locations:

- `uta/engine/observability.py`: language-agnostic trace contracts, no-op
  tracer, and trace/span helper interfaces.
- `uta/engine/agent_events.py`: normalized agent event contracts such as LLM
  turn, tool event, and backend event source metadata.
- `uta/opencode/*`: OpenCode JSONL adapter only. OpenCode-specific parsing
  must stay behind the engine agent-event contract.
- `uta/config.py`: feature flags and self-hosted Opik endpoint settings.
- `tests/`: focused tests for the trace abstraction, OpenCode adapter, and
  CI/batch field parity.
- `docs/design-opik-tracing.md`: detailed architecture and rollout plan.
- `docs/usage-opik-tracing.md`: local and node2 enablement instructions.

Language-specific packages such as `uta/language/java/*` and
`uta/language/python/*` should not own Opik integration. They may pass existing
engine objects such as language id, target id, verification status, and evidence
paths through normal workflow results.

## Code Style

Keep workflow code dependent on neutral interfaces:

```python
trace_context = TraceContext(
    execution_mode="api_trigger",
    language=target.language,
    agent_backend="opencode-jsonl",
    task_id=task_id,
    report_id=report_id,
    provider=provider,
    model=model,
    provider_model=provider_model,
)

with tracer.span(trace_context, name="generation"):
    result = agent.run_turn(prompt)
    tracer.record_llm_turn(OpenCodeJsonlEventAdapter().from_turn_result(result))
```

Avoid direct Opik imports in workflow nodes and language modules. The Opik SDK
should be isolated behind one tracer implementation so disabled tracing and
missing Opik dependencies behave like no-op tracing.

## Trace Contract

Root trace fields:

| Field | Required | Notes |
| --- | --- | --- |
| `uta.execution_mode` | Yes | `api_trigger` or `batch`. |
| `uta.language` | Yes | `java`, `python`, or future language id. |
| `uta.agent_backend` | Yes | `opencode-jsonl` for v1. |
| `uta.repo` | Yes | Stable repo name or URL-safe slug. |
| `uta.branch` | Yes | Working branch when known. |
| `uta.base_ref` | Yes | Diff base when known. |
| `uta.task_id` | Yes | CI task id or batch task/run id. |
| `uta.report_id` | Yes | Empty/null for batch if no report id exists. |
| `uta.fix_session_id` | Yes | Empty/null when not in repair. |
| `uta.target_id` | Yes | Empty/null at root; set on target spans. |
| `uta.provider` | Yes | Parsed provider id or `unknown`. |
| `uta.model` | Yes | Model id without provider if separable, else raw model. |
| `uta.provider_model` | Yes | Exact provider/model string when available. |

LLM span fields:

| Field | Required | Notes |
| --- | --- | --- |
| `llm.provider` | Yes | Provider id or `unknown`. |
| `llm.model` | Yes | Model id or `unknown`. |
| `llm.provider_model` | Yes | Exact configured model string when available. |
| `llm.session_id` | Yes | Agent session id when available. |
| `llm.turn_id` | No | Optional backend turn/message id. |
| `llm.input_tokens` | Yes | Default `0`. |
| `llm.output_tokens` | Yes | Default `0`. |
| `llm.reasoning_tokens` | Yes | Default `0`. |
| `llm.cache_read_tokens` | Yes | Default `0`. |
| `llm.cache_write_tokens` | Yes | Default `0`. |
| `llm.total_tokens` | Yes | Default `0`. |
| `llm.status` | Yes | `completed`, `rate_limited`, `timeout`, `stalled`, or `error`. |
| `llm.elapsed_ms` | Yes | Turn duration when available. |

Recommended business spans:

- `trigger`
- `workspace_prepare`
- `target_selection`
- `context_build`
- `generation`
- `verification`
- `coverage_repair`
- `mutation_repair`
- `push`
- `report_or_callback`

## Testing Strategy

- Unit-test disabled tracing so the no-op path never changes task behavior.
- Unit-test self-hosted Opik configuration without requiring an API key.
- Unit-test root trace metadata parity between API trigger and batch mode.
- Unit-test provider/model parsing so both separated provider/model and exact
  provider-model strings are emitted.
- Unit-test OpenCode JSONL adapter behavior using representative stream events
  and `TurnResult` objects.
- Unit-test failure modes: Opik unavailable, timeout sending spans, missing
  provider/model, missing token buckets, and malformed backend events.
- Integration-test a fake Opik client receiving nested business and LLM spans.
- Regression-test that Java and Python language modules do not import Opik or
  own tracing behavior.

## Boundaries

Always:

- Keep tracing language-agnostic and agent-backend agnostic.
- Include provider and model on every LLM metric/span.
- Keep API trigger and batch-mode trace fields aligned.
- Keep tracing disabled by default and fail open.
- Sanitize secrets, tokens, environment files, and repository credentials.

Ask first:

- Enabling prompt or response body capture.
- Adding DB schema columns for trace ids.
- Adding Comet Cloud support.
- Enabling OpenCode plugins or OpenTelemetry mode.
- Adding target-drift detection or drift gates.

Never:

- Fail UTA generation, repair, enforcement, push, reporting, or callbacks
  because tracing failed.
- Make Opik the source of truth for UTA task status, reports, gates, or cost
  accounting.
- Put Opik SDK calls in `uta/language/java/*` or `uta/language/python/*`.
- Hard-code OpenCode-only concepts into engine-level trace contracts.

## Scope Discovery

| Candidate | Found Evidence | Decision | Reason |
| --- | --- | --- | --- |
| `uta/engine/*` | Existing language-neutral contracts for batch, CI, targets, validation, verification, parsing, scoring, and language registry. | In scope | Tracing contracts belong at the same abstraction level. |
| `uta/opencode/process.py` | Runs `opencode run --format json` and returns `TurnResult` with status, session id, tokens, errors, and patch count. | In scope as adapter source | Current agent backend evidence comes from this stream. |
| `uta/opencode/stream.py` | Parses JSONL events, token buckets, progress, completion, patches, and rate-limit signals. | In scope as adapter input | Reuse existing parsing rather than introducing OpenCode plugins. |
| `uta/config.py` | Owns environment-backed runtime settings. | In scope | Opik enablement and endpoint config belong here. |
| `uta/language/java/*` | Language-specific Java adapters and runners. | Out of scope for Opik SDK calls | May pass language metadata only. |
| `uta/language/python/*` | Language-specific Python adapters and runners. | Out of scope for Opik SDK calls | May pass language metadata only. |
| `uta/api_trigger/*` | CI trigger, report, repair-session, and callback service. | In scope for workflow instrumentation | Must emit same trace fields as batch mode. |
| `uta/tasks/*` | Task DB, manager, progress, and token/cost fields. | In scope for correlation only | Existing DB remains source of truth; no schema change in v1. |
| OpenCode plugins | Official plugin/event system exists, but UTA runs `--pure`. | Out of scope for v1 | Plugin telemetry would require changing isolation behavior. |
| Target-drift detection | Prior discussion identified possible value. | Out of scope | User requested this spec without target-drift detection. |

## Success Criteria

- A later implementation can add tracing without touching Java/Python-specific
  modules except for existing metadata propagation.
- API trigger and batch-mode traces share the same required root metadata keys.
- Every LLM span includes provider, model, exact provider/model, token buckets,
  status, session id, and elapsed time when available.
- UTA behaves identically when tracing is disabled or Opik is unavailable.
- Self-hosted open-source Opik is the documented v1 deployment target.
- The spec leaves target-drift detection out of scope.

## Open Questions For Design

- Whether to persist Opik trace ids in task metadata/events without a DB schema
  migration.
- Whether Opik dependencies should be optional extras or unconditional runtime
  dependencies.
- Whether payload capture should support a per-task override in addition to a
  process-level environment flag.
- Whether trace names should use repo task ids, CI report ids, or a combined
  canonical id for easier dashboard grouping.

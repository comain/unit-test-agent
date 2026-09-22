# Spec: OpenCode Provider Fallback

## Decision Status

- Jira: N/A. User explicitly confirmed this is non-Jira tool work.
- Design doc: `docs/design-opencode-provider-fallback.md`
- Usage doc: `docs/usage-opencode-provider-fallback.md`
- Implementation status: implemented, tested, and deployed to node2 by git pull.

## Objective

Refactor UTA's OpenCode LLM gateway usage so production tasks can automatically move away from unavailable providers or disabled models without manual operator intervention.

Target users are UTA operators and RDC-triggered repair jobs. Success means a task that hits a provider/model availability failure records enough metadata for investigation, stops cooperatively, and is resumed by the normal task scheduler using the next configured provider/model candidate.

## Requirements

- Provider/model routing is configured through an environment-backed list.
- The provider chain is the single source of truth for OpenCode model selection.
- Provider priority for v1 is `token-pool`, then `openai`, then `deepseek`.
- Each provider can have its own ordered model list.
- UTA tries providers in order, and for each provider tries its model list in order.
- When fallback is disabled, UTA still uses the first configured provider/model from the provider chain.
- The new design removes legacy standalone model env vars as model-selection inputs.
- Cheap/small model routing uses the same selected provider-chain model as main routing.
- Provider tokens are supplied as `provider.token` entries so UTA can match each token to the corresponding provider.
- Provider OpenAI-compatible base URLs are supplied as `provider.base_url` entries so UTA can match each endpoint to the corresponding provider.
- Providers with an OpenAI-compatible models API can be probed via `GET /v1/models` to filter out currently unavailable models.
- Task creation must use the authenticated model-list probe before selecting the first provider-chain candidate.
- Fallback is triggered only by rate-limit, disabled-model, unavailable-model, and not-found model errors.
- Fallback is not triggered by timeout, stalled output, unsafe diff, budget exhaustion, compile/test failure, or normal OpenCode command errors.
- On fallback-triggering failures, UTA stops the current task and resumes it so the scheduler can pick it up automatically with the next candidate.
- UTA task records must store routing metadata for investigation.
- Model availability cache is process-local for v1.

## Commands

- Focused tests:
  - `python3 -m pytest tests/test_tiered_routing.py tests/test_tiered_routing_resilience.py tests/test_opencode_process.py tests/test_opencode_config.py`
- Task lifecycle tests:
  - `python3 -m pytest tests/test_tasks.py tests/test_daemon_preemption.py tests/test_daemon_retry.py`
- Full safety pass when implementation is ready:
  - `python3 -m pytest`

## Project Structure

- `uta/config.py`: environment settings for provider chain, provider tokens/base URLs, model availability probing, and fallback behavior.
- `uta/opencode/tiered_router.py`: routing policy, model health, provider/model candidate selection.
- `uta/opencode/config.py`: generated OpenCode config for every configured provider/model candidate.
- `uta/opencode/process.py`: per-turn OpenCode execution and availability-error classification.
- `uta/cli.py`: production task startup applies selected routing metadata and provider fallback stops/resumes tasks.
- `uta/tasks/manager.py`: task metadata persistence and resume events.
- `tests/test_*`: focused unit tests and task lifecycle regression tests.

## Code Style

Keep routing decisions explicit and inspectable:

```python
manager.stop_and_resume_for_provider_fallback(
    task_id,
    provider="token-pool",
    model="token-pool/gpt-5.5",
    candidate_index=0,
    reason="rate_limit",
    phase="generate",
)
```

Use small data objects or plain dicts with stable keys for persisted metadata. Avoid provider-specific conditionals scattered through graph nodes.

## Testing Strategy

- Unit-test env parsing for provider/model chains.
- Unit-test OpenAI-compatible `/v1/models` parsing and failure tolerance.
- Unit-test candidate selection:
  - provider order is respected,
  - model order within provider is respected,
  - unavailable models are skipped,
  - disabled models from API probing are skipped,
  - fallback disabled selects the first configured chain candidate.
- Unit-test OpenCode error classification for rate-limit, model not found, disabled model, and unavailable model errors.
- Integration-test task stop/resume behavior using `TaskManager` and a temporary SQLite DB.
- Regression-test cheap/small model behavior so compile-fix routing uses the same provider-chain-selected model.

## Boundaries

- Always:
  - Record chosen provider/model and fallback reason in task metadata/events.
  - Keep fallback eligibility narrow to provider/model availability classes.
  - Keep OpenAI auth behavior for native `openai` models intact.
- Ask first:
  - Adding new runtime dependencies.
  - Changing task DB schema instead of storing metadata in existing JSON fields/events.
  - Changing public CLI flags.
  - Retrying the same prompt immediately inside the same running task.
- Never:
  - Retry on compile/test failures, unsafe diff, budget exhaustion, timeout, or stalled output.
  - Delete or rewrite existing task history when switching models.
  - Expose API keys in logs, metadata, reports, or docs.
  - Require provider model APIs for providers that do not support them.

## Scope Discovery

| Candidate | Found Evidence | Decision | Reason |
| --- | --- | --- | --- |
| `uta/opencode/process.py` | Builds `opencode run`, injects provider env, detects rate limits. | In scope | Availability failures originate from process output/log parsing. |
| `uta/opencode/tiered_router.py` | Existing `ModelHealthTracker`, `effective_model`, cheap/main routing. | In scope | Natural home for ordered provider/model selection and health; cheap/main routing should converge on one selected model. |
| `uta/opencode/config.py` | Generates `opencode.json` provider/model entries. | In scope | Must register all candidate models, not just main/small. |
| `uta/config.py` | Current provider/model env settings. | In scope | New chain and model API settings belong here. |
| `uta/graph/nodes.py` | Raises and handles `ProviderRateLimitError`, records `PROVIDER_RATE_LIMITED`. | In scope | Needs to stop/resume task on fallback-eligible errors. |
| `uta/tasks/manager.py` | Existing `STOP_REQUESTED`, `STOPPED`, `resume_task`, task events. | In scope | Existing task lifecycle can implement deferred retry. |
| `uta/tasks/db.py` | Existing JSON config snapshots and task events. | In scope only if needed | Prefer existing JSON/event fields; DB schema change is not required in v1. |
| `uta/opencode/server.py` | Legacy/server-mode provider env and auth helpers. | Out of scope | Current process runner is the production path for turns; keep server behavior unchanged unless tests show shared helper drift. |
| `uta/api_trigger/*` | RDC trigger/report service. | Out of scope | The provider fallback is below API trigger service; reports may read task metadata later, but no route change is required in v1. |

## Success Criteria

- A configured chain like `token-pool`, `openai`, `deepseek` with provider-specific model lists selects the first available candidate.
- If a model is disabled or absent from `/v1/models`, UTA skips it before running OpenCode.
- If OpenCode returns a rate-limit or unavailable-model error, UTA records the failed candidate and fallback reason, stops the task, and requeues/resumes it.
- The resumed task uses the next available model candidate.
- Task metadata/events show enough detail to answer: selected model, failed model, fallback reason, candidate index, provider API probe result.
- Existing tests for OpenCode config and rate-limit detection still pass.

## Final Notes

Token values are parsed only for process environment injection. Task metadata and events store provider token presence as `configured` or `missing`.

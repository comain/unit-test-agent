# Usage: OpenCode Provider Fallback

## Status

Provider-chain routing, model probing, provider-token injection, and task stop/resume fallback are implemented and deployed to the server by git pull.

## Basic Configuration

Configure an ordered provider/model chain:

```bash
export UTA_OPENCODE_PROVIDER_FALLBACK_ENABLED=true
export UTA_OPENCODE_PROVIDER_CHAIN='token-pool:token-pool/gpt-5.5,token-pool/gpt-5.5-mini;openai:openai/gpt-5.5,openai/gpt-5.4;deepseek:deepseek/deepseek-v4-pro'
export UTA_OPENCODE_PROVIDER_TOKENS='token-pool.token=${TOKEN_POOL_API_KEY};openai.token=${OPENAI_API_KEY};deepseek.token=${DEEPSEEK_API_KEY}'
export UTA_OPENCODE_PROVIDER_BASE_URLS='token-pool.base_url=https://token-pool.example/v1;openai.base_url=https://api.openai.com/v1;deepseek.base_url=https://api.deepseek.com/v1'
```

Provider order is left to right. Model order is left to right within each provider.

The provider chain is the source of model selection. When fallback is disabled, UTA uses the first configured provider/model and does not advance to later candidates.

Cheap/small model routing uses the same selected provider-chain model as main routing.

Token entries use `provider.token` keys so UTA can match a token to the corresponding provider. Token values are injected into provider runtime env only and are not stored in task metadata, reports, or logs.

Base URL entries use `provider.base_url` keys so UTA can match OpenAI-compatible endpoints to the corresponding provider. `UTA_OPENCODE_PROVIDER_BASE_URLS` takes precedence over legacy shared base URL env such as `UTA_BASE_URL`, `UTA_BAS_URL`, and `OPENAI_BASE_URL`.

## Model Availability API

Providers with OpenAI-compatible model list endpoints can be probed:

```bash
export UTA_OPENCODE_MODEL_API_TIMEOUT_SECONDS=5
export UTA_OPENCODE_MODEL_API_CACHE_SECONDS=300
```

UTA will call the provider's OpenAI-compatible models endpoint when enough provider configuration exists. Models absent from the response are skipped for the current cache window. If the endpoint is unavailable, UTA keeps the configured list.

Task creation uses this authenticated probe before selecting the first model. Runtime fallback still handles rate limits or model errors that happen after selection.

The model availability cache is process-local. Restarting the daemon or CI plugin clears it.

## Runtime Behavior

When OpenCode reports a fallback-eligible failure:

- rate limit,
- disabled model,
- unavailable model,
- model not found,

UTA records the failed provider/model and reason, stops the current task, then queues the same task for resume. The scheduler picks it up again and selects the next available candidate.

UTA does not fallback for:

- compile failures,
- test failures,
- mutation failures,
- unsafe diffs,
- budget exhaustion,
- timeouts,
- stalled output,
- generic command errors.

## Investigation

Inspect task metadata and events:

```bash
uta tasks show <task-id> --task-db /path/to/uta_tasks.db
```

Expected evidence includes:

- selected provider/model,
- failed provider/model,
- fallback reason,
- candidate index,
- model API probe result,
- stop/resume event ids.

## Operations

Disable fallback immediately:

```bash
export UTA_OPENCODE_PROVIDER_FALLBACK_ENABLED=false
```

To choose a fixed model while fallback is disabled, place that model first in `UTA_OPENCODE_PROVIDER_CHAIN`.

Restart the relevant daemon or CI plugin after changing env values.

## Verification

Before enabling on the server:

```bash
python3 -m pytest tests/test_tiered_routing.py tests/test_tiered_routing_resilience.py tests/test_opencode_process.py tests/test_opencode_config.py
python3 -m pytest tests/test_tasks.py tests/test_daemon_preemption.py tests/test_daemon_retry.py
```

After enabling on the server, trigger a controlled task and verify the task event stream shows model selection. For fallback validation, configure a known-disabled first model followed by a valid model, then confirm the task stops, resumes, and continues with the second candidate.

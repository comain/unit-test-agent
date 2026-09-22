# Configuration

Runtime configuration for the generation half. Enforcement needs none of this:
it takes its thresholds from `--coverage-gate` / `--mutation-gate` and runs
without a model, a key, or a network.

## Model and provider selection

**Agent selection**: `UTA_AGENT_HARNESS` names an implementation registered by
agent-core and defaults to `opencode`. UTA's workflow does not import that
implementation. With the default adapter, `UTA_OPENCODE_PROVIDER_CHAIN` remains
the model-selection source. Configure providers left to right, with
comma-separated model fallback within each provider, for example:

```bash
export UTA_OPENCODE_PROVIDER_CHAIN='token-pool:token-pool/gpt-5.5,token-pool/gpt-5.5-mini;openai:openai/gpt-5.5;deepseek:deepseek/deepseek-v4-pro'
export UTA_OPENCODE_PROVIDER_TOKENS='token-pool.token=${TOKEN_POOL_API_KEY};openai.token=${OPENAI_API_KEY};deepseek.token=${DEEPSEEK_API_KEY}'
export UTA_OPENCODE_PROVIDER_BASE_URLS='token-pool.base_url=https://token-pool.example/v1;openai.base_url=https://api.openai.com/v1;deepseek.base_url=https://api.deepseek.com/v1'
export UTA_OPENCODE_PROVIDER_FALLBACK_ENABLED=true
```

When fallback is disabled, UTA uses the first configured provider/model. `model` and `small_model` are both set to the selected provider-chain model.

Supported provider families:

- `openrouter/*` via `OPENROUTER_API_KEY`
- `google/*` via `GEMINI_API_KEY`
- `openai/*` via native OpenCode `/connect`
- `cursor/*` or plain Cursor model names (for example `gpt-5`) via OpenCode's configured Cursor provider and `UTA_OPENCODE_PROVIDER=cursor`
- `tencent/*` or plain Tencent model names (for example `glm-5`) via `TENCENT_API_KEY` and `UTA_OPENCODE_PROVIDER=tencent`
- `ollama/*` via local Ollama and optional `OLLAMA_HOST`

**Cursor auth**: UTA registers configured Cursor models in the workspace OpenCode config, but it does not install plugins or modify the user's global OpenCode config. Authentication and any provider plugin setup remain owned by the OpenCode installation.

**OpenAI / ChatGPT subscription auth**: when the selected provider-chain model uses `openai/*` without an explicit `openai.token`, UTA preserves native OpenCode OAuth behavior and strips inherited `OPENAI_API_KEY` from spawned OpenCode processes.

## Run tuning

**Classes per agent run** (`--classes-per-run`, alias `--batch-size`, default 1; environment variable `UTA_CLASSES_PER_AGENT_RUN`): number of candidate classes included in a single generation prompt. Values greater than 1 reuse the same OpenCode session and context export for the whole batch, which usually shortens end-to-end time at the cost of larger prompts and more work per response. In production task mode, `UTA_SMART_BATCHING_ENABLED=true` batches simple classes up to `UTA_SMART_SIMPLE_BATCH_SIZE` (default `3`) even when this generic value remains `1`, while complex classes still run alone. If you previously set `UTA_BATCH_SIZE` in `.env`, rename that key to `UTA_CLASSES_PER_AGENT_RUN`.

**Explicit class override** (`--class-fqn`, repeatable): bypass git-history candidate selection and run UTA against the exact class FQNs you provide. This is useful for A/B comparisons across prompt/workflow changes because the candidate set stays stable even when recent git activity changes.

**All-files selection** (`--all`): bypass git-history ranking and consider every production Java file under the selected repo/module. This is useful for selector audits and one-time backfills. `--max-files` is ignored in this mode; explicit `--class-fqn` still takes precedence.

## Context cache

**Project context for the agent**: after parsing, UTA writes cached guidance under `.uta_cache/context/`, including:
- `project_summary.md`: Maven + graph statistics
- `test_generation_guidance.md`: cached source-of-truth lookup order, test-construction constraints, and observed repo test patterns
- `session_retrospect.md`: post-run prompt/workflow hints mined from the latest OpenCode session

UTA also preserves the raw slash-init transcript at `.uta_cache/context/opencode_init_output.md` when `/init` is attempted. By default, slash-init is **disabled** (`UTA_OPENCODE_INIT_SLASH_ENABLED=false`) because some OpenCode builds return only a generic task summary and can leave the main build session in a bad state for the next prompt. If you explicitly enable it and `.uta_summary.md` is still missing or nearly empty, UTA will try `/init` and harvest `AGENTS.md`, `CLAUDE.md`, or `.opencode/AGENTS.md` into `.uta_summary.md` when those files are actually created. Timeout: **`UTA_OPENCODE_INIT_SLASH_TIMEOUT`** (default 900s). If slash-init is disabled or nothing is harvested, **`UTA_OPENCODE_INIT_COMMAND`** may still run as a shell fallback; otherwise UTA may create its own stub during `parse_context`.

## Progress, timeouts and reports

**Agent progress streaming**: during long agent turns, agent-core projects
provider-neutral activity into bounded, public-safe events. The report shows
the 13 stable generation phases, safe reasoning synopses and tool activity,
status, and diagnostics without persisting raw reasoning, commands, prompts,
tool output, absolute paths, or provider errors. Progress is sampled and
batched; task row/byte caps emit one `progress_truncated` marker without
changing operation truth.

Toggle the session-message stream with `UTA_OPENCODE_STREAM_PROGRESS` (default `true`).

**Generation timeout control**: the generation turn timeout is `40 minutes * batch size * UTA_OPENCODE_GENERATION_TIMEOUT_RATIO` with a minimum of `300s`. If generation times out, UTA now records `GENERATION_TIMEOUT`, writes `session_retrospect.md`, and keeps the stalled session learnings for later runs.

**Headless OpenCode external-directory permissions**: UTA writes the `permission.external_directory` section of each generated `opencode.json` from [config/opencode_external_dirs.json](/path/to/unit-test-agent/config/opencode_external_dirs.json). Keep machine- or workspace-specific allow patterns there so headless runs can access known sibling repos without triggering interactive permission prompts. Temp scratch paths are still allowed automatically.

**Reports**: UTA report JSON now includes:
- per-project summary metrics
- per-file metric tables
- phase timing details
- mutation breakdown buckets (`KILLED`, `NO_COVERAGE`, etc.)
- session retrospect hints mined from the OpenCode transcript
